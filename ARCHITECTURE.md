# How nf-tui works

nf-tui reads two things Nextflow writes as a side effect of running, and nothing
else: the run's **`.nextflow.log`**, and the **work directories** it mentions.
There is no plugin, no hook and no API — which is why it works on a run someone
else launched, a run that has already finished, and a run on a machine you only
have read access to.

The trade is that those are incidental formats rather than a contract. Every
format nf-tui depends on is therefore pinned by a test built from Nextflow's own
output, so a change upstream fails loudly instead of quietly showing you
nothing. Several of those tests exist because exactly that happened.

Everything lives in `nf_tui.py` (one module, ~3k lines), with two thin entry
points beside it. What follows is the map.

---

## 1. Where a work directory comes from

This is the question everything else depends on: to show a task's log, outputs,
resource metrics or container, nf-tui has to know which directory is the task's.
There are three sources, in order of authority.

**From the log, when the task finishes.** `parse_log()` reads the handler line

```
TaskHandler[id: 42; name: FASTQC (test); status: COMPLETED; exit: 0;
            error: -; workDir: /scratch/work/88/41d2bab240fd98…]
```

`workDir:` is authoritative. It is also the *only* place the full path appears —
so before a task completes, nf-tui does not know it.

Two details that were bugs first:

- On a scheduler the field is **not last**. `GridTaskHandler` appends
  `; started: …; exited: …` after it, so the value ends at a semicolon, not at
  the closing bracket. Reading to the bracket swallowed those into the path and
  silently broke every work-dir feature on every cluster run.
- On a cloud executor the value is an **`s3://…` URI**, kept as a string
  throughout. `Path("s3://bucket/work")` collapses the double slash to
  `s3:/bucket/work`, which no client can fetch.

**Resolved by hash, when the log hasn't said.** Two cases have no `workDir` in
the log at all:

- **in-flight tasks** — Nextflow records the directory on completion, so a
  running task has only its hash;
- **cached tasks** — a `-resume` run logs `[ab/333333] Cached process > …` and
  no handler line ever follows.

`resolve_workdir(root, "ab/cdef12")` lists just `<root>/ab/` and matches the
prefix — targeted, because scanning the whole tree on every refresh of a live
run would be far too expensive. `index_workdirs()` does the whole-tree version
once, for the cached case. Results are cached; a work dir never moves.

**The root itself**, needed by the above, comes from `find_work_root()`:
`-w` / `-work-dir` on the launch command, else the nf-core `workDir : <path>`
banner line, else Nextflow's default `<launch dir>/work`.

---

## 2. Parsing the log

| function | what it does |
|---|---|
| `iter_lines(path)` | stream a file's lines — what every log scan reads through |
| `parse_log(log)` | the whole run as `Task` objects — the core of everything |
| `split_name(name)` | `"NFCORE:SAREK:FASTQC (test)"` → `("NFCORE:SAREK:FASTQC", "test")` |
| `is_done` / `is_failed` | status/exit interpretation, including `CACHED`/`STORED` as done |
| `parse_errors(log)` | failed task → Nextflow's `Error executing process` block |
| `error_summary(block)` | the one line worth showing: the `Caused by:` reason |
| `progress_of(tasks)` | counts, throughput and a queue ETA (`Progress`) |
| `_line_time(line)` | a log line's timestamp, for throughput |

`parse_log` handles **four** ways Nextflow announces a task, not one:

```
[ab/111111] Submitted process    > …     ← ran now
[ab/222222] Re-submitted process > …     ← a retry (errorStrategy)
[ab/333333] Cached process       > …     ← -resume reused it
[skipping]  Stored process       > …     ← storeDir
```

Missing `Cached` meant a resumed run rendered **completely empty** — the most
serious bug this project has had, since `-resume` is routine. `Stored` lines
carry the literal `[skipping]` instead of a hash, so those are keyed by name or
they all collapse into one entry.

Handler lines are matched on the `*TaskHandler[` suffix, which covers every
executor: `LocalTaskHandler`, `GridTaskHandler`, `CachedTaskHandler`,
`AwsBatchTaskHandler` and the rest all end that way.

---

## 3. Task state: what the log cannot tell you

Nextflow writes a handler line when a task **completes**. Until then every
in-flight task looks identical — `SUBMITTED` — so the log alone cannot separate
*queued* from *executing*.

`task_state(task)` resolves it from the filesystem instead:

- `.command.begin` exists → **running** (Nextflow writes it as the task starts)
- work dir staged, no `.command.begin` → **pending**
- `.exitcode` / handler line → **done** or **failed**

