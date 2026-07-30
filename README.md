# nf-tui

[![CI](https://github.com/stavgrossfeld/nf-tui/actions/workflows/ci.yml/badge.svg)](https://github.com/stavgrossfeld/nf-tui/actions/workflows/ci.yml)

A terminal (and web) UI for browsing [Nextflow](https://www.nextflow.io/) runs —
tasks, logs, and output files — parsed live from a run's `.nextflow.log`.
No plugin, no re-run, no Seqera Platform. Point it at a run directory and go.

![nf-tui demo](demo.gif)

## Features

- **Live task tree** grouped by process, coloured by state (`✓` green /
  `✗` red / `⟲` cached / running) and laid out in fixed columns so durations
  and peak memory line up down the pane. Peak memory over 4 GB is highlighted,
  which is usually what you're looking for when hunting an OOM.
- **Works with `-resume`** — cached tasks are shown (`⟲`) with their work dirs
  resolved, so a resumed run's logs, outputs and metrics stay browsable. Retries
  (`errorStrategy 'retry'`) and `storeDir` tasks are shown too.
- **Every executor** — local, SLURM/PBS, k8s and AWS Batch all name their task
  handler `*TaskHandler`, which is what the parser keys on, and the grid form
  (which appends `started:`/`exited:` after the work dir) is handled. Parsing
  and container command construction are covered by tests built from
  Nextflow's own formats; they have not yet been exercised against a live
  scheduler, so reports from a real cluster are welcome.
- **Resource metrics** — each finished task shows its duration and peak memory
  (from `.command.trace`); `s` sorts to float the slowest / hungriest process
  to the top, so the bottleneck is one keypress away.
- **Per-task logs** — task output (`.command.log` with container-pull and
  JVM/Fontconfig noise filtered out) or the raw container log.
- **Failure triage** — `e` jumps to the next failed task and leads with *why*
  it failed: Nextflow's own error report (cause, exit status, the command, and
  stderr), lifted out of the run log and shown above the task's output.
- **Run log** — opens on it, at the tail, where a run says how it went; a live
  run follows new lines as they land. Scroll up and earlier lines backfill a
  chunk at a time, back to the first line of the run. `L` pages the whole file
  in `less`.
- **Output files** — browse a task's work-dir files with sizes; preview text
  and gzip on the host, and **BAM / CRAM / BCF decoded with `samtools` /
  `bcftools` from the task's own container** (reusing its mounts, so the
  reference genome resolves). Press `L` to open any file full in `less`.
- **Live progress** — the header tracks `done/total (%)`, and while a pipeline
  runs it adds what's in flight, throughput, and an ETA for the queued work:
  `15/20 seen (75%) · 5 running · 41.8/min · ~7.2s for queued`. Mid-run totals
  are labelled *seen*, not *tasks* — Nextflow only announces tasks as channels
  emit them, so the denominator is still growing.
- **Queue view** (`p`) — a `squeue`-style list of what's in flight: running
  tasks first with live elapsed times, then pending, with counts and
  throughput. Nextflow's log can't tell queued from executing (it records a
  task's outcome only when it finishes), so nf-tui reads `.command.begin` in
  each work dir — the file Nextflow writes the moment a task starts.
- **Run picker** — with no path, it finds every run under a directory and
  lets you choose (and hop between runs without quitting). Each run is marked
  **running** / **stalled** / **complete** / **failed** and shows its task
  count, percent done, and failures, so a crashed run (killed, OOM, dead node)
  is obvious at a glance.
- **Find any task** — `/` filters the tree by process name or hash as you
  type; `x` filters to failed tasks; `z`/`m` full-screens any pane; `o` opens
  a task's work dir.
- **JSON for agents and scripts** — `nf-tui --json` prints the whole run as
  data: progress, every task with its state and resource metrics, why each
  failure happened, and the `.command.*` files nested per task. Debugging a
  failed run needs neither a terminal nor a walk through the work tree.
- **Cloud executors (AWS Batch, Google Batch)** — the work tree lives in object
  storage, and nf-tui reads it there: select a task and it fetches that task's
  log and lists its outputs through your own `aws` / `gcloud` CLI, so a cloud
  run is as browsable as a local one. No SDK is added; fetches are for the
  selected task only, in the background, and cached. Everything from
  `.nextflow.log` — tasks, statuses, exit codes, progress, failure reports —
  needs no cloud access at all.
- **Web mode** — the same UI in a browser via `nf-tui-web`, streamed with
  [textual-serve](https://github.com/Textualize/textual-serve). `F` loads a
  whole file in-pane there, since the browser has no terminal for `less`.

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install nf-tui
```

or with pip:

```bash
pip install nf-tui
```

This puts `nf-tui`, `nf-tui-web` and `nf-tui-run` on your PATH. To install the
development version instead: `uv tool install git+https://github.com/stavgrossfeld/nf-tui`.

## Usage

```bash
nf-tui                       # search the current directory, pick a run
nf-tui /path/to/run          # open a run directory (or a .nextflow.log)
nf-tui-web /path/to/run      # same UI in a browser (http://localhost:8000)

# launch a pipeline AND watch it live — just prefix your nextflow command:
nf-tui nextflow run nf-core/sarek -profile test,docker --outdir results
```

Anything after `nf-tui nextflow` is passed to Nextflow verbatim, including
options that belong before `run` (`-log`, `-C`, …). `nf-tui-run <args>` is the
older spelling and still works.

Nextflow runs in the background (console output goes to `.nf-tui-run.out`) and
nf-tui opens on the new run's `.nextflow.log`, updating live as tasks complete.
Quitting (`Q`) leaves the pipeline running and prints the PID and how to follow
or stop it.

**Stopping a run.** Nextflow has no `cancel` command — you signal the process.
Press `K` in nf-tui to stop a pipeline it launched (it asks first). That sends
`SIGTERM`, which is the signal Nextflow handles: its handler kills the running
tasks, and on a scheduler cancels the jobs it queued. `SIGINT` is ignored, and
`kill -9` skips the handler and strands submitted jobs.

## JSON output

```bash
nf-tui --json /path/to/run              # the whole run, as data
nf-tui --json --failed /path/to/run     # only what broke
nf-tui --json --logs all /path/to/run   # every task's .command.* files
nf-tui --json --watch 5 /path/to/run    # one object per line while it runs
```

Each task carries its `hash`, `process`, `tag`, `status`, `state`
(running / pending / done / failed / cached), `exit`, `workdir`, resource
`metrics`, the failure `error` (cause plus Nextflow's full report), and its
`logs` — `.command.sh`, `.command.out`, `.command.err`, `.command.log`. By
default only failed tasks carry logs, which is the debugging case and cheap;
`--logs all` includes them everywhere.

`progress.total_is_final` says whether the run is still growing: mid-run
Nextflow has only announced the tasks it has submitted so far, so `pct` is of
what has been seen, not of the finished pipeline.

## On an HPC / remote server

nf-tui only reads a run's files, so it works anywhere the run directory is
reachable — no daemon, no root. Install it in your user space and run it on the
login node against a run on shared storage (`/scratch`, `$WORK`, …):

```bash
# one-time, in your home (no admin needed)
uv tool install nf-tui
# or: pip install --user nf-tui

nf-tui /scratch/$USER/my-run          # watch a run over SSH
```

**Tunnel the web UI to your laptop.** Serve it on the login node and forward the
port — you get the full UI in a local browser, no X11:

```bash
# on the login node (inside tmux/screen so it survives disconnects):
nf-tui-web /scratch/$USER/my-run --host 127.0.0.1 --port 8000

# on your laptop:
ssh -L 8000:localhost:8000 you@login-node
#   then open http://localhost:8000
```

### Running on AWS without Seqera Platform

Launching on AWS is plain Nextflow — set a Batch queue and an S3 work dir in
config and run it; the Platform is optional. nf-tui then watches that run from
wherever Nextflow is running (your laptop, or a small EC2 box):

```bash
nf-tui nextflow run main.nf -profile awsbatch -w s3://my-bucket/work
```

Task states, progress, throughput and failure reports come from the local
`.nextflow.log`. Per-task logs and outputs are pulled from S3 on demand via your
configured `aws` CLI, so nothing extra needs installing or authenticating.

One thing that stays local-only: telling *running* from *queued* uses
`.command.begin` in each work dir, and probing that per task over S3 on every
refresh would be far too slow — cloud tasks in flight are reported together
rather than split.

Notes for clusters:

- **Singularity / Apptainer** — nf-tui reuses each task's own container
  invocation from `.command.run` (image + binds), including the environment
  prefix Nextflow puts in front of it, so BAM/CRAM viewing works with whatever
  engine the pipeline used. The TUI itself needs no container engine; only
  viewing BAM/CRAM does.
- **Shared filesystems** (Lustre/GPFS/NFS) cache file metadata, so live updates
  may lag a few seconds behind the pipeline — that's the filesystem, not nf-tui.
- **`L` (external `less`)** works in the terminal, not the browser; use the
  in-pane preview in the web UI.

## Keys

| Key | Action |
|-----|--------|
| `↑`/`↓`, `→`/`←` | move / expand in the task tree |
| `/` | search — filters the task tree, or finds text in the log if that pane is focused |
| `n` / `N` | next / previous log match |
| `t` / `c` / `g` | task log / container log / full run log |
| `d` | files view — `↑`/`↓` to pick, `Enter` to preview |
| `s` | cycle sort — submission order → slowest → peak memory (heaviest process on top) |
| `e` | jump to the next failed task and show why it failed |
| `p` | queue view — what's running vs pending right now, with elapsed times |
| `L` | open in `less` — the task's `.command.log`, the whole run log, or the selected file (lazy paging + search) |
| `F` | load the whole selected file in-pane (uncapped; the browser's `L`) |
| `Space` / `PageDown` · `b` / `PageUp` · `G` / `Home` | page / jump in a log |
| `z` / `m` | full-screen (maximize) the focused pane (`z`/`m`/`esc` to restore) |
| `Tab` | cycle focus between panes |
| `x` | show failed tasks only |
| `y` | copy the path — the selected output file's, or the task's work dir |
| `o` | open the task's work directory |
| `esc` | step back (content → list → tree → run picker) |
| `K` | stop the pipeline nf-tui launched (asks first; sends SIGTERM) |
| `Q` | quit (Shift+Q, so a stray `q` can't drop a live session) |

## How it works

nf-tui parses `.nextflow.log` for each task's hash, status, exit code, and work
directory — so it works on any completed or in-progress run without a plugin.
It reads both the `TaskHandler[...]` lines (any executor: the class name always
ends in `TaskHandler`) and all four of Nextflow's task announcements —
`Submitted`, `Re-submitted`, `Cached`, and `Stored`. That last pair matters:
a `-resume` run logs **only** `Cached process` lines and no handler lines at
all, so a parser that ignores them shows a resumed run as empty. Cached tasks
carry no work dir in the log, so nf-tui resolves them against the run's work
tree (honouring `-w`). File viewers reuse
the exact `docker`/`singularity` invocation from each task's `.command.run`
(image + mounts), swapping in a `samtools`/`bcftools` image from the run when a
task's own container doesn't ship the tool.

Requires Python ≥ 3.10. Viewing BAM/CRAM needs the run's container images
present locally (Docker/Podman/Singularity). `L` (external `less`) works in the
terminal, not the browser.

## Development

```bash
uv run --extra dev pytest        # run the test suite

# generate a synthetic run to poke at (or stress-test) by hand:
python tests/generate_run.py /tmp/bigrun --tasks 10000 --procs 50
nf-tui /tmp/bigrun
```

Tests cover parsing against the real Nextflow log format, the file viewers,
edge cases, and a 10,000-task scale check (parse < 0.5s, per-render < 50ms,
idle refresh ~free). The scale tests synthesize a `.nextflow.log` rather than
run 10k real tasks; `test_parse_matches_real_format` pins the parser to a
verbatim real log line so the synthetic stays faithful.

## License

MIT — see [LICENSE](LICENSE).
