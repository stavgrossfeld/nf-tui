#!/usr/bin/env python3
"""nf-tui-run — launch `nextflow run ...` and watch it live in nf-tui.

Nextflow runs in the background (its console output goes to a file so it
doesn't fight the TUI); nf-tui opens on the run's .nextflow.log and refreshes
as the pipeline progresses. Quitting nf-tui leaves the pipeline running.

    nf-tui-run nf-core/sarek -profile test,docker --outdir results
    nf-tui-run main.nf --input samples.csv
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


USAGE = ("usage: nf-tui-run <nextflow run args...>\n"
         "  e.g.  nf-tui-run nf-core/sarek -profile test,docker --outdir results\n"
         "        nf-tui-run main.nf --input samples.csv\n\n"
         "Runs `nextflow run` in the background and opens nf-tui on the new\n"
         "run's .nextflow.log. Quitting nf-tui leaves the pipeline running.")


def _log_identity(log: Path) -> tuple[int, int] | None:
    """(inode, creation-ish time) — changes when Nextflow rotates in a new log."""
    try:
        st = log.stat()
    except OSError:
        return None
    return (st.st_ino, int(st.st_ctime))


# Which container engine a command asks for, if any. Matched against the whole
# command line: `-profile docker,test`, `-profile test,docker`, `-with-docker`,
# `-with-singularity`, `-profile singularity` all count.
_ENGINE_FLAGS = {
    "docker": ("docker",),
    "podman": ("podman",),
    "singularity": ("singularity",),
    "apptainer": ("apptainer",),
}


def wanted_engine(cmd: list[str]) -> str | None:
    """The container engine this nextflow command will need, or None.

    Only looks at `-profile` values and `-with-<engine>` flags, so a path or a
    parameter that happens to contain the word "docker" is not mistaken for a
    request to use it.
    """
    for i, arg in enumerate(cmd):
        low = arg.lower()
        for engine, names in _ENGINE_FLAGS.items():
            if any(low == f"-with-{n}" or low == f"--with-{n}" for n in names):
                return engine
        if low in ("-profile", "--profile") and i + 1 < len(cmd):
            profiles = {p.strip().lower() for p in cmd[i + 1].split(",")}
            for engine, names in _ENGINE_FLAGS.items():
                if profiles & set(names):
                    return engine
    return None


DETAIL_MAX = 96


def _short_detail(output: str) -> str:
    """The engine's own words, cut down to one readable line.

    Container engines answer a failed probe with a paragraph: orbstack's docker
    names the socket, then repeats itself twice in dial-error form. The first
    clause is the part that identifies the problem; the rest is noise on screen.
    """
    first = next((l.strip() for l in output.splitlines() if l.strip()), "")
    first = first.split("; ")[0].rstrip(".")       # drop the "check if ..." tail
    if len(first) > DETAIL_MAX:
        first = first[:DETAIL_MAX - 1].rstrip() + "…"
    return first


def engine_problem(engine: str) -> tuple[str, str] | None:
    """None if `engine` is usable, else (what is wrong, the engine's own words).

    Two parts, because they are printed on two lines: the headline has to say
    what to do at a glance, and burying it in front of a daemon's diagnostics
    was the whole complaint about the old one-sentence version.

    Nextflow does not check this up front: it launches, submits a task, and the
    task fails with a connect error buried in .command.err — and because our
    console output goes to a file, nothing appears on screen at all.
    """
    daemon = engine in ("docker", "podman")
    if shutil.which(engine) is None:
        # On a cluster this is nearly always a module that was not loaded, so
        # say so — "not on PATH" alone sends people looking for an install.
        hint = "" if daemon else f"try `module load {engine}`"
        return f"{engine} is not on PATH", hint
    # docker/podman have a daemon worth pinging. singularity/apptainer are just
    # binaries, so the most this can cheaply prove is that one runs: a setuid or
    # user-namespace misconfiguration still only shows up on the first `exec`.
    probe = [engine, "info"] if daemon else [engine, "--version"]
    try:
        r = subprocess.run(probe, capture_output=True, text=True, timeout=25)
    except FileNotFoundError:
        return f"{engine} is not on PATH", ""
    except subprocess.TimeoutExpired:
        return f"{engine} is not responding", f"`{engine} {probe[1]}` timed out after 25s"
    if r.returncode != 0:
        state = "is not running" if daemon else "is failing to run"
        return f"{engine} {state}", _short_detail(r.stderr or r.stdout or "")
    return None


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)              # asked for help: stdout, exit 0
        return
    if not args:
        sys.exit(USAGE)           # missing arguments: stderr, exit 1
    launch(["nextflow", "run", *args])


def start_run(cmd: list[str]):
    """Pre-flight, start nextflow in the background, wait for THIS run's log.

    Returns (process, log_path, console_handle). Split out of launch() so the
    web front end can start a pipeline too: `nf-tui nextflow run …` worked and
    `nf-tui-web nextflow run …` was an argparse error, because only one of them
    knew how to launch anything.
    """
    cwd = Path.cwd()
    console = cwd / ".nf-tui-run.out"          # nextflow's console output

    # Pre-flight the container engine. Without this the run launches, every
    # task dies with a connect error, and the console output that would have
    # said so is redirected to a file nobody is looking at — the failure is
    # silent until you open the run and read a task's error report.
    engine = wanted_engine(cmd)
    if engine:
        problem = engine_problem(engine)
        if problem:
            what, detail = problem
            # Two lines at most. This is printed instead of the run, so the
            # thing to do belongs first and the engine's diagnostics second.
            msg = f"nf-tui: {what}, and this run needs it."
            if detail:
                msg += f"\n        {detail}"
            sys.exit(msg)

    try:
        out = console.open("wb")
        proc = subprocess.Popen(
            cmd,
            stdout=out, stderr=subprocess.STDOUT, cwd=str(cwd),
            start_new_session=True,            # survive nf-tui / terminal exit
        )
    except FileNotFoundError:
        sys.exit(f"nf-tui: `{cmd[0]}` not found on PATH")

    # Wait (up to ~60s) for THIS run's .nextflow.log.
    #
    # The directory usually already holds the previous run's log, so "the file
    # exists" proves nothing — waiting on that opened the last, already-finished
    # run instead of the one just launched. Nextflow rotates the old log to
    # .nextflow.log.1 and creates a fresh file, so the signal is a new inode
    # (or, if there was no log at all, the file simply appearing).
    log = cwd / ".nextflow.log"
    before = _log_identity(log)
    for _ in range(600):
        now = _log_identity(log)
        if now is not None and now != before:
            break
        if proc.poll() is not None:            # nextflow died before starting
            sys.exit(f"nf-tui: nextflow exited early (rc={proc.returncode}). "
                     f"See {console}")
        time.sleep(0.1)
    else:
        print("nf-tui: no new .nextflow.log after 60s — opening what's there.",
              file=sys.stderr)

    # Tell the UI which process this is, so K can stop it.
    os.environ["NF_TUI_PID"] = str(proc.pid)
    return proc, log, out


def launch(cmd: list[str]) -> None:
    """Run a full nextflow command in the background and watch it in nf-tui.

    Takes the whole command (`["nextflow", "run", ...]`) rather than just the
    run arguments, so `nf-tui nextflow …` can pass through exactly what the
    user typed — including any options that belong before `run`.
    """
    cwd = Path.cwd()
    console = cwd / ".nf-tui-run.out"
    proc, log, out = start_run(cmd)
    print(f"nextflow running (PID {proc.pid}); opening nf-tui…")
    # Open the TUI directly on this run's log (a file -> no run picker).
    sys.argv = ["nf-tui", str(log)]
    from nf_tui import main as tui
    try:
        tui()
    finally:
        out.close()
        if proc.poll() is None:
            print(f"\nnextflow still running (PID {proc.pid}).")
            print(f"  follow console:  tail -f {console}")
            print(f"  re-open viewer:  nf-tui {log}")
            # Plain kill (SIGTERM): Nextflow handles that one and kills its
            # running tasks — SIGINT is ignored, and kill -9 skips the handler
            # and strands jobs already queued on a scheduler.
            print(f"  stop pipeline:   kill {proc.pid}")


if __name__ == "__main__":
    main()