Verified against runs pinned to `maxForks 3` and `maxForks 5`: exactly 3 and
exactly 5 reported running. `task_started_at()` reads that file's mtime for the
elapsed times in the queue view.

`IN_FLIGHT` is the shared definition of "not finished" — `NEW`, `SUBMITTED`,
`RUNNING`. `NEW` is easy to miss and real: a live run carries plenty of them,
and omitting it made the queue view report "0 running · 0 pending" while the
tree plainly listed three.

---

## 4. Resource metrics

`parse_trace(workdir)` reads `.command.trace`, Nextflow's per-task resource
dump, into `Metrics` — `realtime`, `%cpu`, `peak_rss`, `%mem`. Absent when
tracing is off, so every use degrades quietly. Misses are cached (keyed by the
status they were read at): without that, a run with no trace files re-opened
every task on every refresh, about 35 ms per tick at 10k tasks.

---

## 5. Containers, for decoding BAM/CRAM

To show a BAM as text nf-tui needs `samtools`, and it reuses the task's *own*
container so the reference genome resolves exactly as it did for the task.

| function | what it does |
|---|---|
| `parse_container_run(workdir)` | `(engine, mounts, image)` out of `.command.run` |
| `task_container(workdir)` | just `(engine, image)`, for labels |
| `find_tool_image(dir, "samtools")` | an image in this run that has the tool |
| `_image_has(engine, image, bin)` | does it *actually* — checked, not guessed |
| `decode_tool(path)` | `.bam`/`.cram` → `samtools view -h`, `.bcf` → `bcftools view` |

Two things that were wrong here:

- The invocation is **not at the start of the line**. Nextflow prefixes the
  Singularity one with `set +u; env - PATH=… singularity exec …`, so matching a
  line that *starts* with `singularity` found nothing — Singularity support
  never worked at all.
- An image whose name mentions the tool need not contain it. A real run picked
  `htslib:1.21` for samtools; htslib ships `tabix` and `bgzip`, not samtools, and
  the viewer ran a doomed command. Candidates are now probed with
  `command -v` inside the image.

---

## 6. Cloud work dirs

When the work tree is in object storage (AWS Batch, Google Batch), the files are
not on the machine at all. `remote_scheme(uri)` detects it, and reads go through
the user's own CLI — no SDK, so nf-tui stays dependency-free:

| function | what it does |
|---|---|
| `remote_tool(scheme)` | the CLI for `s3`/`gs`, if installed |
| `remote_cat(uri)` | object contents as text, cached |
| `remote_ls(uri)` | `(name, size)` under a prefix |
| `remote_forget(prefix)` | drop cached reads, so a live log re-reads |

A call costs hundreds of milliseconds, so **none of this may touch the refresh
path**: fetches are for the selected task only and run in worker threads
(`_fetch_remote_log`, `_fetch_remote_files`, `_fetch_remote_object`). For the
same reason, running-versus-queued is not split for cloud runs — probing
`.command.begin` per task per second over S3 is not viable.

---

## 7. The app

`NfScope(App)` holds one log file, a task tree, and one content pane that
switches between views. `self.view` is `task | container | files | run | queue`.

**The refresh loop** (`_tick` → `action_refresh`, every second) is the spine:

1. skip everything if `.nextflow.log` is unchanged (the common steady state);
2. `parse_log` → group by process → sort if asked;
3. build the header summary and progress bar;
4. `_sync_tree()` updates the tree **in place** — appending and relabelling,
   never clearing, so the cursor, focus and scroll position survive. A full
   `_full_rebuild()` happens only when the filter or sort changes, because the
   in-place path cannot reorder or remove;
5. `_render_current()` redraws the pane for the current view.

**The views**

| view | built by | notes |
|---|---|---|
| run log | `_show_run_log`, `_paint_runlog`, `_runlog_backfill` | opens at the tail; scrolling up backfills the file a chunk at a time |
| task / container log | `_load_task`, `_emit_view` | tails `.command.log`; leads with the failure report for a failed task |
| files | `_populate_files`, `_open_file`, `_run_viewer`, `_preview_extend` | previews text/gzip on the host, BAM/CRAM through the container; plain text grows as you scroll |
| queue | `_show_queue` | `squeue`-style, running first with elapsed times |
| picker | `RunPickerScreen` | a `DataTable` of every run found under a directory |

Two rendering rules learned the hard way, both still load-bearing:

- **A big synchronous write inside an event handler does not paint** in a real
  terminal. The run log is written via `call_after_refresh`; that is why
  `_paint_runlog` exists separately from `_show_run_log`.
- **`RichLog` can only append.** Backfilling earlier log lines rewrites the pane
  and shifts the viewport down by exactly the number of lines prepended, which
  is what keeps the line you were reading under your eye. Growing *forward* is
  the cheap direction — `_preview_extend` just appends, no rewrite — which is
  why a file preview can keep loading as you scroll while the run log's upward
  backfill has to redraw.

