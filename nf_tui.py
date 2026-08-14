#!/usr/bin/env python3
"""nf-tui — a real-time terminal browser for Nextflow tasks.

Point it at a Nextflow run directory (or a .nextflow.log). Tasks parsed
from the log are shown as a live tree grouped by process; the pane below
shows, for the selected task, one of:

  t  task log      — .command.log with container-pull noise filtered out
  c  container log — just the image-pull / setup lines
  d  files         — the work-dir outputs; pick one to preview it, opened
                     with a tool from the task's container (samtools for
                     BAM/CRAM, etc.) using the task's own mounts so the
                     reference resolves. L opens it full in `less`.
  g  run log       — the whole .nextflow.log, tailed live

esc steps back (content -> list -> tree -> run picker); o opens the work
dir. Everything refreshes on a timer while a pipeline runs. With no path,
nf-tui searches the current directory and lets you pick a run.

    python nf_tui.py /path/to/run          # dir containing .nextflow.log
    python nf_tui.py /path/to/.nextflow.log

Works on any completed or in-progress run. No plugin, no re-run required.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (DataTable, Footer, Header, Input, OptionList,
                             RichLog, Static, Tree)
from textual.widgets.option_list import Option

REFRESH_SECONDS = 1.0
RUNLOG_TAIL = 400_000   # bytes of .nextflow.log to read for the initial tail
RUNLOG_MAX_LINES = 600  # lines loaded per step (renders fast + reliably)
RUNLOG_CHUNK = 200_000  # bytes read per backfill step when scrolling up
# Task logs are usually small enough to load whole; these bounds only bite on a
# runaway .command.log, which then backfills on scroll like the run log.
TASKLOG_CHUNK = 1_000_000
TASKLOG_MAX_LINES = 5_000
VIEW_MAX_LINES = 2_000  # in-pane preview cap for host reads (text / gz)
BAM_PREVIEW_LINES = 500 # smaller cap for container-decoded BAM/CRAM/BCF (faster)
# How much `F` pulls in one go. It used to be 200_000, which took 42s and
# 719 MB on a 159 MB file and *still* showed only a tenth of it. Now that
# scrolling to the bottom keeps loading (see _preview_extend), F is just a
# bigger first bite, and the rest of the file is reachable either way.
FULL_MAX_LINES = 20_000
# Above this, hand less `-n`: numbering every line of a big file is what makes
# quitting it take a minute (see pager_flags). ~0.17s of counting at this size.
LESS_LINENUM_MAX = 32 * 1024 * 1024
# Most a follower will pull in one tick, so a task dumping gigabytes into
# .command.log can't drag the whole lot into the pane (see Follower.read_new).
FOLLOW_MAX_CATCHUP = 4 * 1024 * 1024

# Lines we care about in .nextflow.log:
#   ... [bf/407183] Submitted process > NFCORE:...:SRA_FASTQ_FTP (tag)
#   ~> TaskHandler[id: 6; name: ...; status: RUNNING; exit: -; error: -; workDir: /abs/path]
#
# Nextflow announces a task in one of four ways (TaskProcessor.RunType plus the
# storeDir path). Only "Submitted" tasks also get TaskHandler lines: a `-resume`
# run logs ONLY "Cached process" lines — no handler lines at all — so a parser
# that ignores them sees a resumed run as completely empty.
_RUNTYPE_RE = re.compile(
    r"\[([0-9a-f]{2}/[0-9a-f]+|skipping)\] "
    r"(Submitted|Re-submitted|Cached|Stored) process > (.+)$"
)
# Every executor's handler class ends in "TaskHandler" (Local/Grid/Cached/...),
# so matching the suffix covers SLURM, PBS, k8s, AWS Batch, etc.
_HANDLER_RE = re.compile(
    r"TaskHandler\[id: (?P<id>\d+); name: (?P<name>.+?); "
    r"status: (?P<status>\w+); exit: (?P<exit>[^;]+); "
    r"error: (?P<error>[^;]+); workDir: (?P<workdir>[^;\]]+)"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# The container invocation inside .command.run, wherever it appears on the line.
_ENGINE_RE = re.compile(r"\b(docker|podman)\s+run\b"
                        r"|\b(singularity|apptainer)\s+(?:exec|run)\b")
_TIMESTAMP_RE = re.compile(r"^([A-Z][a-z]{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{1,6})")


@dataclass
class Task:
    hash: str = ""            # short hash as shown in console, e.g. "bf/407183"
    name: str = ""
    status: str = "-"
    exit: str = "-"
    workdir: str = ""
    order: int = field(default=0)  # first-seen order, for stable sorting
    cached: bool = False      # reused from a previous run (-resume / storeDir)
    attempts: int = 1         # >1 once Nextflow re-submitted it (errorStrategy retry)
    finished_at: float | None = None   # log time of its completion, for throughput


def _line_time(line: str) -> float | None:
    """Epoch seconds from a log line's 'Jul-15 15:24:40.083' prefix.

    Nextflow omits the year, so a fixed leap year is used — only differences
    between timestamps are ever used, so the absolute value doesn't matter.
    """
    m = _TIMESTAMP_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime("2024 " + m.group(1),
                                 "%Y %b-%d %H:%M:%S.%f").timestamp()
    except ValueError:
        return None


# "... nextflow.Session - Work-dir: /path/to/work [Mac OS X]" — the trailing
# bracket is the OS name, not part of the path.
_WORKDIR_LINE_RE = re.compile(r"nextflow\.Session\s*-\s*Work-dir:\s*"
                              r"(.+?)(?:\s+\[[^\]]*\])?\s*$")
_REMOTE_RE = re.compile(r"^(s3|gs|az|https?|ftp)://", re.I)


def remote_scheme(workdir: str) -> str | None:
    """'s3' for an object-store work dir, else None.

    Cloud executors (AWS Batch, Google Batch, Azure) keep the work tree in object
    storage, so everything nf-tui reads out of a work directory — task logs,
    output files, .command.trace metrics, the .command.begin that separates
    running from queued — is not on this filesystem at all. What comes from
    .nextflow.log still works, and naming the reason beats a bare
    "not available".
    """
    m = _REMOTE_RE.match(workdir or "")
    return m.group(1).lower() if m else None


# Reading an object store goes through its own CLI rather than a Python SDK:
# anyone running Nextflow on AWS Batch already has `aws` configured, and this
# keeps nf-tui dependency-free. Fetches are for the selected task only, run in a
# worker thread, and cached — a call costs hundreds of milliseconds, so none of
# this may touch the 1s refresh path.
REMOTE_TOOLS = {
    "s3": {"bin": "aws",
           "cat": ["aws", "s3", "cp", "{uri}", "-"],
           "ls": ["aws", "s3", "ls", "{uri}/"]},
    "gs": {"bin": "gcloud",
           "cat": ["gcloud", "storage", "cat", "{uri}"],
           "ls": ["gcloud", "storage", "ls", "{uri}/"]},
}
REMOTE_TIMEOUT = 30
# Most of an object kept in memory (and in _remote_cache) by remote_cat.
# Comfortably above every caller's own limit, the largest being the in-pane
# preview at VIEW_MAX_LINES * 200.
REMOTE_CAT_MAX = 1_000_000
_remote_cache: dict[tuple[str, str], object] = {}


def remote_tool(scheme: str | None) -> dict | None:
    """The CLI for this scheme, if it is installed."""
    spec = REMOTE_TOOLS.get(scheme or "")
    return spec if spec and shutil.which(spec["bin"]) else None


def _run_remote(argv: list[str], uri: str) -> str | None:
    cmd = [a.replace("{uri}", uri) for a in argv]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=REMOTE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _run_remote_tail(argv: list[str], uri: str,
                     max_chars: int = REMOTE_CAT_MAX) -> str | None:
    """Run a remote `cat` and keep only its last `max_chars`.

    Not `subprocess.run(capture_output=True)`: that buffers the whole object, so
    a multi-gigabyte task output in S3 was held in memory in full — and then
    stored in `_remote_cache`, which never releases it — to serve a caller that
    wants the last few KB. The transfer itself is unavoidable (object stores
    have no "tail"), but the memory is not.
    """
    cmd = [a.replace("{uri}", uri) for a in argv]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    except (FileNotFoundError, OSError):
        return None
    buf = b""
    deadline = time.monotonic() + REMOTE_TIMEOUT
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                p.kill()
                return None
            if not select.select([p.stdout], [], [], min(left, 1.0))[0]:
                continue
            chunk = p.stdout.read1(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_chars * 4:      # bytes; margin for multibyte UTF-8
                buf = buf[-max_chars * 2:]
        rc = p.wait(timeout=5)
    except Exception:                          # noqa: BLE001
        p.kill()
        return None
    finally:
        if p.stdout is not None:
            try:
                p.stdout.close()
            except OSError:
                pass
    if rc != 0:
        return None
    text = buf.decode("utf-8", errors="replace")
    return text[-max_chars:] if len(text) > max_chars else text


def remote_cat(uri: str, limit: int = 20_000) -> str | None:
    """The tail of an object, as text, or None if absent or unreadable.

    What is cached is the bounded tail, not the object: REMOTE_CAT_MAX is above
    every caller's `limit`, so this is what they'd have got from the full text.
    """
    key = ("cat", uri)
    if key in _remote_cache:
        got = _remote_cache[key]
        return got[-limit:] if isinstance(got, str) else None
    spec = remote_tool(remote_scheme(uri))
    if spec is None:
        return None
    out = _run_remote_tail(spec["cat"], uri)
    _remote_cache[key] = out if out is not None else 0
    return None if out is None else out[-limit:]


def remote_ls(uri: str) -> list[tuple[str, int | None]]:
    """(name, size) for the objects directly under a prefix."""
    key = ("ls", uri)
    if key in _remote_cache:
        got = _remote_cache[key]
        return got if isinstance(got, list) else []
    spec = remote_tool(remote_scheme(uri))
    if spec is None:
        return []
    out = _run_remote(spec["ls"], uri)
    entries: list[tuple[str, int | None]] = []
    for line in (out or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "PRE":                          # aws: a sub-prefix
            entries.append((parts[1].rstrip("/") + "/", None))
        elif len(parts) >= 4 and parts[2].isdigit():   # aws: date time size name
            entries.append((" ".join(parts[3:]), int(parts[2])))
        elif line.startswith(("s3://", "gs://")):      # gcloud: bare URIs
            entries.append((line.rsplit("/", 1)[-1] or line, None))
    _remote_cache[key] = entries
    return entries


def remote_forget(prefix: str) -> None:
    """Drop cached reads under a prefix, so a live task's log can be re-read."""
    for key in [k for k in _remote_cache if k[1].startswith(prefix)]:
        del _remote_cache[key]


def _short_hash(workdir: str) -> str:
    """/…/work/bf/4071830843d52… -> 'bf/407183' (matches console output)."""
    p = Path(workdir)
    return f"{p.parent.name}/{p.name[:6]}"


def split_name(name: str) -> tuple[str, str]:
    """'NFCORE:SRA:FASTQC (SAMPLE1_PE)' -> ('NFCORE:SRA:FASTQC', 'SAMPLE1_PE')."""
    if name.endswith(")") and " (" in name:
        proc, tag = name.rsplit(" (", 1)
        return proc, tag[:-1]
    return name, ""


def is_failed(t: "Task") -> bool:
    if t.status.upper() in ("FAILED", "ABORTED"):
        return True
    e = t.exit.strip()
    return e not in ("-", "0", "")


def is_done(t: "Task") -> bool:
    # CACHED/STORED tasks succeeded in an earlier run; -resume reuses their
    # results, so they count as done for progress and process rollups.
    return t.status.upper() in ("COMPLETED", "CACHED", "STORED")


def iter_lines(path: Path) -> Iterator[str]:
    """Yield a text file's lines (newline stripped), one at a time.

    The reason this exists rather than `read_text().splitlines()`: the log scans
    below run on every refresh tick of a live run, and a long pipeline's
    .nextflow.log reaches hundreds of MB. Slurping one measured ~3.7x its size
    in peak RSS — 600 MB on a 161 MB log — for a pass that never looks
    backwards. Streaming holds a line at a time. A missing or unreadable file
    yields nothing, matching the callers' existing "return empty" behaviour.
    """
    try:
        with path.open("r", errors="replace") as fh:
            for raw in fh:
                yield raw.rstrip("\n")
    except OSError:
        return


def parse_log(log_file: Path) -> list[Task]:
    """Parse a .nextflow.log into a list of Tasks, keyed by short hash.

    TaskHandler lines are authoritative for status/exit/workdir (last one
    wins). Submitted lines fill in the process name / hash for tasks that
    haven't produced a handler line yet.
    """
    tasks: dict[str, Task] = {}
    seen = 0
    if not log_file.exists():
        return []
    for line in iter_lines(log_file):
        m = _HANDLER_RE.search(line)
        if m:
            key = _short_hash(m["workdir"].strip())
            t = tasks.get(key)
            if t is None:
                t = Task(hash=key, order=seen)
                seen += 1
                tasks[key] = t
            t.name = m["name"].strip()
            t.status = m["status"].strip()
            t.exit = m["exit"].strip()
            t.workdir = m["workdir"].strip()
            if t.status.upper() == "COMPLETED":
                t.finished_at = _line_time(line) or t.finished_at
            continue
        m = _RUNTYPE_RE.search(line)
        if m:
            raw_hash, runtype, name = m.group(1), m.group(2), m.group(3).strip()
            # storeDir tasks log a literal "[skipping]" instead of a hash, so key
            # them by name — otherwise every stored task collides on one entry.
            key = raw_hash if raw_hash != "skipping" else f"stored:{name}"
            t = tasks.get(key)
            if t is None:
                t = Task(hash=("-" if raw_hash == "skipping" else raw_hash),
                         order=seen)
                seen += 1
                tasks[key] = t
            if not t.name:
                t.name = name
            if runtype == "Re-submitted":
                t.attempts += 1        # errorStrategy retry: a fresh attempt
            # Only claim CACHED/STORED while no handler line has spoken for this
            # task — a handler is authoritative about what actually ran.
            if t.status == "-":
                if runtype == "Cached":
                    t.status, t.exit, t.cached = "CACHED", "0", True
                elif runtype == "Stored":
                    t.status, t.exit, t.cached = "STORED", "0", True
                else:
                    t.status = "SUBMITTED"
    out = sorted(tasks.values(), key=lambda t: t.order)
    _fill_cached_workdirs(log_file, out)
    return out


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# A task Nextflow has created but not finished. NEW is easy to miss and real:
# it means created-but-not-yet-submitted, and a live log carries plenty of them
# (a demo recording showed three queued tasks counted as nothing at all).
IN_FLIGHT = ("NEW", "SUBMITTED", "RUNNING")

QUEUE_MAX_ROWS = 300      # rows rendered in the queue view
QUEUE_FS_LIMIT = 2_000    # above this many in-flight tasks, skip the fs check


def task_state(t: Task) -> str:
    """A scheduler-style state: cached / failed / done / running / pending.

    The log can't tell "queued" from "executing" — Nextflow only writes a
    TaskHandler line when a task *completes*. But it writes .command.begin in
    the work dir the moment a task starts, so that file is the real signal.
    Verified against a run pinned to maxForks 3: exactly 3 showed as running.
    """
    if t.cached:
        return "cached"
    if is_failed(t):
        return "failed"
    if is_done(t):
        return "done"
    if t.workdir:
        try:
            if (Path(t.workdir) / ".command.begin").exists():
                return "running"
        except OSError:
            pass
    return "pending"


def task_started_at(t: Task) -> float | None:
    """When a running task began, from .command.begin's mtime."""
    if not t.workdir:
        return None
    try:
        return (Path(t.workdir) / ".command.begin").stat().st_mtime
    except OSError:
        return None


@dataclass
class Progress:
    total: int = 0
    done: int = 0
    failed: int = 0
    cached: int = 0
    running: int = 0        # started executing (.command.begin written)
    pending: int = 0        # submitted to the executor, not started yet
    per_min: float | None = None   # completions/min, from the recent window
    eta_secs: float | None = None  # to clear the CURRENT queue, not the whole run

    @property
    def in_flight(self) -> int:
        return self.running + self.pending

    @property
    def pct(self) -> int:
        return round(100 * self.done / self.total) if self.total else 0


