#!/usr/bin/env python3
"""Serve nf-tui in a web browser instead of the terminal.

Same app, same code — textual-serve runs the terminal TUI on the server and
streams it to the browser over a websocket. No Seqera Platform involved.

    python serve.py /path/to/run            # then open http://localhost:8000
    python serve.py /path/to/run --port 9000 --host 0.0.0.0

On an HPC: run this on the login node, then SSH-forward the port
(`ssh -L 8000:localhost:8000 login-node`) and open http://localhost:8000 locally.
"""
import argparse
import shlex
import sys
from pathlib import Path

from textual_serve.server import Server

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser(prog="nf-tui-web",
                                 description="Serve nf-tui over the web.")
    ap.add_argument("run", nargs="?", default=".",
                    help="Nextflow run dir, .nextflow.log, or dir to search")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    # Resolve to an absolute path now (the served command runs with its own
    # working directory, so a bare "." would be ambiguous).
    target = Path(args.run).expanduser().resolve()

    # Pre-flight: if there are no runs here, say so in the terminal instead of
    # serving an app that exits instantly and makes the browser reload-loop.
    from nf_tui import gather_runs
    if not target.is_file() and not gather_runs(target):
        sys.exit(
            f"nf-tui-web: no .nextflow.log found under {target}\n"
            f"pass a run directory, e.g.  nf-tui-web /path/to/run")

    command = (
        f"{shlex.quote(sys.executable)} "
        f"{shlex.quote(str(HERE / 'nf_tui.py'))} {shlex.quote(str(target))}"
    )
    print(f"nf-tui web UI ({target}) on http://{args.host}:{args.port}  "
          f"(Ctrl-C to stop)")
    Server(command, host=args.host, port=args.port).serve()


if __name__ == "__main__":
    main()
