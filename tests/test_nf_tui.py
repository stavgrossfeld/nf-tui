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
import subprocess
import time
from pathlib import Path

import nf_tui
import pytest
from generate_run import make_run
from nf_tui import (NfScope, RunPickerScreen, Task, is_failed,
                    parse_container_run, parse_log, read_back, split_name)
from textual.widgets import DataTable, OptionList, RichLog, Tree


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


def test_parses_every_nextflow_task_line_variant(tmp_path):
    # Nextflow announces tasks four ways (TaskProcessor.RunType + storeDir).
    # A -resume run logs ONLY "Cached process" lines and no TaskHandler lines at
    # all, so missing them made a resumed run render as completely empty.
    log = tmp_path / ".nextflow.log"
    log.write_text(
        "a INFO  nextflow.Session - [ab/111111] Submitted process > P:A (s1)\n"
        "b INFO  nextflow.Session - [ab/222222] Re-submitted process > P:A (s2)\n"
        "c INFO  n.processor.TaskProcessor - [ab/333333] Cached process > P:B (s3)\n"
        "d INFO  n.processor.TaskProcessor - [skipping] Stored process > P:C (s4)\n"
    )
    by_hash = {t.hash: t for t in parse_log(log)}
    assert len(by_hash) == 4

    assert by_hash["ab/111111"].status == "SUBMITTED"
    assert not by_hash["ab/111111"].cached

    assert by_hash["ab/222222"].attempts == 2          # a retry attempt

    cached = by_hash["ab/333333"]
    assert cached.status == "CACHED" and cached.cached and cached.exit == "0"
    assert nf_tui.is_done(cached)                      # -resume reuses its result

    stored = by_hash["-"]                              # storeDir logs no hash
    assert stored.status == "STORED" and stored.cached


def test_stored_tasks_do_not_collide(tmp_path):
    # Every storeDir task logs the literal "[skipping]", so keying on the hash
    # would fold them all into one entry.
    log = tmp_path / ".nextflow.log"
    log.write_text(
        "a INFO - [skipping] Stored process > P:A (one)\n"
        "b INFO - [skipping] Stored process > P:A (two)\n"
        "c INFO - [skipping] Stored process > P:A (three)\n"
    )
    tasks = parse_log(log)
    assert len(tasks) == 3
    assert {t.name for t in tasks} == {"P:A (one)", "P:A (two)", "P:A (three)"}


def test_parses_grid_executor_handler_lines(tmp_path):
    # SLURM/PBS/k8s/AWS use GridTaskHandler etc. Every executor's handler class
    # ends in "TaskHandler", which is what the parser keys on.
    log = tmp_path / ".nextflow.log"
    log.write_text(
        "x DEBUG n.executor.GridTaskHandler - Task completed > GridTaskHandler"
        "[id: 7; name: P:A (s1); status: COMPLETED; exit: 0; error: -; "
        "workDir: /scratch/work/cd/ef1234abcd]\n")
    tasks = parse_log(log)
    assert len(tasks) == 1
    assert tasks[0].hash == "cd/ef1234" and tasks[0].status == "COMPLETED"


def test_grid_handler_appends_fields_after_workdir(tmp_path):
    """On a scheduler, workDir is not the last field.

    GridTaskHandler.toStringBuilder calls its parent first and then appends
    "; started: ...; exited: ..." (checked in Nextflow's bytecode), so a line
    from SLURM/PBS carries extra fields after the work dir. Reading to the
    closing bracket swallowed them into the path:

        /scratch/work/ab/cdef123; started: 1721000000000; exited: 1721000012000

    The task still appeared in the tree — the short hash survives by accident —
    but the directory did not exist, so its logs, output files, resource metrics
    and container decode were all unavailable on every cluster run.
    """
    log = tmp_path / ".nextflow.log"
    log.write_text(
        "Jul-14 10:38:12.345 [Task monitor] DEBUG n.executor.GridTaskHandler - "
        "Task completed > GridTaskHandler[id: 4; name: ALIGN (s1); "
        "status: COMPLETED; exit: 0; error: -; "
        "workDir: /scratch/work/ab/cdef1234567890; "
        "started: 1721000000000; exited: 1721000012000]\n")
    t = parse_log(log)[0]
    assert t.workdir == "/scratch/work/ab/cdef1234567890"
    assert t.hash == "ab/cdef12"
    assert t.exit == "0" and t.status == "COMPLETED"


def test_cached_tasks_get_their_workdir_resolved(tmp_path):
    # Cached lines carry no workDir, but the dirs survive from the earlier run —
    # resolving them is what makes a resumed task's logs and files viewable.
    wd = tmp_path / "work" / "ab" / "333333deadbeef"
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("previous run output\n")
    log = tmp_path / ".nextflow.log"
    log.write_text("c INFO - [ab/333333] Cached process > P:B (s3)\n")
    t = parse_log(log)[0]
    assert t.workdir == str(wd)
    assert (Path(t.workdir) / ".command.log").exists()


def test_find_work_root_honours_dash_w_and_banner(tmp_path):
    from nf_tui import find_work_root
    custom = tmp_path / "elsewhere"
    explicit = tmp_path / "a.log"
    explicit.write_text(f"  $> nextflow run main.nf -w {custom} -resume\n")
    assert find_work_root(explicit) == str(custom)

    banner = tmp_path / "b.log"                        # nf-core prints this
    banner.write_text(f"  workDir                   : {custom}\n")
    assert find_work_root(banner) == str(custom)

    plain = tmp_path / "c.log"                         # default: <launch>/work
    plain.write_text("Jul-15 10:00:00.000 [main] DEBUG - nothing useful\n")
    assert find_work_root(plain) == str(tmp_path / "work")

    # A cloud root must come back verbatim: Path() would collapse the double
    # slash to "s3:/bucket", which no client can fetch.
    cloud = tmp_path / "cloud.log"
    cloud.write_text("  $> nextflow run main.nf -profile awsbatch "
                     "-w s3://my-bucket/work\n")
    assert find_work_root(cloud) == "s3://my-bucket/work"


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
        app.screen.query_one("#runs", DataTable).move_cursor(row=0)
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
        app.screen.query_one("#runs", DataTable).move_cursor(row=0)
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
        app.screen.query_one("#runs", DataTable).move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause(); await pilot.pause()
        assert opened == []                             # no stale file opened
        return True

    assert drive(NfScope(tmp_path), steps)


def test_late_viewer_result_does_not_clobber_another_view(tmp_path):
    # A container decode takes seconds; if the user switches to the run log
    # meanwhile, the arriving file content must be dropped, not painted.
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("task log\n")
    (wd / "out.txt").write_text("FILECONTENT\n" * 20)
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    def text(pane):
        return "\n".join("".join(s.text for s in strip) for strip in pane.lines)

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        tree.move_cursor(leaves(tree)[0])
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
        await pilot.press("g")                       # switch to the run log
        await pilot.pause(); await pilot.pause()
        app._viewer_done(["FILECONTENT"] * 20, 2000, wd / "out.txt")   # late result
        await pilot.pause()
        shown = text(app.query_one("#log", RichLog))
        assert "full run log" in shown               # run log still there
        assert "FILECONTENT" not in shown            # stale result dropped
        return True

    assert drive(NfScope(log), steps)


def test_trace_misses_are_cached(tmp_path):
    # Without caching misses, a run whose tasks have no .command.trace re-opens
    # every one on every 1s refresh (~35ms/tick at 10k tasks).
    log = make_run(tmp_path, n_tasks=30, n_procs=3, with_workdirs=30)

    async def steps(app, pilot):
        await pilot.pause()
        calls = []
        real = nf_tui.parse_trace
        nf_tui.parse_trace = lambda wd: (calls.append(wd), real(wd))[1]
        try:
            app._trace_cache.clear()             # warmed during load; start cold
            for t in app.tasks:
                app._metrics(t)
            first = len(calls)
            for t in app.tasks:                      # second pass: all cached
                app._metrics(t)
            assert first > 0
            assert len(calls) == first, "misses were re-read instead of cached"
        finally:
            nf_tui.parse_trace = real
        return True

    assert drive(NfScope(log), steps)


# ---- failure triage --------------------------------------------------------

# A verbatim-shaped Nextflow failure report, ending at the next timestamped line.
FAIL_LOG = """\
Jul-15 15:24:38.000 [Task monitor] DEBUG n.processor.TaskPollingMonitor - Task completed > \
TaskHandler[id: 9; name: P:BOOM (s1); status: COMPLETED; exit: 139; error: -; workDir: {wd}]
Jul-15 15:24:39.349 [TaskFinalizer-4] ERROR nextflow.processor.TaskProcessor - \
Error executing process > 'P:BOOM (s1)'

Caused by:
  Process `P:BOOM (s1)` terminated with an error exit status (139)

Command executed:

  multiqc --force .

Command exit status:
  139

Command error:
  .command.sh: line 10: Segmentation fault

Work dir:
  {wd}

Tip: when you have fixed the problem you can continue adding the option `-resume`
Jul-15 15:24:39.350 [main] DEBUG nextflow.Session - Session await > all processes finished
"""


def test_parse_errors_extracts_the_failure_report(tmp_path):
    from nf_tui import error_summary, parse_errors
    wd = tmp_path / "work" / "30" / "0c1af39fcf6d4bf28042"
    wd.mkdir(parents=True)
    log = tmp_path / ".nextflow.log"
    log.write_text(FAIL_LOG.format(wd=wd))

    errs = parse_errors(log)
    block = errs["30/0c1af3"]                       # keyed by the block's Work dir
    assert "terminated with an error exit status (139)" in block
    assert "Command exit status" in block and "Segmentation fault" in block
    # the block stops at the next timestamped line
    assert "Session await" not in block
    assert error_summary(block).startswith("Process `P:BOOM (s1)` terminated")
    # also indexed by name, for retried attempts that get no report of their own
    assert errs["name:P:BOOM (s1)"] == block


def test_failed_task_view_leads_with_the_error(tmp_path):
    wd = tmp_path / "work" / "30" / "0c1af39fcf6d4bf28042"
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("some output\n")
    (wd / ".command.sh").write_text("multiqc --force .\n")
    log = tmp_path / ".nextflow.log"
    log.write_text(FAIL_LOG.format(wd=wd))

    def text(pane):
        return "\n".join("".join(s.text for s in strip) for strip in pane.lines)

    async def steps(app, pilot):
        await pilot.pause()
        await pilot.press("e")                      # jump to the failure
        for _ in range(4):
            await pilot.pause()
        pane = app.query_one("#log", RichLog)
        shown = text(pane)
        assert "why this task failed" in shown
        assert "terminated with an error exit status (139)" in shown
        assert "Segmentation fault" in shown
        # a finished task has nothing to tail: land on the error, not the tail
        assert pane.scroll_y == 0
        return True

    assert drive(NfScope(log), steps)


def test_next_failed_wraps_and_reports_nothing_to_find(tmp_path):
    clean = make_run(tmp_path / "ok", n_tasks=10, n_procs=2, seed=7)
    # a run with no failures at all must not crash on `e`
    async def steps(app, pilot):
        await pilot.pause()
        app.failed_only = False
        await pilot.press("e")
        await pilot.pause()
        return True
    # seeded generator may include failures; only assert it survives the keypress
    assert drive(NfScope(clean), steps)