def progress_of(tasks: list[Task], window: float = 300.0,
                check_fs: bool = False) -> Progress:
    """Counts, throughput and a queue ETA.

    With check_fs, in-flight tasks are split into running vs pending by looking
    for .command.begin (one stat each); otherwise they all count as pending,
    which is what the log alone can tell us.

    The ETA covers only the tasks already submitted. Nextflow's total is not
    knowable mid-run — channels keep emitting — so a "time until the pipeline
    finishes" would be invented. This answers the honest question: at the
    current rate, how long to drain what's in flight right now.
    """
    p = Progress(total=len(tasks))
    finished: list[float] = []
    for t in tasks:
        if is_done(t):
            p.done += 1
        if is_failed(t):
            p.failed += 1
        if t.cached:
            p.cached += 1
        if t.status.upper() in IN_FLIGHT:
            if check_fs and task_state(t) == "running":
                p.running += 1
            else:
                p.pending += 1
        if t.finished_at is not None:
            finished.append(t.finished_at)

    if len(finished) >= 2:
        finished.sort()
        recent = [f for f in finished if finished[-1] - f <= window]
        if len(recent) < 3:                 # a slow run: fall back to the last few
            recent = finished[-5:]
        span = recent[-1] - recent[0]
        if span > 0:
            p.per_min = (len(recent) - 1) / span * 60
            if p.per_min > 0 and p.in_flight:
                p.eta_secs = p.in_flight / p.per_min * 60
    return p


# When a task fails Nextflow writes a multi-line report to the log: the cause,
# the command it ran, the exit status, stderr, and the work dir. It's the single
# most useful thing in the file, and it ends at the next timestamped line.
_ERR_START_RE = re.compile(r"ERROR .*? - Error executing process > '(?P<name>.+)'")
_TIMESTAMPED_RE = re.compile(r"^[A-Z][a-z]{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ ")
ERROR_MAX_LINES = 120
# Guard for a malformed log with no timestamps: without it one "block" could
# swallow the entire file. Far above any real Nextflow error report.
ERROR_BLOCK_MAX_LINES = 5_000


def parse_errors(log_file: Path) -> dict[str, str]:
    """Map a failed task's short hash -> its 'Error executing process' block.

    Keyed by the block's own "Work dir:" so it lands on the exact task; blocks
    without one fall back to being keyed by process name.
    """
    out: dict[str, str] = {}

    def emit(name: str, block: list[str]) -> None:
        # the line after "Work dir:" is the path — that identifies the task
        key = None
        for k, line in enumerate(block):
            if line.strip() == "Work dir:" and k + 1 < len(block):
                wd = block[k + 1].strip()
                if wd:
                    key = _short_hash(wd)
                break
        text = "\n".join(block).rstrip()
        if key:
            out[key] = text
        # Also index by process name: a retried attempt fails under the same
        # name but a different work dir, and Nextflow reports only once.
        out.setdefault(f"name:{name}", text)

    # Streamed for the same reason as parse_log — see iter_lines. A block runs
    # from an "Error executing process" line to the next timestamped one, which
    # needs no lookahead, only a little state.
    name: str | None = None
    block: list[str] = []
    for line in iter_lines(log_file):
        if name is not None:
            if not _TIMESTAMPED_RE.match(line):
                # A log with no timestamps at all would otherwise accumulate the
                # whole file here; the report itself is only ERROR_MAX_LINES.
                if len(block) < ERROR_BLOCK_MAX_LINES:
                    block.append(_strip_ansi(line))
                continue
            emit(name, block)                 # this line ends the block …
            name, block = None, []            # … and may start the next one
        m = _ERR_START_RE.search(line)
        if m:
            name = m.group("name").strip()
            block = [_strip_ansi(line.split(" - ", 1)[-1])]
    if name is not None:
        emit(name, block)
    return out


def error_summary(block: str) -> str:
    """The one line worth putting in a header: what actually went wrong."""
    lines = [l.strip() for l in block.splitlines()]
    for k, line in enumerate(lines):
        if line == "Caused by:":
            for follow in lines[k + 1:]:
                if follow:
                    return follow
    return lines[0] if lines else ""


def command_error(block: str) -> str:
    """The `Command error:` section of a Nextflow error report.

    This is usually the actual reason a task died — the tool's own stderr —
    where `Caused by:` only gives Nextflow's framing ("terminated with an error
    exit status (1)"), which says what happened rather than why. A real example:
    the Caused-by line said "exit status (1)" while this section said
    `error during connect: ... docker.sock ... EOF`, i.e. the container runtime
    had gone away.

    Sections in the report start unindented and their content is indented, so
    the block ends at the next unindented line.
    """
    out: list[str] = []
    grabbing = False
    for raw in block.splitlines():
        if not grabbing:
            if raw.strip() == "Command error:":
                grabbing = True
            continue
        if raw.strip() and not raw.startswith((" ", "\t")):
            break                       # the next section began
        out.append(raw.strip())
    text = "\n".join(out).strip()
    return "" if text == "(empty)" else text


def why_failed(block: str) -> str:
    """The most informative one-liner available for a failure.

    Prefers what the command itself printed; falls back to Nextflow's summary
    when the command said nothing.
    """
    err = command_error(block)
    if err:
        first = next((l for l in err.splitlines() if l.strip()), "")
        return first.strip()
    return error_summary(block)


