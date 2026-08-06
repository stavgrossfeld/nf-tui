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
  in `less`; panes load more as you scroll, and `F` pulls a bigger chunk of a
  file at once (the browser's `L`).

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
- **Huge task outputs are read as streams.** The in-pane preview did
  `read_text().splitlines()[:cap]` and the JSON report did `read_text()[-limit:]`,
  so both materialised the whole file before discarding nearly all of it —
  measured at 889 MB and 494 MB of peak RSS on a 226 MB file. A 10 GB output
  (which pipelines do produce) would have exhausted the host before a line
  appeared. Both now cap while reading: on a 9.3 GB file they return in ~2 ms
  with peak RSS flat at 43 MB, and previewing it in the UI takes 0.68 s.
  Paging with `L` was never affected — `less` is handed seekable files directly,
  and opens a 9.3 GB log at its tail in 16 ms.
- **File previews grow as you scroll.** The pane used to decide up front how
  much of a file to materialise, and `F` ("full file") loaded up to 200,000
  lines in one go: on a 159 MB output that took **42 s and 719 MB of RSS, and
  still showed only a tenth of the file**, with no way to reach the rest
  in-pane — while the footer claimed it was "the whole file". Reaching the
  bottom now pulls in the next chunk, the way `less` streams. The same file
  opens in **0.35 s**, and twelve screens of scrolling cost 73 MB. `F` is now a
  bigger first bite rather than a different mechanism, and the run log's
  scroll-up backfill is unchanged.
- **BAM/CRAM and gzip scroll too.** They were the two formats left capped —
  a `samtools view` pipe and a deflate stream can't seek to a byte offset, so
  a BAM stopped dead at 500 lines saying "capped here", which for a genomics
  tool is the wrong file type to give up on. Both now resume by line count
  (re-decoding and dropping what's already shown), so a 33 MB BAM opens in
  1.1 s and each scroll pulls another 500 records in ~0.8 s. Verified against
  a real nf-core/sarek BAM through its own container.
- **Task logs backfill on scroll-up too.** They open at the tail (a runaway
  task can write gigabytes to `.command.log`) and scrolling to the top pulls in
  the previous chunk, the way the run log already did. Small logs are unaffected
  — they still load whole.
- **S3/GCS reads are bounded.** `remote_cat` buffered the entire object through
  the CLI and then stored it in a cache that never releases — for callers that
  only ever want the last 20 KB. It now streams and keeps a bounded tail.
- **The log scans no longer slurp the log.** `parse_log` runs on every refresh
  tick of a live run and `read_text().splitlines()` cost ~3.7x the file's size
  in peak RSS — 600 MB on a 161 MB `.nextflow.log`, every second — for a pass
  that never looks backwards. `parse_log` and `parse_errors` now stream:
  **600 MB → 42 MB**, and slightly faster. `parse_errors` became a state
  machine, with a cap so a malformed log with no timestamps can't turn one
  error block into the whole file.
- **`.command.sh` and the log follower are bounded too.** `_read_all` seeked
  instead of reading a whole script to keep its last 20 KB, and the follower
  pulled every byte appended since the last tick — a task dumping gigabytes into
  `.command.log` dragged all of it into the pane. It now catches up to the
  newest 4 MB.
- **`less` now says how to leave it.** Its status line reads
  `q quit / search G end h help`. Esc looks like it should work — it is what the
  TUI itself uses to step back — but it cannot be rebound: ESC is the first byte
  of every arrow and function key, so less waits after a lone ESC (the binding
  never fires) while a Down arrow's `ESC [ B` *does* match it and kills the
  pager. Measured both ways; saying how to quit is the only safe fix.
- **Quitting `less` on a huge file took a minute.** Landing at end-of-file makes
  less number every line, and `q` blocks until that finishes. On a 10 GB task
  output `less -R +G` painted the tail in 0.11 s and then took **56 s to exit**,
  which looked exactly like a frozen TUI; pressing `G` in an ordinary `less -R`
  cost 52 s on the way out. Files past 32 MB now get `-n`, and both quit in
  0.2 s. Below that, line numbers stay on, so `=`, `v` and `1234G` keep working
  where counting is free. Found by driving the real binary against a 10 GB file
  in a live nf-core/sarek work dir.

### Packaging

- Ships `nf_tui`, `nf_tui_serve` and `nf_tui_run`. The latter two were
  previously installed as top-level `serve` and `run`, which collide with
  existing PyPI packages of those names.
- `nf-tui-run --help` exits 0 (it was exiting 1).
- CI covers Python 3.10–3.14 and installs the built wheel into a clean
  environment to check every entry point starts.
