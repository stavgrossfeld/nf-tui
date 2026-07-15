# nf-tui

A terminal (and web) UI for browsing [Nextflow](https://www.nextflow.io/) runs —
tasks, logs, and output files — parsed live from a run's `.nextflow.log`.
No plugin, no re-run, no Seqera Platform. Point it at a run directory and go.

![nf-tui demo](demo.gif)

## Features

- **Live task tree** grouped by process, with per-process progress and
  status (`✓` / `✗` / running), updated on a timer while a pipeline runs.
- **Per-task logs** — task output (`.command.log` with container-pull and
  JVM/Fontconfig noise filtered out), the raw container log, or the whole
  `.nextflow.log`.
- **Output files** — browse a task's work-dir files with sizes; preview text
  and gzip on the host, and **BAM / CRAM / BCF decoded with `samtools` /
  `bcftools` from the task's own container** (reusing its mounts, so the
  reference genome resolves). Press `L` to open any file full in `zless`.
- **Run picker** — with no path, it finds every run under a directory and
  lets you choose (and hop between runs without quitting).
- **Full-screen any pane**, filter to failed tasks, open a task's work dir.
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
```

On an HPC, run it on the login node against a run on shared storage; for the
web UI, forward the port: `ssh -L 8000:localhost:8000 login-node`.

## Keys

| Key | Action |
|-----|--------|
| `↑`/`↓`, `→`/`←` | move / expand in the task tree |
| `t` / `c` / `g` | task log / container log / full run log |
| `d` | files view — `↑`/`↓` to pick, `Enter` to preview |
| `L` | open the selected file full in `zless` (lazy paging + search) |
| `Space` / `PageDown` · `b` / `PageUp` · `G` / `Home` | page / jump in a log |
| `z` | full-screen the focused pane (`z`/`esc` to restore) |
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
present locally (Docker/Podman/Singularity). `L` (external `zless`) works in the
terminal, not the browser.

## License

MIT — see [LICENSE](LICENSE).