def find_work_root(log_file: Path) -> str:
    """Where this run's work tree lives, as written — a path or an s3:// URI.

    Cached (-resume) tasks are logged without a workDir, so the tree has to be
    found: an explicit -w/-work-dir on the launch command, then an nf-core style
    "workDir : <path>" banner line, else Nextflow's default of <launch dir>/work.

    Returned as a string rather than a Path because a cloud root must survive
    intact: Path("s3://bucket/work") collapses the double slash to "s3:/bucket",
    which is not a URI anything can fetch.
    """
    fallback = ""
    try:
        with log_file.open("r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 400:
                    break
                # Nextflow's own Session logs the *resolved* work dir near the
                # top, whatever set it. That makes it the authoritative answer
                # and the only one that survives `workDir` being set in a
                # nextflow.config instead of on the command line — in which
                # case the launch line carries no -w at all, and a resumed run
                # (whose tasks have no workDir of their own) resolved nothing.
                #   ... nextflow.Session - Work-dir: /scratch/wk [Mac OS X]
                w = _WORKDIR_LINE_RE.search(line)
                if w:
                    val = w.group(1).strip()
                    return (val if remote_scheme(val)
                            else str(Path(val).expanduser()))
                # A completed task's own work dir gives the root two levels up.
                # Kept as a fallback for logs with no Work-dir line at all.
                if not fallback:
                    h = _HANDLER_RE.search(line)
                    if h:
                        wd = h["workdir"].strip()
                        if not remote_scheme(wd):
                            parent = Path(wd).parent.parent
                            if str(parent) not in ("/", "."):
                                fallback = str(parent)
                if "$> nextflow" in line:
                    toks = _strip_ansi(line).split()
                    for flag in ("-w", "-work-dir", "--work-dir"):
                        if flag in toks:
                            j = toks.index(flag)
                            if j + 1 < len(toks):
                                val = toks[j + 1]
                                return (val if remote_scheme(val)
                                        else str(Path(val).expanduser()))
                else:
                    # nf-core prints a banner line: "workDir : /abs/path" (or an
                    # s3:// URI when the run is on Batch).
                    b = re.match(r"\s*workDir\s*:\s*(\S+)", _strip_ansi(line))
                    if b and (b.group(1).startswith("/")
                              or remote_scheme(b.group(1))):
                        return b.group(1)
    except OSError:
        pass
    return fallback or str(log_file.parent / "work")


def index_workdirs(work_root: Path) -> dict[str, str]:
    """Map 'ab/cdef12' -> the full work dir, by scanning <work>/??/* once."""
    index: dict[str, str] = {}
    try:
        groups = sorted(work_root.iterdir())
    except OSError:
        return index
    for g in groups:
        if not g.is_dir() or len(g.name) != 2:
            continue
        try:
            for d in g.iterdir():
                if d.is_dir():
                    index.setdefault(f"{g.name}/{d.name[:6]}", str(d))
        except OSError:
            continue
    return index


def resolve_workdir(work_root: Path, task_hash: str) -> str | None:
    """'ab/cdef12' -> the full work dir, by listing just <work>/ab/.

    Targeted on purpose: an in-flight task has no workDir in the log (Nextflow
    only records it when the task completes), and scanning the whole work tree
    on every refresh of a live run would be far too expensive.
    """
    if "/" not in task_hash:
        return None
    group, prefix = task_hash.split("/", 1)
    try:
        for d in (work_root / group).iterdir():
            if d.name.startswith(prefix) and d.is_dir():
                return str(d)
    except OSError:
        pass
    return None


def _fill_cached_workdirs(log_file: Path, tasks: list[Task]) -> None:
    """Give cached tasks their work dir. They come from an earlier run, so the
    directories already exist — which is what makes their logs, output files and
    resource metrics viewable at all. Scans the work tree once, and only when
    some task actually needs it."""
    # Only cached tasks need this. A merely-submitted task gets its workDir from
    # the handler line moments later, and scanning the work tree for those would
    # re-scan on every 1s refresh of a live run.
    need = [t for t in tasks if t.cached and not t.workdir and t.hash != "-"]
    if not need:
        return
    root = find_work_root(log_file)
    if remote_scheme(root):
        return              # an object store has no local tree to scan
    index = index_workdirs(Path(root))
    if not index:
        return
    for t in need:
        wd = index.get(t.hash)
        if wd:
            t.workdir = wd


def _read_all(path: Path, limit: int = 20000) -> str:
    """The tail of a task's .command.sh, for the failure view.

    Seeks like _tail_text rather than reading the file and slicing: a generated
    .command.sh is usually a few KB, but a process that interpolates a large
    channel into its script can produce a very big one, and nothing here knew
    the difference.
    """
    text = _tail_text(path, limit)
    return text if text is not None else f"[cannot read {path.name}]"


def pager_bin() -> str | None:
    """`less`, or None if it isn't installed (callers must not exec a missing
    command and flash the screen).

    Deliberately not `zless`: it runs `gzip -cdfq file | less`, which makes the
    input a pipe. less cannot seek in a pipe, so `+G` has to read the whole file
    before painting anything — on a 138MB .nextflow.log that never finished,
    versus 0.02s for less on the file directly. Only real .gz needs decompressing
    (see _pager_command), and a run log is never gzipped.
    """
    return "less" if shutil.which("less") else None


# less's status line. `q` is the only way out — Esc cannot be rebound to quit,
# because ESC is the first byte of every arrow/function key: less waits after a
# lone ESC (so the binding never fires) while `ESC [ B` from a Down arrow *does*
# match it and kills the pager. So instead of remapping, say how to leave.
# `?e(END):%f.` prints "(END)" at end of file, otherwise the file name.
PAGER_PROMPT = "q quit   / search   G end   h help   —   ?e(END):%f."


def pager_flags(path: Path) -> str:
    """less options for paging `path`: `-R`, plus `-n` once it gets big.

    Reaching end-of-file makes less number every line of it, and **quitting waits
    for that count to finish**. Measured on a 10 GB task output: `less -R +G`
    painted the tail in 0.11s but then took 56s to exit, and pressing `G` in an
    ordinary `less -R` cost 52s on the way out. With `-n` both quit in 0.2s.

    The stall is invisible until a file is huge, so line numbers stay on below
    the threshold, where counting is free and `=`, `v` and `1234G` keep working.
    """
    try:
        big = path.stat().st_size > LESS_LINENUM_MAX
    except OSError:
        big = False
    return "-Rn" if big else "-R"


def read_back(path: Path, end: int, max_bytes: int = RUNLOG_CHUNK,
              max_lines: int = RUNLOG_MAX_LINES) -> tuple[int, list[str]]:
    """Read the chunk of `path` that ends at byte offset `end`, newest-last.

    Returns (start, lines) where `start` is the byte offset of the first line —
    feed it back as `end` to walk further up the file. Only whole lines are
    returned: a partial line at the front of the window is dropped and skipped
    over in `start`. All slicing is done in bytes so `start` stays exact even
    when decoding replaces malformed characters.
    """
    start = max(0, end - max_bytes)
    try:
        with path.open("rb") as f:
            f.seek(start)
            buf = f.read(end - start)
    except OSError:
        return end, []
    if start > 0:                       # first line is probably cut in half
        nl = buf.find(b"\n")
        if nl == -1:
            return end, []
        start += nl + 1
        buf = buf[nl + 1:]
    parts = buf.split(b"\n")
    if len(parts) > max_lines:           # keep the newest `max_lines` of them
        skipped = b"\n".join(parts[:len(parts) - max_lines]) + b"\n"
        start += len(skipped)
        buf = buf[len(skipped):]
    return start, buf.decode("utf-8", errors="replace").splitlines()


# Container-engine chatter Nextflow captures into .command.log/.err while
# pulling the image, plus benign tool/JVM environment noise (FASTQC etc. run
# with $HOME unset). None of it is task output, so we hide it by default.
_NOISE_RE = re.compile(
    "|".join([
        r"^[0-9a-f]{8,}: ",                                  # docker layer progress
        r"^Unable to find image ",
        r": Pulling from ",
        r"^Digest: sha256:",
        r"^Status: (Downloaded newer image|Image is up to date)",
        r"platform .* does not match the detected host platform",
        r"^INFO:    ",                                       # singularity / apptainer
        r"^Fontconfig error",                                # JVM tools w/o $HOME
        r"^Picked up _?JAVA_",
        r"prefs root node",
        r"Couldn't (flush|read) .*prefs",
    ])
)

# Directory names Nextflow/JVM tools leave behind that are never real outputs.
JUNK_NAMES = {"?", "null"}


def is_container_noise(line: str) -> bool:
    return bool(_NOISE_RE.search(line))


def strip_noise(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not is_container_noise(ln))


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def human_duration(ms: float) -> str:
    """6265 -> '6.3s', 95000 -> '1m35s', 3720000 -> '1h02m'."""
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


@dataclass
class Metrics:
    """The per-task numbers from a work dir's .command.trace (if tracing was
    on). All optional — the file is absent when tracing is disabled."""
    realtime_ms: int | None = None
    pct_cpu: float | None = None
    peak_rss_kb: int | None = None
    pct_mem: float | None = None

    def has_data(self) -> bool:
        return self.realtime_ms is not None


def parse_trace(workdir: str) -> Metrics:
    """Read <workdir>/.command.trace (Nextflow's `key=value` resource dump)."""
    if not workdir:
        return Metrics()
    try:
        text = (Path(workdir) / ".command.trace").read_text(errors="replace")
    except OSError:
        return Metrics()
    d: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()

    def num(key: str) -> float | None:
        try:
            return float(d[key])
        except (KeyError, ValueError):
            return None

    rt, cpu, rss, mem = num("realtime"), num("%cpu"), num("peak_rss"), num("%mem")
    return Metrics(
        realtime_ms=int(rt) if rt is not None else None,
        pct_cpu=cpu,
        peak_rss_kb=int(rss) if rss is not None else None,
        pct_mem=mem,
    )


def read_forward(path: Path, offset: int = 0,
                 max_lines: int = VIEW_MAX_LINES) -> tuple[int, list[str], bool]:
    """Read up to `max_lines` starting at byte `offset`.

    Returns (next_offset, lines, at_eof) so a caller can resume exactly where it
    stopped. That is what lets a preview grow as you scroll rather than deciding
    up front how much of a file to materialise — the same thing `less` does, and
    the reason it opens a 10 GB file instantly.

    Binary handle, decoded per line: `tell()` is only dependable on a binary
    file, and the offset has to stay exact from one chunk to the next.
    """
    out: list[str] = []
    try:
        with path.open("rb") as f:
            f.seek(offset)
            while len(out) < max_lines:
                raw = f.readline()
                if not raw:
                    break
                out.append(raw.rstrip(b"\n").decode("utf-8", errors="replace"))
            pos = f.tell()
            at_eof = not f.peek(1)
    except OSError:
        return offset, [], True
    return pos, out, at_eof


def looks_binary(path: Path, maxbytes: int = 8192) -> bool:
    try:
        with path.open("rb") as f:
            return b"\x00" in f.read(maxbytes)
    except OSError:
        return False


def is_gzip(path: Path) -> bool:
    return path.name.lower().endswith((".gz", ".bgz", ".bgzf"))


def decode_tool(path: Path) -> str | None:
    """A binary format that needs a tool from the task's container to read,
    or None if the host can read it directly (text, or gz via the host)."""
    name = path.name.lower()
    if name.endswith((".bam", ".cram")):
        return "samtools view -h"
    if name.endswith(".bcf"):
        return "bcftools view"
    return None


def head_gzip(path: Path, lines: int, skip: int = 0) -> list[str]:
    """`lines` lines from a gzip file, starting after `skip` of them.

    A deflate stream has no seekable line offset, so resuming means
    decompressing from the start again — but it is *streamed*, so memory stays
    at one line whatever the file's size. That trade is what lets a gzipped
    output keep loading as you scroll instead of stopping at a cap.
    """
    try:
        with gzip.open(path, "rt", errors="replace") as f:
            out = []
            for i, line in enumerate(f):
                if i < skip:
                    continue
                if len(out) >= lines:
                    break
                out.append(line.rstrip("\n"))
            return out
    except OSError as e:
        return [f"(cannot read gzip: {e})"]


def head_text(path: Path, lines: int) -> list[str]:
    """The first `lines` lines of a text file, read as a stream.

    Deliberately not `read_text().splitlines()[:lines]`. That materialises the
    entire file before throwing almost all of it away, which measured ~2.1x the
    file's size in peak RSS and ~2.8s per GB: a 10 GB task output would have
    exhausted a 24 GB host before showing a single line. Pipeline tasks do emit
    files that big, so the cap has to apply while reading, not after.
    """
    try:
        with path.open("r", errors="replace") as f:
            out = []
            for i, line in enumerate(f):
                if i >= lines:
                    break
                out.append(line.rstrip("\n"))
            return out
    except OSError as e:
        return [f"(cannot read: {e})"]


def parse_container_run(workdir: str) -> tuple[str, list[str], str] | None:
    """Reuse the task's own container invocation from .command.run.

    Returns (engine, mount_args, image). mount_args are the -v/-B bind flags
    exactly as Nextflow set them (usually the whole work tree), so staged
    symlinks — including CRAM/BAM reference genomes — resolve just like they
    did for the task itself.
    """
    try:
        text = (Path(workdir) / ".command.run").read_text(errors="replace")
    except OSError:
        return None
    # The invocation is rarely at the start of the line: Nextflow prefixes the
    # singularity one with environment setup —
    #   set +u; env - PATH="$PATH" ${TMP:+SINGULARITYENV_TMP="$TMP"} singularity exec ...
    # — so match the engine anywhere and read from there. Anchoring on the start
    # of the line meant every Singularity/Apptainer run parsed as "no container",
    # which is the HPC case this feature exists for.
    engine = line = None
    for raw in text.splitlines():
        s = raw.strip()
        m = _ENGINE_RE.search(s)
        if m:
            engine = m.group(1) or m.group(2)
            line = s[m.start():]       # drop the env prefix before the engine
            break
    if line is None:
        return None
    try:
        toks = shlex.split(line)
    except ValueError:
        toks = line.split()

    mount_flags = {"-v", "--volume", "-B", "--bind", "--mount"}
    mounts: list[str] = []
    image = None
    i = 0
    while i < len(toks):
        tk = toks[i]
        if tk in mount_flags and i + 1 < len(toks):
            val = toks[i + 1].replace("$NXF_TASK_WORKDIR", workdir)
            flag = "-v" if tk in ("-v", "--volume") else tk
            mounts += [flag, val]
            i += 2
            continue
        if tk in ("/bin/bash", "/bin/sh", "bash", "sh") and i > 0:
            image = toks[i - 1]        # the image sits right before the shell
            break
        i += 1
    if image is None:
        cand = [t for t in toks
                if (("/" in t and ":" in t) or t.endswith((".sif", ".img")))
                and not t.startswith(("-", "/"))]
        image = cand[-1] if cand else None
    if image is None:
        return None
    return engine, mounts, image


def task_container(workdir: str) -> tuple[str, str] | None:
    """(engine, image) — for display labels."""
    spec = parse_container_run(workdir)
    return (spec[0], spec[2]) if spec else None


def find_tool_image(launch_dir: Path, binary: str) -> str | None:
    """Find a locally-present image used somewhere in this run that provides
    `binary` (samtools/bcftools) — for viewing files whose own task container
    doesn't ship the tool (e.g. a GATK task that emits a CRAM)."""
    work = launch_dir / "work"
    if not work.is_dir():
        return None
    # Collect the images this run used, keeping the engine each came with.
    candidates: dict[str, str] = {}          # image -> engine
    try:
        groups = sorted(work.iterdir())
    except OSError:
        return None
    for g in groups:
        if not g.is_dir():
            continue
        for cr in g.glob("*/.command.run"):
            spec = parse_container_run(str(cr.parent))
            if spec:
                candidates.setdefault(spec[2], spec[0])

    # Try images whose name mentions the tool first, then the rest. The name is
    # only a hint: an "htslib" image sounds like it has samtools and does not
    # (htslib ships tabix and bgzip), which silently produced
    # "sh: 1: samtools: not found" instead of a decoded CRAM.
    ranked = sorted(candidates, key=lambda i: binary not in i.lower())
    for img in ranked:
        engine = candidates[img]
        if not _image_has(engine, img, binary):
            continue
        return img
    return None


def _image_has(engine: str, image: str, binary: str) -> bool:
    """Is `binary` actually runnable inside this image? Checked rather than
    guessed from the image name, and only for images already pulled — nothing
    here should start a download."""
    try:
        if engine in ("docker", "podman"):
            present = subprocess.run([engine, "image", "inspect", image],
                                     capture_output=True, timeout=15)
            if present.returncode != 0:
                return False
            probe = [engine, "run", "--rm", "--entrypoint", "sh", image,
                     "-c", f"command -v {shlex.quote(binary)}"]
        else:                                  # singularity / apptainer
            probe = [engine, "exec", image, "sh", "-c",
                     f"command -v {shlex.quote(binary)}"]
        return subprocess.run(probe, capture_output=True,
                              timeout=60).returncode == 0
    except Exception:                          # noqa: BLE001
        return False


class Follower:
    """Incremental file tailer: read_new() returns only the bytes appended
    since the last call (like `tail -f`). Handles truncation/rotation."""

    def __init__(self, path: Path):
        self.path = path
        self.pos = 0

    def read_new(self) -> str:
        try:
            size = self.path.stat().st_size
        except OSError:
            return ""
        if size < self.pos:      # file was truncated or replaced
            self.pos = 0
        if size == self.pos:
            return ""
        if size - self.pos > FOLLOW_MAX_CATCHUP:
            # A chatty task can emit gigabytes between two ticks. Skip to the
            # newest window rather than pulling all of it into the pane: this is
            # a tail, so the recent end is the part worth showing.
            self.pos = size - FOLLOW_MAX_CATCHUP
        try:
            with self.path.open("r", errors="replace") as fh:
                fh.seek(self.pos)
                data = fh.read()
                self.pos = fh.tell()
        except OSError:
            return ""
        return data


# One colour per state, used for both the tree and the queue view so a glance
# means the same thing everywhere.
STATE_STYLE = {
    "failed": "bold red",
    "cached": "dim cyan",
    "done": "green",
    "running": "bold yellow",
    "pending": "blue",
}
# Peak memory at or above this is worth noticing when hunting an OOM.
BIG_MEM_KB = 4 * 1024 * 1024        # 4 GB

TAG_W, STATUS_W, EXIT_W = 30, 10, 12   # tree column widths


def _label_state(t: Task) -> str:
    """The state a label should be coloured by (cheap: no filesystem)."""
    if is_failed(t):
        return "failed"
    if t.cached:
        return "cached"
    if is_done(t):
        return "done"
    return "pending"


def progress_bar(done: int, total: int, width: int = 12) -> tuple[str, str]:
    """('████████', '░░░░') — filled and empty kept apart so the caller can
    colour them differently. A run with no tasks yet still gets a full track
    rather than nothing, so the header doesn't look broken before the first
    task appears."""
    if total <= 0:
        return "", "░" * width
    filled = min(width, round(width * done / total))
    return "█" * filled, "░" * (width - filled)


class NfHeader(Static):
    """The top bar. A plain Static rather than Textual's Header, because Header
    renders its subtitle as unstyled text (so the progress bar could not be
    coloured) and docks an 8-column ⭘ icon for a command palette nf-tui does
    not use."""

    DEFAULT_CSS = """
    NfHeader {
        dock: top; height: 1; width: 100%;
        background: $panel; color: $foreground;
        content-align: center middle;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    """


def _proc_label(proc: str, tasks: list[Task]) -> Text:
    """'FASTQC   3/3 ✓' — last path segment, done/total, status icon."""
    short = proc.split(":")[-1]
    total = len(tasks)
    done = sum(is_done(t) for t in tasks)
    failed = sum(is_failed(t) for t in tasks)
    cached = sum(t.cached for t in tasks)
    if failed:
        icon, style = f"✗ {failed} failed", STATE_STYLE["failed"]
    elif done == total:
        if cached == total:
            icon, style = "⟲ cached", STATE_STYLE["cached"]
        else:
            icon, style = "✓", STATE_STYLE["done"]
    else:
        icon, style = "…", STATE_STYLE["running"]
    if cached and cached != total:
        icon += f"  ({cached} cached)"
    label = Text()
    label.append(short, style="bold")
    label.append(f"   {done}/{total} ", style="dim")
    label.append(icon, style=style)
    return label


def _task_label(t: Task, m: "Metrics | None" = None,
                state: str | None = None) -> Text:
    """A tree row, coloured by state and laid out in fixed columns so
    durations and memory line up down the pane instead of drifting with the
    length of each task's name.

    `state` overrides the log-derived one: Nextflow reports a task as SUBMITTED
    until it finishes, so without it the tree would label a task the header is
    counting as running "SUBMITTED".
    """
    _, tag = split_name(t.name)
    state = state or _label_state(t)
    style = STATE_STYLE[state]
    mark = {"failed": "✗", "cached": "⟲", "done": "✓",
            "running": "▶"}.get(state, "•")
    # Show the state we actually determined, not the log's stale wording.
    shown = "RUNNING" if state == "running" else t.status

    exit_str = "" if t.exit in ("-", "") else f"exit={t.exit}"
    if t.attempts > 1:
        exit_str += f" retry×{t.attempts - 1}"

    label = Text()
    label.append(f"{mark} ", style=style)
    label.append(f"{(tag or t.hash)[:TAG_W]:<{TAG_W}} ")
    label.append(f"{shown[:STATUS_W]:<{STATUS_W}} ", style=style)
    label.append(f"{exit_str:<{EXIT_W}}",
                 style=STATE_STYLE["failed"] if exit_str.startswith("exit=")
                 and t.exit not in ("0",) else "dim")
    if m is not None and m.has_data():
        label.append(f"{human_duration(m.realtime_ms):>8}", style="dim")
        if m.peak_rss_kb:
            big = m.peak_rss_kb >= BIG_MEM_KB
            label.append(f"{human_size(m.peak_rss_kb * 1024):>11}",
                         style="bold magenta" if big else "dim")
    return label


class LogView(RichLog):
    """RichLog with less-style paging keys (active when the pane is focused).
    Built-ins already give ↑/↓, PageUp/PageDown, Home=top, End=bottom, wheel."""

    BINDINGS = [
        Binding("space,ctrl+f", "page_down", "Page down", show=False),
        Binding("b,ctrl+b", "page_up", "Page up", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
        Binding("home", "scroll_home", "Top", show=False),
        Binding("end", "scroll_end", "Bottom", show=False),
    ]

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        # Near the top of the run log? Pull in the previous chunk of the file.
        if new_value <= 2:
            for name in ("_runlog_backfill", "_tasklog_backfill"):
                fn = getattr(self.app, name, None)
                if fn is not None:
                    fn()          # each is a no-op outside its own view
        # Near the bottom of a file preview? Pull in the next chunk, so a big
        # file keeps going as you scroll instead of stopping at a fixed cap.
        if self.max_scroll_y and new_value >= self.max_scroll_y - 2:
            extend = getattr(self.app, "_preview_extend", None)
            if extend is not None:
                extend()


class FileList(OptionList):
    """File list whose paging keys scroll the sibling content pane, so you can
    browse files (↑/↓, Enter to open) and page the open file without a focus
    dance. Up/Down still move the file selection."""

    BINDINGS = [
        Binding("pagedown,space,ctrl+f", "scroll_content('page_down')", show=False),
        Binding("pageup,ctrl+b", "scroll_content('page_up')", show=False),
        Binding("G", "scroll_content('scroll_end')", show=False),
        Binding("less_than_sign", "scroll_content('scroll_home')", show=False),
    ]

    def action_scroll_content(self, method: str) -> None:
        try:
            log = self.app.query_one("#log", RichLog)
        except Exception:      # noqa: BLE001
            return
        getattr(log, f"action_{method}")()


class NfScope(App):
    TITLE = "nf-tui"
    # The palette icon sits over the task tree and its tooltip covers the first
    # rows; nf-tui has no palette commands of its own, so it only gets in the way.
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #tasks { height: 1fr; border: round $panel; }
    #tasks:focus { border: round $accent; }
    #bottom { height: 50%; }
    #files { width: 38%; border: round $panel; display: none; }
    #files:focus { border: round $accent; }
    #log { width: 1fr; border: round $panel; padding: 0 1; }
    #log:focus { border: round $accent; }
    #search { display: none; height: 3; border: round $accent; }
    #search.on { display: block; }
    """
    BINDINGS = [
        Binding("tab", "focus_next_pane", "Next pane", show=False),
        Binding("l,enter", "focus_log", "Scroll log"),
        Binding("escape", "back", "Back to tasks"),
        Binding("slash", "search", "Search"),
        Binding("t", "view_task", "Task log"),
        Binding("c", "view_container", "Container log"),
        Binding("d", "view_files", "Files"),
        Binding("L", "pager", "Open in less"),
        Binding("F", "full_file", "Full file"),
        Binding("g", "view_run", "Run log"),
        Binding("p", "view_queue", "Queue"),
        Binding("z,m", "zoom", "Full screen"),
        Binding("f", "toggle_follow", "Follow"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("e", "next_failed", "Next failure"),
        Binding("n", "next_match", "Next match", show=False),
        Binding("N", "prev_match", "Prev match", show=False),
        Binding("x", "toggle_failed", "Failed only"),
        Binding("y", "copy_path", "Copy path"),
        Binding("o", "open_workdir", "Work dir"),
        Binding("r", "refresh", "Refresh"),
        # Shift+Q, not q: quitting is one keystroke from many others and a
        # stray lowercase q should not tear down a session you are watching.
        Binding("K", "stop_pipeline", "Stop run"),
        Binding("Q", "quit", "Quit"),
    ]

    def __init__(self, target: Path):
        super().__init__()
        self.target = target                 # dir to search, or a .nextflow.log
        if target.is_file():
            self.log_file: Path | None = target
            self._runs: list[RunInfo] = []
        else:
            self._runs = gather_runs(target)
            self.log_file = self._runs[0].path if len(self._runs) == 1 else None
        self.tasks: list[Task] = []
        self.failed_only = False
        self.query_str = ""         # / search: substring over task name or hash
        self._search_mode = "tasks"  # what / is searching: tasks | log
        self._log_query = ""        # the log search term
        self._log_matches: list[int] = []   # line numbers that matched
        self._log_i = -1            # which match we are sitting on
        self.sort_mode = "order"    # order | slowest | memory  (s cycles)
        self.web = bool(os.environ.get("NF_TUI_WEB"))  # served in a browser: no less
        # Set when nf-tui launched the pipeline itself, so K can stop it.
        pid = os.environ.get("NF_TUI_PID")
        self.pipeline_pid = int(pid) if pid and pid.isdigit() else None
        self.follow = True
        self.view = "task"   # task | container | files | run | queue
        self._progress: Progress | None = None
        self._placeholder = None                 # the '(no tasks yet)' leaf
        self._auto_selected = False              # have we put the cursor on a task yet
        self._select_tries = 0                   # bounded retries while the tree lays out
        self._live_states: dict[str, str] = {}   # in-flight hash -> running/pending
        self._work_root: Path | None = None      # this run's work/ tree
        self._workdir_cache: dict[str, str] = {}  # task hash -> resolved work dir
        self._sig: tuple | None = None   # skip tree work when nothing changed
        self._built_filter: tuple | None = None  # (failed_only, query) at last full build
        self._shown: tuple | None = None # what the log pane currently shows
        self._tailer: Follower | None = None
        self._task_by_hash: dict[str, Task] = {}
        self._log_stat: tuple | None = None   # (size, mtime) of last-parsed log
        self._force_refresh = False           # re-parse even if the log is unchanged
        self._groups: dict = {}          # proc name -> its tasks (rebuilt per parse)
        self._proc_nodes: dict = {}      # proc name -> TreeNode  (updated in place)
        self._task_nodes: dict = {}      # task hash -> TreeNode
        self._files: list[Path] = []     # entries backing the #files list
        self._files_task: Task | None = None  # task whose dir backs #files
        self._remote_files: list[str] = []    # object URIs, when the dir is remote
        self._last_file: Path | None = None  # last previewed file (for F = full)
        self._tool_image_cache: dict = {}  # binary -> image that provides it
        # task hash -> (status when read, metrics); see _metrics for why misses cache
        self._trace_cache: dict[str, tuple[str, Metrics]] = {}
        self._errors: dict[str, str] = {}     # failed task -> its error report
        self._errors_stat: tuple | None = None   # log stat the errors were read at
        self._runlog_lines: list[str] = []  # run-log lines currently loaded
        self._runlog_start: int = 0      # byte offset of the first loaded line
        self._backfilling = False        # guard: backfill moves scroll_y itself
        # Where the open file preview stopped, so scrolling can resume from it.
        self._view_path: Path | None = None
        self._view_pos: int | None = None
        self._view_eof: bool = True
        self._view_shown: int | None = None   # lines shown, for pipe-based files
        self._extending = False
        # Task-log counterpart of the run log's backfill state. That pane shows
        # a *filtered* view of .command.log, so the raw lines are kept in order
        # to re-emit them when a backfill rewrites the pane.
        self._tasklog_raw: list[str] = []
        self._tasklog_start: int = 0
        self._tasklog_task: Task | None = None

    def compose(self) -> ComposeResult:
        yield NfHeader(id="hdr")
        with Vertical():
            tree: Tree = Tree("processes", id="tasks")
            tree.show_root = False
            tree.guide_depth = 3
            yield tree
            yield Input(placeholder="filter tasks by name or hash — enter to keep, esc to clear",
                        id="search")
            with Horizontal(id="bottom"):
                # Files view: a clickable list on the left…
                yield FileList(id="files")
                # …and the scrollable content pane on the right (also the
                # single log pane for the task / container / run views).
                log = LogView(id="log", highlight=True, wrap=False,
                              markup=False, auto_scroll=True)
                log.can_focus = True
                yield log
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(REFRESH_SECONDS, self._tick)  # live updates
        if self.log_file is not None:
            self.load_run(self.log_file)
        else:
            self._open_picker()             # multiple runs -> choose one first

    def _tick(self) -> None:
        # A transient error on the 1s timer (e.g. the log replaced mid-read on a
        # busy filesystem) must never crash a long-running live session.
        try:
            self.action_refresh()
        except Exception as e:                        # noqa: BLE001
            self.notify(f"refresh error (continuing): {e}", severity="warning")

    # ---- run selection (a pushed screen, so it stays one app) --------------

    def _open_picker(self) -> None:
        self._runs = gather_runs(self.target)   # re-scan for fresh statuses
        if not self._runs:
            self.exit()
            return
        self.push_screen(RunPickerScreen(self.target, self._runs), self._on_run_picked)

    def _on_run_picked(self, path: Path | None) -> None:
        if path is None:
            if self.log_file is None:       # cancelled before any run loaded
                self.exit()
        else:
            self.load_run(path)

    def load_run(self, path: Path) -> None:
        self.log_file = path
        self.sub_title = str(path)
        self.view = "run"          # open on the run log (follows the tail if live)
        self._sig = None
        self._log_stat = None
        self._force_refresh = True
        self._built_filter = None
        self._shown = None
        self._tailer = None
        self._proc_nodes = {}
        self._task_nodes = {}
        self._tool_image_cache = {}
        # Run-scoped caches/state — must NOT carry over, or a task in the new run
        # whose short hash matches one in the old run would show stale metrics,
        # and the old run's file list could be reopened.
        self._trace_cache = {}
        self._work_root = None
        self._workdir_cache = {}
        self._live_states = {}
        self._placeholder = None
        self._auto_selected = False
        self._log_query = ""
        self._log_matches = []
        self._log_i = -1
        self._select_tries = 0
        self._files = []
        self._files_task = None
        self._last_file = None
        self._runlog_lines = []
        self._runlog_start = 0
        tree = self.query_one("#tasks", Tree)
        tree.clear()
        self.query_one("#files", OptionList).display = False
        self.action_refresh()
        # Select the first task so t / L / d act on a task straight away.
        # Deferred: right after the rebuild the new nodes have no line assigned
        # yet, so move_cursor here would silently leave the cursor on the group.
        self.call_after_refresh(self._select_first_task)
        tree.focus()

    def _select_first_task(self) -> None:
        """Put the cursor on a task, so t / d / L act on one straight away.

        Retried until a task exists rather than attempted once: a run opened the
        moment it launches has an empty tree for the first half-minute, and
        giving up then left the cursor parked on a process group — so `d` showed
        an empty file list and `t` a process summary, for the rest of the run.
        """
        if self._auto_selected:
            return
        trees = self.query("#tasks")
        if not trees:
            return
        tree = trees.first(Tree)
        node = tree.cursor_node
        if node is not None and isinstance(node.data, Task):
            self._auto_selected = True      # already on one (user, or earlier us)
            return
        for proc in tree.root.children:
            if proc.children:
                tree.move_cursor(proc.children[0])
                # A node added in this same pass has no line yet, so move_cursor
                # quietly does nothing. Confirm it took, and if not try again
                # once the tree has been laid out.
                landed = tree.cursor_node
                if landed is not None and isinstance(landed.data, Task):
                    self._auto_selected = True
                elif self._select_tries < 20:
                    self._select_tries += 1
                    self.call_after_refresh(self._select_first_task)
                return

    # ---- task list (grouped tree) ------------------------------------------

    def action_refresh(self) -> None:
        if self.log_file is None:           # no run loaded yet (picker is up)
            return
        if len(self.screen_stack) > 1:      # the run picker is on top of us
            return
        # Skip the (whole-file) re-parse when .nextflow.log hasn't grown — the
        # common steady state, and free for completed runs. Still refresh the
        # pane so a live .command.log tail keeps updating.
        try:
            st = self.log_file.stat()
            stat = (st.st_size, st.st_mtime)
        except OSError:
            stat = None
        if stat is not None and stat == self._log_stat and not self._force_refresh:
            self._render_current()
            return
        self._log_stat = stat
        self._force_refresh = False
        self.tasks = parse_log(self.log_file)
        self._task_by_hash = {t.hash: t for t in self.tasks}
        # Group once per parse (not per keypress): proc name -> its tasks.
        self._groups = {}
        for t in self._visible_tasks():
            self._groups.setdefault(split_name(t.name)[0], []).append(t)
        if self.sort_mode != "order":
            self._sort_groups()
        # Header summary so the TOTAL (across all process groups) is visible.
        live = self._run_is_live()
        # Splitting pending from running costs a lookup per in-flight task, so
        # only do it for a live run, and not when the queue is enormous.
        inflight = [t for t in self.tasks
                    if t.status.upper() in IN_FLIGHT]
        check_fs = live and len(inflight) <= QUEUE_FS_LIMIT
        if check_fs:
            self._resolve_inflight_workdirs(inflight)
        prog = progress_of(self.tasks, check_fs=check_fs)
        # Remember which in-flight tasks have actually started, so the tree can
        # say RUNNING where the header counts one — same lookup, reused.
        self._live_states = ({t.hash: task_state(t) for t in inflight}
                             if check_fs else {})
        self._progress = prog
        nproc = len({split_name(t.name)[0] for t in self.tasks})
        # While a run is live the denominator keeps growing — Nextflow only
        # announces tasks as channels emit them — so call it what it is
        # ("seen") rather than implying the run is 93% done when it isn't.
        noun = "seen" if live else "tasks"
        filled, track = progress_bar(prog.done, prog.total)
        summary = (f"{prog.pct}%  {prog.done:,}/{prog.total:,} {noun} "
                   f"· {nproc} processes")
        if prog.cached:
            summary += f" · {prog.cached:,} cached"
        if prog.failed:
            summary += f" · {prog.failed} failed"
        # Live extras: what's in flight, how fast, and how long to drain it.
        if live:
            if prog.running:
                summary += f" · {prog.running:,} running"
            if prog.pending:
                summary += f" · {prog.pending:,} pending"
            if prog.per_min:
                summary += f" · {prog.per_min:.1f}/min"
            if prog.eta_secs:
                summary += f" · ~{human_duration(prog.eta_secs * 1000)} for queued"
        if self.failed_only:
            # x is sticky, and the toast that announced it has long since gone
            # by the time you look at the tree. Without this the header counts
            # every task while the tree shows two, which reads as a broken tree
            # rather than a filter that is still on.
            summary += (f" · showing failed only "
                        f"({prog.failed:,} of {prog.total:,}) — x for all")
        if self.query_str:
            summary += f' · filter "{self.query_str}": {len(self._visible_tasks())} shown'
        if self.sort_mode != "order":
            summary += f" · sorted by {self.sort_mode}"
        loc = str(self.log_file).replace(str(Path.home()), "~")
        self.sub_title = f"{filled}{track} {summary}  —  {loc}"   # window title
        # The bar carries the run's disposition: red if anything failed, yellow
        # while work is still moving, green once it's cleanly done.
        bar_style = ("bold red" if prog.failed
                     else "bold yellow" if live else "bold green")
        header = Text()
        header.append("nf-tui", style="bold")
        header.append("  ")
        header.append(filled, style=bar_style)
        header.append(track, style="dim")
        header.append(f"  {summary}")
        header.append(f"  —  {loc}", style="dim")
        self._set_header(header)
        # A full rebuild (clear + re-add) is needed when the filter OR the sort
        # changes, because the in-place sync only appends/updates, never reorders.
        build_key = (self.failed_only, self.query_str, self.sort_mode)
        sig = (tuple((t.hash, t.status, t.exit) for t in self.tasks)
               + tuple(sorted(self._live_states.items())) + build_key)
        if sig != self._sig:
            self._sig = sig
            if build_key != self._built_filter:
                self._built_filter = build_key
                self._full_rebuild()   # filter/sort changed: repopulate from scratch
            else:
                self._sync_tree()      # in place: never disturbs cursor/focus/scroll
        # A run opened at launch has no tasks for its first half-minute; keep
        # trying until there is one to land on.
        if not self._auto_selected:
            self._select_first_task()
        self._render_current()

    def _visible_tasks(self) -> list[Task]:
        tasks = self.tasks
        if self.failed_only:
            tasks = [t for t in tasks if is_failed(t)]
        q = self.query_str.strip().lower()
        if q:
            tasks = [t for t in tasks if q in t.name.lower() or q in t.hash.lower()]
        return tasks

    def _error_block(self, t: Task) -> tuple[str, bool] | None:
        """(report, is_this_exact_task) for a failed task, if the log has one.

        Parsed lazily (a full scan) and cached until the log grows, since only
        failed tasks need it. A retried attempt has no report of its own, so we
        fall back to the one for its process — flagged, because it describes a
        sibling attempt rather than this exact task."""
        if not is_failed(t) or self.log_file is None:
            return None
        if self._errors_stat != self._log_stat:
            self._errors = parse_errors(self.log_file)
            self._errors_stat = self._log_stat
        exact = self._errors.get(t.hash)
        if exact is not None:
            return exact, True
        loose = self._errors.get(f"name:{t.name}")
        return (loose, False) if loose is not None else None

    def _metrics(self, t: Task) -> Metrics | None:
        """Cached .command.trace for a finished task (None until it finishes).

        Misses are cached too, keyed by the status they were read at: without
        that, a run whose tasks have no trace files re-opens every one of them on
        every 1s refresh (~35ms per tick at 10k tasks). Keying on status still
        lets a trace written just after a task finishes be picked up."""
        if not (is_done(t) or is_failed(t)):
            return None
        ent = self._trace_cache.get(t.hash)
        if ent is not None and ent[0] == t.status:
            return ent[1]
        m = parse_trace(t.workdir)
        self._trace_cache[t.hash] = (t.status, m)
        return m

    def _sort_groups(self) -> None:
        """Reorder tasks within each process group by the active sort, and float
        the heaviest process group to the top — so the run's bottleneck (slowest
        or hungriest task) surfaces even when each process has one task."""
        def metric(t: Task) -> int:
            m = self._metrics(t)
            if self.sort_mode == "memory":
                return m.peak_rss_kb if m and m.peak_rss_kb else 0
            return m.realtime_ms if m and m.realtime_ms else 0     # slowest
        for tasks in self._groups.values():
            tasks.sort(key=metric, reverse=True)
        self._groups = dict(sorted(
            self._groups.items(),
            key=lambda kv: max((metric(t) for t in kv[1]), default=0),
            reverse=True))

    def _full_rebuild(self) -> None:
        tree = self.query_one("#tasks", Tree)
        tree.clear()
        self._proc_nodes = {}
        self._task_nodes = {}
        self._placeholder = None
        self._sync_tree()
        if not self._proc_nodes:
            if self.query_str:
                msg = f'(no tasks match "{self.query_str}")'
            elif self.failed_only:
                msg = "(no failed tasks)"
            else:
                msg = "(no tasks yet)"
            self._placeholder = tree.root.add_leaf(msg)

    def _sync_tree(self) -> None:
        """Update the tree IN PLACE — update labels, append new nodes. Never
        clears, so the cursor, focus and scroll position are left untouched."""
        tree = self.query_one("#tasks", Tree)
        # Drop the "(no tasks yet)" leaf once real ones arrive: this sync only
        # appends, so otherwise it sits above the tree for the rest of the run
        # — which is every run opened with `nf-tui nextflow run`.
        if self._placeholder is not None and self._groups:
            try:
                self._placeholder.remove()
            except Exception:                      # noqa: BLE001 — already gone
                pass
            self._placeholder = None
        for proc, tasks in self._groups.items():
            pnode = self._proc_nodes.get(proc)
            if pnode is None:
                pnode = tree.root.add(_proc_label(proc, tasks), data=proc)
                pnode.expand()
                self._proc_nodes[proc] = pnode
            else:
                pnode.set_label(_proc_label(proc, tasks))
            for t in tasks:
                label = _task_label(t, self._metrics(t),
                                    self._live_states.get(t.hash))
                leaf = self._task_nodes.get(t.hash)
                if leaf is None:
                    self._task_nodes[t.hash] = pnode.add_leaf(label, data=t)
                else:
                    leaf.set_label(label)
                    # Re-point at the freshly parsed Task, not just the label.
                    # A leaf created while its task was SUBMITTED otherwise kept
                    # that object for the rest of the session, so anything
                    # reading node.data saw a task that never finished — `e`
                    # reported "no failed tasks" on a run whose header said two.
                    leaf.data = t

    def _selected(self) -> Task | None:
        node = self.query_one("#tasks", Tree).cursor_node
        if node is None or not isinstance(node.data, Task):
            return None
        # _sync_tree keeps node.data current; the by-hash lookup is belt and
        # braces for a node built before the latest parse landed.
        return self._task_by_hash.get(node.data.hash, node.data)

    # ---- in-pane log (same pane, scrollable, live) -------------------------

    def _emit_view(self, log: RichLog, text: str) -> None:
        """Write only the lines that belong to the current per-task view."""
        want_noise = self.view == "container"
        for line in text.splitlines():
            if is_container_noise(line) != want_noise:
                continue
            log.write(line)

    def _log_header(self, log: RichLog, t: Task) -> None:
        log.write(f"{t.name}   [{t.hash}]   {t.status}")
        log.write(t.workdir or "(no work dir known yet)")
        m = self._metrics(t)
        if m is not None and m.has_data():
            bits = [f"ran {human_duration(m.realtime_ms)}"]
            if m.pct_cpu is not None:
                bits.append(f"{m.pct_cpu / 100:.1f}× cpu")   # trace %cpu is per-core-summed
            if m.peak_rss_kb:
                bits.append(f"{human_size(m.peak_rss_kb * 1024)} peak")
            log.write("  ·  ".join(bits))
        if self.view == "task":
            # A failed task: lead with why it failed. That's what you opened it
            # for — otherwise the reason is buried in the run log.
            found = self._error_block(t)
            if found:
                err, exact = found
                log.write("──────── ✗ why this task failed ────────" if exact else
                          "──────── ✗ error reported for another attempt of this "
                          "process (Nextflow reports once) ────────")
                lines = err.splitlines()
                log.write("\n".join(lines[:ERROR_MAX_LINES]))
                if len(lines) > ERROR_MAX_LINES:
                    log.write(f"… ({len(lines) - ERROR_MAX_LINES} more lines of the "
                              f"error report — press g for the full run log)")
            elif is_failed(t):
                log.write("──────── ✗ failed — no error report in the log "
                          "(see the task output below) ────────")
            sh = Path(t.workdir) / ".command.sh" if t.workdir else None
            if sh and sh.exists():
                log.write("──────── .command.sh ────────")
                log.write(_read_all(sh))
            log.write("──────── task output — .command.log (live) ────────")
        else:  # container
            log.write("──────── container setup log (live) ────────")

    def _load_task(self, t: Task) -> None:
        """Fully redraw the log pane for a task in the current view."""
        log = self.query_one("#log", RichLog)
        # A failed task is finished, so there's nothing to tail — start at the
        # top, where the error report is, instead of the end of its output.
        show_error = self.view == "task" and is_failed(t)
        log.auto_scroll = self.follow and not show_error
        log.highlight = True             # small task logs — highlight is fine
        log.clear()
        self._log_header(log, t)
        scheme = remote_scheme(t.workdir)
        if scheme:
            self._tailer = None                    # nothing local to tail
            if remote_tool(scheme) is None:
                spec = REMOTE_TOOLS.get(scheme, {})
                log.write(f"(task log is in {scheme} object storage; install "
                          f"`{spec.get('bin', scheme)}` and nf-tui will fetch it)")
                return
            log.write(f"… fetching {t.workdir}/.command.log …")
            self._fetch_remote_log(t.workdir, self.view)
            return
        self._tailer = Follower(Path(t.workdir) / ".command.log") if t.workdir else None
        if self._tailer is None or not self._tailer.path.exists():
            log.write("(.command.log not written yet)")
            return
        # Load the tail, not the whole file: a runaway task can write gigabytes
        # to .command.log. Scrolling to the top backfills the rest, exactly as
        # the run log does.
        try:
            size = self._tailer.path.stat().st_size
        except OSError:
            size = 0
        self._tasklog_start, self._tasklog_raw = read_back(
            self._tailer.path, size, max_bytes=TASKLOG_CHUNK,
            max_lines=TASKLOG_MAX_LINES)
        self._tailer.pos = size          # live appends continue from the end
        self._tasklog_task = t
        raw = "\n".join(self._tasklog_raw)
        before = len(log.lines)
        self._emit_view(log, raw)
        if len(log.lines) == before:   # nothing matched this view
            if self.view == "container":
                log.write("(no container-setup logs for this task)")
            elif is_done(t):
                log.write("(no task output — its results are output files; "
                          "press c for the container log, o to open the work dir)")
            else:
                log.write("(no task output yet — press c for the container log)")
        if show_error:
            # Land on the error, not wherever the previous content sat.
            self.call_after_refresh(lambda: log.scroll_home(animate=False))

    # ---- object-store work dirs (AWS Batch and friends) --------------------

    @work(thread=True, exclusive=True, group="remote")
    def _fetch_remote_log(self, workdir: str, view: str) -> None:
        """Pull a cloud task's log in the background and paint it."""
        name = ".command.log"
        # A live task's log grows, so don't serve a stale copy of it.
        remote_forget(f"{workdir}/{name}")
        body = remote_cat(f"{workdir}/{name}")
        if body is None:                       # not written yet, or unreadable
            body = remote_cat(f"{workdir}/.command.err") or ""
        self.call_from_thread(self._paint_remote_log, workdir, view, body)

    def _paint_remote_log(self, workdir: str, view: str, body: str) -> None:
        # The selection or the view may have moved on while we were fetching.
        t = self._selected()
        if self.view != view or t is None or t.workdir != workdir:
            return
        panes = self.query("#log")
        if not panes:
            return
        log = panes.first(RichLog)
        log.auto_scroll = False
        log.clear()
        self._log_header(log, t)
        if body.strip():
            self._emit_view(log, body)
        else:
            log.write("(no output in the object store yet)")

    @work(thread=True, exclusive=True, group="remote-files")
    def _fetch_remote_files(self, workdir: str) -> None:
        remote_forget(f"{workdir}/")
        self.call_from_thread(self._paint_remote_files, workdir,
                              remote_ls(workdir))

    def _paint_remote_files(self, workdir: str,
                            entries: list[tuple[str, int | None]]) -> None:
        if self.view != "files" or (self._files_task or Task()).workdir != workdir:
            return
        files = self.query_one("#files", OptionList)
        files.clear_options()
        self._files = []                       # these are URIs, not local paths
        self._remote_files = [f"{workdir}/{n}" for n, _ in entries
                              if not n.endswith("/")]
        shown = [(n, sz) for n, sz in entries if not n.startswith(".")]
        if not shown:
            files.add_option(Option("(nothing listed)"))
            return
        for i, (name, size) in enumerate(shown):
            label = f"   {name}   {human_size(size) if size else ''}"
            files.add_option(Option(label, id=f"r{i}"))
        self._remote_files = [f"{workdir}/{n}" for n, _ in shown]
        files.highlighted = 0
        self._open_remote_file(self._remote_files[0])

    def _open_remote_file(self, uri: str) -> None:
        self._viewer_header = [f"── {uri.rsplit('/', 1)[-1]} ──", uri,
                               f"$ {REMOTE_TOOLS[remote_scheme(uri)]['bin']} "
                               f"cat   (object store)"]
        panes = self.query("#log")
        if panes:
            log = panes.first(RichLog)
            log.clear()
            for h in self._viewer_header:
                log.write(h)
            log.write("… fetching …")
        self._fetch_remote_object(uri)

    @work(thread=True, exclusive=True, group="remote-object")
    def _fetch_remote_object(self, uri: str) -> None:
        body = remote_cat(uri, limit=VIEW_MAX_LINES * 200)
        lines = (body or "").splitlines()[:VIEW_MAX_LINES]
        if body is None:
            lines = ["(could not read this object — check credentials "
                     "and that it exists)"]
        elif not lines:
            lines = ["(empty)"]
        self.call_from_thread(self._viewer_done, lines, VIEW_MAX_LINES, None)

    def _run_is_live(self) -> bool:
        """A run is live if a task is still running/submitted, or its log was
        written in the last ~20s (Nextflow keeps appending while it runs)."""
        if any(t.status.upper() in IN_FLIGHT for t in self.tasks):
            return True
        try:
            return (time.time() - self.log_file.stat().st_mtime) < 20
        except OSError:
            return False

    def _resolve_inflight_workdirs(self, inflight: list[Task]) -> None:
        """Give in-flight tasks their work dir so their state is knowable.

        Their workDir isn't in the log yet — Nextflow records it on completion —
        so it's looked up by hash and cached; a work dir never moves."""
        if not inflight:
            return
        if self._work_root is None and self.log_file is not None:
            root = find_work_root(self.log_file)
            # Resolving by hash means listing a directory; skip it for a cloud
            # work tree, where that would be an API call per task per refresh.
            self._work_root = None if remote_scheme(root) else Path(root)
        if self._work_root is None:
            return
        for t in inflight:
            if t.workdir:
                continue
            wd = self._workdir_cache.get(t.hash)
            if wd is None:
                wd = resolve_workdir(self._work_root, t.hash)
                if wd:
                    self._workdir_cache[t.hash] = wd
            if wd:
                t.workdir = wd

    def _show_queue(self, log: RichLog) -> None:
        """A scheduler-style view of what's in flight — running first (longest
        first), then pending, like `squeue` for the pipeline."""
        log.auto_scroll = False
        log.clear()
        rows: list[tuple[int, float, Text]] = []
        now = time.time()
        counts = {"running": 0, "pending": 0}
        for t in self.tasks:
            if t.status.upper() not in IN_FLIGHT:
                continue
            state = task_state(t)
            if state not in counts:
                continue
            counts[state] += 1
            began = task_started_at(t) if state == "running" else None
            elapsed = (now - began) if began else 0.0
            proc, tag = split_name(t.name)
            row = Text()
            row.append(f"{state:<8} ", style=STATE_STYLE[state])
            row.append(
                f"{human_duration(elapsed * 1000) if began else '—':>8}  ",
                style="dim")
            row.append(f"{(tag or t.hash)[:28]:<28} ")
            row.append(f"{proc.split(':')[-1][:30]:<30} ", style="bold")
            row.append(t.hash, style="dim")
            rows.append((0 if state == "running" else 1, -elapsed, row))
        p = self._progress
        head = [f"──────── queue: {counts['running']:,} running · "
                f"{counts['pending']:,} pending"
                + (f" · {p.per_min:.1f}/min" if p and p.per_min else "")
                + " ────────",
                f"{'STATE':<8} {'ELAPSED':>8}  {'TASK':<28} {'PROCESS':<30} HASH"]
        if not rows:
            head.append("")
            head.append("(nothing in flight — every announced task has finished)")
            head.append("Nextflow only announces tasks as it submits them, so a "
                        "running pipeline may still have work it hasn't queued.")
        rows.sort(key=lambda r: (r[0], r[1]))
        body: list[Text] = [r[2] for r in rows[:QUEUE_MAX_ROWS]]
        if len(rows) > QUEUE_MAX_ROWS:
            body.append(Text(f"… and {len(rows) - QUEUE_MAX_ROWS:,} more in flight"))
        # One styled block: per-line writes are far slower, and Text.join keeps
        # each row's colours.
        out = Text("\n").join([Text(h, style="bold") for h in head] + body)
        log.write(out)

    def _runlog_header(self) -> str:
        """Header line: says where in the file the loaded window starts, so the
        top of the pane is never mistaken for the top of the log."""
        follow = self.follow and self._run_is_live()
        state = "live — following" if follow else "complete"
        where = ("from the start" if self._runlog_start <= 0
                 else "scroll up to load earlier lines")
        return (f"──────── {self.log_file.name}   (full run log, {state} — "
                f"{where}; L to open it all in less) ────────")

    def _show_run_log(self, log: RichLog) -> None:
        """Load the tail of .nextflow.log into the pane, positioned at the end —
        where a run says how it went. Scrolling up backfills earlier lines a
        chunk at a time; a live run keeps following new ones."""
        log.highlight = True
        log.clear()
        self._runlog_lines = []
        self._runlog_start = 0
        if not self.log_file.exists():
            self._tailer = None
            log.write(f"({self.log_file} not found)")
            return
        size = self.log_file.stat().st_size
        self._tailer = Follower(self.log_file)
        self._tailer.pos = size   # continue from the end for live appends
        follow = self.follow and self._run_is_live()
        self._runlog_start, self._runlog_lines = read_back(
            self.log_file, size, max_bytes=RUNLOG_TAIL)
        content = self._runlog_header() + "\n" + "\n".join(self._runlog_lines)
        # Paint deferred (after the event handler) — a big synchronous write
        # inside an event handler fails to render in real terminals.
        self.call_after_refresh(lambda: self._paint_runlog(content, follow))

    def _paint_runlog(self, content: str, follow: bool) -> None:
        if self.view != "run":
            return
        panes = self.query("#log")
        if not panes:
            return
        log = panes.first(RichLog)
        log.auto_scroll = follow      # only a live run chases new lines
        log.write(content)
        # Always open at the end: that's where a run reports how it went.
        log.scroll_end(animate=False)

    def _tasklog_backfill(self) -> None:
        """Scrolled to the top of a task log: prepend the previous chunk.

        Same shape as _runlog_backfill, with one wrinkle: this pane shows a
        *filtered* view (task output vs container noise), so the raw lines are
        what gets prepended, and the viewport shifts by however many survive the
        filter — not by how many were read.
        """
        if self.view not in ("task", "container") or self._backfilling:
            return
        t = self._tasklog_task
        if t is None or self._tailer is None or self._tasklog_start <= 0:
            return
        self._backfilling = True
        try:
            start, older = read_back(self._tailer.path, self._tasklog_start,
                                     max_bytes=TASKLOG_CHUNK,
                                     max_lines=TASKLOG_MAX_LINES)
            self._tasklog_start = start
            if not older:
                return
            self._tasklog_raw = older + self._tasklog_raw
            panes = self.query("#log")
            if not panes:
                return
            log = panes.first(RichLog)
            keep = log.scroll_y
            before = len(log.lines)
            log.auto_scroll = False
            log.clear()
            self._log_header(log, t)
            self._emit_view(log, "\n".join(self._tasklog_raw))
            added = len(log.lines) - before
            log.scroll_y = keep + max(0, added)
        finally:
            self._backfilling = False

    def _preview_extend(self) -> None:
        """Scrolled to the bottom of a file preview: append the next chunk.

        The forward counterpart to _runlog_backfill, and much cheaper: RichLog
        appends natively, so nothing is rewritten and the viewport stays put.
        Loading `F`-style in one go cost 42s and 719 MB on a 159 MB file and
        still showed only a tenth of it; this walks the same file for the price
        of one chunk at a time.
        """
        if self.view != "files" or self._extending:
            return
        p, pos = self._view_path, self._view_pos
        if p is None or self._view_eof:
            return
        if pos is None:
            # gzip / container decode: a pipe, so resume by line count. The
            # worker re-runs the decoder with the lines already shown skipped
            # and appends what comes back; _viewer_done clears _extending.
            shown = self._view_shown
            t = self._files_task or self._selected()
            if shown is None or t is None:
                return
            self._extending = True
            self._run_viewer(t, p, decode_tool(p), is_gzip(p),
                             skip=shown, append=True)
            return
        self._extending = True
        try:
            new_pos, lines, at_eof = read_forward(p, pos, VIEW_MAX_LINES)
            self._view_pos, self._view_eof = new_pos, at_eof
            if not lines:
                return
            panes = self.query("#log")
            if not panes:
                return
            log = panes.first(RichLog)
            log.write("\n".join(lines))
            if at_eof:
                log.write("─── (end of file) ───")
        finally:
            self._extending = False

    def _runlog_backfill(self) -> None:
        """Scrolled near the top: prepend the previous chunk of the file.

        RichLog can only append, so the pane is rewritten with the older lines
        in front and the viewport shifted down by however many were added —
        which keeps the line you were reading exactly where it was.
        """
        if self.view != "run" or self._backfilling or self.log_file is None:
            return
        if self._runlog_start <= 0:       # already at the top of the file
            return
        self._backfilling = True
        try:
            start, older = read_back(self.log_file, self._runlog_start)
            if not older:
                self._runlog_start = start
                return
            self._runlog_start = start
            self._runlog_lines = older + self._runlog_lines
            panes = self.query("#log")
            if not panes:
                return
            log = panes.first(RichLog)
            keep = log.scroll_y
            # Leave auto_scroll off: we are far from the bottom now, and the
            # refresh tick turns following back on when you return there.
            log.auto_scroll = False
            log.clear()
            log.write(self._runlog_header() + "\n" + "\n".join(self._runlog_lines))
            log.scroll_y = keep + len(older)
        finally:
            self._backfilling = False

    def _container_desc(self, t: Task | None) -> str:
        cont = task_container(t.workdir) if (t and t.workdir) else None
        return f"{cont[0]}:{cont[1].split('/')[-1]}" if cont else "no container found"

    def _viewer_spec(self, workdir: str, tool: str):
        """(engine, mounts, image) for decoding a BAM/CRAM/BCF. Keeps the task's
        mounts (so the reference resolves) but swaps in a samtools/bcftools
        image from the run if the task's own container lacks the tool."""
        spec = parse_container_run(workdir)
        if spec is None:
            return None
        engine, mounts, image = spec
        binary = tool.split()[0]                       # samtools / bcftools
        alt = "htslib" if binary == "samtools" else binary
        low = image.lower()
        if binary in low or alt in low:                # task image already has it
            return engine, mounts, image
        found = self._tool_image_cache.get(binary, False)
        if found is False:
            found = find_tool_image(self.log_file.parent, binary)
            self._tool_image_cache[binary] = found
        return engine, mounts, (found or image)

    def _file_label(self, p: Path) -> str:
        try:
            if p.is_symlink() and not p.exists():
                return f"🔗 {p.name}   (broken)"
            if p.is_dir():
                return f"📁 {p.name}/"
            size = human_size(p.stat().st_size)
        except OSError:
            size = "?"
        icon = "🔗" if p.is_symlink() else "  "
        return f"{icon} {p.name}   {size}"

    def _populate_files(self, t: Task) -> None:
        """Fill the left file list for a task. Content opens on selection."""
        self._files_task = t          # the files belong to this task (for decode)
        self._tailer = None
        files = self.query_one("#files", OptionList)
        files.clear_options()
        self._files = []
        log = self.query_one("#log", RichLog)
        log.clear()
        scheme = remote_scheme(t.workdir)
        if scheme:
            if remote_tool(scheme) is None:
                spec = REMOTE_TOOLS.get(scheme, {})
                files.add_option(Option(f"({scheme}: install "
                                        f"{spec.get('bin', scheme)})"))
                log.write(f"This task's work dir is in object storage:\n"
                          f"  {t.workdir}\n\n"
                          f"Install `{spec.get('bin', scheme)}` and nf-tui will "
                          f"list and read it directly. Otherwise fetch it "
                          f"yourself:\n  aws s3 cp --recursive {t.workdir} ./task/")
                return
            files.add_option(Option("… listing …"))
            log.write(f"… listing {t.workdir} …")
            self._fetch_remote_files(t.workdir)
            return
        wd = Path(t.workdir) if t.workdir else None
        if wd is None or not wd.exists():
            files.add_option(Option("(work dir not available)"))
            return
        entries = [p for p in sorted(wd.iterdir())
                   if not p.name.startswith(".") and p.name not in JUNK_NAMES]
        if not entries:
            files.add_option(Option("(no files yet)"))
            log.write("(no files in the work dir yet)")
            return
        for p in entries:
            files.add_option(Option(self._file_label(p), id=str(len(self._files))))
            self._files.append(p)
        # Highlight + open the first file so there's content immediately and
        # <enter> has something selected.
        files.highlighted = 0
        self._open_file(self._files[0])

    def _open_file(self, p: Path, full: bool = False) -> None:
        """Render a file in the right pane using a tool from the task's container.
        `full` lifts the preview cap (the in-pane alternative to L, and the only
        way to see a whole file in the browser, where L can't run)."""
        self._last_file = p
        # Drop any previous file's resume point until this one reports its own.
        self._view_path, self._view_pos, self._view_eof = None, None, True
        self._view_shown, self._extending = None, False
        # The files belong to the task whose dir we listed; prefer it. The tree
        # cursor (_selected) can be off a task leaf (e.g. on a process group),
        # which used to hand a None task to the container decode below.
        t = self._files_task or self._selected()
        log = self.query_one("#log", RichLog)
        try:
            real = p.resolve()
        except OSError:
            real = p
        header = [f"── {p.name} ──"]
        if p.is_symlink():
            header.append(f"→ {real}")
        else:
            header.append(str(p))          # full path for real (non-symlink) files
        if p.is_dir():
            log.clear()
            for h in header:
                log.write(h)
            log.write("(directory)")
            for c in sorted(p.iterdir()):
                if c.name.startswith("."):     # skip .java, .userPrefs, etc.
                    continue
                log.write(f"  {c.name}")
            return
        tool = decode_tool(p)
        gz = is_gzip(p)
        if tool is None and not gz and looks_binary(p):
            log.clear()
            for h in header:
                log.write(h)
            try:
                sz = human_size(p.stat().st_size)
            except OSError:
                sz = "?"
            hint = "F for full" if self.web else "L for less"
            log.write(f"(binary file, {sz} — no text viewer; press {hint})")
            return
        if tool:
            # Name the image that will actually run the tool, which is often not
            # the task's own — a mosdepth container has no samtools in it.
            spec = self._viewer_spec(t.workdir, tool) if (t and t.workdir) else None
            where = (f"{spec[0]}:{spec[2].split('/')[-1]}" if spec
                     else self._container_desc(t))
            header.append(f"$ {tool} {p.name}   (in {where})")
        elif gz:
            header.append(f"$ gunzip -c {p.name}   (host)")
        else:
            header.append(f"$ cat {p.name}   (host)")
        self._viewer_header = header
        log.clear()
        for h in header:
            log.write(h)
        log.write("… loading full file …" if full else "… loading …")
        # Focus stays on the file list; its paging keys scroll this pane.
        self._run_viewer(t, p, tool, gz, full)

    @work(thread=True, exclusive=True)
    def _run_viewer(self, t: Task, p: Path, tool: str | None, gz: bool,
                    full: bool = False, skip: int = 0,
                    append: bool = False) -> None:
        """Render `p` into the pane. `skip` starts that many lines in and
        `append` adds to what is already there — together they are how a gzip
        or container-decoded file keeps loading as you scroll, since neither
        can be resumed from a byte offset the way plain text can."""
        text_cap = FULL_MAX_LINES if full else VIEW_MAX_LINES
        bam_cap = FULL_MAX_LINES if full else BAM_PREVIEW_LINES
        # Text and gzip are read directly on the host — fast, no container.
        if tool is None:
            if gz:
                out = head_gzip(p, text_cap, skip)
                self.call_from_thread(self._viewer_done, out or ["(empty)"],
                                      text_cap, p, None, len(out) < text_cap,
                                      skip + len(out), append)
                return
            # Plain text resumes by byte offset instead (see _preview_extend),
            # so it never needs the line-skip path.
            pos, out, at_eof = read_forward(p, 0, text_cap)
            self.call_from_thread(self._viewer_done, out or ["(empty)"], text_cap,
                                  p, pos, at_eof)
            return
        # BAM/CRAM/BCF: decode with a samtools/bcftools image + the task's mounts.
        spec = self._viewer_spec(t.workdir, tool) if (t and t.workdir) else None
        if spec is None:
            self.call_from_thread(self._viewer_done,
                                  ["(no container found to decode this file)"],
                                  VIEW_MAX_LINES, p)
            return
        engine, mounts, image = spec
        # cd into the task work dir so relative references (e.g. a CRAM's
        # -T genome.fasta) resolve exactly as they did for the task.
        # `tail -n +N` starts N lines in: the decode itself is a pipe, so this
        # re-decodes from the start each time. Bounded memory, and it is what
        # lets a BAM keep scrolling instead of stopping at the first 500 lines.
        window = (f"tail -n +{skip + 1} | head -n {bam_cap}" if skip
                  else f"head -n {bam_cap}")
        inner = (f"cd {shlex.quote(t.workdir)} && "
                 f"{tool} {shlex.quote(str(p))} 2>&1 | {window}")
        if engine in ("docker", "podman"):
            try:
                chk = subprocess.run([engine, "image", "inspect", image],
                                     capture_output=True, timeout=20)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                chk = None
            if chk is None:
                self.call_from_thread(self._viewer_done, [f"({engine} not available)"],
                                      VIEW_MAX_LINES, p)
                return
            if chk.returncode != 0:
                self.call_from_thread(self._viewer_done, [
                    f"(image not present locally: {image})",
                    f"pull it first:  {engine} pull {image}",
                ], VIEW_MAX_LINES, p)
                return
            cmd = ([engine, "run", "--rm"] + mounts
                   + ["-w", t.workdir, image, "sh", "-c", inner])
        else:
            cmd = [engine, "exec"] + mounts + [image, "sh", "-c", inner]
        ok = False
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            ok = r.returncode == 0 and bool(r.stdout)
            out = (r.stdout or r.stderr or "(no output)").splitlines()
        except FileNotFoundError:
            out = [f"({engine} is not installed / not on PATH)"]
        except subprocess.TimeoutExpired:
            out = ["(viewer timed out after 120s)"]
        except Exception as e:                       # noqa: BLE001
            out = [f"(error running viewer: {e})"]
        # A failed decode is a message, not the file: it must not be labelled
        # "end of file", and scrolling must not try to fetch more of it.
        self.call_from_thread(self._viewer_done, out, bam_cap, p, None,
                              (not ok) or len(out) < bam_cap,
                              (skip + len(out)) if ok else None, append)

    def _pager_command(self, t: Task, p: Path, pager: str) -> str:
        """Shell string that pages a file lazily. BAM/CRAM/BCF are decoded by
        the task's container tool and piped to the pager; gz is decompressed
        through a pipe; plain files are handed to the pager directly so it can
        seek them instead of slurping the whole thing."""
        tool = decode_tool(p)
        if tool is None:
            if is_gzip(p):                        # must decompress; pipe is forced
                return (f"gzip -cdfq {shlex.quote(str(p))} 2>&1 | "
                        f"{pager} -R -P{shlex.quote(PAGER_PROMPT)}")
            # seekable: opens instantly, and -n on a big one so quitting is too
            return (f"{pager} {pager_flags(p)} -P{shlex.quote(PAGER_PROMPT)} "
                    f"{shlex.quote(str(p))}")
        spec = self._viewer_spec(t.workdir, tool) if (t and t.workdir) else None
        if spec is None:
            return (f"echo '(no container found to decode {p.name})' | "
                    f"{pager} -R -P{shlex.quote(PAGER_PROMPT)}")
        engine, mounts, image = spec
        # No -i: the container must NOT read the terminal, or it steals the
        # keystrokes meant for the pager (samtools reads the file, not stdin).
        inner = f"cd {shlex.quote(t.workdir)} && exec {tool} {shlex.quote(str(p))}"
        if engine in ("docker", "podman"):
            parts = ([engine, "run", "--rm"] + mounts
                     + ["-w", t.workdir, image, "sh", "-c", inner])
        else:
            # singularity/apptainer: `exec`, and none of docker's --rm/-w — the
            # inner `cd` already puts us in the work dir.
            parts = [engine, "exec"] + mounts + [image, "sh", "-c", inner]
        return (" ".join(shlex.quote(x) for x in parts)
                + f" 2>&1 | {pager} -R -P{shlex.quote(PAGER_PROMPT)}")

    def _current_file(self) -> tuple[Task | None, Path | None]:
        if self.view != "files":
            return None, None
        t = self._files_task or self._selected()   # the task the file list belongs to
        files = self.query_one("#files", OptionList)
        idx = files.highlighted
        if idx is None or idx >= len(self._files):
            return t, None
        return t, self._files[idx]

    def action_full_file(self) -> None:
        """Load the whole selected file in-pane (uncapped). Works in the terminal
        and the browser — the web has no external `less`."""
        if self.view != "files":
            self.notify("switch to the files view (d) to load a file in full")
            return
        _, p = self._current_file()
        if p is None or p.is_dir():
            return
        self._open_file(p, full=True)

    def _page(self, command: str) -> None:
        """Hand the real terminal to the pager for the duration of the command."""
        with self.suspend():
            subprocess.run(["sh", "-c", command])

    def action_pager(self) -> None:
        # In the browser there is no terminal to hand to less; load in-pane.
        if self.web:
            if self.view == "files":
                self.notify("browser mode: loading the full file in-pane (no less)")
                self.action_full_file()
            elif self.view in ("task", "container"):
                # The whole .command.log is already in the pane here.
                self.notify("browser mode: this log is fully loaded — scroll the pane")
            else:
                self.notify("browser mode: scroll the pane, or press F in the "
                            "files view (external less needs a terminal)")
            return
        pager = pager_bin()
        if pager is None:
            self.notify("no `less` on PATH")
            return
        # Run log: page the whole .nextflow.log on the host, opened at the end
        # (+G) to match the pane. No cap here — less pages it lazily.
        if self.view == "run":
            if self.log_file is None or not self.log_file.exists():
                return
            self._page(f"{pager} {pager_flags(self.log_file)} +G "
                       f"-P{shlex.quote(PAGER_PROMPT)} "
                       f"{shlex.quote(str(self.log_file))}")
            return
        # Task / container view: page this task's own .command.log. It's the raw
        # file, so the container noise the task view filters out is included —
        # the pager hides nothing.
        if self.view in ("task", "container"):
            t = self._selected()
            if t is None:
                self.notify("select a task first")
                return
            p = Path(t.workdir) / ".command.log" if t.workdir else None
            if p is None or not p.exists():
                self.notify("no .command.log for this task yet")
                return
            # Still running: open at the end to watch it. Finished: at the top,
            # where the error or the story starts.
            at_end = "" if (is_done(t) or is_failed(t)) else "+G "
            self._page(f"{pager} {pager_flags(p)} {at_end}"
                       f"-P{shlex.quote(PAGER_PROMPT)} {shlex.quote(str(p))}")
            return
        if self.view != "files":
            self.notify("switch to the files view (d), or g for the run log")
            return
        t, p = self._current_file()
        if p is None or p.is_dir():
            return
        # Only BAM/CRAM/BCF need the container; check its image is present.
        tool = decode_tool(p)
        if tool is not None:
            spec = self._viewer_spec(t.workdir, tool) if (t and t.workdir) else None
            if spec and spec[0] in ("docker", "podman"):
                chk = subprocess.run([spec[0], "image", "inspect", spec[2]],
                                     capture_output=True)
                if chk.returncode != 0:
                    self.notify(f"image not present — {spec[0]} pull {spec[2]}")
                    return
        self._page(self._pager_command(t, p, pager))

    def _viewer_done(self, lines: list[str], cap: int = VIEW_MAX_LINES,
                     path: Path | None = None, pos: int | None = None,
                     at_eof: bool = True, shown: int | None = None,
                     append: bool = False) -> None:
        # A container decode can take seconds; by the time it lands the user may
        # have switched views or picked another file. Dropping a stale result is
        # right — otherwise it clobbers the run log with file content.
        if self.view != "files":
            return
        if path is not None and self._last_file is not None and path != self._last_file:
            return
        panes = self.query("#log")
        if not panes:
            return
        log = panes.first(RichLog)
        log.auto_scroll = False          # a file: stay put so we can start at the top
        if not append:
            log.clear()
            for h in getattr(self, "_viewer_header", []):
                log.write(h)
            # The "there is more" hint goes in this rule, at the top, because
            # appends cannot remove anything: a hint written under the body
            # would be stranded mid-file by the next chunk.
            if at_eof:
                log.write("─" * 30)
            else:
                log.write("──── scroll for more"
                          f"{'' if self.web else '; L for less'}"
                          "; o opens the work dir ────")
        # Write the body in one shot — per-line writes are ~100x slower.
        if lines:
            log.write("\n".join(lines))
        # Remember where this file stopped so scrolling to the bottom resumes
        # from here: plain text by byte offset, gzip and container decodes by
        # how many lines have been shown (those are pipes and can't seek).
        self._view_path, self._view_pos, self._view_eof = path, pos, at_eof
        self._view_shown = shown
        self._extending = False
        # Only ever written at the real end, so it cannot end up mid-pane.
        if at_eof and (append or shown is not None):
            log.write("─── (end of file) ───")
        # Scroll to the top after the content is laid out (doing it now, before
        # the virtual size is measured, doesn't stick). On an append we must not
        # jump — the reader is at the bottom, which is why more was loaded.
        if not append:
            self.call_after_refresh(lambda: log.scroll_home(animate=False))

    def _render_current(self) -> None:
        """Every tick / selection change: (re)draw the pane for the current view."""
        panes = self.query("#log")          # empty while another screen is up
        if not panes:
            return
        log = panes.first(RichLog)

        # Queue: independent of the tree, redrawn every tick — states and
        # elapsed times move on their own.
        if self.view == "queue":
            self._shown = ("queue",)
            self._tailer = None
            self._show_queue(log)
            return

        # Run log: the whole .nextflow.log, independent of tree selection.
        if self.view == "run":
            if self._shown != ("run",):
                self._shown = ("run",)
                self._show_run_log(log)
            elif self.follow and self._tailer is not None:
                new = self._tailer.read_new().splitlines()
                if new:
                    # Follow only while parked at the bottom. If you scrolled up
                    # to read, arriving lines must not yank the viewport back
                    # down; scrolling to the bottom again resumes following.
                    log.auto_scroll = log.scroll_y >= log.max_scroll_y - 1
                    for line in new:
                        log.write(line)     # raw, unfiltered
                    # Keep the backing list in step, or a backfill rewrite would
                    # drop the lines that arrived while we were following.
                    self._runlog_lines.extend(new)
            return

        # Task / container / files view: follow the tree selection.
        t = self._selected()
        if t is not None:
            key = (self.view, t.hash)
            if self._shown != key:
                self._shown = key
                if self.view == "files":
                    self._populate_files(t)
                else:
                    self._load_task(t)
            elif self.view != "files" and self.follow and self._tailer is not None:
                new = self._tailer.read_new()                   # live append
                if new:
                    # Keep the raw list in step, or a backfill rewrite would
                    # drop whatever arrived while we were following.
                    self._tasklog_raw.extend(new.splitlines())
                    self._emit_view(log, new)
            return

        # A process group (or nothing) is selected: show a summary, once.
        node = self.query_one("#tasks", Tree).cursor_node
        key = (self.view, f"proc:{node.data}" if node is not None else None)
        if key != self._shown:
            self._shown = key
            self._tailer = None
            log.clear()
            if node is not None and isinstance(node.data, str):
                members = self._groups.get(node.data, [])
                lines = [f"{node.data}   ({len(members)} tasks)"]
                # .plain: this pane takes strings, not styled tree labels
                lines += [f"  {_task_label(x).plain}   [{x.hash}]"
                          for x in members[:40]]
                if len(members) > 40:
                    lines.append(f"  … and {len(members) - 40} more "
                                 f"(expand the group to see them)")
                log.write("\n".join(lines))     # one write, not N — stays snappy

    # ---- events / actions --------------------------------------------------

    def on_tree_node_highlighted(self, _event) -> None:
        self._render_current()

    def on_tree_node_selected(self, event) -> None:
        # <enter> on a task jumps focus into the log pane so you can scroll it.
        if isinstance(event.node.data, Task):
            target = "#files" if self.view == "files" else "#log"
            self.query_one(target).focus()

    def on_option_list_option_selected(self, event) -> None:
        # A file was clicked / entered in the left list -> open it on the right.
        # Focus stays on the list; its paging keys scroll the content pane.
        # Guard on the list id: the run picker's OptionList selection bubbles up
        # to this app handler too, and must not be treated as a file.
        if event.option_list.id != "files":
            return
        idx = event.option.id
        if idx and idx.startswith("r"):             # a remote entry
            n = int(idx[1:])
            if n < len(self._remote_files):
                self._open_remote_file(self._remote_files[n])
            return
        if idx is not None and idx.isdigit() and int(idx) < len(self._files):
            self._open_file(self._files[int(idx)])

    def action_focus_log(self) -> None:
        log = self.query_one("#log", RichLog)
        tree = self.query_one("#tasks", Tree)
        # Toggle focus between the two panes (both stay visible).
        (tree if log.has_focus else log).focus()

    def action_focus_next_pane(self) -> None:
        # Cycle focus through the visible panes (tree / file list / content)
        # so any of them can be focused and then full-screened with z.
        self.screen.focus_next()

    def action_back(self) -> None:
        # Escape hierarchy (each press peels back one level):
        #   search open       -> clear the filter
        #   full screen       -> restore the split
        #   run log           -> task view + tree
        #   focus on log pane -> tree
        #   failed-only on    -> show every task again
        #   on the tree       -> back to the run selector (if launched from it)
        tree = self.query_one("#tasks", Tree)
        log = self.query_one("#log", RichLog)
        files = self.query_one("#files", OptionList)
        search = self.query_one("#search", Input)
        if search.has_focus or search.has_class("on"):
            self._close_search(clear=True)        # esc in search clears the filter
            return
        if self.screen.maximized is not None:
            self.screen.minimize()
            return
        if self.view == "files":
            if log.has_focus:
                files.focus()                     # file content -> file list
                return
            self._set_view("task", "task log")   # file list -> task view + tree
            tree.focus()
            return
        if self.view in ("run", "queue"):
            self._set_view("task", "task log")   # run/queue -> task view + tree
            tree.focus()
            return
        if log.has_focus:
            tree.focus()                          # log pane -> tree
            return
        if self.view != "task":
            self._set_view("task", "task log")   # container -> task view
            return
        if self.failed_only:
            # A filter is a level too. Esc already clears the `/` search, and
            # leaving x armed here meant the next Esc dropped you out to the run
            # picker with the tree still hiding everything that worked — which
            # is how you come back to a run and think the tree is broken.
            self.action_toggle_failed()          # failed-only -> the whole tree
            return
        if not self.target.is_file():
            self._open_picker()                   # task tree -> the run picker

    def _set_view(self, view: str, label: str) -> None:
        self.view = view
        self._shown = None              # force a redraw in the new view
        self.query_one("#files", OptionList).display = (view == "files")
        self._render_current()
        if view == "files":
            self.query_one("#files", OptionList).focus()
        self.notify(label)

    def action_view_task(self) -> None:
        self._set_view("task", "task log")

    def action_view_container(self) -> None:
        self._set_view("container", "container log")

    def action_view_files(self) -> None:
        self._set_view("files", "produced files")

    def action_view_run(self) -> None:
        self._set_view("run", "full run log")

    def action_view_queue(self) -> None:
        self._set_view("queue", "queue — what is in flight")

    def action_zoom(self) -> None:
        # Toggle full-screen for whichever pane is focused (tree / file list /
        # content). Textual maximizes the focused widget to fill the screen.
        if self.screen.maximized is not None:
            self.screen.minimize()
        else:
            w = self.focused or self.query_one("#tasks", Tree)
            self.screen.maximize(w)

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        # auto_scroll off lets you scroll back without new lines yanking you down.
        self.query_one("#log", RichLog).auto_scroll = self.follow
        self.notify(f"follow {'ON' if self.follow else 'OFF (scroll freely)'}")

    def _copy(self, text: str, what: str) -> None:
        """Put `text` on the clipboard by both routes available.

        Textual's copy uses OSC 52, which is the only thing that reaches your
        laptop's clipboard from a login node over SSH — but not every terminal
        honours it. A local helper (pbcopy / wl-copy / xclip) covers the
        terminals that don't, when one is running locally. The path is also
        shown, so it is readable even where neither route lands.
        """
        try:
            self.copy_to_clipboard(text)
        except Exception:                          # noqa: BLE001
            pass
        for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
            if shutil.which(cmd[0]):
                try:
                    subprocess.run(cmd, input=text.encode(), timeout=5)
                except Exception:                  # noqa: BLE001
                    pass
                break
        self.notify(f"copied {what}:\n{text}")

    def action_copy_path(self) -> None:
        """`y` — copy what you're looking at: the selected output file's path in
        the files view, otherwise the task's work directory."""
        if self.view == "files":
            _, p = self._current_file()
            if p is not None:
                self._copy(str(p), "file path")
                return
        t = self._selected() or self._files_task
        if t is None or not t.workdir:
            self.notify("no work directory for this selection", severity="warning")
            return
        self._copy(t.workdir, "work dir")

    def action_open_workdir(self) -> None:
        t = self._selected()
        if t and t.workdir:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.run([opener, t.workdir], check=False)
            self.notify(f"Opened {t.workdir}")

    def action_toggle_failed(self) -> None:
        self.failed_only = not self.failed_only
        self._sig = None            # force a rebuild
        self._force_refresh = True  # re-group even though the log is unchanged
        self.action_refresh()
        n = sum(is_failed(t) for t in self.tasks)
        self.notify(f"showing {'failed only' if self.failed_only else 'all'} "
                    f"({n} failed)")

    def _set_header(self, content: Text) -> None:
        """Paint the top bar. Defensive: another screen may be on top."""
        bars = self.query("#hdr")
        if bars:
            bars.first(Static).update(content)

    def _pipeline_alive(self) -> bool:
        if self.pipeline_pid is None:
            return False
        try:
            os.kill(self.pipeline_pid, 0)      # signal 0: existence check only
            return True
        except OSError:
            return False

    def action_stop_pipeline(self) -> None:
        """Stop the pipeline nf-tui launched, after confirming.

        Nextflow has no `cancel` command: you signal the process. SIGTERM is
        the one it handles — measured, not assumed: SIGINT was ignored (the run
        kept submitting for 20s), while SIGTERM stopped it in ~2s and its log
        showed "[SIGTERM handler] Killing running tasks (4)" with no orphaned
        task processes left behind. That handler is also what cancels jobs
        already queued on a scheduler. SIGKILL would skip it and strand them.
        """
        if self.pipeline_pid is None:
            self.notify("nf-tui didn't launch this run — stop it where you "
                        "started it", severity="warning")
            return
        if not self._pipeline_alive():
            self.notify(f"pipeline (PID {self.pipeline_pid}) is no longer running")
            return

        def stop(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                os.kill(self.pipeline_pid, signal.SIGTERM)
            except OSError as e:
                self.notify(f"could not signal PID {self.pipeline_pid}: {e}",
                            severity="error")
                return
            self.notify(f"sent SIGTERM to PID {self.pipeline_pid} — Nextflow is "
                        "shutting down; watch the run log", severity="warning")

        p = self._progress
        detail = (f"PID {self.pipeline_pid}"
                  + (f"  ·  {p.running} running, {p.pending} queued" if p else "")
                  + "\n\nNextflow shuts down gracefully and cleans up the tasks it"
                    " started\n(on a scheduler, that cancels the jobs it queued).")
        self.push_screen(ConfirmScreen("Stop this pipeline?", detail), stop)

    def action_next_failed(self) -> None:
        """Jump the tree to the next failed task (wrapping), and show why it
        failed. On a big run this beats hunting for the ✗ by eye."""
        tree = self.query_one("#tasks", Tree)
        leaves = [n for grp in tree.root.children for n in grp.children
                  if isinstance(n.data, Task)]
        failed = [n for n in leaves if is_failed(n.data)]
        if not failed:
            self.notify("no failed tasks")
            return
        line = tree.cursor_line
        nxt = next((n for n in failed if n.line > line), failed[0])
        for grp in tree.root.children:        # a collapsed group hides its leaves
            grp.expand()
        tree.move_cursor(nxt)
        tree.focus()
        if self.view not in ("task", "files"):
            self._set_view("task", "task log")
        t = nxt.data
        found = self._error_block(t)
        why = error_summary(found[0]) if found else f"exit={t.exit}"
        self.notify(f"{split_name(t.name)[0].split(':')[-1]}: {why}",
                    severity="warning")

    def action_cycle_sort(self) -> None:
        order = ["order", "slowest", "memory"]
        self.sort_mode = order[(order.index(self.sort_mode) + 1) % len(order)]
        self._sig = None            # force a rebuild in the new order
        self._force_refresh = True  # re-group even though the log is unchanged
        self.action_refresh()
        labels = {"order": "submission order", "slowest": "slowest first",
                  "memory": "peak memory first"}
        self.notify(f"sorted by {labels[self.sort_mode]}"
                    + ("" if self.sort_mode == "order" else " (heaviest process on top)"))

    # ---- / search over the task tree ---------------------------------------

    def action_search(self) -> None:
        """`/` — filter the task tree, or search the log if that's what you're
        reading. Which one is decided by focus, so the pane with the highlighted
        border is the thing being searched."""
        log = self.query_one("#log", RichLog)
        self._search_mode = "log" if (log.has_focus and self.view != "files") \
            else "tasks"
        box = self.query_one("#search", Input)
        box.add_class("on")
        if self._search_mode == "log":
            box.placeholder = "search this log — enter / n for next, N for previous"
            box.value = self._log_query
        else:
            box.placeholder = ("filter tasks by name or hash — "
                               "enter to keep, esc to clear")
            box.value = self.query_str  # reopen with the current filter to edit
        box.focus()

    def _apply_query(self, text: str) -> None:
        self.query_str = text
        self._sig = None                # force a rebuild
        self._force_refresh = True      # re-group even though the log is unchanged
        self.action_refresh()

    # ---- searching the log pane --------------------------------------------

    def _log_text_lines(self) -> list[str]:
        """The pane's contents as plain strings, styles dropped."""
        panes = self.query("#log")
        if not panes:
            return []
        return ["".join(seg.text for seg in strip)
                for strip in panes.first(RichLog).lines]

    def _search_log(self, text: str, *, announce: bool = True) -> None:
        self._log_query = text
        needle = text.strip().lower()
        self._log_matches = ([i for i, line in enumerate(self._log_text_lines())
                              if needle in line.lower()] if needle else [])
        self._log_i = -1
        if not self._log_matches:
            if announce and needle:
                self.notify(f'no match for "{text}" in this log')
            return
        self._jump_to_match(0, announce=announce)

    def _jump_to_match(self, index: int, *, announce: bool = True) -> None:
        if not self._log_matches:
            if announce:
                self.notify("search the log with / first")
            return
        self._log_i = index % len(self._log_matches)
        line = self._log_matches[self._log_i]
        panes = self.query("#log")
        if not panes:
            return
        log = panes.first(RichLog)
        # Following would drag the view straight back to the tail.
        log.auto_scroll = False
        log.scroll_to(y=line, animate=False)
        if announce:
            preview = self._log_text_lines()[line].strip()[:60]
            self.notify(f"{self._log_i + 1}/{len(self._log_matches)}  ·  {preview}")

    def action_next_match(self) -> None:
        self._jump_to_match(self._log_i + 1)

    def action_prev_match(self) -> None:
        self._jump_to_match(self._log_i - 1)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
        if self._search_mode == "log":
            # Quietly as you type; the count is reported on enter.
            self._search_log(event.value, announce=False)
        else:
            self._apply_query(event.value)   # filter live as you type

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search":
            return
        event.input.remove_class("on")
        if self._search_mode == "log":
            self._search_log(event.value)    # report the count now
            self.query_one("#log", RichLog).focus()
        else:
            self.query_one("#tasks", Tree).focus()

    def _close_search(self, clear: bool) -> None:
        box = self.query_one("#search", Input)
        box.remove_class("on")
        if self._search_mode == "log":
            # Hand the pane back rather than the tree, so a second escape steps
            # out of the view the way it does without a search.
            if clear:
                box.value = ""
                self._log_query = ""
                self._log_matches, self._log_i = [], -1
            self.query_one("#log", RichLog).focus()
            return
        if clear and self.query_str:
            box.value = ""
            self._apply_query("")
        self.query_one("#tasks", Tree).focus()


def resolve_log(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.is_dir():
        return p / ".nextflow.log"
    return p


# ---- run discovery + picker -----------------------------------------------

@dataclass
class RunInfo:
    path: Path            # the .nextflow.log (or rotated .nextflow.log.N)
    runname: str
    pipeline: str
    status: str           # OK / ERR / ? (from .nextflow/history)
    mtime: float
    finished: bool        # log tail has a Nextflow completion marker
    progress: "Progress | None" = None   # task counts, if they were computed


# Lines Nextflow writes at the very end of a run — their presence in the tail
# means the process exited cleanly (vs. being killed / the node dying).
_DONE_MARKERS = ("Execution complete -- Goodbye", "Goodbye", "Workflow completed")
# Seconds of log silence before an unfinished run reads as stalled. Generous on
# purpose: a healthy run writes nothing while a long task runs or while jobs sit
# in a scheduler queue, so a short window would flag working HPC runs as dead.
# The picker shows the age too, so "stalled · 2d ago" vs "35m ago" stays legible.
STALE_AFTER = 1800.0
PICKER_COUNT_MAX_BYTES = 80_000_000   # above this, skip a run's task counts


def _log_finished(path: Path) -> bool:
    """True if the log's tail carries a completion marker. Reads only the last
    few KB (seek), not the whole file — the picker scans every run at startup
    and a .nextflow.log can be hundreds of MB."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8000))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    return any(mark in tail for mark in _DONE_MARKERS)


def _ago(seconds: float) -> str:
    """A coarse 'time since' for the picker: '12s', '5m', '3h', '2d' ago."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _right(text: str) -> Text:
    """Right-justified table cell — numbers only line up when they're aligned."""
    return Text(text, justify="right")


def _run_stats(r: "RunInfo") -> str:
    """'24 tasks · 100% done · 2 failed   ' for a picker row (empty if not
    counted). A run that never finished only ever announced part of its work,
    so its total is labelled "seen" — otherwise a run that died early reads as
    100% done just because everything it managed to start also finished."""
    p = r.progress
    if p is None or not p.total:
        return ""
    done_run = r.status in ("OK", "ERR") or r.finished
    bits = [f"{p.total:,} tasks" if done_run else f"{p.total:,} seen",
            f"{p.pct}% done"]
    if p.cached:
        bits.append(f"{p.cached:,} cached")
    if p.failed:
        bits.append(f"{p.failed} failed")
    if p.running:
        bits.append(f"{p.running:,} running")
    return " · ".join(bits) + "   —   "


def run_state(r: "RunInfo", now: float | None = None) -> tuple[str, str]:
    """(icon, word) describing a run's disposition for the picker.

    history OK/ERR is authoritative when present. Otherwise a completion marker
    in the log means done; a recent write means it's still going; anything else
    stopped without finishing — killed, OOM, or a dead node."""
    if r.status == "OK":
        return "✓", "complete"
    if r.status == "ERR":
        return "✗", "failed"
    if r.finished:
        return "✓", "complete"
    age = (now if now is not None else time.time()) - r.mtime
    if age < STALE_AFTER:
        return "●", "running"
    return "⚠", "stalled"


def _scan_header(path: Path, max_lines: int = 500) -> tuple[str, str, str]:
    """Pull (run name, session UUID, command) from the top of a log."""
    runname = session = command = ""
    try:
        with path.open("r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > max_lines:
                    break
                if not runname and "Run name:" in line:
                    runname = line.split("Run name:", 1)[1].strip()
                elif not session and "Session UUID:" in line:
                    session = line.split("Session UUID:", 1)[1].strip()
                elif not command and "$> nextflow" in line:
                    command = line.split("$>", 1)[1].strip()
                if runname and session and command:
                    break
    except OSError:
        pass
    return runname, session, command


def _pipeline_of(command: str) -> str:
    """'nextflow run nf-core/fetchngs -r 1.12.0 -profile ...' -> 'nf-core/fetchngs -r 1.12.0'."""
    parts = command.split()
    if "run" not in parts:
        return command[:48]
    rest = parts[parts.index("run") + 1:]
    out: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("-r", "-revision") and i + 1 < len(rest):
            out += rest[i:i + 2]
            i += 2
            continue
        if tok.startswith("-"):
            break
        out.append(tok)
        i += 1
    return " ".join(out) or (rest[0] if rest else "")


def _history_status(launch_dir: Path) -> tuple[dict, dict]:
    """Map session-uuid -> status and run-name -> status from .nextflow/history."""
    by_session: dict[str, str] = {}
    by_name: dict[str, str] = {}
    try:
        text = (launch_dir / ".nextflow" / "history").read_text(errors="replace")
    except OSError:
        return by_session, by_name
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) >= 6:
            _, _, name, status, _, session = cols[:6]
            by_session[session] = status
            by_name[name] = status
    return by_session, by_name


def discover_logs(base: Path, max_depth: int = 3) -> list[Path]:
    """Find .nextflow.log* files under base, skipping heavy/irrelevant dirs."""
    skip = {"work", ".nextflow", "results", ".git", "node_modules"}
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.is_file() and e.name.startswith(".nextflow.log"):
                found.append(e)
            elif (e.is_dir() and depth < max_depth
                  and e.name not in skip and not e.is_symlink()):
                walk(e, depth + 1)

    try:
        base = base.resolve()
    except OSError:
        return found       # cwd/path vanished — caller reports it cleanly
    walk(base, 0)
    return found


def gather_runs(base: Path) -> list[RunInfo]:
    infos: list[RunInfo] = []
    for p in discover_logs(base):
        runname, session, command = _scan_header(p)
        by_session, by_name = _history_status(p.parent)
        status = by_session.get(session) or by_name.get(runname) or "?"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        rot = "" if p.name == ".nextflow.log" else f" ({p.name})"
        # Task counts need a full parse. Cheap in practice (~35ms for a
        # 10k-task log), but skip enormous logs so the picker stays instant.
        prog = None
        try:
            if p.stat().st_size <= PICKER_COUNT_MAX_BYTES:
                prog = progress_of(parse_log(p))
        except OSError:
            pass
        infos.append(RunInfo(
            path=p,
            runname=(runname or p.parent.name) + rot,
            pipeline=_pipeline_of(command),
            status=status,
            mtime=mtime,
            finished=_log_finished(p),
            progress=prog,
        ))
    infos.sort(key=lambda r: r.mtime, reverse=True)
    return infos


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no gate, used before anything that kills real compute."""

    CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: 74; height: auto; border: thick $error; padding: 1 2;
           background: $surface; }
    """
    BINDINGS = [Binding("escape,n", "no", "No"), Binding("y", "yes", "Yes")]

    def __init__(self, question: str, detail: str = ""):
        super().__init__()
        self.question = question
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(Text(self.question, style="bold"))
            if self.detail:
                yield Static(Text(self.detail, style="dim"))
            yield Static(Text("\ny = yes     n / esc = no", style="dim"))

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class RunPickerScreen(Screen):
    """Pick which discovered run to open. Dismisses with the chosen log path
    (or None if cancelled). A screen — not a separate App — so the whole
    session is one app (needed for the web/textual-serve mode)."""

    CSS = "#runs { height: 1fr; }"
    BINDINGS = [Binding("Q,escape", "cancel", "Cancel")]

    def __init__(self, base: Path, runs: list[RunInfo]):
        super().__init__()
        self.base = base
        self.runs = runs

    def compose(self) -> ComposeResult:
        yield NfHeader(id="hdr")
        # A real table: one row per run, aligned columns, sortable by eye.
        yield DataTable(id="runs", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        head = Text()
        head.append("nf-tui", style="bold")
        head.append(f"  select a run under {self.base}   —   ")
        for mark, word, style in (("●", "running", STATE_STYLE["running"]),
                                  ("⚠", "stalled", "bold red"),
                                  ("✓", "complete", STATE_STYLE["done"]),
                                  ("✗", "failed", STATE_STYLE["failed"])):
            head.append(f"{mark} {word}", style=style)
            head.append(" · ", style="dim")
        self.query_one("#hdr", Static).update(head)
        self.app.sub_title = f"select a run under {self.base}"
        table = self.query_one("#runs", DataTable)
        table.add_columns("", "STATE", "WHEN", "AGE",
                          "TASKS", "DONE", "FAIL", "CACHED",
                          "RUN", "PIPELINE", "WHERE")
        now = time.time()
        for i, r in enumerate(self.runs):
            mark, word = run_state(r, now)
            p = r.progress
            counted = p is not None and p.total
            table.add_row(
                mark,
                word,
                datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d %H:%M"),
                _right(_ago(now - r.mtime)),
                _right(f"{p.total:,}" if counted else "—"),
                _right(f"{p.pct}%" if counted else "—"),
                _right(str(p.failed) if counted and p.failed else ""),
                _right(f"{p.cached:,}" if counted and p.cached else ""),
                r.runname,
                r.pipeline,
                str(r.path.parent).replace(str(Path.home()), "~"),
                key=str(i),
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is not None:
            self.dismiss(self.runs[int(key)].path)

    def action_cancel(self) -> None:
        self.dismiss(None)


LOG_CHARS = 20_000        # per captured file, so one runaway log can't dominate


def _tail_text(path: Path, limit: int = LOG_CHARS) -> str | None:
    """The last `limit` characters of a file, or None if it isn't there.

    Seeks to the end rather than reading the file and slicing: a task can emit a
    multi-gigabyte .command.out, and reading one whole costs ~2.1x its size in
    RAM — enough to kill `--json` on a run that a pipeline produced happily.
    """
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            # 4 bytes is the longest a UTF-8 character gets, so this window
            # always holds at least `limit` characters.
            start = max(0, size - limit * 4)
            f.seek(start)
            buf = f.read()
    except OSError:
        return None
    text = buf.decode("utf-8", errors="replace")
    if start > 0:
        # The window opens mid-line; drop the fragment so the tail starts clean.
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    return text[-limit:] if len(text) > limit else text


def run_report(log_file: Path, *, logs: str = "failed",
               failed_only: bool = False) -> dict:
    """Everything nf-tui knows about a run, as plain data.

    Built for agents and scripts: the same parsing the UI uses, but nested per
    task and including the command files, so debugging a failure needs neither
    a terminal nor a walk through Nextflow's work tree.

    `logs` selects which tasks carry their .command.* contents: "failed" (the
    default — the debugging case, and cheap), "all", or "none".
    """
    tasks = parse_log(log_file)
    work_root = find_work_root(log_file)
    # An in-flight task carries no workDir in the log — Nextflow records it only
    # when the task completes — so resolve it by hash first. Without this every
    # executing task reports as "pending", since there is no .command.begin to
    # look for and no work dir to look in.
    local_root = None if remote_scheme(work_root) else Path(work_root)
    for t in tasks:
        if local_root is not None and not t.workdir \
                and t.status.upper() in IN_FLIGHT:
            found = resolve_workdir(local_root, t.hash)
            if found:
                t.workdir = found
    prog = progress_of(tasks, check_fs=True)
    errors = parse_errors(log_file)

    out_tasks = []
    for t in tasks:
        if failed_only and not is_failed(t):
            continue
        proc, tag = split_name(t.name)
        m = parse_trace(t.workdir) if t.workdir else Metrics()
        state = task_state(t)
        entry: dict = {
            "hash": t.hash,
            "name": t.name,
            "process": proc,
            "tag": tag,
            "status": t.status,
            "state": state,
            "exit": t.exit,
            "cached": t.cached,
            "attempts": t.attempts,
            "workdir": t.workdir or None,
            # Cloud executors keep the work tree in object storage, so a reader
            # knows why no logs or metrics are attached to this task.
            "workdir_remote": remote_scheme(t.workdir),
            "failed": is_failed(t),
        }
        if m.has_data():
            entry["metrics"] = {
                "realtime_ms": m.realtime_ms,
                "pct_cpu": m.pct_cpu,
                "peak_rss_kb": m.peak_rss_kb,
                "pct_mem": m.pct_mem,
            }
        if is_failed(t):
            block = errors.get(t.hash) or errors.get(f"name:{t.name}")
            if block:
                entry["error"] = {"summary": error_summary(block),
                                  # what the command itself printed — usually
                                  # the actual reason, where summary is only
                                  # Nextflow's "exit status (N)" framing
                                  "command_error": command_error(block),
                                  "why": why_failed(block),
                                  "report": block}
        want = logs == "all" or (logs == "failed" and is_failed(t))
        if want and t.workdir:
            wd = Path(t.workdir)
            captured = {}
            for key, name in (("script", ".command.sh"),
                              ("out", ".command.out"),
                              ("err", ".command.err"),
                              ("log", ".command.log")):
                body = _tail_text(wd / name)
                if body:
                    captured[key] = body
            if captured:
                entry["logs"] = captured
        out_tasks.append(entry)

    return {
        "log": str(log_file),
        "work_dir": str(work_root),
        "live": bool(tasks) and any(
            t.status.upper() in IN_FLIGHT for t in tasks),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "progress": {
            "total": prog.total, "done": prog.done, "failed": prog.failed,
            "cached": prog.cached, "running": prog.running,
            "pending": prog.pending, "pct": prog.pct,
            "per_min": round(prog.per_min, 2) if prog.per_min else None,
            "eta_secs": round(prog.eta_secs) if prog.eta_secs else None,
            # Mid-run the total keeps growing (Nextflow announces tasks as
            # channels emit), so `pct` is of what has been seen so far.
            "total_is_final": not any(
                t.status.upper() in IN_FLIGHT for t in tasks),
        },
        "processes": sorted({split_name(t.name)[0] for t in tasks}),
        "tasks": out_tasks,
    }


def main() -> None:
    # `nf-tui nextflow run …` — prefix any nextflow command to launch it and
    # watch it live. Handled before argparse, which would reject the trailing
    # pipeline arguments, and passed through verbatim so options that belong
    # before `run` (e.g. `-log`, `-C`) still work.
    argv = sys.argv[1:]
    if argv and argv[0] == "nextflow":
        if len(argv) == 1:
            sys.exit("nf-tui: give a full nextflow command, e.g.\n"
                     "  nf-tui nextflow run nf-core/sarek -profile test,docker")
        from nf_tui_run import launch
        launch(argv)
        return

    ap = argparse.ArgumentParser(
        prog="nf-tui",
        description="Browse Nextflow tasks and logs. With no path, searches the "
                    "current directory for runs and lets you pick one.",
        epilog="launch a pipeline and watch it live:  "
               "nf-tui nextflow run nf-core/sarek -profile test,docker")
    ap.add_argument("path", nargs="?", default=".",
                    help="a run directory, a .nextflow.log, or a directory to search")
    ap.add_argument("--json", action="store_true",
                    help="print the run as JSON instead of opening the UI: "
                         "progress, every task, and the command logs")
    ap.add_argument("--logs", choices=["none", "failed", "all"], default="failed",
                    help="which tasks carry their .command.* contents (default: "
                         "failed — the debugging case, and cheap)")
    ap.add_argument("--failed", action="store_true",
                    help="with --json, report only the failed tasks")
    ap.add_argument("--watch", type=float, metavar="SECS",
                    help="with --json, keep printing one JSON object per line "
                         "every SECS while the run is live")
    args = ap.parse_args()

    target = Path(args.path).expanduser()
    try:
        target = target.resolve()
    except OSError:
        sys.exit(
            f"nf-tui: cannot access '{args.path}'. If this directory was deleted "
            "and recreated by a running pipeline, your shell is in a stale copy — "
            "run  cd .. && cd -  (or pass an absolute path) and try again.")

    if not target.is_file() and not gather_runs(target):
        sys.exit(f"nf-tui: no .nextflow.log found under {target}")

    if args.json:
        log = target if target.is_file() else gather_runs(target)[0].path
        emit = lambda: json.dumps(                      # noqa: E731
            run_report(log, logs=args.logs, failed_only=args.failed))
        if not args.watch:
            print(emit())
            return
        # One object per line (JSON Lines) so a reader can consume updates as
        # they arrive rather than waiting for the run to finish.
        try:
            while True:
                print(emit(), flush=True)
                report = run_report(log, logs="none")
                if not report["live"]:
                    return
                time.sleep(max(0.5, args.watch))
        except KeyboardInterrupt:
            return
    # One app for the whole session: the run picker is a screen inside it
    # (so it works over the web via textual-serve, which serves one app).
    NfScope(target).run()


if __name__ == "__main__":
    main()
