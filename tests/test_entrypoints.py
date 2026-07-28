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
