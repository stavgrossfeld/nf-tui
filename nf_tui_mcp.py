"""An MCP server that hands nf-tui's view of a Nextflow run to an agent.

Everything the TUI can work out about a run — task states, why a task failed,
resource metrics, what a task wrote, the run log — is reachable here as tools,
so an agent can diagnose a run without a terminal and without walking the work
tree itself.

Speaks JSON-RPC 2.0 over stdio directly rather than through the MCP SDK: the
protocol needed here is a few hundred lines, and nf-tui deliberately ships no
SDKs (the cloud support goes through the user's own `aws`/`gcloud` for the same
reason). One less dependency to keep current, and it installs anywhere Python
does.

Run it as `nf-tui-mcp`; point a client at that command.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

import nf_tui as nf

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "nf-tui", "version": "1.0.0"}

# Responses go into an agent's context window, so every one of them is bounded.
MAX_TASKS = 400            # tasks per get_run before it starts summarising
MAX_OUTPUT_LINES = 400     # lines per read_output call
MAX_LOG_LINES = 300        # lines per tail_log call


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _log_path(run: str) -> Path:
    """Accept a run directory or a .nextflow.log, like the CLI does."""
    p = Path(run).expanduser()
    if p.is_dir():
        return p / ".nextflow.log"
    return p


def _require_log(run: str) -> Path:
    log = _log_path(run)
    if not log.exists():
        raise ValueError(f"no .nextflow.log at {log}")
    return log


def _find_task(report: dict, task_hash: str) -> dict | None:
    """Match on the short hash, tolerating a full-length one or a bare prefix."""
    want = task_hash.strip()
    for t in report.get("tasks", []):
        if t["hash"] == want:
            return t
    for t in report.get("tasks", []):
        if t["hash"].startswith(want) or want.startswith(t["hash"]):
            return t
        wd = t.get("workdir") or ""
        if want and want in wd:
            return t
    return None


def _visible_files(workdir: str) -> list[Path]:
    """What the files view would show: Nextflow's own .command.* plumbing is
    hidden, because it is reachable through get_task instead."""
    try:
        return [p for p in sorted(Path(workdir).iterdir())
                if not p.name.startswith(".") and p.name not in nf.JUNK_NAMES]
    except OSError:
        return []


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def tool_list_runs(root: str = ".", limit: int = 25) -> dict:
    """Every run found under a directory, newest first.

    Uses the same discovery the run picker does, so what an agent sees here and
    what a person sees in the UI cannot drift apart.
    """
    infos = nf.gather_runs(Path(root).expanduser())
    infos.sort(key=lambda r: r.mtime, reverse=True)
    out = []
    for r in infos[: max(1, min(int(limit), 100))]:
        prog = r.progress
        out.append({
            "run": str(r.path.parent),
            "log": str(r.path),
            "runname": r.runname,
            "pipeline": r.pipeline,
            "status": r.status,
            "finished": r.finished,
            "modified": r.mtime,
            "progress": ({"total": prog.total, "done": prog.done,
                          "failed": prog.failed, "pct": prog.pct}
                         if prog is not None else None),
        })
    return {"root": str(Path(root).expanduser()), "runs": out, "count": len(out)}


def tool_get_run(run: str, include_tasks: bool = True,
                 failed_only: bool = False) -> dict:
    """Progress and every task's state. Logs are deliberately not included —
    use get_task or get_failures, so one call can't flood a context window."""
    log = _require_log(run)
    rep = nf.run_report(log, logs="none", failed_only=failed_only)
    if not include_tasks:
        rep.pop("tasks", None)
        return rep
    tasks = rep.get("tasks", [])
    if len(tasks) > MAX_TASKS:
        rep["tasks"] = tasks[:MAX_TASKS]
        rep["tasks_truncated"] = {
            "shown": MAX_TASKS, "total": len(tasks),
            "hint": "call get_run with failed_only=true, or get_task by hash",
        }
    return rep


def tool_get_failures(run: str) -> dict:
    """The diagnosis call: every failed task with why it failed and its logs.

    This is the one an agent should reach for first when a run has gone wrong —
    it answers "what broke and why" in a single round trip.
    """
    log = _require_log(run)
    rep = nf.run_report(log, logs="failed", failed_only=True)
    failures = []
    for t in rep.get("tasks", []):
        err = t.get("error") or {}
        failures.append({
            "hash": t["hash"],
            "name": t["name"],
            "process": t["process"],
            "tag": t.get("tag"),
            "exit": t.get("exit"),
            "attempts": t.get("attempts"),
            "workdir": t.get("workdir"),
            "cause": err.get("summary"),
            "report": err.get("report"),
            "logs": t.get("logs"),
        })
    return {
        "run": str(log.parent),
        "progress": rep.get("progress"),
        "failed_count": len(failures),
        "failures": failures,
    }


