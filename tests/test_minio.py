"""nf-tui against a real S3 API: MinIO in a container, read with the real AWS CLI.

The rest of the suite fakes `aws` with a shim. These don't: they start MinIO,
upload a run's work tree to a bucket, and point nf-tui at a .nextflow.log whose
work dirs are s3:// URIs — the shape of an AWS Batch run. What runs here is
what a user on S3 or S3-compatible storage (MinIO, Ceph) actually executes.

They need docker and the aws CLI, and are skipped without them — except under
NF_TUI_REQUIRE_MINIO=1, where a missing prerequisite fails instead. CI sets it,
so these can't quietly stop running and still show green.

Every AWS setting is pinned per test: endpoint, credentials, region, and config
files pointed at paths that don't exist. Without that, a forgotten endpoint
sends requests to real AWS, and a developer's ~/.aws profile gets used.
"""
from __future__ import annotations

import asyncio
import gzip
import os
import shutil
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import nf_tui as nf
import nf_tui_mcp as mcp
from generate_run import make_run
from nf_tui import NfScope
from textual.widgets import RichLog

pytestmark = pytest.mark.minio

# quay.io, not Docker Hub: minio/minio stopped being pullable there. Pinned,
# because MinIO no longer updates community images and :latest could vanish or
# change under the tests.
MINIO_IMAGE = "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z"
USER, PASSWORD = "nftuitest", "nftuitest-secret"
BUCKET = "nf-tui-test"


# ---- the store ---------------------------------------------------------------

