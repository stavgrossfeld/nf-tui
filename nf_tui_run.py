#!/usr/bin/env python3
"""nf-tui-run — launch `nextflow run ...` and watch it live in nf-tui.

Nextflow runs in the background (its console output goes to a file so it
doesn't fight the TUI); nf-tui opens on the run's .nextflow.log and refreshes
as the pipeline progresses. Quitting nf-tui leaves the pipeline running.

    nf-tui-run nf-core/sarek -profile test,docker --outdir results
    nf-tui-run main.nf --input samples.csv
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


USAGE = ("usage: nf-tui-run <nextflow run args...>\n"
         "  e.g.  nf-tui-run nf-core/sarek -profile test,docker --outdir results\n"
         "        nf-tui-run main.nf --input samples.csv\n\n"
         "Runs `nextflow run` in the background and opens nf-tui on the new\n"
         "run's .nextflow.log. Quitting nf-tui leaves the pipeline running.")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)              # asked for help: stdout, exit 0
        return
    if not args:
        sys.exit(USAGE)           # missing arguments: stderr, exit 1

    cwd = Path.cwd()
    console = cwd / ".nf-tui-run.out"          # nextflow's console output

    try:
        out = console.open("wb")
        proc = subprocess.Popen(
            ["nextflow", "run", *args],
            stdout=out, stderr=subprocess.STDOUT, cwd=str(cwd),
            start_new_session=True,            # survive nf-tui / terminal exit
        )
    except FileNotFoundError:
        sys.exit("nf-tui-run: `nextflow` not found on PATH")

    # Wait (up to ~60s) for this run's .nextflow.log to appear.
    log = cwd / ".nextflow.log"
    baseline = log.stat().st_mtime if log.exists() else 0.0
    for _ in range(600):
        if log.exists() and log.stat().st_mtime >= baseline:
            break
        if proc.poll() is not None:            # nextflow died before starting
            sys.exit(f"nf-tui-run: nextflow exited early (rc={proc.returncode}). "
                     f"See {console}")
        time.sleep(0.1)

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
            print(f"  stop pipeline:   kill {proc.pid}")


if __name__ == "__main__":
    main()
