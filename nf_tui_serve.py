#!/usr/bin/env python3
"""Serve nf-tui in a web browser instead of the terminal.

Same app, same code — textual-serve runs the terminal TUI on the server and
streams it to the browser over a websocket. Still a local process reading local
files; nothing is uploaded anywhere.

    python nf_tui_serve.py /path/to/run            # then open http://localhost:8000
    python nf_tui_serve.py /path/to/run --port 9000 --host 0.0.0.0

It also launches, exactly like `nf-tui nextflow run ...` does:

    nf-tui-web nextflow run nf-core/sarek -profile test,docker --outdir out
    nf-tui-web --port 9000 nextflow run main.nf

Options come before the word `nextflow`; everything from `nextflow` onward is
handed to Nextflow untouched, so its flags can't be mistaken for ours.

On an HPC: run this on the login node, then SSH-forward the port
(`ssh -L 8000:localhost:8000 login-node`) and open http://localhost:8000 locally.
"""
import argparse
import shlex
import sys
from pathlib import Path

from textual_serve.server import Server

HERE = Path(__file__).resolve().parent


def split_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """(our options, the nextflow command) — split at the first `nextflow`.

    `nextflow run` has its own `-profile`, `-resume`, `--outdir` and so on, and
    argparse would either claim them or reject them. Splitting on the literal
    word means we never have to know which flags are Nextflow's.
    """
    for i, arg in enumerate(argv):
        if arg == "nextflow":
            return list(argv[:i]), list(argv[i:])
    return list(argv), []


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    ours, nf_cmd = split_argv(argv)

    ap = argparse.ArgumentParser(
        prog="nf-tui-web",
        description="Serve nf-tui over the web, and optionally launch the run.",
        epilog="nf-tui-web [options] nextflow run <pipeline> ...  launches, "
               "then serves the new run.")
    ap.add_argument("run", nargs="?", default=".",
                    help="Nextflow run dir, .nextflow.log, or dir to search")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(ours)

    proc = None
    if nf_cmd:
        # Same launcher the terminal front end uses, so the container pre-flight
        # and the wait for *this* run's log behave identically.
        from nf_tui_run import start_run
        proc, log, _out = start_run(nf_cmd)
        print(f"nextflow running (PID {proc.pid}); serving nf-tui…")
        target = log
    else:
        # Resolve to an absolute path now (the served command runs with its own
        # working directory, so a bare "." would be ambiguous).
        target = Path(args.run).expanduser().resolve()

        # Pre-flight: if there are no runs here, say so in the terminal instead
        # of serving an app that exits instantly and browser-reload-loops.
        from nf_tui import gather_runs
        if not target.is_file() and not gather_runs(target):
            sys.exit(
                f"nf-tui-web: no .nextflow.log found under {target}\n"
                f"pass a run directory, e.g.  nf-tui-web /path/to/run")

    # NF_TUI_WEB tells the app it's served in a browser (no real terminal), so
    # it offers in-pane "full file" viewing instead of shelling out to less.
    # NF_TUI_PID goes in the command line rather than relying on the browser
    # session inheriting our environment, so K can stop the run we started.
    env = "NF_TUI_WEB=1"
    if proc is not None:
        env += f" NF_TUI_PID={proc.pid}"
    command = (
        f"{env} {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(HERE / 'nf_tui.py'))} {shlex.quote(str(target))}"
    )
    print(f"nf-tui web UI ({target}) on http://{args.host}:{args.port}  "
          f"(Ctrl-C to stop)")
    try:
        Server(command, host=args.host, port=args.port).serve()
    finally:
        if proc is not None and proc.poll() is None:
            print(f"\nnextflow still running (PID {proc.pid}).")
            print(f"  re-open viewer:  nf-tui {target}")
            print(f"  stop pipeline:   kill {proc.pid}")


if __name__ == "__main__":
    main()