**Nothing decides up front how much of a file to show.** `read_forward` returns
`(next_offset, lines, at_eof)`, so a preview resumes exactly where it stopped
and grows when you reach the bottom. The alternative — loading a fixed large cap
in one go — measured 42 s and 719 MB on a 159 MB file and still showed a tenth
of it. Gzip is the exception: a deflate stream cannot be resumed from a byte
offset without decompressing from the start, so that path stays capped and says
so.

**Following** only happens while the pane is parked at the bottom. Otherwise
every arriving line yanked the viewport back and made scrolling up impossible
during a live run.

**The log is scanned as a stream.** `parse_log` re-reads `.nextflow.log` on
every tick of a live run, so `read_text().splitlines()` was paying ~3.7x the
file's size in peak RSS once a second — 600 MB on a 161 MB log. Everything now
reads through `iter_lines`, which holds one line at a time (600 MB → 42 MB).
`parse_errors` became a forward state machine for the same reason: a block runs
from an `Error executing process` line to the next timestamped one, which needs
state rather than lookahead.

**Every host read is capped while reading, not after.** Tasks emit outputs of
arbitrary size, so `head_text` / `head_gzip` stop at the line cap and
`_tail_text` seeks to the end. The obvious spellings — `read_text().splitlines()
[:cap]` and `read_text()[-limit:]` — were both in place and cost 889 MB and
494 MB of peak RSS on a 226 MB file; at 10 GB they exhaust the machine. Streaming
holds ~43 MB whatever the size. `less` needs no such care on plain files, since
it seeks: it opens a 9.3 GB file at the tail in 16 ms. It cannot seek a *pipe*,
which is why the gz path pages from the top rather than passing `+G` — with `+G`
a piped 10 GB decompression had painted nothing after 25 s.

**`less` is fast to open a huge file and slow to leave one.** Reaching EOF makes
it number every line, and `q` waits for the count: `less -R +G` on a 10 GB output
painted in 0.11 s then took 56 s to exit, which is indistinguishable from a hung
TUI. `pager_flags()` adds `-n` past 32 MB (0.2 s to quit) and keeps numbering
below it, where it is free and `=`/`v`/`1234G` still work.

---

## 8. The JSON report

`run_report(log, logs=…, failed_only=…)` is the same parsing the UI uses,
returned as data for agents and scripts: progress, every task with its state and
metrics, the failure cause and Nextflow's full report, and the `.command.*` files
nested per task. `nf-tui --json` prints it; `--watch` re-prints one object per
line while the run is live.

Logs default to failed tasks only — the debugging case, and it keeps the
document small on a big run. `progress.total_is_final` says whether the
denominator is still moving, because mid-run Nextflow has only announced the
tasks it has submitted so far.

Worth knowing why this exists next to `nextflow log`: that command is good, but
it **cannot read a running pipeline** (the cache DB is locked by the live
process) and its TSV breaks once you ask for multi-line fields like `stderr`.

---

## 9. Entry points

| module | command | what it does |
|---|---|---|
| `nf_tui.py` | `nf-tui` | the UI, and `--json`; `nf-tui nextflow run …` delegates |
| `nf_tui_run.py` | `nf-tui-run` | launches `nextflow run` in the background and opens the UI on the new run |
| `nf_tui_serve.py` | `nf-tui-web` | serves the same app over the web via textual-serve |

`nf_tui_run.launch()` waits for **this** run's log by watching for the inode to
change, because the directory usually already holds the previous run's
`.nextflow.log` — waiting for the file to merely exist opened the last, finished
run instead.

These two are named `nf_tui_*` rather than `run`/`serve` because both of those
are real packages on PyPI and would have collided on install.

---

## 10. Tests and the demo

The suite (79 tests across `tests/test_nf_tui.py` and
`tests/test_entrypoints.py`) is built around verbatim Nextflow output:
`test_parse_matches_real_format` pins a real sarek handler line,
`test_grid_handler_appends_fields_after_workdir` a real grid one, and the
Singularity tests a real `.command.run`. `tests/generate_run.py` synthesises
runs for the scale checks, since 10k real tasks per test is impractical — those
budgets are deliberately loose, because they exist to catch a complexity
regression, not to benchmark the machine.

`demo.tape` (vhs) plus `smooth_retime.py` produce `demo.gif`. The retimer exists
because vhs drops frames when it cannot render as fast as it captures, so its
raw output plays several times too fast; it also finds where the first task
appears and eases the playback rate down from there, rather than cutting.
