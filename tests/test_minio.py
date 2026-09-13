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
    # The log itself, in the bucket too — the shape of a head job's upload.
    log_uri = f"s3://{BUCKET}/logs/run1/.nextflow.log"
    aws("s3", "cp", "--only-show-errors", str(s3log), log_uri, env=minio.env)
    return SimpleNamespace(
        log=s3log, run=str(s3log), log_uri=log_uri,
        failed_hash=failed.hash, failed_local=wd,
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


# ---- a .nextflow.log that lives in S3 ------------------------------------------
# Nextflow can't write its log there (-log s3://… makes a local "s3:" folder),
# so these model what does happen: something uploads it, and nf-tui is handed
# the URI. The log is mirrored to a local cache; work dirs stay in S3.

import json                                                    # noqa: E402

import nf_tui_serve                                            # noqa: E402


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """A cache of this test's own, and no mirrors left over from another test."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    nf._log_mirrors.clear()
    yield tmp_path / "xdg" / "nf-tui" / "logs"
    nf._log_mirrors.clear()


def _put(minio, key: str, text: str, tmp_path: Path) -> str:
    body = tmp_path / f"upload-{uuid.uuid4().hex[:6]}.log"
    body.write_text(text)
    uri = f"s3://{BUCKET}/{key}"
    aws("s3", "cp", "--only-show-errors", str(body), uri, env=minio.env)
    return uri


def test_cli_json_reads_a_log_stored_in_s3(s3run, cache, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["nf-tui", "--json", s3run.log_uri])
    nf.main()
    out = json.loads(capsys.readouterr().out)
    assert out["log"] == s3run.log_uri, "report should name the object, not the cache"
    assert Path(out["log_cache"]).is_relative_to(cache)
    assert len(out["tasks"]) == len(nf.parse_log(s3run.log))
    failed = next(t for t in out["tasks"] if t["hash"] == s3run.failed_hash)
    assert failed["logs"]["err"] == ERR           # work dirs are still read from S3


def test_cli_says_plainly_when_the_s3_log_is_missing(cache, monkeypatch):
    """It used to mangle the URI into a local path and report that instead."""
    uri = f"s3://{BUCKET}/logs/nope/.nextflow.log"
    monkeypatch.setattr("sys.argv", ["nf-tui", "--json", uri])
    with pytest.raises(SystemExit) as e:
        nf.main()
    assert str(e.value) == f"nf-tui: there is no object at {uri}"


def test_a_prefix_is_not_a_log(cache):
    with pytest.raises(nf.RemoteError, match="is a prefix, not a log"):
        nf.mirror_log(f"s3://{BUCKET}/logs/run1/")


def test_the_copy_carries_the_objects_own_time(s3run, cache):
    """A copy dated "now" would make every finished run look live for 20s."""
    m = nf.mirror_log(s3run.log_uri)
    assert m.local.is_relative_to(cache)
    assert m.local.read_text() == s3run.log.read_text()
    assert abs(m.local.stat().st_mtime - nf._s3_head(s3run.log_uri)["modified"]) < 1


def test_an_unchanged_log_is_not_downloaded_again(s3run, cache, monkeypatch):
    m = nf.mirror_log(s3run.log_uri)
    downloads = []
    monkeypatch.setattr(nf, "_download_log", lambda *a: downloads.append(a))
    assert nf.refresh_log_mirror(m, force=True) is False
    assert downloads == [], "a finished run's log was fetched again"


def test_a_changed_log_is_picked_up_but_checks_are_paced(minio, cache, tmp_path):
    uri = _put(minio, "logs/growing/.nextflow.log", "first line\n", tmp_path)
    m = nf.mirror_log(uri)
    _put(minio, "logs/growing/.nextflow.log", "first line\nsecond line\n", tmp_path)
    assert nf.refresh_log_mirror(m) is False, "checked again inside the pacing window"
    assert nf.refresh_log_mirror(m, force=True) is True
    assert m.local.read_text() == "first line\nsecond line\n"


def test_a_failed_recheck_keeps_the_last_copy(s3run, cache, monkeypatch):
    m = nf.mirror_log(s3run.log_uri)
    before = m.local.read_text()
    monkeypatch.setenv("AWS_ENDPOINT_URL", _closed_endpoint())
    with pytest.raises(nf.RemoteError, match="could not connect"):
        nf.refresh_log_mirror(m, force=True)
    assert m.local.read_text() == before


def test_a_copy_cached_by_an_earlier_session_is_not_trusted(minio, cache, tmp_path):
    uri = _put(minio, "logs/stale/.nextflow.log", "old run\n", tmp_path)
    nf.mirror_log(uri)
    nf._log_mirrors.clear()                       # a new session, same cache dir
    _put(minio, "logs/stale/.nextflow.log", "new run\n", tmp_path)
    assert nf.mirror_log(uri).local.read_text() == "new run\n"


def test_mcp_get_failures_takes_an_s3_log(s3run, cache):
    got = mcp.tool_get_failures(s3run.log_uri)
    assert got["run"] == s3run.log_uri
    failure = next(f for f in got["failures"] if f["hash"] == s3run.failed_hash)
    assert failure["logs"]["err"] == ERR


def test_mcp_explains_that_list_runs_is_local_only(cache):
    with pytest.raises(ValueError, match="searches local directories"):
        mcp.tool_list_runs(f"s3://{BUCKET}/logs/")


def test_web_serves_an_s3_log_by_its_uri(s3run, cache, monkeypatch):
    """The served app gets the URI, so it keeps re-checking on its own."""
    seen = {}

    class FakeServer:
        def __init__(self, command, host, port):
            seen["command"] = command

        def serve(self):
            pass

    monkeypatch.setattr(nf_tui_serve, "Server", FakeServer)
    nf_tui_serve.main(["--port", "8123", s3run.log_uri])
    assert seen["command"].endswith(" " + s3run.log_uri)


def test_web_refuses_a_missing_s3_log_in_the_terminal(cache, monkeypatch):
    uri = f"s3://{BUCKET}/logs/nope/.nextflow.log"
    with pytest.raises(SystemExit) as e:
        nf_tui_serve.main([uri])
    assert "there is no object at" in str(e.value)


def test_ui_follows_a_log_that_grows_in_s3(minio, s3run, cache, tmp_path,
                                          monkeypatch):
    """A head job re-uploading its log mid-run: new tasks appear on screen."""
    monkeypatch.setattr(nf, "LOG_MIRROR_RECHECK", 0.5)
    lines = s3run.log.read_text().splitlines(keepends=True)
    half = "".join(lines[: len(lines) // 2])
    key = "logs/live/.nextflow.log"
    m = nf.mirror_log(_put(minio, key, half, tmp_path))
    first = len(nf.parse_log(m.local))

    async def steps(app, pilot):
        await _settle(app, pilot)
        assert len(app.tasks) == first
        assert m.uri in app.sub_title, "header should show where the log is"
        _put(minio, key, "".join(lines), tmp_path)
        for _ in range(60):                       # up to ~15s
            await pilot.pause(0.25)
            if len(app.tasks) > first:
                break
        assert len(app.tasks) == len(nf.parse_log(s3run.log)), \
            f"still showing {len(app.tasks)} tasks after the log grew in S3"
        return True

    assert _drive(NfScope(m.local, mirror=m), steps)
