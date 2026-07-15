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
                     reference resolves. L opens it full in `zless`.
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
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, OptionList, RichLog, Tree
from textual.widgets.option_list import Option

REFRESH_SECONDS = 1.0
RUNLOG_TAIL = 400_000   # chars of .nextflow.log to show at most
VIEW_MAX_LINES = 2_000  # in-pane preview cap for host reads (text / gz)
BAM_PREVIEW_LINES = 500 # smaller cap for container-decoded BAM/CRAM/BCF (faster)

# Lines we care about in .nextflow.log:
#   ... [bf/407183] Submitted process > NFCORE:...:SRA_FASTQ_FTP (tag)
#   ~> TaskHandler[id: 6; name: ...; status: RUNNING; exit: -; error: -; workDir: /abs/path]
_SUBMIT_RE = re.compile(r"\[([0-9a-f]{2}/[0-9a-f]+)\] Submitted process > (.+)$")
_HANDLER_RE = re.compile(
    r"TaskHandler\[id: (?P<id>\d+); name: (?P<name>.+?); "
    r"status: (?P<status>\w+); exit: (?P<exit>[^;]+); "
    r"error: (?P<error>[^;]+); workDir: (?P<workdir>[^\]]+)\]"
)


@dataclass
class Task:
    hash: str = ""            # short hash as shown in console, e.g. "bf/407183"
    name: str = ""
    status: str = "-"
    exit: str = "-"
    workdir: str = ""
    order: int = field(default=0)  # first-seen order, for stable sorting


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
    return t.status.upper() == "COMPLETED"


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
    for line in log_file.read_text(errors="replace").splitlines():
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
            continue
        m = _SUBMIT_RE.search(line)
        if m:
            key = m.group(1)
            t = tasks.get(key)
            if t is None:
                t = Task(hash=key, order=seen)
                seen += 1
                tasks[key] = t
            if not t.name:
                t.name = m.group(2).strip()
            if t.status == "-":
                t.status = "SUBMITTED"
    return sorted(tasks.values(), key=lambda t: t.order)


def _read_all(path: Path, limit: int = 20000) -> str:
    try:
        data = path.read_text(errors="replace")
    except OSError as e:
        return f"[cannot read {path.name}: {e}]"
    return data[-limit:] if len(data) > limit else data


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


def head_gzip(path: Path, lines: int) -> list[str]:
    try:
        with gzip.open(path, "rt", errors="replace") as f:
            out = []
            for i, line in enumerate(f):
                if i >= lines:
                    break
                out.append(line.rstrip("\n"))
            return out
    except OSError as e:
        return [f"(cannot read gzip: {e})"]


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
    engine = line = None
    for raw in text.splitlines():
        s = raw.strip()
        if (s.startswith("docker ") or s.startswith("podman ")) and " run " in s:
            engine = s.split(None, 1)[0]
            line = s
            break
        if (s.startswith(("singularity ", "apptainer "))) and (" exec " in s or " run " in s):
            engine = s.split(None, 1)[0]
            line = s
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
    alt = "htslib" if binary == "samtools" else binary
    seen: set[str] = set()
    try:
        groups = sorted(work.iterdir())
    except OSError:
        return None
    for g in groups:
        if not g.is_dir():
            continue
        for cr in g.glob("*/.command.run"):
            spec = parse_container_run(str(cr.parent))
            if not spec:
                continue
            engine, _, img = spec
            if img in seen:
                continue
            seen.add(img)
            low = img.lower()
            if binary in low or alt in low:
                try:
                    ok = subprocess.run([engine, "image", "inspect", img],
                                        capture_output=True, timeout=15).returncode == 0
                except Exception:                     # noqa: BLE001
                    ok = False
                if ok:
                    return img
    return None


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
        try:
            with self.path.open("r", errors="replace") as fh:
                fh.seek(self.pos)
                data = fh.read()
                self.pos = fh.tell()
        except OSError:
            return ""
        return data