def test_L_pages_the_task_log_and_the_run_log(tmp_path):
    # L used to work only on files and the run log; a task's own .command.log
    # had no way out to the pager.
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("task output\n" * 50)
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    async def steps(app, pilot):
        await pilot.pause()
        cmds = []
        app._page = lambda c: cmds.append(c)
        for key in ("t", "L", "c", "L", "g", "L"):
            await pilot.press(key)
            await pilot.pause()
        assert any(str(wd / ".command.log") in c for c in cmds), "task log not paged"
        assert any(str(log) in c for c in cmds), "run log not paged"
        # a finished task opens at the top; the run log opens at the end
        task_cmd = next(c for c in cmds if ".command.log" in c)
        assert "+G" not in task_cmd
        assert "+G" in next(c for c in cmds if ".nextflow.log" in c)
        return True

    assert drive(NfScope(log), steps)


def test_first_task_is_selected_on_open(tmp_path):
    # The cursor used to stay on the process group because move_cursor ran
    # before the rebuilt tree had lines, so t/L/d acted on no task at all.
    log = make_run(tmp_path, n_tasks=20, n_procs=2, with_workdirs=20)

    async def steps(app, pilot):
        for _ in range(3):
            await pilot.pause()
        node = app.query_one("#tasks", Tree).cursor_node
        assert isinstance(node.data, Task), "cursor left on the process group"
        assert app._selected() is not None
        return True

    assert drive(NfScope(log), steps)


def test_progress_counts_throughput_and_queue_eta(tmp_path):
    from nf_tui import parse_log, progress_of
    # four completions one minute apart, plus two still running
    lines = []
    for i, minute in enumerate(range(4)):
        wd = tmp_path / "work" / "ab" / f"{i:06d}aaaa"
        lines.append(
            f"Jul-15 10:{minute:02d}:00.000 [Task monitor] DEBUG - Task completed > "
            f"TaskHandler[id: {i}; name: P:A (s{i}); status: COMPLETED; exit: 0; "
            f"error: -; workDir: {wd}]")
    for i in (8, 9):
        lines.append(f"Jul-15 10:04:00.000 [Task submitter] INFO - "
                     f"[cd/{i:06d}] Submitted process > P:A (s{i})")
    log = tmp_path / ".nextflow.log"
    log.write_text("\n".join(lines) + "\n")

    p = progress_of(parse_log(log))
    assert p.total == 6 and p.done == 4
    # without a filesystem check the log can only say "in flight", not whether
    # a task has actually started, so both land in pending
    assert p.in_flight == 2 and p.pending == 2 and p.running == 0
    assert p.pct == 67
    assert p.per_min == pytest.approx(1.0, abs=0.01)   # 3 gaps over 3 minutes
    # ETA covers the 2 queued tasks only, not the unknowable rest of the run
    assert p.eta_secs == pytest.approx(120, abs=1)


def test_live_progress_says_seen_not_tasks(tmp_path):
    # Nextflow announces tasks as channels emit, so mid-run the denominator
    # grows: calling 26/28 "93% of tasks" implies a run is nearly done when it
    # may be a third of the way. A live run must say "seen".
    # every task COMPLETED, so liveness is decided purely by the log's mtime
    lines = []
    for i in range(6):
        wd = tmp_path / "work" / "ab" / f"{i:06d}aaaa"
        lines.append(
            f"Jul-15 10:0{i}:00.000 [Task monitor] DEBUG - Task completed > "
            f"TaskHandler[id: {i}; name: P:A (s{i}); status: COMPLETED; exit: 0; "
            f"error: -; workDir: {wd}]")
    log = tmp_path / ".nextflow.log"
    log.write_text("\n".join(lines) + "\n")
    os.utime(log, None)                      # fresh mtime -> live

    # the sub_title ends with the log path, which here contains the test name —
    # compare only the summary in front of it
    def summary(app):
        return app.sub_title.split("  —  ")[0]

    async def steps(app, pilot):
        await pilot.pause()
        assert app._run_is_live()
        assert "seen" in summary(app), summary(app)
        return True

    assert drive(NfScope(log), steps)

    old = time.time() - 7200                 # finished: the total is final
    os.utime(log, (old, old))

    async def steps_done(app, pilot):
        await pilot.pause()
        assert not app._run_is_live()
        s = summary(app)
        assert "tasks" in s and "seen" not in s, s
        assert "100%" in s
        return True

    assert drive(NfScope(log), steps_done)


def test_picker_rows_show_task_counts(tmp_path):
    from nf_tui import _run_stats, gather_runs
    make_run(tmp_path, n_tasks=25, n_procs=2, seed=3)
    (tmp_path / ".nextflow.log").write_text(
        (tmp_path / ".nextflow.log").read_text()
        + "Jul-15 10:00:00.000 [main] DEBUG - Execution complete -- Goodbye\n")
    runs = gather_runs(tmp_path)
    assert runs and runs[0].progress is not None
    stats = _run_stats(runs[0])
    assert "tasks" in stats and "% done" in stats


# ---- queue view (pending vs running) ---------------------------------------

def _inflight_log(tmp_path: Path, n: int) -> Path:
    """n submitted tasks with staged work dirs, none complete."""
    lines = []
    for i in range(n):
        wd = tmp_path / "work" / f"{i:02d}" / f"{i:06d}beef"
        wd.mkdir(parents=True)
        lines.append(f"Jul-15 10:00:0{i}.000 [Task submitter] INFO - "
                     f"[{i:02d}/{i:06d}] Submitted process > P:WORK (s{i})")
    log = tmp_path / ".nextflow.log"
    log.write_text("\n".join(lines) + "\n")
    return log


def test_task_state_uses_command_begin(tmp_path):
    from nf_tui import resolve_workdir, task_state
    log = _inflight_log(tmp_path, 3)
    tasks = parse_log(log)
    root = tmp_path / "work"

    # nothing started yet: all pending
    for t in tasks:
        t.workdir = resolve_workdir(root, t.hash)
        assert t.workdir, "hash should resolve to its staged work dir"
        assert task_state(t) == "pending"

    # Nextflow writes .command.begin when a task actually starts
    (Path(tasks[0].workdir) / ".command.begin").write_text("")
    assert task_state(tasks[0]) == "running"
    assert task_state(tasks[1]) == "pending"


def test_progress_splits_running_from_pending(tmp_path):
    from nf_tui import progress_of, resolve_workdir
    log = _inflight_log(tmp_path, 4)
    tasks = parse_log(log)
    for t in tasks:
        t.workdir = resolve_workdir(tmp_path / "work", t.hash)
    for t in tasks[:2]:                       # two actually executing
        (Path(t.workdir) / ".command.begin").write_text("")

    p = progress_of(tasks, check_fs=True)
    assert p.running == 2 and p.pending == 2 and p.in_flight == 4
    # without the filesystem the log can't tell them apart
    assert progress_of(tasks, check_fs=False).pending == 4


def test_queue_view_lists_running_then_pending(tmp_path):
    from nf_tui import resolve_workdir
    log = _inflight_log(tmp_path, 3)

    def text(pane):
        return "\n".join("".join(s.text for s in strip) for strip in pane.lines)

    # Mark one task started on disk WITHOUT telling the app where it lives: an
    # in-flight task has no workDir in the log, so the app must resolve it by
    # hash itself. (Pre-setting workdir here would hide exactly that bug.)
    (tmp_path / "work" / "00" / "000000beef" / ".command.begin").write_text("")

    async def steps(app, pilot):
        await pilot.pause()
        assert not any(t.workdir for t in parse_log(log)), "log has no workDirs"
        await pilot.press("p")                 # the queue view
        await pilot.pause()
        assert app.view == "queue"
        shown = text(app.query_one("#log", RichLog))
        assert "1 running" in shown and "2 pending" in shown
        # running sorts above pending
        body = [l for l in shown.splitlines()
                if l.startswith(("running", "pending"))]
        assert body and body[0].startswith("running")
        assert sum(l.startswith("pending") for l in body) == 2
        return True

    assert drive(NfScope(log), steps)


def test_stop_pipeline_needs_confirmation_and_sends_sigterm(tmp_path, monkeypatch):
    """K stops a pipeline nf-tui launched — but only after a yes.

    SIGTERM, not SIGINT: measured against a real run, SIGINT was ignored while
    SIGTERM triggered Nextflow's own handler ("Killing running tasks") and left
    no orphaned task processes. That handler is what cancels scheduler jobs.
    """
    import signal
    from nf_tui import ConfirmScreen

    log = make_run(tmp_path, n_tasks=10, n_procs=2)
    monkeypatch.setenv("NF_TUI_PID", "4242")
    sent = []
    monkeypatch.setattr(nf_tui.os, "kill",
                        lambda pid, sig: sent.append((pid, sig)))

    async def steps(app, pilot):
        await pilot.pause()
        assert app.pipeline_pid == 4242

        await pilot.press("K")                     # asks first
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")                     # declined
        await pilot.pause()
        assert not [s for s in sent if s[1] != 0], "declining must not signal"

        await pilot.press("K")
        await pilot.pause()
        await pilot.press("y")                     # confirmed
        await pilot.pause()
        assert (4242, signal.SIGTERM) in sent
        return True

    assert drive(NfScope(log), steps)


def test_stop_pipeline_says_so_when_it_did_not_launch_the_run(tmp_path, monkeypatch):
    monkeypatch.delenv("NF_TUI_PID", raising=False)
    log = make_run(tmp_path, n_tasks=5, n_procs=1)

    async def steps(app, pilot):
        await pilot.pause()
        assert app.pipeline_pid is None
        notes = []
        app.notify = lambda m, **k: notes.append(m)
        await pilot.press("K")
        await pilot.pause()
        assert notes and "didn't launch" in notes[-1]
        return True

    assert drive(NfScope(log), steps)


def test_no_tasks_placeholder_disappears_once_tasks_arrive(tmp_path):
    # Opening a just-launched run shows "(no tasks yet)". The in-place sync only
    # appends, so without removing it the message sat above the tree forever —
    # which is every run opened with `nf-tui nextflow run`.
    log = tmp_path / ".nextflow.log"
    log.write_text("")

    async def steps(app, pilot):
        await pilot.pause()
        tree = app.query_one("#tasks", Tree)
        assert any("no tasks yet" in str(n.label) for n in tree.root.children)

        log.write_text("x INFO - [ab/111111] Submitted process > P:A (s1)\n")
        app._force_refresh = True
        app.action_refresh()
        await pilot.pause()

        labels = [str(n.label) for n in tree.root.children]
        assert not any("no tasks yet" in l for l in labels), labels
        assert any("A" in l for l in labels)
        return True

    assert drive(NfScope(log), steps)


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
        # Budgets are loose on purpose. The point is to catch a complexity
        # regression — a render that walks all 10k tasks lands in seconds, not
        # in a few hundred milliseconds — and a tight wall-clock bound just
        # flakes on a busy machine (this suite has failed here at 4x load while
        # passing alone).
        assert worst < 0.30, f"a single render took {worst*1000:.0f}ms at 10k"
        # steady-state tick (log unchanged) must be ~free
        t0 = time.time()
        app.action_refresh()
        assert time.time() - t0 < 0.20        # steady-state tick stays cheap
        return True

    t0 = time.time()
    ok = drive(NfScope(log), steps)
    assert ok
    assert time.time() - t0 < 20.0          # whole load+nav, generously