def _prerequisite_problem() -> str | None:
    if shutil.which("aws") is None:
        return "the aws CLI is not installed"
    if shutil.which("docker") is None:
        return "docker is not installed"
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "docker did not answer within 30s"
    if r.returncode != 0:
        return "docker is installed but its daemon isn't answering"
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _aws_env(endpoint: str, home: Path) -> dict[str, str]:
    """An environment where the aws CLI can only ever talk to this endpoint."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(
        AWS_ENDPOINT_URL=endpoint,
        AWS_ACCESS_KEY_ID=USER,
        AWS_SECRET_ACCESS_KEY=PASSWORD,
        AWS_DEFAULT_REGION="us-east-1",
        AWS_EC2_METADATA_DISABLED="true",
        AWS_CONFIG_FILE=str(home / "no-aws-config"),
        AWS_SHARED_CREDENTIALS_FILE=str(home / "no-aws-credentials"),
    )
    return env


def aws(*args: str, env: dict[str, str]) -> str:
    r = subprocess.run(["aws", *args], capture_output=True, text=True,
                       env=env, timeout=180)
    if r.returncode != 0:
        raise AssertionError(f"aws {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


@pytest.fixture(scope="session")
def minio(tmp_path_factory):
    problem = _prerequisite_problem()
    if problem:
        if os.environ.get("NF_TUI_REQUIRE_MINIO") == "1":
            pytest.fail(f"NF_TUI_REQUIRE_MINIO=1, but {problem}")
        pytest.skip(f"MinIO tests need docker and the aws CLI: {problem}")

    port = _free_port()
    name = f"nf-tui-minio-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-p", f"127.0.0.1:{port}:9000",
         "-e", f"MINIO_ROOT_USER={USER}", "-e", f"MINIO_ROOT_PASSWORD={PASSWORD}",
         MINIO_IMAGE, "server", "/data"],
        capture_output=True, text=True, timeout=600)
    if run.returncode != 0:
        # The prerequisites are there, so this is a real failure, not a skip.
        pytest.fail(f"could not start {MINIO_IMAGE}: {run.stderr.strip()}")
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"{endpoint}/minio/health/live",
                                            timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                logs = subprocess.run(["docker", "logs", name],
                                      capture_output=True, text=True).stdout
                pytest.fail(f"MinIO never became healthy:\n{logs[-2000:]}")
            time.sleep(0.5)
        home = tmp_path_factory.mktemp("aws-home")
        yield SimpleNamespace(endpoint=endpoint, env=_aws_env(endpoint, home),
                              home=home, container=name)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(autouse=True)
def _aws_points_at_minio(minio, monkeypatch, request):
    """Pin every AWS setting for the code under test, which reads os.environ."""
    request.node._minio = minio        # conftest reports the store on failure
    for key in [k for k in os.environ if k.startswith("AWS_")]:
        monkeypatch.delenv(key)
    for key, value in minio.env.items():
        if key.startswith("AWS_"):
            monkeypatch.setenv(key, value)
    nf._remote_cache.clear()
    yield
    nf._remote_cache.clear()


# ---- the run -------------------------------------------------------------------

TRACE = ("nextflow.trace/v2\nrealtime=4210\n%cpu=187.5\n"
         "peak_rss=524288\n%mem=1.2\n")
ERR = "samtools: [E::hts_open] fail to open file 'x.bam'\n"
BIG_LINES = [f"line {i:07d} " + "x" * 40 for i in range(20_000)]


@pytest.fixture(scope="session")
def s3run(minio, tmp_path_factory):
    """A run uploaded to the bucket, and a log that points at it."""
    base = tmp_path_factory.mktemp("s3run")
    local_log = make_run(base, n_tasks=40, n_procs=4, with_workdirs=40, seed=7)
    work = base / "work"
    failed = next(t for t in nf.parse_log(local_log)
                  if nf.is_failed(t) and t.workdir)
    wd = Path(failed.workdir)
    (wd / ".command.err").write_text(ERR)
    (wd / ".command.trace").write_text(TRACE)
    with gzip.open(wd / "reads.fq.gz", "wt") as f:
        for i in range(5000):
            f.write(f"@r{i}\nACGT\n+\nIIII\n")
    (wd / "big.txt").write_text("\n".join(BIG_LINES) + "\n")
    (wd / "no-newline.txt").write_text("first\nlast, with no newline")
    (wd / "empty.txt").write_text("")

    aws("s3", "mb", f"s3://{BUCKET}", env=minio.env)
    aws("s3", "sync", "--quiet", str(work), f"s3://{BUCKET}/work", env=minio.env)

    # In a directory of its own, so no local work/ sits beside the log.
    s3log = base / "launch" / ".nextflow.log"
    s3log.parent.mkdir()
    s3log.write_text(local_log.read_text().replace(str(work), f"s3://{BUCKET}/work"))
    return SimpleNamespace(
        log=s3log, run=str(s3log), failed_hash=failed.hash, failed_local=wd,
        failed_uri=failed.workdir.replace(str(work), f"s3://{BUCKET}/work"))


def _closed_endpoint() -> str:
    return f"http://127.0.0.1:{_free_port()}"   # nothing listens once closed


# ---- reading the store -----------------------------------------------------------

def test_remote_cat_reads_an_object(s3run):
    got = nf.remote_cat(f"{s3run.failed_uri}/.command.log")
    assert got == (s3run.failed_local / ".command.log").read_text()


def test_a_missing_object_is_absent_not_an_error(s3run):
    assert nf.remote_cat(f"{s3run.failed_uri}/.command.out") is None
    assert nf.remote_ls(f"s3://{BUCKET}/work/zz/nothing-here") == []


def test_remote_ls_lists_names_and_real_sizes(s3run):
    listed = dict(nf.remote_ls(s3run.failed_uri))
    for name in ("big.txt", "reads.fq.gz", ".command.log", "empty.txt"):
        assert listed[name] == (s3run.failed_local / name).stat().st_size


def test_an_unreachable_store_says_so_and_names_the_endpoint(s3run, monkeypatch):
    dead = _closed_endpoint()
    monkeypatch.setenv("AWS_ENDPOINT_URL", dead)
    with pytest.raises(nf.RemoteError) as e:
        nf.remote_cat(f"{s3run.failed_uri}/.command.log")
    assert "could not connect" in str(e.value) and dead in str(e.value)
    with pytest.raises(nf.RemoteError):
        nf.remote_ls(s3run.failed_uri)


def test_a_failure_is_not_cached(s3run, minio, monkeypatch):
    """One bad moment used to mean "doesn't exist" for the whole session."""
    uri = f"{s3run.failed_uri}/.command.log"
    monkeypatch.setenv("AWS_ENDPOINT_URL", _closed_endpoint())
    with pytest.raises(nf.RemoteError):
        nf.remote_cat(uri)
    monkeypatch.setenv("AWS_ENDPOINT_URL", minio.endpoint)   # the store is back
    assert "processing" in nf.remote_cat(uri)


def test_bad_credentials_say_access_denied(s3run, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wrong")
    with pytest.raises(nf.RemoteError) as e:
        nf.remote_cat(f"{s3run.failed_uri}/.command.log")
    assert "access denied" in str(e.value)


def test_a_bucket_that_does_not_exist_is_an_error_when_listed(s3run):
    # `cp` reports a missing bucket as a plain 404, exactly like a missing key;
    # only a listing can tell them apart, which is why the UI asks for one.
    with pytest.raises(nf.RemoteError) as e:
        nf.remote_ls("s3://no-such-bucket-nf-tui/work/ab/cd")
    assert "NoSuchBucket" in str(e.value)


# ---- --json ------------------------------------------------------------------------

def test_json_report_carries_remote_logs_and_metrics(s3run):
    rep = nf.run_report(s3run.log, logs="failed")
    assert "remote_error" not in rep
    task = next(t for t in rep["tasks"] if t["hash"] == s3run.failed_hash)
    assert task["logs"]["err"] == ERR
    assert task["logs"]["log"] == (s3run.failed_local / ".command.log").read_text()
    assert task["logs"]["script"].startswith("#!/bin/bash")
    assert task["metrics"] == {"realtime_ms": 4210, "pct_cpu": 187.5,
                               "peak_rss_kb": 524288, "pct_mem": 1.2}


def test_json_report_records_a_store_failure_once(s3run, monkeypatch):
    """Unreachable is recorded, and reading stops — not one timeout per task."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", _closed_endpoint())
    calls = []
    real = nf.remote_cat

    def counting(uri, limit=20_000):
        calls.append(uri)
        return real(uri, limit)

    monkeypatch.setattr(nf, "remote_cat", counting)
    rep = nf.run_report(s3run.log, logs="all")
    assert "could not connect" in rep["remote_error"]
    assert all("could not connect" in t["logs_error"] for t in rep["tasks"]
               if t["workdir"])
    assert len(calls) == 1, f"kept reading a dead store: {len(calls)} reads"


