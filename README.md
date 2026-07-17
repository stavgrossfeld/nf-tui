# nf-tui

[![CI](https://github.com/stavgrossfeld/nf-tui/actions/workflows/ci.yml/badge.svg)](https://github.com/stavgrossfeld/nf-tui/actions/workflows/ci.yml)

A terminal (and web) UI for browsing [Nextflow](https://www.nextflow.io/) runs —
tasks, logs, and output files — parsed live from a run's `.nextflow.log`.
No plugin, no re-run, no Seqera Platform. Point it at a run directory and go.

![nf-tui demo](demo.gif)

## Features

- **Live task tree** grouped by process, with per-process progress and
  status (`✓` / `✗` / running), updated on a timer while a pipeline runs.
- **Per-task logs** — task output (`.command.log` with container-pull and
  JVM/Fontconfig noise filtered out) or the raw container log.
- **Run log** — opens on it, at the tail, where a run says how it went; a live
  run follows new lines as they land. Scroll up and earlier lines backfill a
  chunk at a time, back to the first line of the run. `L` pages the whole file
  in `less`.
- **Output files** — browse a task's work-dir files with sizes; preview text
  and gzip on the host, and **BAM / CRAM / BCF decoded with `samtools` /
  `bcftools` from the task's own container** (reusing its mounts, so the
  reference genome resolves). Press `L` to open any file full in `less`.
- **Run picker** — with no path, it finds every run under a directory and
  lets you choose (and hop between runs without quitting).
- **Find any task** — `/` filters the tree by process name or hash as you
  type; `x` filters to failed tasks; `z`/`m` full-screens any pane; `o` opens
  a task's work dir.
- **Web mode** — the same UI in a browser via `nf-tui-web`, streamed with
  [textual-serve](https://github.com/Textualize/textual-serve).

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/stavgrossfeld/nf-tui
```

or from a clone:

```bash
git clone https://github.com/stavgrossfeld/nf-tui
uv tool install ./nf-tui
```

This puts `nf-tui` and `nf-tui-web` on your PATH. (Plain `pip install .` works too.)

## Usage

```bash
nf-tui                       # search the current directory, pick a run
nf-tui /path/to/run          # open a run directory (or a .nextflow.log)
nf-tui-web /path/to/run      # same UI in a browser (http://localhost:8000)

# launch a pipeline AND watch it live, in one step:
nf-tui-run nf-core/sarek -profile test,docker --outdir results
```

`nf-tui-run` passes its arguments to `nextflow run`, starts it in the
background (console output goes to `.nf-tui-run.out`), and opens nf-tui on the
new run's `.nextflow.log` — updating live as tasks complete. Quitting nf-tui
(`q`) leaves the pipeline running; it prints the PID and how to follow or stop
it. (Equivalent by hand: `nextflow run … & nf-tui .nextflow.log`.)

## On an HPC / remote server

nf-tui only reads a run's files, so it works anywhere the run directory is
reachable — no daemon, no root. Install it in your user space and run it on the
login node against a run on shared storage (`/scratch`, `$WORK`, …):

```bash
# one-time, in your home (no admin needed)
uv tool install git+https://github.com/stavgrossfeld/nf-tui
# or: pip install --user git+https://github.com/stavgrossfeld/nf-tui

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

Notes for clusters:

- **Singularity / Apptainer** are supported — nf-tui reuses each task's own
  container invocation from `.command.run` (image + binds), so BAM/CRAM viewing
  works with whatever engine the pipeline used. The TUI itself needs no
  container engine; only viewing BAM/CRAM does.
- **Shared filesystems** (Lustre/GPFS/NFS) cache file metadata, so live updates
  may lag a few seconds behind the pipeline — that's the filesystem, not nf-tui.
- **`L` (external `less`)** works in the terminal, not the browser; use the
  in-pane preview in the web UI.

## Keys

| Key | Action |
|-----|--------|
| `↑`/`↓`, `→`/`←` | move / expand in the task tree |
| `/` | filter the tree by task name or hash — `Enter` keeps it, `esc` clears |
| `t` / `c` / `g` | task log / container log / full run log |
| `d` | files view — `↑`/`↓` to pick, `Enter` to preview |
| `L` | open the selected file — or the whole run log — in `less` (lazy paging + search) |
| `Space` / `PageDown` · `b` / `PageUp` · `G` / `Home` | page / jump in a log |
| `z` / `m` | full-screen (maximize) the focused pane (`z`/`m`/`esc` to restore) |
| `Tab` | cycle focus between panes |
| `x` | show failed tasks only |
| `o` | open the task's work directory |
| `esc` | step back (content → list → tree → run picker) |
| `q` | quit |

## How it works

nf-tui parses `.nextflow.log` (the `TaskHandler[...]` and `Submitted process`
lines) for each task's hash, status, exit code, and work directory — so it
works on any completed or in-progress run without a plugin. File viewers reuse
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