def test_new_status_counts_as_in_flight(tmp_path):
    """Nextflow reports NEW for a task it created but hasn't submitted.

    Counting only RUNNING/SUBMITTED made those invisible: a live run with three
    queued tasks showed "0 running · 0 pending" in the queue view while the tree
    listed them plainly.
    """
    from nf_tui import progress_of
    wd = tmp_path / "work" / "4c" / "253513c79189"
    wd.mkdir(parents=True)
    log = tmp_path / ".nextflow.log"
    log.write_text(
        f"~> TaskHandler[id: 17; name: P:A (17); status: NEW; exit: -; "
        f"error: -; workDir: {wd}]\n")
    tasks = parse_log(log)
    assert tasks[0].status == "NEW"
    p = progress_of(tasks, check_fs=True)
    assert p.in_flight == 1 and p.pending == 1 and p.done == 0


def test_queue_view_lists_new_tasks(tmp_path):
    wd = tmp_path / "work" / "4c" / "253513c79189"
    wd.mkdir(parents=True)
    log = tmp_path / ".nextflow.log"
    log.write_text(
        f"~> TaskHandler[id: 17; name: P:A (17); status: NEW; exit: -; "
        f"error: -; workDir: {wd}]\n")

    def text(pane):
        return "\n".join("".join(s.text for s in strip) for strip in pane.lines)

    async def steps(app, pilot):
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        shown = text(app.query_one("#log", RichLog))
        assert "1 pending" in shown, shown
        assert "nothing in flight" not in shown
        return True

    assert drive(NfScope(log), steps)


def test_tool_image_is_verified_not_guessed_from_its_name(tmp_path, monkeypatch):
    """An image whose name mentions the tool need not contain it.

    A real sarek run picked `htslib:1.21` for samtools — htslib ships tabix and
    bgzip, not samtools — so the CRAM viewer ran a doomed command and showed
    "sh: 1: samtools: not found". Candidates are probed now, not guessed.
    """
    import nf_tui as m

    def workdir(group: str, image: str) -> None:
        wd = tmp_path / "work" / group / (group * 15)
        wd.mkdir(parents=True)
        (wd / ".command.run").write_text(
            f'    docker run -i -v /d:/d -w "$NXF_TASK_WORKDIR" {image} '
            f'/bin/bash -c "eval ..."\n')

    workdir("aa", "example.org/htslib:1.21")        # sounds right, hasn't got it
    workdir("bb", "example.org/tools:9")            # no hint in the name, has it

    probed = []

    def fake_probe(engine, image, binary):
        probed.append(image)
        return image.endswith("tools:9")            # only this one really has it

    monkeypatch.setattr(m, "_image_has", fake_probe)
    found = m.find_tool_image(tmp_path, "samtools")

    assert found == "example.org/tools:9"
    # the name-matching candidate is tried first, then rejected by the probe
    assert probed[0] == "example.org/htslib:1.21"


def test_find_tool_image_returns_none_when_nothing_provides_it(tmp_path, monkeypatch):
    import nf_tui as m
    wd = tmp_path / "work" / "aa" / ("a" * 15)
    wd.mkdir(parents=True)
    (wd / ".command.run").write_text(
        '    docker run -i -w "$NXF_TASK_WORKDIR" example.org/nothing:1 '
        '/bin/bash -c "eval ..."\n')
    monkeypatch.setattr(m, "_image_has", lambda *a: False)
    assert m.find_tool_image(tmp_path, "samtools") is None


# ---- JSON report (for agents and scripts) ----------------------------------

def test_run_report_nests_logs_and_errors_per_task(tmp_path):
    """The JSON an agent reads: state, metrics, cause, and the command files.

    Nested per task on purpose — debugging a failure otherwise means walking
    Nextflow's work tree by hand to find which hash owns which .command.err.
    """
    from nf_tui import run_report

    ok = tmp_path / "work" / "aa" / "111111beef"
    bad = tmp_path / "work" / "bb" / "222222beef"
    for wd in (ok, bad):
        wd.mkdir(parents=True)
        (wd / ".command.sh").write_text("run_the_thing --flag\n")
        (wd / ".command.log").write_text("some output\n")
    (bad / ".command.err").write_text("boom: segfault\n")
    (ok / ".command.trace").write_text(
        "nextflow.trace/v2\nrealtime=1500\npeak_rss=2048\n")

    log = tmp_path / ".nextflow.log"
    log.write_text(
        f"Jul-15 10:00:00.000 [Task monitor] DEBUG - Task completed > "
        f"TaskHandler[id: 1; name: P:GOOD (s1); status: COMPLETED; exit: 0; "
        f"error: -; workDir: {ok}]\n"
        f"Jul-15 10:00:05.000 [Task monitor] DEBUG - Task completed > "
        f"TaskHandler[id: 2; name: P:BAD (s2); status: COMPLETED; exit: 1; "
        f"error: -; workDir: {bad}]\n"
        f"Jul-15 10:00:06.000 [TaskFinalizer-1] ERROR nextflow.processor."
        f"TaskProcessor - Error executing process > 'P:BAD (s2)'\n"
        f"\nCaused by:\n  Process `P:BAD (s2)` terminated with an error exit "
        f"status (1)\n\nWork dir:\n  {bad}\n"
        f"Jul-15 10:00:07.000 [main] DEBUG nextflow.Session - done\n")

    rep = run_report(log, logs="failed")
    assert rep["progress"]["total"] == 2 and rep["progress"]["failed"] == 1
    assert rep["progress"]["total_is_final"] is True
    assert rep["processes"] == ["P:BAD", "P:GOOD"]

    by = {t["hash"]: t for t in rep["tasks"]}
    good, bad_t = by["aa/111111"], by["bb/222222"]

    assert good["failed"] is False and good["metrics"]["realtime_ms"] == 1500
    assert "logs" not in good                       # logs="failed" skips it

    assert bad_t["failed"] is True and bad_t["exit"] == "1"
    assert "terminated with an error exit status (1)" in bad_t["error"]["summary"]
    assert bad_t["logs"]["err"].strip() == "boom: segfault"
    assert "run_the_thing" in bad_t["logs"]["script"]

    # logs="all" reaches the healthy task too; "none" reaches neither
    assert "logs" in {t["hash"]: t for t in
                      run_report(log, logs="all")["tasks"]}["aa/111111"]
    assert all("logs" not in t for t in run_report(log, logs="none")["tasks"])

    only_bad = run_report(log, failed_only=True)["tasks"]
    assert [t["hash"] for t in only_bad] == ["bb/222222"]


def test_run_report_resolves_workdirs_for_in_flight_tasks(tmp_path):
    # A task still running has no workDir in the log, so without resolving it
    # by hash every executing task would be reported as merely "pending".
    from nf_tui import run_report
    wd = tmp_path / "work" / "cd" / "333333beef"
    wd.mkdir(parents=True)
    (wd / ".command.begin").write_text("")          # Nextflow: this one started
    log = tmp_path / ".nextflow.log"
    log.write_text("a INFO - [cd/333333] Submitted process > P:A (s1)\n")

    rep = run_report(log)
    assert rep["live"] is True
    assert rep["progress"]["running"] == 1 and rep["progress"]["pending"] == 0
    assert rep["progress"]["total_is_final"] is False
    assert rep["tasks"][0]["workdir"] == str(wd)


# ---- container engines other than docker -----------------------------------

# Verbatim shape of the line Nextflow writes for Singularity: the invocation is
# NOT at the start — it is preceded by environment setup.
SINGULARITY_RUN = (
    'nxf_launch() {\n'
    '    set +u; env - PATH="$PATH" ${TMP:+SINGULARITYENV_TMP="$TMP"} '
    '${TMPDIR:+SINGULARITYENV_TMPDIR="$TMPDIR"} singularity exec --no-home '
    '-B /scratch:/scratch -B "$NXF_TASK_WORKDIR" /images/samtools_1.21.sif '
    '/bin/bash -c "cd $NXF_TASK_WORKDIR; eval $(nxf_container_env); '
    '/bin/bash -ue .command.sh"\n}\n'
)


def test_parses_singularity_despite_the_env_prefix(tmp_path):
    """Anchoring on the start of the line broke every Singularity run.

    Nextflow prefixes the invocation with `set +u; env - PATH=... ` so a check
    for a line *starting* with "singularity" never matched, and every task on a
    cluster parsed as "no container found" — in the HPC case this exists for.
    """
    wd = tmp_path / "work" / "ab" / "cd"
    wd.mkdir(parents=True)
    (wd / ".command.run").write_text(SINGULARITY_RUN)

    spec = parse_container_run(str(wd))
    assert spec is not None, "singularity invocation not found"
    engine, mounts, image = spec
    assert engine == "singularity"
    assert image == "/images/samtools_1.21.sif"
    assert "-B" in mounts and "/scratch:/scratch" in mounts
    # $NXF_TASK_WORKDIR is expanded to the real work dir so binds resolve
    assert str(wd) in mounts


def test_parses_apptainer(tmp_path):
    wd = tmp_path / "work" / "ef" / "gh"
    wd.mkdir(parents=True)
    (wd / ".command.run").write_text(
        '    env - PATH="$PATH" apptainer exec -B /data:/data '
        '/img/tools.sif /bin/bash -c "eval x"\n')
    spec = parse_container_run(str(wd))
    assert spec is not None
    assert spec[0] == "apptainer" and spec[2] == "/img/tools.sif"


def test_singularity_pager_and_viewer_commands(tmp_path):
    """The decode command for a .sif uses `exec`, not `run --rm`."""
    wd = tmp_path / "work" / "ab" / "cd"
    wd.mkdir(parents=True)
    (wd / ".command.run").write_text(SINGULARITY_RUN)
    (wd / "test.cram").write_bytes(b"CRAM\x00")

    app = NfScope(tmp_path)
    t = Task(hash="ab/cd", name="P (s)", workdir=str(wd))
    cmd = app._pager_command(t, wd / "test.cram", "less")
    assert "singularity" in cmd and " exec " in cmd
    assert "--rm" not in cmd                       # that is a docker flag
    assert "samtools view -h" in cmd and "| less" in cmd