# ---- MCP ------------------------------------------------------------------------------

def test_mcp_lists_outputs_from_s3(s3run):
    out = mcp.tool_list_outputs(s3run.run, s3run.failed_hash)
    names = {o["name"]: o["size"] for o in out["outputs"]}
    assert names["big.txt"] == (s3run.failed_local / "big.txt").stat().st_size
    assert "reads.fq.gz" in names
    assert not any(n.startswith(".command") for n in names), \
        "Nextflow's plumbing belongs to get_task, not the outputs"


def test_mcp_get_failures_includes_logs_from_s3(s3run):
    got = mcp.tool_get_failures(s3run.run)
    assert got["failed_count"] >= 1
    failure = next(f for f in got["failures"] if f["hash"] == s3run.failed_hash)
    assert failure["logs"]["err"] == ERR


def test_mcp_pages_a_large_object_exactly(s3run):
    """Every page, in order, reassembles the file byte for byte."""
    lines, offset, pages = [], 0, 0
    while True:
        page = mcp.tool_read_output(s3run.run, s3run.failed_hash, "big.txt",
                                    offset=offset)
        assert page["offset_unit"] == "bytes"
        lines += page["lines"]
        offset = page["next_offset"]
        pages += 1
        if page["at_eof"]:
            break
        assert pages < 200, "paging never reached the end"
    assert lines == BIG_LINES
    assert offset == (s3run.failed_local / "big.txt").stat().st_size


def test_mcp_reads_the_last_line_without_a_newline(s3run):
    page = mcp.tool_read_output(s3run.run, s3run.failed_hash, "no-newline.txt")
    assert page["lines"] == ["first", "last, with no newline"]
    assert page["at_eof"]


def test_mcp_reads_an_empty_object(s3run):
    page = mcp.tool_read_output(s3run.run, s3run.failed_hash, "empty.txt")
    assert page["lines"] == [] and page["at_eof"]


def test_mcp_pages_gzip_by_line(s3run):
    page = mcp.tool_read_output(s3run.run, s3run.failed_hash, "reads.fq.gz",
                                offset=4, max_lines=4)
    assert page["encoding"] == "gzip"
    assert page["lines"] == ["@r1", "ACGT", "+", "IIII"]
    assert page["next_offset"] == 8


def test_mcp_names_a_file_that_is_not_there(s3run):
    with pytest.raises(ValueError, match="is not in s3://"):
        mcp.tool_read_output(s3run.run, s3run.failed_hash, "not-there.txt")


def test_mcp_reports_an_unreachable_store_instead_of_no_outputs(s3run, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", _closed_endpoint())
    with pytest.raises(ValueError, match="could not connect"):
        mcp.tool_list_outputs(s3run.run, s3run.failed_hash)


# ---- the UI ------------------------------------------------------------------------------

def _drive(app, steps):
    async def run():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            return await steps(app, pilot)
    return asyncio.run(run())


def _pane_text(app) -> str:
    pane = app.query_one("#log", RichLog)
    return "\n".join("".join(seg.text for seg in strip) for strip in pane.lines)


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_ui_shows_a_task_log_from_s3(s3run):
    expected = (s3run.failed_local / ".command.log").read_text().splitlines()[0]

    async def steps(app, pilot):
        await pilot.press("e")                  # jump to the failed task
        await _settle(app, pilot)
        assert app._selected().hash == s3run.failed_hash
        await pilot.press("t")
        await _settle(app, pilot)
        assert expected in _pane_text(app)
        return True

    assert _drive(NfScope(s3run.log), steps)


def test_ui_lists_a_tasks_outputs_from_s3(s3run):
    async def steps(app, pilot):
        await pilot.press("e")
        await _settle(app, pilot)
        await pilot.press("d")
        await _settle(app, pilot)
        assert any(u.endswith("/big.txt") for u in app._remote_files)
        return True

    assert _drive(NfScope(s3run.log), steps)


def test_ui_says_the_store_is_unreachable_rather_than_empty(s3run, monkeypatch):
    """The bug this replaced: every failure read "(no output ... yet)"."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", _closed_endpoint())

    async def steps(app, pilot):
        await pilot.press("e")
        await pilot.press("t")
        await _settle(app, pilot)
        text = _pane_text(app)
        assert "couldn't read the object store" in text
        assert "could not connect" in text
        assert "no output in the object store yet" not in text
        return True

    assert _drive(NfScope(s3run.log), steps)
