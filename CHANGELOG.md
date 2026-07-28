# Changelog

## 1.0.0 — first public release

The first release published to PyPI: `uv tool install nf-tui`.

### Nextflow coverage

- **`-resume` runs work.** A resumed run logs only `Cached process` lines and
  no `TaskHandler` lines at all, so a parser that ignores them shows the run as
  completely empty — which is what happened before. All four of Nextflow's task
  announcements are now read (`Submitted`, `Re-submitted`, `Cached`, `Stored`).
  Cached tasks carry no work dir in the log, so they're resolved against the
  run's work tree (honouring `-w`), keeping their logs and outputs browsable.
- **Retries** (`errorStrategy 'retry'`) are shown with an attempt count, and
  `storeDir` tasks no longer collapse into a single entry.
- **Every executor**: local, SLURM/PBS, k8s and AWS Batch all name their handler
  class `*TaskHandler`, which is what the parser keys on.

### Views

- Opens on the run log at the tail; live runs follow, and scrolling up backfills
  the file a chunk at a time, back to the first line.
- **Resource metrics** per task (duration, peak memory) from `.command.trace`,
  with `s` to sort the slowest or hungriest process to the top.
- **Failure triage** (`e`): jumps to the next failure and leads with Nextflow's
  own error report — cause, exit status, command, stderr.
- **Queue view** (`p`): a `squeue`-style list of what's running versus pending,
  with live elapsed times. The log can't distinguish the two (Nextflow records a
  task only when it finishes), so this reads `.command.begin` in each work dir.
- **Live progress** in the header: counts, throughput, and an ETA for queued
  work. Mid-run totals are labelled *seen*, not *tasks* — Nextflow announces
  tasks as channels emit them, so the denominator is still growing.
- **Run picker** as a table: state, age, task count, percent done, failures.
  Runs that stopped without finishing are marked *stalled*.
- `/` filters the task tree; `L` pages the task log, run log or any output file
  in `less`; `F` loads a whole file in-pane (the browser's `L`).

### Fixes worth naming

- Paging used `zless`, which pipes through `gzip` and so hands `less` a stream
  it cannot seek: `+G` had to read the whole file first. On a 138 MB log that
  never finished; `less` on the file opens in 0.02 s.
- Following the run log yanked the viewport back to the bottom on every new
  line, making it impossible to scroll up while a pipeline ran.
- `F`/`L` crashed on a container file when the tree cursor wasn't on a task.
- Switching runs kept the previous run's caches, so a task whose short hash
  collided showed the wrong metrics.
- A slow container decode landing after a view change repainted the wrong pane.

### Packaging

- Ships `nf_tui`, `nf_tui_serve` and `nf_tui_run`. The latter two were
  previously installed as top-level `serve` and `run`, which collide with
  existing PyPI packages of those names.
- `nf-tui-run --help` exits 0 (it was exiting 1).
- CI covers Python 3.10–3.14 and installs the built wheel into a clean
  environment to check every entry point starts.
