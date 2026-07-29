"""Tests for the nf-tui-web and nf-tui-run entry points.

These two had no coverage at all despite both having broken in the past, and
they are what a released package exposes on PATH. Nothing here starts a real
server or pipeline — the point is that the modules import, their CLI contracts
hold, and their pre-flight checks fire instead of failing obscurely later.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import nf_tui_run
import nf_tui_serve
import pytest
from generate_run import make_run


def run_cli(script: str, *args: str, cwd: Path | None = None):
    """Invoke an entry point the way an installed console script would."""
    return subprocess.run(
        [sys.executable, "-c",
         f"import {script}; {script}.main()", *args],
        capture_output=True, text=True, cwd=str(cwd) if cwd else None, timeout=60)


# ---- nf-tui-run ------------------------------------------------------------

def test_run_help_exits_zero_and_prints_usage():
    # --help is a request, not an error: it belongs on stdout with exit 0.
    r = run_cli("nf_tui_run", "--help")
    assert r.returncode == 0
    assert "usage: nf-tui-run" in r.stdout
    assert r.stderr == ""


def test_run_without_arguments_is_a_usage_error():
    r = run_cli("nf_tui_run")
    assert r.returncode == 1
    assert "usage: nf-tui-run" in r.stderr


def test_run_reports_a_missing_nextflow_clearly(tmp_path, monkeypatch):
    # With no `nextflow` on PATH the user must get a plain sentence, not a
    # FileNotFoundError traceback.
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        monkeypatch.setattr(sys, "argv", ["nf-tui-run", "main.nf"])
        nf_tui_run.main()
    assert "nextflow" in str(e.value).lower()


def test_run_waits_for_the_new_log_not_the_previous_one(tmp_path, monkeypatch):
    """A directory usually already holds the last run's .nextflow.log.

    Waiting merely for the file to "exist" matched that stale log instantly, so
    nf-tui opened the previous, already-finished run instead of the one just
    launched. Nextflow rotates the old log aside and creates a fresh file, so
    the identity must change before we open anything.
    """
    import threading
    import time

    log = tmp_path / ".nextflow.log"
    log.write_text("previous, finished run\n")
    before = nf_tui_run._log_identity(log)
    assert before is not None                      # the stale log is present

    # nothing has rotated yet: the identity is unchanged, so we must NOT open
    assert nf_tui_run._log_identity(log) == before

    def rotate():                                  # what `nextflow run` does
        time.sleep(0.3)
        log.rename(tmp_path / ".nextflow.log.1")
        log.write_text("the new run\n")

    threading.Thread(target=rotate, daemon=True).start()
    deadline = time.time() + 10
    while time.time() < deadline:
        now = nf_tui_run._log_identity(log)
        if now is not None and now != before:
            break
        time.sleep(0.05)

    assert nf_tui_run._log_identity(log) != before
    assert log.read_text() == "the new run\n"      # the fresh one, not the stale


def test_log_identity_handles_a_missing_file(tmp_path):
    assert nf_tui_run._log_identity(tmp_path / "nope.log") is None


# ---- nf-tui-web ------------------------------------------------------------

def test_serve_refuses_a_directory_with_no_runs(tmp_path, monkeypatch):
    # Previously this served an app that exited instantly, leaving the browser
    # reload-looping with no explanation.
    monkeypatch.setattr(sys, "argv", ["nf-tui-web", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        nf_tui_serve.main()
    assert "no .nextflow.log" in str(e.value)


def test_serve_builds_a_command_for_a_real_run(tmp_path, monkeypatch):
    log = make_run(tmp_path, n_tasks=5, n_procs=1)
    captured = {}

    class FakeServer:
        def __init__(self, command, host, port):
            captured.update(command=command, host=host, port=port)

        def serve(self):
            captured["served"] = True

    monkeypatch.setattr(nf_tui_serve, "Server", FakeServer)
    monkeypatch.setattr(sys, "argv",
                        ["nf-tui-web", str(log), "--port", "8123"])
    nf_tui_serve.main()

    assert captured["served"] and captured["port"] == 8123
    cmd = captured["command"]
    # the served command must carry the web flag and an absolute path: it runs
    # with its own working directory, so a relative path would not resolve
    assert "NF_TUI_WEB=1" in cmd
    assert str(log.resolve()) in cmd


# ---- `nf-tui nextflow run …` passthrough ------------------------------------

def test_nf_tui_nextflow_passes_the_command_through_verbatim(monkeypatch):
    """`nf-tui nextflow run …` prefixes an ordinary nextflow command.

    It must reach nextflow exactly as typed — including options that belong
    before `run` — so nothing the user relies on is dropped or reordered.
    """
    import nf_tui

    seen = {}
    monkeypatch.setattr(nf_tui_run, "launch", lambda cmd: seen.setdefault("cmd", cmd))
    monkeypatch.setattr(sys, "argv",
                        ["nf-tui", "nextflow", "run", "nf-core/sarek",
                         "-profile", "test,docker", "--outdir", "out"])
    nf_tui.main()
    assert seen["cmd"] == ["nextflow", "run", "nf-core/sarek",
                           "-profile", "test,docker", "--outdir", "out"]

    seen.clear()
    monkeypatch.setattr(sys, "argv",
                        ["nf-tui", "nextflow", "-log", "x.log", "run", "main.nf"])
    nf_tui.main()
    assert seen["cmd"] == ["nextflow", "-log", "x.log", "run", "main.nf"]


def test_nf_tui_nextflow_alone_explains_itself(monkeypatch):
    import nf_tui
    monkeypatch.setattr(sys, "argv", ["nf-tui", "nextflow"])
    with pytest.raises(SystemExit) as e:
        nf_tui.main()
    assert "full nextflow command" in str(e.value)
