# nf-tui demo — launch a pipeline, then actually debug it.
#
# Resumed on purpose. A fresh sarek run takes minutes to reach the interesting
# tasks, and the alternative is a gif that spends most of itself waiting. With
# -resume the whole tree is there at once, which is also the routine case: you
# are usually looking at a run that has already been somewhere.
#
# The beats are the things you actually do when something breaks: jump to the
# failure and read why, then open one task — its task log, its container log,
# its outputs, and a BAM decoded through that task's own container.
#
# Every beat changes the screen. A still pane is dead air, and smooth_retime
# squeezes it out, so "sleep and admire" does not survive into the cut.
#
# Render with:
#   vhs demo.tape
#   REAL_SECS=<tape wall time> WIDTH=1100 MAX_HOLD=1.2 \
#     python smooth_retime.py demo_raw.gif demo.gif
#
# Expects docker running and a previous run in ~/scratch/demo_sarek to resume.

Output demo_raw.gif
Set FontSize 16
Set Width 1500
Set Height 820
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

Hide
Type "export PATH=$HOME/.local/bin:$PATH; cd ~/scratch/demo_sarek; clear"
Enter
Sleep 2s
Show

# ── Prefix any nextflow command to launch it and watch it ──────────────────
Type "nf-tui nextflow run nf-core/sarek -profile test,docker --outdir out -resume"
Sleep 800ms
Enter

# Nextflow starts and the tree fills in. smooth_retime runs everything up to
# the first task at 14x, so this costs the viewer a moment, not the real wait.
Sleep 26s

# ── What broke, and why ────────────────────────────────────────────────────
# e jumps to the next failed task and leads with Nextflow's own error report:
# the cause, the exit status, and the .command.sh that ran.
Type "e"
Sleep 3s

# ── One task in detail: mark duplicates ────────────────────────────────────
Type "/markdup"
Sleep 1200ms
Enter
Sleep 600ms
Down
Sleep 900ms

# its task output (.command.log, with container noise filtered out)
Type "t"
Sleep 2200ms

# the raw container log behind it
Type "c"
Sleep 1600ms

# ── Its outputs — and the BAM read through its own container ───────────────
Type "d"
Sleep 1400ms
Down
Sleep 900ms
Down
Sleep 1s
Enter
Sleep 3500ms

# ── The run log: opens at the tail, pages back and keeps loading ───────────
Type "g"
Sleep 1400ms
Type "bbbb"
Sleep 2200ms

Type "Q"
Sleep 1500ms