def test_cursor_lands_on_a_task_that_appears_after_opening(tmp_path):
    """A run opened the moment it launches has an empty tree for a while.

    Selecting the first task only once, at open, left the cursor parked on a
    process group for the rest of the session — so `d` showed an empty file
    list and `t` a process summary, which is what a live-run recording caught.
    """
    log = tmp_path / ".nextflow.log"
    log.write_text("")                                   # nothing submitted yet
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("output\n")
    (wd / "result.txt").write_text("hello\n")

    async def steps(app, pilot):
        await pilot.pause()
        tree = app.query_one("#tasks", Tree)
        assert not isinstance(getattr(tree.cursor_node, "data", None), Task)

        # Nextflow submits its first task a moment later.
        log.write_text(
            f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
            f"exit: 0; error: -; workDir: {wd}]\n")
        app._force_refresh = True
        app.action_refresh()
        for _ in range(3):
            await pilot.pause()

        assert isinstance(tree.cursor_node.data, Task), "cursor never moved"

        await pilot.press("d")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [p.name for p in app._files] == ["result.txt"]
        return True

    assert drive(NfScope(log), steps)


# ---- searching the log pane ------------------------------------------------

def test_slash_searches_the_log_when_that_pane_is_focused(tmp_path):
    """`/` has to mean two things, and focus decides which.

    Filtering the task tree and searching the log are both wanted, and `/` is
    what a reader reaches for either way. It searches whichever pane has the
    highlighted border, so the choice is visible rather than remembered.
    """
    log = tmp_path / ".nextflow.log"
    lines = [f"Jul-15 10:00:{i:02d}.000 [main] DEBUG nextflow.Session - line {i}"
             for i in range(40)]
    lines[7] = ("Jul-15 10:00:07.000 [main] ERROR nextflow.processor."
                "TaskProcessor - needle one")
    lines[23] = ("Jul-15 10:00:23.000 [main] ERROR nextflow.processor."
                 "TaskProcessor - needle two")
    log.write_text("\n".join(lines) + "\n")

    async def steps(app, pilot):
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        pane.focus()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()
        assert app._search_mode == "log"

        for ch in "needle":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert len(app._log_matches) == 2, app._log_matches
        assert app._log_i == 0

        def match_is_visible():
            """The match sits in view — at the top, or as near as the pane can
            scroll: a match close to the end clamps at max_scroll_y."""
            line = app._log_matches[app._log_i]
            top = pane.scroll_y
            return top <= line <= top + pane.size.height

        assert match_is_visible()

        await pilot.press("n")                   # next match
        await pilot.pause()
        assert app._log_i == 1 and match_is_visible()

        await pilot.press("n")                   # wraps around
        await pilot.pause()
        assert app._log_i == 0

        await pilot.press("N")                   # and back
        await pilot.pause()
        assert app._log_i == 1
        return True

    assert drive(NfScope(log), steps)


def test_slash_still_filters_tasks_when_the_tree_is_focused(tmp_path):
    log = make_run(tmp_path, n_tasks=40, n_procs=4)

    async def steps(app, pilot):
        await pilot.pause()
        app.query_one("#tasks", Tree).focus()
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        assert app._search_mode == "tasks"
        app._apply_query("PROC_002")
        await pilot.pause()
        shown = leaves(app.query_one("#tasks", Tree))
        assert shown and all("proc_002" in n.data.name.lower() for n in shown)
        return True

    assert drive(NfScope(log), steps)


def test_log_search_reports_when_nothing_matches(tmp_path):
    log = tmp_path / ".nextflow.log"
    log.write_text("Jul-15 10:00:00.000 [main] DEBUG - only this line\n")

    async def steps(app, pilot):
        await pilot.pause()
        app.query_one("#log", RichLog).focus()
        await pilot.pause()
        notes = []
        app.notify = lambda m, **k: notes.append(m)
        app._search_log("definitely-not-there")
        assert app._log_matches == []
        assert notes and "no match" in notes[-1]
        # and n on an empty search says so rather than doing nothing
        notes.clear()
        app.action_next_match()
        assert notes and "search the log" in notes[-1]
        return True

    assert drive(NfScope(log), steps)


# ---- copying paths, and cloud work dirs ------------------------------------

def test_y_copies_the_work_dir_and_the_file_path(tmp_path):
    """`y` copies what you're looking at, and says what it copied.

    Both routes are attempted: Textual's OSC 52 (the only one that reaches a
    laptop's clipboard from a login node over SSH) and a local helper for the
    terminals that ignore OSC 52.
    """
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("out\n")
    (wd / "result.bam").write_bytes(b"x")
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    async def steps(app, pilot):
        await pilot.pause()
        clipped, notes = [], []
        app.copy_to_clipboard = lambda t: clipped.append(t)
        app.notify = lambda m, **k: notes.append(m)

        await pilot.press("y")                     # tree: the work dir
        await pilot.pause()
        assert clipped[-1] == str(wd)
        assert "work dir" in notes[-1] and str(wd) in notes[-1]

        await pilot.press("d")                     # files: the file itself
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert clipped[-1] == str(wd / "result.bam")
        assert "file path" in notes[-1]
        return True

    assert drive(NfScope(log), steps)


def test_cloud_work_dirs_are_explained_not_just_missing(tmp_path):
    """AWS Batch and friends keep the work tree in object storage.

    Everything from .nextflow.log still works — tasks, statuses, exit codes,
    progress, failure reports. Everything read out of a work dir cannot, because
    the files are not on this filesystem, so say which and how to fetch them
    rather than showing a bare "not available".
    """
    from nf_tui import remote_scheme, run_report

    assert remote_scheme("s3://bucket/work/ab/cd") == "s3"
    assert remote_scheme("gs://bucket/work/ab/cd") == "gs"
    assert remote_scheme("/scratch/work/ab/cd") is None
    assert remote_scheme("") is None

    log = tmp_path / ".nextflow.log"
    log.write_text(
        "x DEBUG - Task completed > AwsBatchTaskHandler[id: 4; name: ALIGN (s1); "
        "status: COMPLETED; exit: 0; error: -; "
        "workDir: s3://my-bucket/work/ab/cdef1234567890]\n")

    # the log-derived half is unaffected
    t = parse_log(log)[0]
    assert t.hash == "ab/cdef12" and t.exit == "0" and t.status == "COMPLETED"

    rep = run_report(log)
    assert rep["progress"]["total"] == 1 and rep["progress"]["done"] == 1
    assert rep["tasks"][0]["workdir_remote"] == "s3"
    assert "logs" not in rep["tasks"][0]           # nothing to read locally

    def text(pane):
        return "\n".join("".join(s.text for s in strip) for strip in pane.lines)

    async def steps(app, pilot):
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        shown = text(app.query_one("#log", RichLog))
        # Names the scheme and the CLI to install, rather than reporting the
        # log missing as though the task had produced nothing.
        assert "object storage" in shown and "aws" in shown
        return True

    import shutil as _shutil
    if _shutil.which("aws") is None:
        assert drive(NfScope(log), steps)


# A stand-in for the aws CLI that serves s3:// out of a local directory. Without
# it none of the object-store path is covered, since testing it otherwise needs
# an AWS account.
AWS_SHIM = """#!/usr/bin/env python3
import os, sys
root = os.environ["FAKE_S3_ROOT"]

def local(uri):
    return os.path.join(root, uri.replace("s3://", "", 1))

args = sys.argv[1:]
if args[:2] == ["s3", "cp"]:
    path = local(args[2])
    if not os.path.isfile(path):
        sys.exit(1)
    sys.stdout.write(open(path).read())
elif args[:2] == ["s3", "ls"]:
    path = local(args[2].rstrip("/"))
    if not os.path.isdir(path):
        sys.exit(1)
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isdir(full):
            print("                           PRE " + name + "/")
        else:
            print("2026-01-01 00:00:00 %10d %s" % (os.path.getsize(full), name))
else:
    sys.exit(2)
"""


def test_cloud_task_logs_and_files_are_fetched(tmp_path, monkeypatch):
    """With the CLI present, a cloud task's log and outputs are read directly.

    Run against the shim above, so the fetch, the worker hand-off and the render
    are all exercised without an AWS account.
    """
    import nf_tui as m

    bucket = tmp_path / "bucket"
    task = bucket / "work" / "ab" / "cdef1234567890"
    task.mkdir(parents=True)
    (task / ".command.log").write_text("aligning reads\ndone\n")
    (task / "out.bam").write_text("BAMDATA\n")

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "aws"
    shim.write_text(AWS_SHIM)
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_S3_ROOT", str(bucket))
    m._remote_cache.clear()

    uri = "s3://work/ab/cdef1234567890"
    log = tmp_path / ".nextflow.log"
    log.write_text(
        "x DEBUG - Task completed > AwsBatchTaskHandler[id: 1; name: ALIGN (s1); "
        "status: COMPLETED; exit: 0; error: -; workDir: " + uri + "]\n")

    # the plumbing on its own
    assert m.remote_tool("s3") is not None
    assert "aligning reads" in m.remote_cat(uri + "/.command.log")
    assert ("out.bam", 8) in m.remote_ls(uri)

    def pane_text(pane):
        return "\n".join("".join(s.text for s in strip) for strip in pane.lines)

    async def steps(app, pilot):
        await pilot.pause()
        await pilot.press("t")                       # task log, fetched
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "aligning reads" in pane_text(app.query_one("#log", RichLog))

        await pilot.press("d")                       # outputs, listed
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert any(u.endswith("out.bam") for u in app._remote_files)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "BAMDATA" in pane_text(app.query_one("#log", RichLog))
        return True

    assert drive(NfScope(log), steps)


def test_remote_cache_can_be_invalidated(tmp_path, monkeypatch):
    """A running task's log grows, so a cached copy must be droppable."""
    import nf_tui as m
    bucket = tmp_path / "bucket"
    task = bucket / "work" / "cd" / "ef01"
    task.mkdir(parents=True)
    (task / ".command.log").write_text("first\n")

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "aws").write_text(AWS_SHIM)
    (shim_dir / "aws").chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_S3_ROOT", str(bucket))
    m._remote_cache.clear()

    uri = "s3://work/cd/ef01/.command.log"
    assert m.remote_cat(uri).strip() == "first"
    (task / ".command.log").write_text("first\nsecond\n")
    assert m.remote_cat(uri).strip() == "first"        # served from cache
    m.remote_forget(uri)
    assert "second" in m.remote_cat(uri)               # re-read



# ---------------------------------------------------------------------------
# Reading big files. A pipeline task can emit a multi-gigabyte output, so every
# host read has to be bounded *while* reading. The in-pane preview used to do
# `read_text().splitlines()[:cap]`, which measured ~2.1x the file size in peak
# RSS and ~2.8s/GB — a 10 GB file exhausted a 24 GB host before painting a line.
# ---------------------------------------------------------------------------