def tool_get_task(run: str, task_hash: str, include_logs: bool = True) -> dict:
    """One task in full: state, metrics, its error report, its .command.* files."""
    log = _require_log(run)
    rep = nf.run_report(log, logs="all" if include_logs else "none")
    task = _find_task(rep, task_hash)
    if task is None:
        raise ValueError(f"no task matching {task_hash!r} in this run")
    if task.get("workdir"):
        task = dict(task)
        task["outputs"] = [
            {"name": p.name, "size": p.stat().st_size if p.is_file() else None,
             "dir": p.is_dir()}
            for p in _visible_files(task["workdir"])
        ]
    return task


def tool_list_outputs(run: str, task_hash: str) -> dict:
    """What a task actually wrote, with sizes."""
    log = _require_log(run)
    rep = nf.run_report(log, logs="none")
    task = _find_task(rep, task_hash)
    if task is None:
        raise ValueError(f"no task matching {task_hash!r} in this run")
    wd = task.get("workdir")
    if not wd:
        return {"hash": task["hash"], "workdir": None, "outputs": [],
                "note": "no work dir known yet (task has not completed)"}
    return {
        "hash": task["hash"],
        "name": task["name"],
        "workdir": wd,
        "outputs": [
            {"name": p.name, "size": p.stat().st_size if p.is_file() else None,
             "dir": p.is_dir()}
            for p in _visible_files(wd)
        ],
    }


def tool_read_output(run: str, task_hash: str, name: str,
                     offset: int = 0, max_lines: int = MAX_OUTPUT_LINES) -> dict:
    """Read a chunk of one of a task's output files.

    Paged the same way the UI scrolls: `next_offset` feeds straight back in, so
    an agent can walk a multi-gigabyte file without ever holding it in memory.
    """
    log = _require_log(run)
    rep = nf.run_report(log, logs="none")
    task = _find_task(rep, task_hash)
    if task is None:
        raise ValueError(f"no task matching {task_hash!r} in this run")
    wd = task.get("workdir")
    if not wd:
        raise ValueError("this task has no work dir yet")
    target = Path(wd) / name
    if not target.exists():
        raise ValueError(f"{name!r} is not in {wd}")
    lines_cap = max(1, min(int(max_lines), MAX_OUTPUT_LINES))
    if nf.is_gzip(target):
        lines = nf.head_gzip(target, lines_cap, skip=int(offset))
        return {"file": str(target), "encoding": "gzip",
                "lines": lines, "next_offset": int(offset) + len(lines),
                "offset_unit": "lines", "at_eof": len(lines) < lines_cap}
    if nf.decode_tool(target) is not None:
        raise ValueError(
            f"{name!r} needs {nf.decode_tool(target)!r} from the task's "
            "container to read; open it in the UI, or run that tool yourself")
    pos, lines, at_eof = nf.read_forward(target, int(offset), lines_cap)
    return {"file": str(target), "encoding": "text", "lines": lines,
            "next_offset": pos, "offset_unit": "bytes", "at_eof": at_eof}


def tool_tail_log(run: str, lines: int = 100) -> dict:
    """The end of .nextflow.log — where a run says how it went."""
    log = _require_log(run)
    want = max(1, min(int(lines), MAX_LOG_LINES))
    text = nf._tail_text(log, limit=want * 400) or ""
    tail = text.splitlines()[-want:]
    return {"log": str(log), "lines": tail}


def tool_search_log(run: str, pattern: str, max_hits: int = 50) -> dict:
    """Find lines in .nextflow.log matching a substring (case-insensitive)."""
    log = _require_log(run)
    needle = pattern.lower()
    hits = []
    for n, line in enumerate(nf.iter_lines(log), 1):
        if needle in line.lower():
            hits.append({"line": n, "text": line[:500]})
            if len(hits) >= max(1, min(int(max_hits), 200)):
                break
    return {"log": str(log), "pattern": pattern, "hits": hits,
            "count": len(hits)}


