"""Tests for the MCP server.

Mostly against `handle()` directly, which is the whole protocol surface, plus
one end-to-end run through the stdio loop so the transport itself is covered.
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

import nf_tui_mcp as mcp
import pytest

FAIL_LOG = """\
Jul-15 15:24:38.100 [main] DEBUG nextflow.cli.Launcher - $> nextflow run main.nf
Jul-15 15:24:38.200 [Task monitor] DEBUG n.processor.TaskPollingMonitor - Task \
completed > TaskHandler[id: 1; name: P:GOOD (s1); status: COMPLETED; exit: 0; \
error: -; workDir: {good}]
Jul-15 15:24:39.000 [Task monitor] DEBUG n.processor.TaskPollingMonitor - Task \
completed > TaskHandler[id: 2; name: P:BOOM (s2); status: COMPLETED; exit: 139; \
error: -; workDir: {bad}]
Jul-15 15:24:39.100 [main] ERROR nextflow.Nextflow - Error executing process > \
'P:BOOM (s2)'

Caused by:
  Process `P:BOOM (s2)` terminated with an error exit status (139)

Command error:
  .command.sh: line 3: Segmentation fault

Work dir:
  {bad}

Jul-15 15:24:40.000 [main] DEBUG nextflow.Session - Execution complete -- Goodbye
"""


@pytest.fixture
def run_dir(tmp_path):
    good = tmp_path / "work" / "aa" / ("1" * 30)
    bad = tmp_path / "work" / "bb" / ("2" * 30)
    for d in (good, bad):
        d.mkdir(parents=True)
        (d / ".command.log").write_text("some output\n")
        (d / ".command.sh").write_text("#!/bin/bash\nrun_it\n")
        (d / ".command.err").write_text("Segmentation fault\n")
    (good / "result.txt").write_text("".join(f"line {i}\n" for i in range(50)))
    with gzip.open(good / "reads.gz", "wt") as f:
        f.write("".join(f"gz {i}\n" for i in range(50)))
    (good / "aln.bam").write_bytes(b"\x1f\x8b" + b"\0" * 40)
    (good / ".hidden").write_text("x\n")
    (tmp_path / ".nextflow.log").write_text(FAIL_LOG.format(good=good, bad=bad))
    return tmp_path


def call(_tool, **args):
    """`_tool` is underscored so it can't collide with a tool's own `name`
    argument — read_output takes one."""
    reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": _tool, "arguments": args}})
    res = reply["result"]
    return res, json.loads(res["content"][0]["text"]) if not res["isError"] else None


# ----------------------------------------------------------------- protocol

def test_initialize_reports_tools_capability():
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"}})["result"]
    assert "tools" in r["capabilities"]
    assert r["serverInfo"]["name"] == "nf-tui"


def test_initialize_echoes_a_newer_client_protocol():
    """A client speaking a newer revision shouldn't be answered with an older
    number — that's what makes some clients refuse to continue."""
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}})["result"]
    assert r["protocolVersion"] == "2025-06-18"


def test_initialized_notification_gets_no_reply():
    assert mcp.handle({"jsonrpc": "2.0",
                       "method": "notifications/initialized"}) is None


def test_unknown_method_is_a_protocol_error():
    r = mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "nope/nope"})
    assert r["error"]["code"] == -32601


def test_unknown_tool_is_rejected():
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "nonexistent", "arguments": {}}})
    assert r["error"]["code"] == -32602


def test_tools_list_is_complete_and_schema_shaped():
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
    names = {t["name"] for t in r["tools"]}
    assert names == {"list_runs", "get_run", "get_failures", "get_task",
                     "list_outputs", "read_output", "tail_log", "search_log"}
    for t in r["tools"]:
        assert t["description"] and t["inputSchema"]["type"] == "object"
        assert "handler" not in t          # must not leak the callable


# --------------------------------------------------------------------- tools

def test_get_run_reports_progress_without_logs(run_dir):
    _, d = call("get_run", run=str(run_dir))
    assert d["progress"]["total"] == 2
    assert d["progress"]["failed"] == 1
    assert all("logs" not in t for t in d["tasks"]), "get_run must stay small"


def test_get_failures_gives_cause_and_logs(run_dir):
    _, d = call("get_failures", run=str(run_dir))
    assert d["failed_count"] == 1
    f = d["failures"][0]
    assert "139" in f["cause"]
    assert "Segmentation fault" in f["report"]
    assert f["logs"], "a failure should carry its .command.* files"


