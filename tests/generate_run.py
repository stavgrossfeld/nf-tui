#!/usr/bin/env python3
"""Generate a synthetic Nextflow run for testing nf-tui at scale.

nf-tui reads a run's `.nextflow.log`, so a fabricated log with thousands of
`TaskHandler` lines is a faithful stress test without running a real pipeline.

    python tests/generate_run.py /tmp/bigrun --tasks 10000 --procs 50
    nf-tui /tmp/bigrun
"""
from __future__ import annotations

import argparse
import hashlib
import random
from pathlib import Path


def make_run(base: Path, n_tasks: int = 10_000, n_procs: int = 50,
             with_workdirs: int = 0, seed: int = 0) -> Path:
    """Write <base>/.nextflow.log describing n_tasks across n_procs processes.

    with_workdirs: create real work dirs (with .command.sh/.command.log) for
    the first N tasks, so the files view is exercisable; the rest are log-only.
    Returns the path to the written .nextflow.log.
    """
    rng = random.Random(seed)
    base = Path(base)
    (base / "work").mkdir(parents=True, exist_ok=True)
    procs = [f"NFCORE_TEST:FLOW:PROC_{i:03d}" for i in range(n_procs)]

    lines: list[str] = []
    for k in range(n_tasks):
        proc = procs[k % n_procs]
        tag = f"sample_{k:05d}"
        h = hashlib.md5(f"task-{k}".encode()).hexdigest()   # noqa: S324 (not security)
        wd = base / "work" / h[:2] / h[2:32]

        r = rng.random()
        if r < 0.88:
            status, exit_ = "COMPLETED", "0"
        elif r < 0.95:
            status, exit_ = "RUNNING", "-"
        else:
            status, exit_ = "COMPLETED", str(rng.randint(1, 137))   # failed

        if k < with_workdirs:
            wd.mkdir(parents=True, exist_ok=True)
            (wd / ".command.sh").write_text(f"#!/bin/bash\necho {tag}\n")
            (wd / ".command.log").write_text(f"processing {tag}\ndone\n")
            (wd / "out.txt").write_text(f"result for {tag}\n")

        lines.append(
            f"~> TaskHandler[id: {k + 1}; name: {proc} ({tag}); "
            f"status: {status}; exit: {exit_}; error: -; workDir: {wd}]"
        )

    log = base / ".nextflow.log"
    log.write_text("\n".join(lines) + "\n")
    return log


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a synthetic Nextflow run.")
    ap.add_argument("dir", help="output run directory")
    ap.add_argument("--tasks", type=int, default=10_000)
    ap.add_argument("--procs", type=int, default=50)
    ap.add_argument("--workdirs", type=int, default=0,
                    help="create real work dirs for the first N tasks")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    log = make_run(Path(args.dir), args.tasks, args.procs, args.workdirs, args.seed)
    print(f"wrote {log}  ({args.tasks} tasks, {args.procs} processes, "
          f"{log.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