TOOLS: list[dict] = [
    {
        "name": "list_runs",
        "description": "Find Nextflow runs under a directory, with progress for "
                       "each. Start here when you don't know the run path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "directory to search"},
                "limit": {"type": "integer"},
            },
        },
        "handler": tool_list_runs,
    },
    {
        "name": "get_run",
        "description": "Progress and every task's state for one run (no logs). "
                       "Accepts a run directory or a .nextflow.log path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string"},
                "include_tasks": {"type": "boolean"},
                "failed_only": {"type": "boolean"},
            },
            "required": ["run"],
        },
        "handler": tool_get_run,
    },
    {
        "name": "get_failures",
        "description": "Every failed task with the cause, Nextflow's full error "
                       "report and the task's .command.* logs. The first call to "
                       "make when a run has gone wrong.",
        "inputSchema": {
            "type": "object",
            "properties": {"run": {"type": "string"}},
            "required": ["run"],
        },
        "handler": tool_get_failures,
    },
    {
        "name": "get_task",
        "description": "One task in full: state, exit code, resource metrics, "
                       "error report, output file list and .command.* logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string"},
                "task_hash": {"type": "string",
                              "description": "short hash, e.g. 8d/8b3561"},
                "include_logs": {"type": "boolean"},
            },
            "required": ["run", "task_hash"],
        },
        "handler": tool_get_task,
    },
    {
        "name": "list_outputs",
        "description": "The files a task wrote, with sizes.",
        "inputSchema": {
            "type": "object",
            "properties": {"run": {"type": "string"},
                           "task_hash": {"type": "string"}},
            "required": ["run", "task_hash"],
        },
        "handler": tool_list_outputs,
    },
    {
        "name": "read_output",
        "description": "Read a chunk of one of a task's output files. Feed "
                       "next_offset back in to continue; works on files far too "
                       "large to hold in a context window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string"},
                "task_hash": {"type": "string"},
                "name": {"type": "string", "description": "file name in the work dir"},
                "offset": {"type": "integer"},
                "max_lines": {"type": "integer"},
            },
            "required": ["run", "task_hash", "name"],
        },
        "handler": tool_read_output,
    },
    {
        "name": "tail_log",
        "description": "The end of the run's .nextflow.log.",
        "inputSchema": {
            "type": "object",
            "properties": {"run": {"type": "string"},
                           "lines": {"type": "integer"}},
            "required": ["run"],
        },
        "handler": tool_tail_log,
    },
    {
        "name": "search_log",
        "description": "Find lines in .nextflow.log containing a substring.",
        "inputSchema": {
            "type": "object",
            "properties": {"run": {"type": "string"},
                           "pattern": {"type": "string"},
                           "max_hits": {"type": "integer"}},
            "required": ["run", "pattern"],
        },
        "handler": tool_search_log,
    },
]

HANDLERS: dict[str, Callable[..., Any]] = {t["name"]: t["handler"] for t in TOOLS}
TOOL_SPECS = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

def _result(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code,
                                                      "message": message}}


def handle(msg: dict) -> dict | None:
    """One request in, one response out. None means "notification, stay quiet"."""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        # Echo the client's protocol version when it names one: a client that
        # speaks a newer revision should not be told a older number back.
        version = params.get("protocolVersion") or PROTOCOL_VERSION
        return _result(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOL_SPECS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return _error(req_id, -32602, f"unknown tool: {name}")
        try:
            payload = fn(**args)
            text = json.dumps(payload, indent=1, default=str)
            return _result(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except TypeError as e:                          # bad arguments
            return _result(req_id, {
                "content": [{"type": "text", "text": f"bad arguments: {e}"}],
                "isError": True})
        except Exception as e:                          # noqa: BLE001
            # Report the failure as tool output rather than a protocol error:
            # the agent can read it and try something else.
            return _result(req_id, {
                "content": [{"type": "text",
                             "text": f"{type(e).__name__}: {e}"}],
                "isError": True})
    if req_id is None:
        return None                                     # unknown notification
    return _error(req_id, -32601, f"method not found: {method}")


def main() -> int:
    """Read newline-delimited JSON-RPC on stdin, write responses on stdout.

    Nothing may go to stdout except protocol messages — a stray print would
    corrupt the stream — so diagnostics go to stderr.
    """
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            print(json.dumps(_error(None, -32700, f"parse error: {e}")),
                  flush=True)
            continue
        try:
            reply = handle(msg)
        except Exception:                               # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            reply = _error(msg.get("id"), -32603, "internal error")
        if reply is not None:
            print(json.dumps(reply, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