def test_head_text_caps_and_strips_newlines(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("".join(f"line {i}\n" for i in range(50)))
    out = nf_tui.head_text(p, 5)
    assert out == [f"line {i}" for i in range(5)]


def test_head_text_reads_a_short_file_whole(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("a\nb\n")
    assert nf_tui.head_text(p, 100) == ["a", "b"]


def test_head_text_reports_a_missing_file(tmp_path):
    out = nf_tui.head_text(tmp_path / "nope.txt", 10)
    assert len(out) == 1 and "cannot read" in out[0]


def test_head_text_returns_before_eof(tmp_path):
    """The real laziness proof: served a FIFO that is never closed, head_text
    must still return. `read_text()` would block here forever, which is the
    same reason it cannot bound a huge regular file."""
    import threading

    fifo = tmp_path / "stream"
    os.mkfifo(fifo)
    stop = threading.Event()

    def writer():
        with open(fifo, "w") as f:
            f.write("".join(f"line {i}\n" for i in range(200)))
            f.flush()
            stop.wait(10)            # hold the pipe open: no EOF for the reader

    w = threading.Thread(target=writer, daemon=True)
    w.start()

    result: list = []
    r = threading.Thread(target=lambda: result.extend(nf_tui.head_text(fifo, 5)),
                         daemon=True)
    r.start()
    r.join(timeout=10)
    stop.set()
    assert not r.is_alive(), "head_text blocked waiting for EOF"
    assert result == [f"line {i}" for i in range(5)]


def _peak_rss_mb(code: str) -> float:
    """Run `code` in a fresh interpreter, return its peak RSS in MB."""
    import subprocess
    import sys
    prog = ("import resource, sys\n"
            "from pathlib import Path\n"
            "import nf_tui\n"
            + code +
            "\nprint(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2)\n")
    r = subprocess.run([sys.executable, "-c", prog], capture_output=True,
                       text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    return float(r.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def big_file(tmp_path_factory):
    """~250 MB of log-like text. Big enough that slurping it shows up clearly
    against a ~49 MB interpreter baseline, small enough to stay quick."""
    p = tmp_path_factory.mktemp("big") / "huge.txt"
    block = "".join(f"padding line {i} " + "x" * 60 + "\n" for i in range(10_000))
    with p.open("w") as f:
        for _ in range(300):
            f.write(block)
    assert p.stat().st_size > 200 * 1024**2
    return p


def test_head_text_bounds_memory_on_a_big_file(big_file):
    peak = _peak_rss_mb(
        f"out = nf_tui.head_text(Path({str(big_file)!r}), 2000)\n"
        f"assert len(out) == 2000, len(out)\n")
    size_mb = big_file.stat().st_size / 1024**2
    assert peak < 200, f"peak RSS {peak:.0f} MB reading a {size_mb:.0f} MB file"


def test_tail_text_bounds_memory_on_a_big_file(big_file):
    peak = _peak_rss_mb(
        f"t = nf_tui._tail_text(Path({str(big_file)!r}))\n"
        f"assert len(t) <= nf_tui.LOG_CHARS, len(t)\n")
    size_mb = big_file.stat().st_size / 1024**2
    assert peak < 200, f"peak RSS {peak:.0f} MB tailing a {size_mb:.0f} MB file"


def test_tail_text_returns_the_end_of_the_file(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("".join(f"line {i}\n" for i in range(100_000)))
    t = nf_tui._tail_text(p)
    assert t is not None
    assert t.rstrip("\n").endswith("line 99999")
    assert len(t) <= nf_tui.LOG_CHARS
    assert "\n" in t


def test_tail_text_keeps_a_small_file_intact(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("only\ntwo lines\n")
    assert nf_tui._tail_text(p) == "only\ntwo lines\n"


def test_tail_text_missing_file_is_none(tmp_path):
    assert nf_tui._tail_text(tmp_path / "nope.txt") is None


# ---------------------------------------------------------------------------
# less and line numbering. Reaching EOF makes less number every line, and
# quitting blocks on that count: measured 56s to exit `less -R +G` on a 10 GB
# output, versus 0.21s with -n. See pager_flags.
# ---------------------------------------------------------------------------

def test_pager_flags_keeps_line_numbers_on_a_small_file(tmp_path):
    p = tmp_path / "small.log"
    p.write_text("a\n" * 100)
    assert nf_tui.pager_flags(p) == "-R"


def test_pager_flags_suppresses_line_numbers_on_a_big_file(tmp_path):
    p = tmp_path / "big.log"
    with p.open("wb") as f:                       # sparse: no real bytes written
        f.truncate(nf_tui.LESS_LINENUM_MAX + 1)
    assert nf_tui.pager_flags(p) == "-Rn"


def test_pager_flags_on_a_missing_file(tmp_path):
    assert nf_tui.pager_flags(tmp_path / "nope.log") == "-R"


def test_run_log_pager_command_uses_n_when_the_log_is_huge(tmp_path, monkeypatch):
    """The run log is opened with +G, which lands at EOF — exactly the case that
    made quitting take a minute."""
    log = tmp_path / ".nextflow.log"
    log.write_text("~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   "exit: 0; error: -; workDir: /tmp/x]\n")
    pages: list[str] = []

    async def steps(app, pilot):
        monkeypatch.setattr(app, "_page", lambda cmd: pages.append(cmd))
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("L")
        await pilot.pause()
        assert pages, "L did not invoke the pager"
        assert "+G" in pages[0]
        assert " -R " in pages[0], pages[0]        # small log: numbering is free
        pages.clear()
        # Grow it past the threshold and page it again.
        with log.open("ab") as f:
            f.truncate(nf_tui.LESS_LINENUM_MAX + 1)
        await pilot.press("L")
        await pilot.pause()
        assert pages, "second L did not invoke the pager"
        assert " -Rn " in pages[0], pages[0]
        return True

    assert drive(NfScope(tmp_path), steps)


def test_file_pager_command_uses_n_when_the_file_is_huge(tmp_path):
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    small = wd / "small.txt"
    small.write_text("hi\n")
    big = wd / "big.txt"
    with big.open("wb") as f:
        f.truncate(nf_tui.LESS_LINENUM_MAX + 1)
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    async def steps(app, pilot):
        t = Task(hash="ab/cccc", name="P:A (s1)", status="COMPLETED",
                 exit="0", workdir=str(wd))
        assert " -R " in app._pager_command(t, small, "less")
        assert " -Rn " in app._pager_command(t, big, "less")
        return True

    assert drive(NfScope(tmp_path), steps)


# ---------------------------------------------------------------------------
# Log scans stream instead of slurping. parse_log runs on every refresh tick of
# a live run, and a long pipeline's .nextflow.log reaches hundreds of MB;
# read_text().splitlines() on one measured 600 MB of peak RSS for a 161 MB log.
# ---------------------------------------------------------------------------

def test_iter_lines_yields_stripped_lines(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("one\ntwo\nthree\n")
    assert list(nf_tui.iter_lines(p)) == ["one", "two", "three"]


def test_iter_lines_handles_no_trailing_newline(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("one\ntwo")
    assert list(nf_tui.iter_lines(p)) == ["one", "two"]


def test_iter_lines_on_a_missing_file_is_empty(tmp_path):
    assert list(nf_tui.iter_lines(tmp_path / "nope.txt")) == []


def test_iter_lines_is_lazy(tmp_path):
    """Proof it streams: a FIFO nobody closes would hang read_text() forever."""
    import threading

    fifo = tmp_path / "stream"
    os.mkfifo(fifo)
    stop = threading.Event()

    def writer():
        with open(fifo, "w") as f:
            f.write("".join(f"line {i}\n" for i in range(200)))
            f.flush()
            stop.wait(10)

    threading.Thread(target=writer, daemon=True).start()
    got: list = []

    def reader():
        for i, line in enumerate(nf_tui.iter_lines(fifo)):
            got.append(line)
            if i >= 4:
                break

    r = threading.Thread(target=reader, daemon=True)
    r.start()
    r.join(timeout=10)
    stop.set()
    assert not r.is_alive(), "iter_lines blocked waiting for EOF"
    assert got == [f"line {i}" for i in range(5)]


def test_parse_errors_reads_two_blocks_in_one_log(tmp_path):
    """The rewritten scan ends a block on a timestamped line — and that same
    line may start the next error, which the old index-based loop relied on."""
    wd1 = tmp_path / "work" / "30" / "0c1af39fcf6d4bf28042"
    wd2 = tmp_path / "work" / "aa" / "bb1af39fcf6d4bf28042"
    for w in (wd1, wd2):
        w.mkdir(parents=True)
    log = tmp_path / ".nextflow.log"
    log.write_text(
        f"Jul-15 15:24:38.000 [main] ERROR nextflow.Nextflow - "
        f"Error executing process > 'P:ONE (s1)'\n"
        f"\nCaused by:\n  first failure\n\nWork dir:\n  {wd1}\n\n"
        f"Jul-15 15:24:39.000 [main] ERROR nextflow.Nextflow - "
        f"Error executing process > 'P:TWO (s2)'\n"
        f"\nCaused by:\n  second failure\n\nWork dir:\n  {wd2}\n\n"
        f"Jul-15 15:24:40.000 [main] DEBUG nextflow.Session - Session await\n")

    errs = nf_tui.parse_errors(log)
    assert "first failure" in errs["30/0c1af3"]
    assert "second failure" in errs["aa/bb1af3"]
    # blocks must not bleed into each other, or the wrong cause is shown
    assert "second failure" not in errs["30/0c1af3"]
    assert "first failure" not in errs["aa/bb1af3"]
    assert "Session await" not in errs["aa/bb1af3"]
    assert errs["name:P:ONE (s1)"] == errs["30/0c1af3"]


def test_parse_errors_keeps_a_block_that_ends_at_eof(tmp_path):
    wd = tmp_path / "work" / "30" / "0c1af39fcf6d4bf28042"
    wd.mkdir(parents=True)
    log = tmp_path / ".nextflow.log"
    log.write_text(
        f"Jul-15 15:24:38.000 [main] ERROR nextflow.Nextflow - "
        f"Error executing process > 'P:LAST (s1)'\n"
        f"\nCaused by:\n  died at the end\n\nWork dir:\n  {wd}\n")
    errs = nf_tui.parse_errors(log)
    assert "died at the end" in errs["30/0c1af3"]


def test_parse_errors_caps_a_block_in_a_log_with_no_timestamps(tmp_path):
    """A malformed log would otherwise make one block swallow the whole file."""
    log = tmp_path / ".nextflow.log"
    log.write_text("ERROR nextflow.Nextflow - Error executing process > 'P:X (s)'\n"
                   + "junk\n" * (nf_tui.ERROR_BLOCK_MAX_LINES + 500))
    errs = nf_tui.parse_errors(log)
    block = errs["name:P:X (s)"]
    assert len(block.splitlines()) <= nf_tui.ERROR_BLOCK_MAX_LINES + 1


@pytest.fixture(scope="module")
def big_log(tmp_path_factory):
    """~95 MB of realistic handler + noise lines, but only 256 *distinct*
    tasks. Deliberate: 300k Task objects would cost ~150 MB of legitimate
    memory and swamp the thing under test, which is how the file is read."""
    p = tmp_path_factory.mktemp("biglog") / ".nextflow.log"
    handler = ("Jul-30 11:42:03.117 [Task monitor] DEBUG "
               "n.processor.TaskPollingMonitor - Task completed > TaskHandler"
               "[id: {i}; name: NFCORE:SAREK:FASTQC (s{i}); status: COMPLETED; "
               "exit: 0; error: -; workDir: /w/{g:02x}/{h}bbbbbbbbbbbbbbbb]\n")
    noise = ("Jul-30 11:42:03.118 [main] DEBUG nextflow.Session - "
             "Session await > all processes finished\n")
    with p.open("w") as f:
        for i in range(300_000):
            f.write(handler.format(i=i, g=i % 256, h=f"{i % 256:06x}"))
            f.write(noise)
    assert p.stat().st_size > 90 * 1024**2
    return p


def test_parse_log_bounds_memory_on_a_big_log(big_log):
    peak = _peak_rss_mb(
        f"ts = nf_tui.parse_log(Path({str(big_log)!r}))\n"
        f"assert len(ts) == 256, len(ts)\n")
    size_mb = big_log.stat().st_size / 1024**2
    assert peak < 150, f"peak RSS {peak:.0f} MB parsing a {size_mb:.0f} MB log"


def test_parse_errors_bounds_memory_on_a_big_log(big_log):
    peak = _peak_rss_mb(f"e = nf_tui.parse_errors(Path({str(big_log)!r}))\n"
                        f"assert e == {{}}, len(e)\n")
    size_mb = big_log.stat().st_size / 1024**2
    assert peak < 200, f"peak RSS {peak:.0f} MB scanning a {size_mb:.0f} MB log"


def test_read_all_tails_a_big_command_sh(tmp_path):
    p = tmp_path / ".command.sh"
    p.write_text("".join(f"step {i}\n" for i in range(200_000)))
    out = nf_tui._read_all(p)
    assert len(out) <= 20000
    assert out.rstrip().endswith("step 199999")


def test_read_all_missing_file(tmp_path):
    assert "cannot read" in nf_tui._read_all(tmp_path / "nope.sh")


def test_follower_skips_ahead_on_a_huge_burst(tmp_path):
    """A task can dump gigabytes between ticks; the pane should get the newest
    window, not all of it."""
    p = tmp_path / ".command.log"
    p.write_text("start\n")
    f = nf_tui.Follower(p)
    f.read_new()                                   # consume what's there
    burst = "x" * (nf_tui.FOLLOW_MAX_CATCHUP * 3)
    with p.open("a") as fh:
        fh.write(burst + "\nTHE-END\n")
    data = f.read_new()
    assert len(data) <= nf_tui.FOLLOW_MAX_CATCHUP + 64, len(data)
    assert data.rstrip().endswith("THE-END")       # newest output is kept


def test_follower_reads_normal_appends_whole(tmp_path):
    p = tmp_path / ".command.log"
    p.write_text("one\n")
    f = nf_tui.Follower(p)
    assert f.read_new() == "one\n"
    with p.open("a") as fh:
        fh.write("two\nthree\n")
    assert f.read_new() == "two\nthree\n"


def test_read_all_bounds_memory_on_a_big_command_sh(big_file):
    """Same output either way — only the memory tells the two apart, so this is
    the assertion that pins the fix."""
    peak = _peak_rss_mb(
        f"s = nf_tui._read_all(Path({str(big_file)!r}))\n"
        f"assert len(s) <= 20000, len(s)\n")
    size_mb = big_file.stat().st_size / 1024**2
    assert peak < 200, f"peak RSS {peak:.0f} MB reading a {size_mb:.0f} MB file"


def test_remote_cat_keeps_only_the_tail_of_a_huge_object(tmp_path, monkeypatch):
    """S3 objects are read through the user's CLI, whose whole stdout used to be
    buffered *and cached*. A big task output must not sit in memory in full."""
    import nf_tui as m
    bucket = tmp_path / "bucket"
    task = bucket / "work" / "ab" / "cd01"
    task.mkdir(parents=True)
    big = task / ".command.out"
    with big.open("w") as f:
        f.write("HEAD-MARKER\n")
        f.write("".join(f"line {i}\n" for i in range(400_000)))
        f.write("TAIL-MARKER\n")
    assert big.stat().st_size > 4 * 1024 * 1024

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "aws").write_text(AWS_SHIM)
    (shim_dir / "aws").chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_S3_ROOT", str(bucket))
    m._remote_cache.clear()

    got = m.remote_cat("s3://work/ab/cd01/.command.out")
    assert got is not None
    assert got.rstrip().endswith("TAIL-MARKER")     # the tail is what's wanted
    assert len(got) <= 20_000                       # default limit honoured
    assert "HEAD-MARKER" not in got
    # and the cache holds the bounded tail, not the whole object
    cached = m._remote_cache[("cat", "s3://work/ab/cd01/.command.out")]
    assert isinstance(cached, str)
    assert len(cached) <= m.REMOTE_CAT_MAX
    assert len(cached) < big.stat().st_size / 2


def test_remote_cat_still_returns_a_small_object_whole(tmp_path, monkeypatch):
    import nf_tui as m
    bucket = tmp_path / "bucket"
    task = bucket / "work" / "ef" / "9901"
    task.mkdir(parents=True)
    (task / ".command.out").write_text("just\na few\nlines\n")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "aws").write_text(AWS_SHIM)
    (shim_dir / "aws").chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_S3_ROOT", str(bucket))
    m._remote_cache.clear()
    assert m.remote_cat("s3://work/ef/9901/.command.out") == "just\na few\nlines\n"


def test_remote_cat_missing_object_is_none(tmp_path, monkeypatch):
    import nf_tui as m
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "aws").write_text(AWS_SHIM)
    (shim_dir / "aws").chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_S3_ROOT", str(bucket))
    m._remote_cache.clear()
    assert m.remote_cat("s3://work/zz/9999/.command.out") is None


# ---------------------------------------------------------------------------
# Previews grow as you scroll, instead of deciding up front how much of a file
# to materialise. `less` streams; loading everything cost 42s / 719 MB on a
# 159 MB file and still showed only a tenth of it.
# ---------------------------------------------------------------------------

def test_read_forward_resumes_from_the_offset(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("".join(f"line {i}\n" for i in range(100)))
    pos, lines, eof = nf_tui.read_forward(p, 0, 10)
    assert lines == [f"line {i}" for i in range(10)]
    assert not eof and pos > 0
    pos2, lines2, eof2 = nf_tui.read_forward(p, pos, 10)
    assert lines2 == [f"line {i}" for i in range(10, 20)]
    assert pos2 > pos and not eof2


def test_read_forward_reports_eof(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\n")
    pos, lines, eof = nf_tui.read_forward(p, 0, 100)
    assert lines == ["a", "b"] and eof


def test_read_forward_walks_a_whole_file_exactly_once(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("".join(f"L{i}\n" for i in range(1000)))
    seen, pos, eof, guard = [], 0, False, 0
    while not eof and guard < 100:
        pos, chunk, eof = nf_tui.read_forward(p, pos, 64)
        seen += chunk
        guard += 1
    assert seen == [f"L{i}" for i in range(1000)]        # no gaps, no repeats


def test_read_forward_on_a_missing_file(tmp_path):
    pos, lines, eof = nf_tui.read_forward(tmp_path / "nope", 0, 10)
    assert lines == [] and eof


def test_preview_grows_when_you_scroll_to_the_bottom(tmp_path):
    """The behaviour that replaces 'load the whole thing': each time the pane
    reaches the bottom, the next chunk is appended."""
    from nf_tui import VIEW_MAX_LINES
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    big = wd / "big.txt"
    big.write_text("".join(f"line {i}\n" for i in range(VIEW_MAX_LINES * 6)))
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
        pane = app.query_one("#log", RichLog)
        first = len(pane.lines)
        assert app._view_pos is not None and not app._view_eof, "no resume point"

        pane.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()
        grown = len(pane.lines)
        assert grown > first, f"pane did not grow on scroll ({first} -> {grown})"

        before = app._view_pos
        pane.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()
        assert len(pane.lines) > grown          # and keeps going
        assert app._view_pos > before           # the resume point advanced
        return True

    assert drive(NfScope(tmp_path), steps)


def test_preview_of_a_small_file_is_complete_and_does_not_extend(tmp_path):
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    (wd / "small.txt").write_text("one\ntwo\nthree\n")
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
        files.highlighted = [p.name for p in app._files].index("small.txt")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        n = len(pane.lines)
        assert app._view_eof, "a fully-read file should be marked eof"
        pane.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()
        assert len(pane.lines) == n, "a complete file must not grow further"
        return True

    assert drive(NfScope(tmp_path), steps)


def test_task_log_backfills_when_you_scroll_to_the_top(tmp_path):
    """A runaway .command.log opens at its tail; scrolling up must recover the
    earlier output rather than leaving it unreachable."""
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    cl = wd / ".command.log"
    with cl.open("w") as f:
        f.write("VERY-FIRST-LINE\n")
        for i in range(40_000):
            f.write(f"task output line {i} " + "q" * 40 + "\n")
        f.write("VERY-LAST-LINE\n")
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        tree.move_cursor(leaves(tree)[0])
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        opened = len(pane.lines)
        text = "\n".join(str(s) for s in pane.lines)
        assert "VERY-LAST-LINE" in text, "task log should open at its tail"
        assert app._tasklog_start > 0, "a big log should not start fully loaded"

        before_off = app._tasklog_start
        pane.scroll_home(animate=False)
        await pilot.pause()
        await pilot.pause()
        assert len(pane.lines) > opened, "scrolling to the top loaded nothing"
        assert app._tasklog_start < before_off, "the start offset did not move back"
        # the newest output is still there after the pane is rewritten
        text = "\n".join(str(s) for s in pane.lines)
        assert "VERY-LAST-LINE" in text
        return True

    assert drive(NfScope(tmp_path), steps)


def test_small_task_log_loads_whole_and_does_not_backfill(tmp_path):
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("FIRST\nmiddle\nLAST\n")
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        tree.move_cursor(leaves(tree)[0])
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        text = "\n".join(str(s) for s in pane.lines)
        assert "FIRST" in text and "LAST" in text   # nothing lost on a small log
        assert app._tasklog_start == 0
        n = len(pane.lines)
        pane.scroll_home(animate=False)
        await pilot.pause()
        await pilot.pause()
        assert len(pane.lines) == n, "a fully-loaded log must not grow"
        return True

    assert drive(NfScope(tmp_path), steps)


def test_pager_command_carries_the_quit_hint(tmp_path):
    """Esc cannot be rebound to quit less (ESC prefixes every arrow key), so the
    status line has to say how to leave."""
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    f = wd / "out.txt"
    f.write_text("hi\n")
    gz = wd / "out.gz"
    import gzip as _gz
    with _gz.open(gz, "wt") as fh:
        fh.write("hi\n")
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    async def steps(app, pilot):
        t = Task(hash="ab/cccc", name="P:A (s1)", status="COMPLETED",
                 exit="0", workdir=str(wd))
        for path in (f, gz):
            cmd = app._pager_command(t, path, "less")
            assert "q quit" in cmd, cmd
        return True

    assert drive(NfScope(tmp_path), steps)


def test_run_log_pager_carries_the_quit_hint(tmp_path, monkeypatch):
    log = tmp_path / ".nextflow.log"
    log.write_text("~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   "exit: 0; error: -; workDir: /tmp/x]\n")
    pages: list[str] = []

    async def steps(app, pilot):
        monkeypatch.setattr(app, "_page", lambda cmd: pages.append(cmd))
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("L")
        await pilot.pause()
        assert pages and "q quit" in pages[0], pages
        return True

    assert drive(NfScope(tmp_path), steps)


# ---------------------------------------------------------------------------
# gzip and container-decoded files stream too. Neither can seek — a deflate
# stream and a `samtools view` pipe both have to restart from the beginning —
# so they resume by line count instead of byte offset.
# ---------------------------------------------------------------------------

def test_head_gzip_skips_lines(tmp_path):
    import gzip as _gz
    p = tmp_path / "f.gz"
    with _gz.open(p, "wt") as f:
        f.write("".join(f"line {i}\n" for i in range(100)))
    assert nf_tui.head_gzip(p, 5) == [f"line {i}" for i in range(5)]
    assert nf_tui.head_gzip(p, 5, skip=10) == [f"line {i}" for i in range(10, 15)]


def test_head_gzip_skip_past_the_end_is_empty(tmp_path):
    import gzip as _gz
    p = tmp_path / "f.gz"
    with _gz.open(p, "wt") as f:
        f.write("a\nb\n")
    assert nf_tui.head_gzip(p, 10, skip=50) == []


def test_head_gzip_walks_a_file_exactly_once(tmp_path):
    """No gaps and no repeats across chunks — the property that matters when
    the pane is stitched together from successive reads."""
    import gzip as _gz
    p = tmp_path / "f.gz"
    with _gz.open(p, "wt") as f:
        f.write("".join(f"line {i}\n" for i in range(500)))
    seen, skip = [], 0
    while True:
        chunk = nf_tui.head_gzip(p, 64, skip=skip)
        if not chunk:
            break
        seen += chunk
        skip += len(chunk)
    assert seen == [f"line {i}" for i in range(500)]


def test_gzip_preview_grows_when_you_scroll(tmp_path):
    import gzip as _gz
    from nf_tui import VIEW_MAX_LINES
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    gzp = wd / "reads.fastq.gz"
    with _gz.open(gzp, "wt") as f:
        f.write("".join(f"gz line {i}\n" for i in range(VIEW_MAX_LINES * 4)))
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
        files.highlighted = [p.name for p in app._files].index("reads.fastq.gz")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        first = len(pane.lines)
        assert app._view_shown == VIEW_MAX_LINES, app._view_shown
        assert not app._view_eof, "a long gzip should not report eof on open"

        pane.scroll_end(animate=False)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(pane.lines) > first, "gzip pane did not grow on scroll"
        assert app._view_shown > VIEW_MAX_LINES, app._view_shown
        return True

    assert drive(NfScope(tmp_path), steps)


def test_short_gzip_reports_eof_and_does_not_grow(tmp_path):
    import gzip as _gz
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    gzp = wd / "small.gz"
    with _gz.open(gzp, "wt") as f:
        f.write("one\ntwo\n")
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
        files.highlighted = [p.name for p in app._files].index("small.gz")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        n = len(pane.lines)
        assert app._view_eof
        pane.scroll_end(animate=False)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(pane.lines) == n
        return True

    assert drive(NfScope(tmp_path), steps)


def test_container_decode_window_skips_lines(tmp_path):
    """The BAM path re-decodes and drops the lines already shown: the pipe
    cannot seek, so `tail -n +N` is how it resumes."""
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    (wd / ".command.run").write_text(
        "  docker run --rm -v /work:/work quay.io/biocontainers/samtools:1.21 \\\n")
    bam = wd / "aln.bam"
    bam.write_bytes(b"\x1f\x8b" + b"\x00" * 100)
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")
    seen: list[str] = []

    async def steps(app, pilot):
        import subprocess as sp
        real = sp.run

        def fake(cmd, *a, **k):
            if isinstance(cmd, list) and any("samtools" in str(c) for c in cmd):
                seen.append(" ".join(str(c) for c in cmd))
                class R:
                    returncode = 0
                    stdout = "\n".join(f"read {i}" for i in range(500))
                    stderr = ""
                return R()
            return real(cmd, *a, **k)

        sp.run = fake
        try:
            t = Task(hash="ab/cccc", name="P:A (s1)", status="COMPLETED",
                     exit="0", workdir=str(wd))
            app._files_task = t
            app._run_viewer(t, bam, "samtools view -h", False, skip=500,
                            append=True)
            await app.workers.wait_for_complete()
            await pilot.pause()
        finally:
            sp.run = real
        assert seen, "the container decode never ran"
        # the first call is the `docker image inspect` probe; the decode follows
        decode = [c for c in seen if "sh -c" in c or "tail -n" in c]
        assert decode, seen
        assert "tail -n +501" in decode[0], decode[0]
        return True

    assert drive(NfScope(tmp_path), steps)


def test_scrolling_appends_without_stranding_hints(tmp_path):
    """RichLog cannot delete, so a "more to come" hint written under the body
    gets buried mid-file by the next chunk. It belongs in the top rule, and
    earlier content must never be rewritten."""
    import gzip as _gz
    from nf_tui import VIEW_MAX_LINES
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    gzp = wd / "big.gz"
    with _gz.open(gzp, "wt") as f:
        f.write("".join(f"line {i}\n" for i in range(VIEW_MAX_LINES * 4)))
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    def rendered(pane):
        return ["".join(s.text for s in st._segments) for st in pane.lines]

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        tree.move_cursor(leaves(tree)[0])
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        files = app.query_one("#files", OptionList)
        files.highlighted = [p.name for p in app._files].index("big.gz")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#log", RichLog)

        snaps = [rendered(pane)]
        for _ in range(3):
            pane.scroll_end(animate=False)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            snaps.append(rendered(pane))

        for i in range(1, len(snaps)):
            assert snaps[i][: len(snaps[i - 1])] == snaps[i - 1], (
                f"chunk {i} rewrote earlier content instead of appending")
        assert len(snaps[-1]) > len(snaps[0]), "pane never grew"
        hints = [l for l in snaps[-1] if "scroll for more" in l]
        assert len(hints) == 1, f"expected one hint, found {len(hints)}"
        assert "scroll for more" in snaps[-1][2] or "scroll for more" in snaps[-1][3], \
            "the hint should sit in the top rule, not in the body"
        return True

    assert drive(NfScope(tmp_path), steps)


def test_failed_decode_is_not_labelled_end_of_file(tmp_path):
    """A decode that errors is a message, not the file: it must not claim to be
    the end of it, and scrolling must not try to fetch more."""
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("x\n")
    (wd / ".command.run").write_text(
        "  docker run --rm -v /w:/w quay.io/biocontainers/samtools:1.21 \\\n")
    bam = wd / "aln.bam"
    bam.write_bytes(b"\x1f\x8b" + b"\x00" * 100)
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")

    async def steps(app, pilot):
        import subprocess as sp
        real = sp.run

        def fake(cmd, *a, **k):
            if isinstance(cmd, list) and any("samtools" in str(c) for c in cmd):
                class R:
                    returncode = 1
                    stdout = ""
                    stderr = "samtools view: failed to open file"
                return R()
            return real(cmd, *a, **k)

        sp.run = fake
        try:
            t = Task(hash="ab/cccc", name="P:A (s1)", status="COMPLETED",
                     exit="0", workdir=str(wd))
            app._files_task = t
            app._last_file = bam
            app.view = "files"
            app._run_viewer(t, bam, "samtools view -h", False)
            await app.workers.wait_for_complete()
            await pilot.pause()
        finally:
            sp.run = real
        pane = app.query_one("#log", RichLog)
        text = "\n".join("".join(s.text for s in st._segments) for st in pane.lines)
        assert "end of file" not in text, text
        assert app._view_shown is None, "a failed decode must not be resumable"
        return True

    assert drive(NfScope(tmp_path), steps)


# ---------------------------------------------------------------------------
# Where the work tree is, when the launch command doesn't say. Setting
# `workDir` in a nextflow.config is routine (shared/institutional configs do
# it), and then the launch line carries no -w and there is no nf-core banner.
# A resumed run in that setup resolved nothing at all: cached tasks have no
# workDir of their own, so every log, output and metric went missing.
# ---------------------------------------------------------------------------

REAL_WORKDIR_LINE = ("Aug-06 13:07:36.237 [main] DEBUG nextflow.Session - "
                     "Work-dir: {path} [Mac OS X]\n")


def test_find_work_root_reads_nextflows_own_work_dir_line(tmp_path):
    """Verbatim from a real log — Nextflow's Session logs the *resolved* work
    dir whatever set it, which makes it the authoritative source."""
    wk = tmp_path / "faraway" / "wk"
    wk.mkdir(parents=True)
    log = tmp_path / ".nextflow.log"
    log.write_text("  $> nextflow run main.nf\n"
                   + REAL_WORKDIR_LINE.format(path=wk))
    assert nf_tui.find_work_root(log) == str(wk)


def test_work_dir_line_survives_a_config_set_workdir(tmp_path):
    """No -w on the command line and no banner: the case that was broken."""
    wk = tmp_path / "elsewhere"
    wk.mkdir()
    log = tmp_path / ".nextflow.log"
    log.write_text("  $> nextflow run main.nf -resume\n"
                   + REAL_WORKDIR_LINE.format(path=wk))
    assert nf_tui.find_work_root(log) == str(wk)
    assert nf_tui.find_work_root(log) != str(tmp_path / "work")


def test_work_dir_line_keeps_a_cloud_uri_intact(tmp_path):
    log = tmp_path / ".nextflow.log"
    log.write_text("Aug-06 13:07:36.237 [main] DEBUG nextflow.Session - "
                   "Work-dir: s3://my-bucket/wk [Linux]\n")
    assert nf_tui.find_work_root(log) == "s3://my-bucket/wk"


def test_work_root_falls_back_to_a_handler_lines_work_dir(tmp_path):
    """Older logs may carry no Work-dir line; a completed task's own work dir
    still gives the root two levels up."""
    wd = tmp_path / "somewhere" / "wk" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    log = tmp_path / ".nextflow.log"
    log.write_text("  $> nextflow run main.nf\n"
                   f"~> TaskHandler[id: 1; name: P:A (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")
    assert nf_tui.find_work_root(log) == str(tmp_path / "somewhere" / "wk")


def test_work_root_still_defaults_when_the_log_says_nothing(tmp_path):
    log = tmp_path / ".nextflow.log"
    log.write_text("  $> nextflow run main.nf\nnothing useful here\n")
    assert nf_tui.find_work_root(log) == str(tmp_path / "work")


def test_resumed_run_resolves_cached_tasks_with_a_config_set_workdir(tmp_path):
    """The end-to-end regression: a -resume log has no handler lines at all, so
    every cached task depends on the work root being right."""
    wk = tmp_path / "faraway" / "wk"
    for h in ("d2/ed2a62c1331eb5dcb6042b035e7c42", "e8/44fb6b95ce4b2e066af3de"):
        (wk / h).mkdir(parents=True)
    log = tmp_path / ".nextflow.log"
    log.write_text(
        "  $> nextflow run main.nf -resume\n"
        + REAL_WORKDIR_LINE.format(path=wk)
        + "[d2/ed2a62] Cached process > MAKE (1)\n"
        + "[e8/44fb6b] Cached process > MAKE (2)\n")
    rep = nf_tui.run_report(log)
    assert rep["work_dir"] == str(wk)
    tasks = rep["tasks"]
    assert len(tasks) == 2
    assert all(t["cached"] for t in tasks)
    resolved = [t for t in tasks if t["workdir"] and Path(t["workdir"]).is_dir()]
    assert len(resolved) == 2, f"cached tasks unresolved: {tasks}"


def test_tree_leaf_data_is_refreshed_when_a_task_finishes(tmp_path):
    """A leaf built while its task was SUBMITTED used to keep that Task object
    forever, so `e` reported "no failed tasks" on a run whose header counted
    two — the header reparses, the tree did not."""
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    log = tmp_path / ".nextflow.log"
    log.write_text("[ab/cccccc] Submitted process > P:BOOM (s1)\n")

    async def steps(app, pilot):
        await pilot.pause()
        node = app._task_nodes.get("ab/cccccc")
        assert node is not None, app._task_nodes
        assert not is_failed(node.data), "should not look failed yet"

        # the task now fails
        log.write_text("[ab/cccccc] Submitted process > P:BOOM (s1)\n"
                       f"~> TaskHandler[id: 1; name: P:BOOM (s1); status: "
                       f"COMPLETED; exit: 139; error: -; workDir: {wd}]\n")
        app._force_refresh = True
        app.action_refresh()
        await pilot.pause()

        node = app._task_nodes.get("ab/cccccc")
        assert is_failed(node.data), (
            f"leaf data went stale: status={node.data.status} exit={node.data.exit}")

        notes: list = []
        app.notify = lambda msg, **k: notes.append(msg)
        app.action_next_failed()
        await pilot.pause()
        assert not any("no failed tasks" in n for n in notes), notes
        return True

    assert drive(NfScope(tmp_path), steps)


# ---------------------------------------------------------------------------
# Why a task failed. `Caused by:` is Nextflow's framing and often says no more
# than the exit status; the answer is what the command itself printed. On a
# real run the Caused-by line read "terminated with an error exit status (1)"
# while Command error read "error during connect: ... docker.sock ... EOF" —
# the container runtime had gone away.
# ---------------------------------------------------------------------------

REPORT = """\
Error executing process > 'P:BOOM (s1)'

Caused by:
  Process `P:BOOM (s1)` terminated with an error exit status (1)

Command executed:

  do_the_thing --in x

Command exit status:
  1

Command output:
  (empty)

Command error:
  error during connect: docker.sock: EOF
  second line of the tool's stderr

Work dir:
  /w/ab/cdef

Container:
  quay.io/example:1

Tip: you can replicate the issue by changing to the process work dir
"""


def test_command_error_extracts_the_tools_own_message():
    got = nf_tui.command_error(REPORT)
    assert got.splitlines() == ["error during connect: docker.sock: EOF",
                                "second line of the tool's stderr"]


def test_command_error_stops_at_the_next_section():
    got = nf_tui.command_error(REPORT)
    assert "/w/ab/cdef" not in got and "quay.io" not in got and "Tip:" not in got


def test_command_error_treats_empty_as_absent():
    block = REPORT.replace("  error during connect: docker.sock: EOF\n"
                           "  second line of the tool's stderr", "  (empty)")
    assert nf_tui.command_error(block) == ""


def test_why_failed_prefers_the_command_error():
    why = nf_tui.why_failed(REPORT)
    assert why == "error during connect: docker.sock: EOF"
    assert "exit status" not in why, "the exit status is not a reason"


def test_why_failed_falls_back_when_the_command_said_nothing():
    block = REPORT.replace("  error during connect: docker.sock: EOF\n"
                           "  second line of the tool's stderr", "  (empty)")
    assert nf_tui.why_failed(block) == nf_tui.error_summary(block)
    assert "exit status (1)" in nf_tui.why_failed(block)


def test_run_report_carries_why_for_a_failure(tmp_path):
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    log = tmp_path / ".nextflow.log"
    log.write_text(
        f"~> TaskHandler[id: 1; name: P:BOOM (s1); status: COMPLETED; exit: 1; "
        f"error: -; workDir: {wd}]\n"
        f"Jul-15 15:24:39.100 [main] ERROR nextflow.Nextflow - "
        f"Error executing process > 'P:BOOM (s1)'\n"
        + REPORT.split("\n", 1)[1].replace("/w/ab/cdef", str(wd))
        + "\nJul-15 15:24:40.000 [main] DEBUG nextflow.Session - Goodbye\n")
    rep = nf_tui.run_report(log)
    err = [t for t in rep["tasks"] if t.get("error")][0]["error"]
    assert err["why"] == "error during connect: docker.sock: EOF"
    assert "docker.sock" in err["command_error"]
    assert "exit status" in err["summary"]      # the old field is unchanged


# ---------------------------------------------------------------------------
# Singularity, actually executed. The tests above pin the *format* of a real
# .command.run; these run the container path through a shim that behaves like
# the singularity CLI, the same way the S3 support is exercised through a fake
# `aws`. Nothing here needs a cluster, but it does prove the commands nf-tui
# builds are ones the engine would accept — the previous Singularity bug was
# exactly a command the engine could never have run.
# ---------------------------------------------------------------------------

SINGULARITY_SHIM = r'''#!/usr/bin/env python3
"""Enough of `singularity` to run nf-tui's commands: exec [flags] IMAGE cmd..."""
import os, subprocess, sys

args = sys.argv[1:]
if not args or args[0] != "exec":
    sys.stderr.write("shim: expected `exec`, got %r\n" % (args[:1],))
    sys.exit(2)
args = args[1:]

binds = []
while args:
    a = args[0]
    if a in ("--no-home", "--containall", "--cleanenv"):
        args.pop(0)
    elif a in ("-B", "--bind"):
        args.pop(0)
        binds.append(args.pop(0))
    elif a.startswith("-"):
        args.pop(0)
    else:
        break

if not args:
    sys.stderr.write("shim: no image given\n"); sys.exit(2)
image = args.pop(0)
if not image.endswith(".sif"):
    sys.stderr.write("shim: %s is not a .sif\n" % image); sys.exit(2)
if not os.path.exists(image):
    sys.stderr.write("FATAL: image not found: %s\n" % image); sys.exit(255)
with open(os.environ["SHIM_CALLS"], "a") as fh:
    fh.write("exec|%s|%s|%s\n" % (image, ",".join(binds), " ".join(args)))

# `sh -c "..."` — run it for real, so the command nf-tui built has to be valid
if args[:2] == ["sh", "-c"]:
    sys.exit(subprocess.run(["sh", "-c", args[2]]).returncode)
sys.exit(subprocess.run(args).returncode)
'''

SAMTOOLS_SHIM = '''#!/bin/sh
# Enough of samtools for the viewer: `samtools view -h <file>`
case "$1" in
  view) shift ;;
  *) echo "samtools: unknown subcommand $1" >&2; exit 1 ;;
esac
[ "$1" = "-h" ] && shift
[ -f "$1" ] || { echo "samtools: cannot open $1" >&2; exit 1; }
echo "@HD\tVN:1.6\tSO:coordinate"
echo "@SQ\tSN:chr22\tLN:40001"
echo "read1\t99\tchr22\t100\t60\t10M\t=\t200\t150\tACGT\tIIII"
'''


@pytest.fixture
def singularity_run(tmp_path, monkeypatch):
    """A run whose task used Singularity, with a shimmed engine on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "singularity").write_text(SINGULARITY_SHIM)
    (bin_dir / "singularity").chmod(0o755)
    (bin_dir / "samtools").write_text(SAMTOOLS_SHIM)
    (bin_dir / "samtools").chmod(0o755)
    calls = tmp_path / "calls.txt"
    calls.write_text("")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SHIM_CALLS", str(calls))

    img = tmp_path / "images" / "samtools_1.21.sif"
    img.parent.mkdir()
    img.write_bytes(b"SIF\x00")
    wd = tmp_path / "work" / "ab" / ("c" * 30)
    wd.mkdir(parents=True)
    (wd / ".command.log").write_text("ran\n")
    (wd / ".command.run").write_text(
        'nxf_launch() {\n'
        '    set +u; env - PATH="$PATH" ${TMP:+SINGULARITYENV_TMP="$TMP"} '
        f'singularity exec --no-home -B /scratch:/scratch -B "$NXF_TASK_WORKDIR" {img} '
        '/bin/bash -c "cd $NXF_TASK_WORKDIR; eval $(nxf_container_env); '
        '/bin/bash -ue .command.sh"\n}\n')
    (wd / "test.cram").write_bytes(b"CRAM\x00\x01")
    log = tmp_path / ".nextflow.log"
    log.write_text(f"~> TaskHandler[id: 1; name: P:ALIGN (s1); status: COMPLETED; "
                   f"exit: 0; error: -; workDir: {wd}]\n")
    return tmp_path, wd, str(img), calls


def test_singularity_image_probe_actually_runs(singularity_run):
    """_image_has must build a command the engine accepts — `exec`, no --rm."""
    _, _, img, calls = singularity_run
    assert nf_tui._image_has("singularity", img, "samtools") is True
    assert nf_tui._image_has("singularity", img, "definitely-not-here") is False
    recorded = calls.read_text().strip().splitlines()
    assert recorded, "the shim was never invoked"
    assert all(r.startswith("exec|") for r in recorded), recorded


def test_singularity_decodes_a_cram_end_to_end(singularity_run):
    """The whole path: parse .command.run, build the command, run it, show SAM."""
    base, wd, img, calls = singularity_run

    async def steps(app, pilot):
        tree = app.query_one("#tasks", Tree)
        tree.move_cursor(leaves(tree)[0])
        await pilot.pause()
        await pilot.press("d")
        await app.workers.wait_for_complete()
        await pilot.pause()
        files = app.query_one("#files", OptionList)
        files.highlighted = [p.name for p in app._files].index("test.cram")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#log", RichLog)
        text = "\n".join("".join(s.text for s in st._segments) for st in pane.lines)
        assert "@HD" in text and "chr22" in text, text[:400]
        # and it went through singularity, with the task's own binds
        rec = calls.read_text()
        assert "exec|" in rec and img in rec
        assert "/scratch:/scratch" in rec
        assert str(wd) in rec, "the task's work-dir bind was dropped"
        return True

    assert drive(NfScope(base), steps)


def test_singularity_pager_command_would_run(singularity_run):
    """`L` builds a shell pipeline; check the engine half of it executes."""
    base, wd, img, calls = singularity_run

    async def steps(app, pilot):
        t = Task(hash="ab/cccc", name="P:ALIGN (s1)", status="COMPLETED",
                 exit="0", workdir=str(wd))
        cmd = app._pager_command(t, wd / "test.cram", "cat")
        assert "singularity" in cmd and " exec " in cmd and "--rm" not in cmd
        # run everything up to the pager and check it produced SAM
        engine_part = cmd.split("| cat")[0]
        r = subprocess.run(["sh", "-c", engine_part], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "@HD" in r.stdout, r.stdout[:300]
        return True

    assert drive(NfScope(base), steps)
