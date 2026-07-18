"""Tests for nf-tui — parsing fidelity, edge cases, and 10k-task scale.

Run:  uv run --extra dev pytest        (or: pip install pytest && pytest)

The scale tests use a synthesized .nextflow.log (generate_run) because running
10k real tasks per test is impractical; a `test_parse_matches_real_format` test
pins the parser to the genuine Nextflow log format so the synthetic stays
faithful.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import nf_tui
from generate_run import make_run
from nf_tui import (NfScope, RunPickerScreen, Task, is_failed,
                    parse_container_run, parse_log, read_back, split_name)
from textual.widgets import OptionList, RichLog, Tree


def drive(app: NfScope, steps):
    """Run an app headless, apply an async `steps(app, pilot)`, return its value."""
    async def _run():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            return await steps(app, pilot)
    return asyncio.run(_run())


def leaves(tree: Tree):
    out = []
    def rec(n):
        for c in n.children:
            if hasattr(c.data, "hash"):
                out.append(c)
            rec(c)
    rec(tree.root)
    return out


# ---- parsing fidelity ------------------------------------------------------

# A verbatim line from a real nf-core/sarek .nextflow.log — the parser must
# keep handling this exact shape. If Nextflow changes it, this test fails.
REAL_HANDLER = (
    "Jul-14 10:38:12.345 [Task monitor] DEBUG n.processor.TaskPollingMonitor - "
    "Task completed > TaskHandler[id: 42; name: NFCORE_SAREK:SAREK:"
    "BAM_MARKDUPLICATES:GATK4_MARKDUPLICATES (test); status: COMPLETED; "
    "exit: 0; error: -; workDir: /scratch/work/88/41d2bab240fd98690a71bdcb6ab0d7]"
)
REAL_SUBMIT = (
    "Jul-14 10:38:01.030 [Task submitter] INFO  nextflow.Session - "
    "[88/41d2ba] Submitted process > NFCORE_SAREK:SAREK:BAM_MARKDUPLICATES:"
    "GATK4_MARKDUPLICATES (test)"
)


def test_parse_matches_real_format(tmp_path):
    log = tmp_path / ".nextflow.log"
    log.write_text(REAL_HANDLER + "\n")
    tasks = parse_log(log)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.hash == "88/41d2ba"
    assert t.status == "COMPLETED"
    assert t.exit == "0"
    assert t.name.endswith("GATK4_MARKDUPLICATES (test)")
    assert t.workdir == "/scratch/work/88/41d2bab240fd98690a71bdcb6ab0d7"
    assert split_name(t.name) == (
        "NFCORE_SAREK:SAREK:BAM_MARKDUPLICATES:GATK4_MARKDUPLICATES", "test")


def test_parse_submit_line(tmp_path):
    log = tmp_path / ".nextflow.log"
    log.write_text(REAL_SUBMIT + "\n")
    tasks = parse_log(log)
    assert len(tasks) == 1
    assert tasks[0].hash == "88/41d2ba"
    assert tasks[0].status == "SUBMITTED"


def test_synthetic_matches_parser(tmp_path):
    log = make_run(tmp_path, n_tasks=200, n_procs=5)
    tasks = parse_log(log)
    assert 195 <= len(tasks) <= 200          # a few short-hash collisions tolerated
    assert {split_name(t.name)[0] for t in tasks}.__len__() == 5
    assert any(is_failed(t) for t in tasks)  # generator seeds some failures


def test_missing_log_is_empty(tmp_path):
    assert parse_log(tmp_path / "nope.log") == []


# ---- container-run parsing -------------------------------------------------

def test_parse_container_run(tmp_path):
    wd = tmp_path / "work" / "ab" / "cd"
    wd.mkdir(parents=True)
    (wd / ".command.run").write_text(
        'nxf_launch() {\n'
        '    docker run -i -v /data:/data -v /scratch:/scratch -w "$NXF_TASK_WORKDIR" '
        '-u $(id -u):$(id -g) --name box quay.io/biocontainers/samtools:1.21 '
        '/bin/bash -c "eval ..."\n}\n'
    )
    spec = parse_container_run(str(wd))
    assert spec is not None
    engine, mounts, image = spec
    assert engine == "docker"
    assert image == "quay.io/biocontainers/samtools:1.21"
    assert mounts == ["-v", "/data:/data", "-v", "/scratch:/scratch"]


# ---- app behaviour (no crash) ----------------------------------------------

def test_app_loads_and_views(tmp_path):
    log = make_run(tmp_path, n_tasks=60, n_procs=4, with_workdirs=60)

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        assert len(leaves(tree)) >= 55
        # cycle every view — none should raise
        for key in ("down", "down", "c", "t", "g", "t"):
            await pilot.press(key)
            await pilot.pause()
        # files view opens + previews without crashing
        await pilot.press("d")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#files", OptionList).option_count >= 1
        # esc walks back out
        for _ in range(4):
            await pilot.press("escape")
            await pilot.pause()
        return True

    assert drive(NfScope(log), steps)


def test_failed_filter(tmp_path):
    log = make_run(tmp_path, n_tasks=300, n_procs=3, seed=1)

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        total = len(leaves(tree))
        await pilot.press("x")
        await pilot.pause()
        failed = len(leaves(tree))
        assert 0 < failed < total
        await pilot.press("x")
        await pilot.pause()
        assert len(leaves(tree)) == total
        return True

    assert drive(NfScope(log), steps)


def test_search_filters_tree_by_name_and_hash(tmp_path):
    from textual.widgets import Input
    log = make_run(tmp_path, n_tasks=300, n_procs=6)

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        box = app.query_one("#search", Input)
        total = len(leaves(tree))

        await pilot.press("slash")               # open the search box
        await pilot.pause()
        assert box.has_class("on") and box.has_focus

        app._apply_query("PROC_003")             # narrow to one process
        await pilot.pause()
        narrowed = leaves(tree)
        assert 0 < len(narrowed) < total
        assert all("proc_003" in n.data.name.lower() for n in narrowed)

        h = app.tasks[5].hash                    # narrow to a single hash
        app._apply_query(h)
        await pilot.pause()
        assert [n.data.hash for n in leaves(tree)] == [h]

        app._apply_query("no-such-task-xyz")     # no match -> a message, no crash
        await pilot.pause()
        assert leaves(tree) == []

        await pilot.press("slash")               # reopen, esc clears the filter
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_str == "" and len(leaves(tree)) == total
        assert not box.has_class("on")
        return True

    assert drive(NfScope(log), steps)


def test_search_survives_live_refresh(tmp_path):
    # A filter must stay applied when the 1s refresh re-parses a growing log.
    log = make_run(tmp_path, n_tasks=120, n_procs=4)

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        app._apply_query("PROC_001")
        await pilot.pause()
        shown = len(leaves(tree))
        assert 0 < shown < 120
        app._force_refresh = True                # simulate the timer re-parsing
        app.action_refresh()
        await pilot.pause()
        assert app.query_str == "PROC_001"
        assert len(leaves(tree)) == shown        # filter still applied
        return True

    assert drive(NfScope(log), steps)


def test_broken_symlink_and_binary(tmp_path):
    wd = tmp_path / "work" / "aa" / ("a" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    (wd / ".command.sh").write_text("echo hi\n")
    (wd / "data.bin").write_bytes(bytes(range(256)) * 8)
    os.symlink(tmp_path / "gone", wd / "broken.link")
    log = tmp_path / ".nextflow.log"
    log.write_text(
        f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; exit: 0; "
        f"error: -; workDir: {wd}]\n")

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        tree.move_cursor(leaves(tree)[0])
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        names = [p.name for p in app._files]
        assert "data.bin" in names and "broken.link" in names   # no crash listing
        # opening the binary shows the guard, not garbage
        files = app.query_one("#files", OptionList)
        files.highlighted = names.index("data.bin")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = "\n".join(str(x) for x in app.query_one("#log", RichLog).lines)
        assert "binary" in text.lower()
        return True

    assert drive(NfScope(log), steps)


def test_no_crash_with_picker_open(tmp_path):
    # Two runs -> the run picker screen shows first. The 1s refresh timer must
    # not touch the viewer (whose #log isn't on the active screen) and crash.
    make_run(tmp_path, n_tasks=20, n_procs=2)
    (tmp_path / ".nextflow.log.1").write_text((tmp_path / ".nextflow.log").read_text())

    async def steps(app, pilot):
        assert isinstance(app.screen, RunPickerScreen)
        app.action_refresh()                      # timer fires while picker up
        await pilot.pause()
        app.screen.query_one("#runs", OptionList).highlighted = 0
        await pilot.press("enter")                # pick a run
        await pilot.pause()
        await pilot.pause()
        assert app.log_file is not None
        app.query_one("#tasks", Tree).focus()
        for _ in range(5):                        # esc walks run -> task -> picker
            if isinstance(app.screen, RunPickerScreen):
                break
            await pilot.press("escape")
            await pilot.pause()
        assert isinstance(app.screen, RunPickerScreen)
        app.action_refresh()                      # the exact crash scenario
        await pilot.pause()
        return True

    assert drive(NfScope(tmp_path), steps)


# ---- run log on load -------------------------------------------------------

def test_run_log_shows_on_load_without_a_keypress(tmp_path):
    # Opening a run must land on the run log already painted — no `g` needed.
    log = make_run(tmp_path, n_tasks=30, n_procs=3)

    async def steps(app, pilot):
        await pilot.pause()
        assert app.view == "run"
        pane = app.query_one("#log", RichLog)
        assert len(pane.lines) > 1, "run log pane is empty on load"
        assert "full run log" in "\n".join(str(x) for x in pane.lines)
        return True

    assert drive(NfScope(log), steps)


def test_run_log_opens_at_the_end_and_scrolls_up(tmp_path):
    # The pane holds only the last RUNLOG_MAX_LINES, so its top is an arbitrary
    # point, not the launch command. Both live and finished runs open at the end
    # (where the outcome is); only a live run auto-follows new lines.
    log = make_run(tmp_path, n_tasks=30, n_procs=3)

    async def steps(app, pilot):
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        assert pane.scroll_y == pane.max_scroll_y, "run log should open at the end"
        assert pane.auto_scroll == app._run_is_live()
        # and you can scroll back up through the loaded tail
        pane.scroll_up(animate=False)
        await pilot.pause()
        assert pane.scroll_y < pane.max_scroll_y
        return True

    assert drive(NfScope(log), steps)          # just written: live -> follows

    old = time.time() - 3600                   # age it past the live window
    os.utime(log, (old, old))
    assert drive(NfScope(log), steps)          # finished: at end, not following


# ---- run log backfill ------------------------------------------------------

def test_read_back_walks_a_file_exactly(tmp_path):
    # Byte accounting must be exact, or backfill would drop or duplicate lines.
    # Includes a non-ASCII line: decoding must not desync the byte offsets.
    p = tmp_path / ".nextflow.log"
    want = [f"line {i} — ünïcode" if i % 50 == 0 else f"line {i} " + "x" * (i % 40)
            for i in range(2000)]
    p.write_text("\n".join(want) + "\n")

    end, got, steps = p.stat().st_size, [], 0
    while end > 0:
        start, lines = read_back(p, end, max_bytes=3_000, max_lines=100)
        assert lines, "backfill stalled before reaching the top"
        got = lines + got
        end = start
        steps += 1
    assert steps > 1, "test should need several chunks to be meaningful"
    assert got == want                      # reconstructed the file exactly


def test_read_back_on_a_missing_file_is_empty(tmp_path):
    assert read_back(tmp_path / "nope.log", 100) == (100, [])


def test_scrolling_up_backfills_to_the_top_of_the_log(tmp_path):
    # Must exceed RUNLOG_MAX_LINES, or the whole log loads at once and this
    # would pass without ever backfilling.
    log = make_run(tmp_path, n_tasks=3_000, n_procs=10)
    want = log.read_bytes().decode("utf-8", errors="replace").splitlines()
    assert len(want) > nf_tui.RUNLOG_MAX_LINES

    async def steps(app, pilot):
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        assert len(app._runlog_lines) < len(want), "log should start partly loaded"
        for _ in range(60):                 # scroll up until the top is loaded
            pane.scroll_home(animate=False)
            await pilot.pause()
            if app._runlog_start == 0:
                break
        assert app._runlog_start == 0, "never reached the top of the file"
        assert app._runlog_lines == want    # every line, in order, no gaps
        return True

    assert drive(NfScope(log), steps)


def test_backfill_keeps_the_viewport_on_the_same_line(tmp_path):
    log = make_run(tmp_path, n_tasks=3_000, n_procs=10)

    async def steps(app, pilot):
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        before = len(app._runlog_lines)
        pane.scroll_home(animate=False)
        await pilot.pause()
        anchor = str(pane.lines[int(pane.scroll_y)])
        added = len(app._runlog_lines) - before
        assert added > 0, "scrolling to the top should have backfilled"
        # the viewport shifted down by exactly the prepended lines, so the line
        # being read stays put rather than jumping
        assert pane.scroll_y == added
        assert str(pane.lines[int(pane.scroll_y)]) == anchor
        return True

    assert drive(NfScope(log), steps)


def test_following_pauses_while_scrolled_up(tmp_path):
    # A live run appends every second. Scrolling up to read must not be yanked
    # back to the bottom by arriving lines; returning to the bottom resumes.
    log = make_run(tmp_path, n_tasks=3_000, n_procs=10)
    os.utime(log, None)                       # fresh mtime -> live

    def text(strip):
        return "".join(seg.text for seg in strip)

    def append(line):
        with log.open("a") as f:
            f.write(line + "\n")

    async def steps(app, pilot):
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        assert app._run_is_live() and pane.auto_scroll

        pane.scroll_up(animate=False)
        pane.scroll_up(animate=False)
        await pilot.pause()
        parked = pane.scroll_y
        for i in range(3):
            append(f"~> new line {i}")
            app.action_refresh()
            await pilot.pause()
        assert pane.scroll_y == parked, "following yanked the viewport back down"
        assert any("new line 2" in text(s) for s in pane.lines), "lines still collect"

        pane.scroll_end(animate=False)        # back to the bottom -> follow again
        await pilot.pause()
        append("~> newest line")
        app.action_refresh()
        await pilot.pause()
        assert pane.scroll_y == pane.max_scroll_y
        assert "newest line" in text(pane.lines[-1])
        return True

    assert drive(NfScope(log), steps)


def test_plain_files_go_to_less_directly_not_through_a_pipe(tmp_path):
    # zless runs `gzip -cdfq file | less`, which makes stdin a pipe. less can't
    # seek a pipe, so +G must read the whole file before painting — on a 138MB
    # run log that never finished. Plain files must be passed as an argument.
    wd = tmp_path / "work" / "ab" / "cd"
    wd.mkdir(parents=True)
    plain, gz = wd / "out.txt", wd / "out.txt.gz"
    plain.write_text("hello\n")
    gz.write_bytes(b"\x1f\x8b\x00")
    t = Task(hash="ab/cd", name="P (s)", workdir=str(wd))

    app = NfScope(tmp_path)
    plain_cmd = app._pager_command(t, plain, "less")
    assert "|" not in plain_cmd, "plain file must not be piped into less"
    assert plain_cmd.endswith(str(plain))          # handed over as an argument

    gz_cmd = app._pager_command(t, gz, "less")     # gz has to be decompressed
    assert "gzip -cdfq" in gz_cmd and "| less" in gz_cmd


def test_run_log_pager_seeks_to_the_end(tmp_path):
    # +G is only safe because less gets the file itself and can seek to it.
    assert nf_tui.pager_bin() in ("less", None)


# ---- metrics, sort, stale runs, web parity ---------------------------------

def _write_trace(workdir: Path, realtime_ms: int, peak_rss_kb: int) -> None:
    (workdir / ".command.trace").write_text(
        f"nextflow.trace/v2\nrealtime={realtime_ms}\n%cpu=200\n"
        f"%mem=10\npeak_rss={peak_rss_kb}\n")


def test_parse_trace_reads_and_degrades(tmp_path):
    from nf_tui import parse_trace
    _write_trace(tmp_path, 6265, 448244)
    m = parse_trace(str(tmp_path))
    assert m.has_data() and m.realtime_ms == 6265 and m.peak_rss_kb == 448244
    assert not parse_trace(str(tmp_path / "missing")).has_data()   # no crash
    assert not parse_trace("").has_data()


def test_sort_floats_the_slowest_process(tmp_path):
    from nf_tui import parse_log
    log = make_run(tmp_path, n_tasks=40, n_procs=4, with_workdirs=40)
    # give each task a trace; make one process clearly the slowest
    for t in parse_log(log):
        proc = t.name.split(":")[-1].split(" ")[0]
        slow = proc.endswith("PROC_002")
        _write_trace(Path(t.workdir), 9000 if slow else 100, 1000)

    async def steps(app, pilot):
        await pilot.pause()
        tree = app.query_one("#tasks", Tree)
        app.action_cycle_sort()                  # -> slowest
        await pilot.pause()
        assert app.sort_mode == "slowest"
        first_group = next(g for g in tree.root.children if g.children)
        assert "PROC_002" in str(first_group.label)   # slowest process floated up
        return True

    assert drive(NfScope(log), steps)


def test_run_state_classifies_runs():
    from nf_tui import RunInfo, run_state
    now = 1_000_000.0
    def mk(status, age, finished):
        return RunInfo(Path("/x"), "r", "p", status, now - age, finished)
    assert run_state(mk("OK", 1e6, False), now)[1] == "complete"
    assert run_state(mk("ERR", 1e6, False), now)[1] == "failed"
    assert run_state(mk("?", 1e6, True), now)[1] == "complete"    # marker in log
    assert run_state(mk("?", 5, False), now)[1] == "running"      # recent write
    assert run_state(mk("?", 1e6, False), now)[1] == "stalled"    # died silently


def test_log_finished_detects_completion(tmp_path):
    from nf_tui import _log_finished
    done = tmp_path / "done.log"
    done.write_text("... lots of log ...\nExecution complete -- Goodbye\n")
    assert _log_finished(done)
    partial = tmp_path / "partial.log"
    partial.write_text("Submitted process > FOO\n... running ...\n")
    assert not _log_finished(partial)


def test_full_file_lifts_the_preview_cap(tmp_path, monkeypatch):
    from nf_tui import VIEW_MAX_LINES
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    big = wd / "big.txt"
    big.write_text("\n".join(f"line {i}" for i in range(VIEW_MAX_LINES + 3000)) + "\n")
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        tree.move_cursor(leaves(tree)[0])
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        files = app.query_one("#files", OptionList)
        files.highlighted = [p.name for p in app._files].index("big.txt")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        capped = len(app.query_one("#log", RichLog).lines)
        await pilot.press("F")                   # load the whole file in-pane
        await app.workers.wait_for_complete()
        await pilot.pause()
        full = len(app.query_one("#log", RichLog).lines)
        assert capped <= VIEW_MAX_LINES + 10
        assert full > capped                     # F showed strictly more
        return True

    assert drive(NfScope(tmp_path), steps)


def test_full_file_on_container_file_without_tree_cursor(tmp_path):
    # Repro for the AttributeError crash: F on a BAM/CRAM while the tree cursor
    # is NOT on a task leaf. _open_file must use the files' task, not _selected().
    wd = tmp_path / "work" / "d0" / ("7" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    (wd / "test.recal.cram").write_bytes(b"CRAM\x00fake")
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        tree.move_cursor(leaves(tree)[0])
        await pilot.pause()
        await pilot.press("d")                   # files view: populates _files_task
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        files = app.query_one("#files", OptionList)
        files.highlighted = [p.name for p in app._files].index("test.recal.cram")
        # move the tree cursor OFF the task, onto its process group node
        tree.move_cursor(tree.root.children[0])
        await pilot.pause()
        assert app._selected() is None           # the exact precondition of the bug
        await pilot.press("F")                   # must not raise
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = "\n".join(str(x) for x in app.query_one("#log", RichLog).lines)
        assert "test.recal.cram" in text         # it rendered (a decode message)
        return True

    assert drive(NfScope(tmp_path), steps)


def test_log_finished_reads_only_the_tail(tmp_path):
    # Must detect the end marker without reading the whole (possibly huge) file.
    from nf_tui import _log_finished
    big = tmp_path / "big.log"
    big.write_text("filler line\n" * 200_000 + "Execution complete -- Goodbye\n")
    partial = tmp_path / "partial.log"
    partial.write_text("still running\n" * 1000)
    assert _log_finished(big)
    assert not _log_finished(partial)
    # a marker only in the HEAD (not the tail) must not count as finished
    head_only = tmp_path / "head.log"
    head_only.write_text("Goodbye\n" + "more log\n" * 200_000)
    assert not _log_finished(head_only)


def test_switching_runs_resets_per_run_state(tmp_path):
    # A task in the new run whose short hash matches one in the old run must show
    # the new run's metrics, not the cached old ones.
    def mk(root: Path, dur_ms: int) -> Path:
        wd = root / "work" / "ab" / ("c" * 30)
        wd.mkdir(parents=True)
        (wd / ".command.log").write_text("x\n")
        (wd / ".command.trace").write_text(
            f"nextflow.trace/v2\nrealtime={dur_ms}\npeak_rss=1000\n")
        log = root / ".nextflow.log"
        log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                       f"exit: 0; error: -; workDir: {wd}]\n")
        return log

    a = mk(tmp_path / "a", 5000)
    b = mk(tmp_path / "b", 99000)

    async def steps(app, pilot):
        await pilot.pause()
        assert app._metrics(app.tasks[0]).realtime_ms == 5000
        app.load_run(b)
        await pilot.pause()
        assert app._metrics(app.tasks[0]).realtime_ms == 99000   # not the cached 5000
        assert app._files == [] and app._files_task is None      # file state reset too
        return True

    assert drive(NfScope(a), steps)


def test_picking_a_run_does_not_open_a_stale_file(tmp_path):
    # The picker's OptionList selection bubbles to the app's file handler; after
    # browsing files it must not reopen a previous run's file.
    from generate_run import make_run
    make_run(tmp_path, n_tasks=20, n_procs=2, with_workdirs=20)
    (tmp_path / ".nextflow.log.1").write_text((tmp_path / ".nextflow.log").read_text())

    async def steps(app, pilot):
        await pilot.pause()
        assert isinstance(app.screen, RunPickerScreen)
        app.screen.query_one("#runs", OptionList).highlighted = 0
        await pilot.press("enter")
        await pilot.pause(); await pilot.pause()
        tree = app.query_one("#tasks", Tree)
        tree.move_cursor(leaves(tree)[0])
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
        assert len(app._files) >= 1                     # files got populated
        for _ in range(6):
            if isinstance(app.screen, RunPickerScreen):
                break
            await pilot.press("escape")
            await pilot.pause()
        opened = []
        app._open_file = lambda p, full=False: opened.append(p)
        app.screen.query_one("#runs", OptionList).highlighted = 0
        await pilot.press("enter")
        await pilot.pause(); await pilot.pause()
        assert opened == []                             # no stale file opened
        return True

    assert drive(NfScope(tmp_path), steps)


# ---- scale -----------------------------------------------------------------

def test_parse_10k_is_fast(tmp_path):
    log = make_run(tmp_path, n_tasks=10_000, n_procs=50)
    t0 = time.time()
    tasks = parse_log(log)
    dt = time.time() - t0
    assert len(tasks) >= 9_900
    assert dt < 0.5, f"parse of 10k tasks took {dt:.2f}s"


def test_app_10k_loads_and_navigates(tmp_path):
    log = make_run(tmp_path, n_tasks=10_000, n_procs=50)

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        assert sum(len(g.children) for g in tree.root.children) >= 9_900
        app.view = "task"                      # measure task rendering per leaf
        # per-render work must stay tiny even at 10k
        worst = 0.0
        for lf in leaves(tree)[:40]:
            tree.cursor_line = lf.line
            app._shown = None
            t0 = time.time()
            app._render_current()
            worst = max(worst, time.time() - t0)
        assert worst < 0.05, f"a single render took {worst*1000:.0f}ms at 10k"
        # steady-state tick (log unchanged) must be ~free
        t0 = time.time()
        app.action_refresh()
        assert time.time() - t0 < 0.02
        return True

    t0 = time.time()
    ok = drive(NfScope(log), steps)
    assert ok
    assert time.time() - t0 < 5.0           # whole load+nav budget