def test_get_task_by_hash_includes_outputs(run_dir):
    _, run = call("get_run", run=str(run_dir))
    good = [t for t in run["tasks"] if not t["failed"]][0]
    _, d = call("get_task", run=str(run_dir), task_hash=good["hash"])
    names = {o["name"] for o in d["outputs"]}
    assert "result.txt" in names
    assert ".hidden" not in names, "Nextflow plumbing should stay hidden"


def test_get_task_accepts_a_hash_prefix(run_dir):
    _, run = call("get_run", run=str(run_dir))
    h = run["tasks"][0]["hash"]
    _, d = call("get_task", run=str(run_dir), task_hash=h.split("/")[0])
    assert d["hash"] == h


def test_read_output_pages_a_text_file(run_dir):
    _, run = call("get_run", run=str(run_dir))
    good = [t for t in run["tasks"] if not t["failed"]][0]
    _, p1 = call("read_output", run=str(run_dir), task_hash=good["hash"],
                 name="result.txt", max_lines=10)
    assert p1["lines"] == [f"line {i}" for i in range(10)]
    assert not p1["at_eof"] and p1["offset_unit"] == "bytes"
    _, p2 = call("read_output", run=str(run_dir), task_hash=good["hash"],
                 name="result.txt", offset=p1["next_offset"], max_lines=10)
    assert p2["lines"] == [f"line {i}" for i in range(10, 20)]


def test_read_output_pages_a_gzip_file(run_dir):
    _, run = call("get_run", run=str(run_dir))
    good = [t for t in run["tasks"] if not t["failed"]][0]
    _, p1 = call("read_output", run=str(run_dir), task_hash=good["hash"],
                 name="reads.gz", max_lines=10)
    assert p1["encoding"] == "gzip" and p1["offset_unit"] == "lines"
    _, p2 = call("read_output", run=str(run_dir), task_hash=good["hash"],
                 name="reads.gz", offset=p1["next_offset"], max_lines=10)
    assert p2["lines"][0] == "gz 10"


def test_read_output_refuses_a_bam_with_an_explanation(run_dir):
    _, run = call("get_run", run=str(run_dir))
    good = [t for t in run["tasks"] if not t["failed"]][0]
    res, _ = call("read_output", run=str(run_dir), task_hash=good["hash"],
                  name="aln.bam")
    assert res["isError"]
    assert "samtools" in res["content"][0]["text"]


def test_list_outputs_hides_command_files(run_dir):
    _, run = call("get_run", run=str(run_dir))
    good = [t for t in run["tasks"] if not t["failed"]][0]
    _, d = call("list_outputs", run=str(run_dir), task_hash=good["hash"])
    names = {o["name"] for o in d["outputs"]}
    assert "result.txt" in names
    assert not any(n.startswith(".") for n in names)


def test_tail_log_and_search_log(run_dir):
    _, t = call("tail_log", run=str(run_dir), lines=2)
    assert "Goodbye" in t["lines"][-1]
    _, s = call("search_log", run=str(run_dir), pattern="error executing")
    assert s["count"] == 1 and s["hits"][0]["line"] > 0


def test_list_runs_finds_the_run(run_dir):
    _, d = call("list_runs", root=str(run_dir.parent))
    assert d["count"] >= 1
    assert any(Path(r["log"]).parent == run_dir for r in d["runs"])


# -------------------------------------------------------------------- errors

def test_missing_run_is_a_tool_error_not_a_crash(tmp_path):
    res, _ = call("get_run", run=str(tmp_path / "nothing-here"))
    assert res["isError"] and "no .nextflow.log" in res["content"][0]["text"]


def test_unknown_task_hash_is_a_tool_error(run_dir):
    res, _ = call("get_task", run=str(run_dir), task_hash="zz/999999")
    assert res["isError"] and "no task matching" in res["content"][0]["text"]


def test_bad_arguments_are_reported_not_raised(run_dir):
    res, _ = call("get_run", nonsense=1)
    assert res["isError"] and "bad arguments" in res["content"][0]["text"]


# ---------------------------------------------------------------- transport

def test_stdio_loop_end_to_end(run_dir):
    """The real transport: newline-delimited JSON-RPC over stdin/stdout, and
    nothing but protocol on stdout."""
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_failures", "arguments": {"run": str(run_dir)}}},
    ]
    p = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "nf_tui_mcp.py")],
        input="\n".join(json.dumps(r) for r in reqs) + "\n",
        capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    lines = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
    assert [m["id"] for m in lines] == [1, 2], "a notification must not reply"
    payload = json.loads(lines[1]["result"]["content"][0]["text"])
    assert payload["failed_count"] == 1
