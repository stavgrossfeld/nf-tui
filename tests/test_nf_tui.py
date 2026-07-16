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