def _proc_label(proc: str, tasks: list[Task]) -> str:
    """'FASTQC   3/3 ✓' — last path segment, done/total, status icon."""
    short = proc.split(":")[-1]
    total = len(tasks)
    done = sum(is_done(t) for t in tasks)
    failed = sum(is_failed(t) for t in tasks)
    if failed:
        icon = f"✗ {failed} failed"
    elif done == total:
        icon = "✓"
    else:
        icon = "…"
    return f"{short}   {done}/{total} {icon}"


def _task_label(t: Task) -> str:
    _, tag = split_name(t.name)
    mark = "✗" if is_failed(t) else "✓" if is_done(t) else "•"
    exit_str = "" if t.exit in ("-", "") else f" exit={t.exit}"
    return f"{mark} {tag or t.hash}   {t.status}{exit_str}"


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
    CSS = """
    #tasks { height: 1fr; border: round $panel; }
    #tasks:focus { border: round $accent; }
    #bottom { height: 50%; }
    #files { width: 38%; border: round $panel; display: none; }
    #files:focus { border: round $accent; }
    #log { width: 1fr; border: round $panel; padding: 0 1; }
    #log:focus { border: round $accent; }
    """
    BINDINGS = [
        Binding("tab", "focus_next_pane", "Next pane", show=False),
        Binding("l,enter", "focus_log", "Scroll log"),
        Binding("escape", "back", "Back to tasks"),
        Binding("t", "view_task", "Task log"),
        Binding("c", "view_container", "Container log"),
        Binding("d", "view_files", "Files"),
        Binding("L", "pager", "Open in less"),
        Binding("g", "view_run", "Run log"),
        Binding("z", "zoom", "Full screen"),
        Binding("f", "toggle_follow", "Follow"),
        Binding("x", "toggle_failed", "Failed only"),
        Binding("o", "open_workdir", "Work dir"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
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
        self.follow = True
        self.view = "task"          # "task" | "container" | "files" | "run"
        self._sig: tuple | None = None   # skip tree work when nothing changed
        self._built_filter: bool | None = None  # failed_only used for last full build
        self._shown: tuple | None = None # what the log pane currently shows
        self._tailer: Follower | None = None
        self._task_by_hash: dict[str, Task] = {}
        self._log_stat: tuple | None = None   # (size, mtime) of last-parsed log
        self._force_refresh = False           # re-parse even if the log is unchanged
        self._groups: dict = {}          # proc name -> its tasks (rebuilt per parse)
        self._proc_nodes: dict = {}      # proc name -> TreeNode  (updated in place)
        self._task_nodes: dict = {}      # task hash -> TreeNode
        self._files: list[Path] = []     # entries backing the #files list
        self._tool_image_cache: dict = {}  # binary -> image that provides it

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            tree: Tree = Tree("processes", id="tasks")
            tree.show_root = False
            tree.guide_depth = 3
            yield tree
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
        self.set_interval(REFRESH_SECONDS, self.action_refresh)  # live updates
        if self.log_file is not None:
            self.load_run(self.log_file)
        else:
            self._open_picker()             # multiple runs -> choose one first

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
        self.view = "task"
        self._sig = None
        self._log_stat = None
        self._force_refresh = True
        self._built_filter = None
        self._shown = None
        self._tailer = None
        self._proc_nodes = {}
        self._task_nodes = {}
        self._tool_image_cache = {}
        self.query_one("#tasks", Tree).clear()
        self.query_one("#files", OptionList).display = False
        self.action_refresh()
        self.query_one("#tasks", Tree).focus()

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
        # Header summary so the TOTAL (across all process groups) is visible.
        n = len(self.tasks)
        nproc = len({split_name(t.name)[0] for t in self.tasks})
        done = sum(is_done(t) for t in self.tasks)
        failed = sum(is_failed(t) for t in self.tasks)
        summary = f"{done:,}/{n:,} tasks · {nproc} processes"
        if failed:
            summary += f" · {failed} failed"
        loc = str(self.log_file).replace(str(Path.home()), "~")
        self.sub_title = f"{summary}  —  {loc}"
        sig = tuple((t.hash, t.status, t.exit) for t in self.tasks) + (self.failed_only,)
        if sig != self._sig:
            self._sig = sig
            if self.failed_only != self._built_filter:
                self._built_filter = self.failed_only
                self._full_rebuild()   # filter changed: repopulate from scratch
            else:
                self._sync_tree()      # in place: never disturbs cursor/focus/scroll
        self._render_current()

    def _visible_tasks(self) -> list[Task]:
        if self.failed_only:
            return [t for t in self.tasks if is_failed(t)]
        return self.tasks

    def _full_rebuild(self) -> None:
        tree = self.query_one("#tasks", Tree)
        tree.clear()
        self._proc_nodes = {}
        self._task_nodes = {}
        self._sync_tree()
        if not self._proc_nodes:
            tree.root.add_leaf("(no failed tasks)" if self.failed_only
                               else "(no tasks yet)")

    def _sync_tree(self) -> None:
        """Update the tree IN PLACE — update labels, append new nodes. Never
        clears, so the cursor, focus and scroll position are left untouched."""
        tree = self.query_one("#tasks", Tree)
        for proc, tasks in self._groups.items():
            pnode = self._proc_nodes.get(proc)
            if pnode is None:
                pnode = tree.root.add(_proc_label(proc, tasks), data=proc)
                pnode.expand()
                self._proc_nodes[proc] = pnode
            else:
                pnode.set_label(_proc_label(proc, tasks))
            for t in tasks:
                leaf = self._task_nodes.get(t.hash)
                if leaf is None:
                    self._task_nodes[t.hash] = pnode.add_leaf(_task_label(t), data=t)
                else:
                    leaf.set_label(_task_label(t))

    def _selected(self) -> Task | None:
        node = self.query_one("#tasks", Tree).cursor_node
        if node is None or not isinstance(node.data, Task):
            return None
        # node.data may be a stale Task object; return the freshest by hash.
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
        if self.view == "task":
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
        log.auto_scroll = self.follow    # tailing views follow new lines
        log.clear()
        self._log_header(log, t)
        self._tailer = Follower(Path(t.workdir) / ".command.log") if t.workdir else None
        if self._tailer is None or not self._tailer.path.exists():
            log.write("(.command.log not written yet)")
            return
        raw = self._tailer.read_new()   # whole file (pos started at 0)
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

    def _show_run_log(self, log: RichLog) -> None:
        """Load the whole .nextflow.log into the pane, then keep tailing it."""
        log.auto_scroll = self.follow
        log.clear()
        if not self.log_file.exists():
            self._tailer = None
            log.write(f"({self.log_file} not found)")
            return
        size = self.log_file.stat().st_size
        log.write(f"──────── {self.log_file.name}   (full run log, live) ────────")
        if size > RUNLOG_TAIL:
            log.write(f"(showing last {RUNLOG_TAIL // 1000} KB of {size // 1000} KB — "
                      f"open the file for the complete history)")
        log.write(_read_all(self.log_file, limit=RUNLOG_TAIL))
        self._tailer = Follower(self.log_file)
        self._tailer.pos = size   # continue from the end for live appends

    def _container_desc(self, t: Task) -> str:
        cont = task_container(t.workdir) if t.workdir else None
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
        self._tailer = None
        files = self.query_one("#files", OptionList)
        files.clear_options()
        self._files = []
        log = self.query_one("#log", RichLog)
        log.clear()
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

    def _open_file(self, p: Path) -> None:
        """Render a file in the right pane using a tool from the task's container."""
        t = self._selected()
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
            log.write(f"(binary file, {sz} — no text viewer; press L for less)")
            return
        if tool:
            header.append(f"$ {tool} {p.name}   (in {self._container_desc(t)})")
        elif gz:
            header.append(f"$ gunzip -c {p.name}   (host)")
        else:
            header.append(f"$ cat {p.name}   (host)")
        self._viewer_header = header
        log.clear()
        for h in header:
            log.write(h)
        log.write("… loading …")
        # Focus stays on the file list; its paging keys scroll this pane.
        self._run_viewer(t, p, tool, gz)

    @work(thread=True, exclusive=True)
    def _run_viewer(self, t: Task, p: Path, tool: str | None, gz: bool) -> None:
        # Text and gzip are read directly on the host — fast, no container.
        if tool is None:
            if gz:
                out = head_gzip(p, VIEW_MAX_LINES)
            else:
                try:
                    out = p.read_text(errors="replace").splitlines()[:VIEW_MAX_LINES]
                except OSError as e:
                    out = [f"(cannot read: {e})"]
            self.call_from_thread(self._viewer_done, out or ["(empty)"], VIEW_MAX_LINES)
            return
        # BAM/CRAM/BCF: decode with a samtools/bcftools image + the task's mounts.
        spec = self._viewer_spec(t.workdir, tool) if (t and t.workdir) else None
        if spec is None:
            self.call_from_thread(self._viewer_done,
                                  ["(no container found to decode this file)"])
            return
        engine, mounts, image = spec
        # cd into the task work dir so relative references (e.g. a CRAM's
        # -T genome.fasta) resolve exactly as they did for the task.
        inner = (f"cd {shlex.quote(t.workdir)} && "
                 f"{tool} {shlex.quote(str(p))} 2>&1 | head -n {BAM_PREVIEW_LINES}")
        if engine in ("docker", "podman"):
            try:
                chk = subprocess.run([engine, "image", "inspect", image],
                                     capture_output=True, timeout=20)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                chk = None
            if chk is None:
                self.call_from_thread(self._viewer_done, [f"({engine} not available)"])
                return
            if chk.returncode != 0:
                self.call_from_thread(self._viewer_done, [
                    f"(image not present locally: {image})",
                    f"pull it first:  {engine} pull {image}",
                ])
                return
            cmd = ([engine, "run", "--rm"] + mounts
                   + ["-w", t.workdir, image, "sh", "-c", inner])
        else:
            cmd = [engine, "exec"] + mounts + [image, "sh", "-c", inner]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            out = (r.stdout or r.stderr or "(no output)").splitlines()
        except FileNotFoundError:
            out = [f"({engine} is not installed / not on PATH)"]
        except subprocess.TimeoutExpired:
            out = ["(viewer timed out after 120s)"]
        except Exception as e:                       # noqa: BLE001
            out = [f"(error running viewer: {e})"]
        self.call_from_thread(self._viewer_done, out, BAM_PREVIEW_LINES)

    def _pager_command(self, t: Task, p: Path) -> str:
        """Shell string that pages a file with `zless` (lazy, handles gz + text).
        BAM/CRAM/BCF are decoded by the task's container tool, then piped to
        zless; everything else is opened straight on the host."""
        tool = decode_tool(p)
        if tool is None:                          # gz or text -> host zless
            return f"zless -R {shlex.quote(str(p))}"
        spec = self._viewer_spec(t.workdir, tool) if (t and t.workdir) else None
        if spec is None:
            return f"echo '(no container found to decode {p.name})' | zless -R"
        engine, mounts, image = spec
        # No -i: the container must NOT read the terminal, or it steals the
        # keystrokes meant for zless (samtools reads the file, not stdin).
        inner = f"cd {shlex.quote(t.workdir)} && exec {tool} {shlex.quote(str(p))}"
        parts = ([engine, "run", "--rm"] + mounts
                 + ["-w", t.workdir, image, "sh", "-c", inner])
        return " ".join(shlex.quote(x) for x in parts) + " 2>&1 | zless -R"

    def action_pager(self) -> None:
        if self.view != "files":
            self.notify("switch to the files view (d) first")
            return
        files = self.query_one("#files", OptionList)
        idx = files.highlighted
        if idx is None or idx >= len(self._files):
            return
        t, p = self._selected(), self._files[idx]
        if t is None:
            return
        # Only BAM/CRAM/BCF need the container; check its image is present.
        tool = decode_tool(p)
        if tool is not None:
            spec = self._viewer_spec(t.workdir, tool) if t.workdir else None
            if spec and spec[0] in ("docker", "podman"):
                chk = subprocess.run([spec[0], "image", "inspect", spec[2]],
                                     capture_output=True)
                if chk.returncode != 0:
                    self.notify(f"image not present — {spec[0]} pull {spec[2]}")
                    return
        with self.suspend():                # hand the real terminal to zless
            subprocess.run(["sh", "-c", self._pager_command(t, p)])

    def _viewer_done(self, lines: list[str], cap: int = VIEW_MAX_LINES) -> None:
        log = self.query_one("#log", RichLog)
        log.auto_scroll = False          # a file: stay put so we can start at the top
        log.clear()
        for h in getattr(self, "_viewer_header", []):
            log.write(h)
        log.write("─" * 30)
        # Write the body in one shot — per-line writes are ~100x slower.
        if lines:
            log.write("\n".join(lines))
        if len(lines) >= cap:
            log.write(f"─── (preview capped at {cap:,} lines — "
                      f"press L for the full file, o to open the work dir) ───")
        # Scroll to the top after the content is laid out (doing it now, before
        # the virtual size is measured, doesn't stick).
        self.call_after_refresh(lambda: log.scroll_home(animate=False))

    def _render_current(self) -> None:
        """Every tick / selection change: (re)draw the pane for the current view."""
        panes = self.query("#log")          # empty while another screen is up
        if not panes:
            return
        log = panes.first(RichLog)

        # Run log: the whole .nextflow.log, independent of tree selection.
        if self.view == "run":
            if self._shown != ("run",):
                self._shown = ("run",)
                self._show_run_log(log)
            elif self.follow and self._tailer is not None:
                for line in self._tailer.read_new().splitlines():
                    log.write(line)     # raw, unfiltered
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
                self._emit_view(log, self._tailer.read_new())   # live append
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
                lines += [f"  {_task_label(x)}   [{x.hash}]" for x in members[:40]]
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
        idx = event.option.id
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
        #   full screen       -> restore the split
        #   run log           -> task view + tree
        #   focus on log pane -> tree
        #   on the tree       -> back to the run selector (if launched from it)
        tree = self.query_one("#tasks", Tree)
        log = self.query_one("#log", RichLog)
        files = self.query_one("#files", OptionList)
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
        if self.view == "run":
            self._set_view("task", "task log")   # run log -> task view + tree
            tree.focus()
            return
        if log.has_focus:
            tree.focus()                          # log pane -> tree
            return
        if self.view != "task":
            self._set_view("task", "task log")   # container -> task view
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
        infos.append(RunInfo(
            path=p,
            runname=(runname or p.parent.name) + rot,
            pipeline=_pipeline_of(command),
            status=status,
            mtime=mtime,
        ))
    infos.sort(key=lambda r: r.mtime, reverse=True)
    return infos


class RunPickerScreen(Screen):
    """Pick which discovered run to open. Dismisses with the chosen log path
    (or None if cancelled). A screen — not a separate App — so the whole
    session is one app (needed for the web/textual-serve mode)."""

    CSS = "#runs { height: 1fr; }"
    BINDINGS = [Binding("q,escape", "cancel", "Cancel")]

    def __init__(self, base: Path, runs: list[RunInfo]):
        super().__init__()
        self.base = base
        self.runs = runs

    def compose(self) -> ComposeResult:
        yield Header()
        yield OptionList(id="runs")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = f"select a run under {self.base}"
        ol = self.query_one("#runs", OptionList)
        icons = {"OK": "✓", "ERR": "✗"}
        for i, r in enumerate(self.runs):
            when = datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d %H:%M")
            mark = icons.get(r.status, "…")
            loc = str(r.path.parent).replace(str(Path.home()), "~")
            ol.add_option(Option(
                f"{mark}  {when}   {r.runname}   {r.pipeline}\n     {loc}",
                id=str(i),
            ))
        if self.runs:
            ol.highlighted = 0        # so <enter> selects immediately
        ol.focus()

    def on_option_list_option_selected(self, event) -> None:
        self.dismiss(self.runs[int(event.option.id)].path)

    def action_cancel(self) -> None:
        self.dismiss(None)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="nf-tui",
        description="Browse Nextflow tasks and logs. With no path, searches the "
                    "current directory for runs and lets you pick one.")
    ap.add_argument("path", nargs="?", default=".",
                    help="a run directory, a .nextflow.log, or a directory to search")
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
    # One app for the whole session: the run picker is a screen inside it
    # (so it works over the web via textual-serve, which serves one app).
    NfScope(target).run()


if __name__ == "__main__":
    main()
