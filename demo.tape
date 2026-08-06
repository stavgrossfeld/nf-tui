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
Type "export PATH=$HOME/.local/bin:$PATH; cd ~/scratch/demo_sarek; docker run --rm $(docker images --format '{{.Repository}}:{{.Tag}}' | grep -m1 samtools) true >/dev/null 2>&1; clear"
Enter
Sleep 2s
Show

# ── Point it at a run and go ───────────────────────────────────────────────
# A finished run, opened directly. Deliberate: the tree's order follows the
# order Nextflow announced the tasks, which differs from run to run, so
# counted scrolling is only reliable against a fixed log. (`nf-tui nextflow
# run ...` launches and watches one live — that is in the README.)
Type "nf-tui .nextflow.log"
Sleep 800ms
Enter
Sleep 3s

# ── Walk down the tree to mark duplicates ──────────────────────────────────
# Just scrolling, no search. One key at a time with a gap: `Down 4` sends
# four presses back to back and Textual does not register them all, which
# left the cursor short of the task and showed an empty file list.
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Down
Sleep 260ms
Sleep 700ms

# ── Its task log ───────────────────────────────────────────────────────────
# .command.log, with the container-pull and JVM noise filtered out.
Type "t"
Sleep 4s

# ── Its outputs — and the BAM read through its own container ───────────────
Type "d"
Sleep 1600ms
Down
Sleep 900ms
Down
Sleep 1s
Enter
# Generous on purpose: this is a real `docker run` + samtools decode, and a
# cold start under vhs's render load takes several seconds. Overshooting costs
# nothing now — cap_still_runs squeezes whatever is left standing still.
Sleep 9s

# ── The run log: opens at the tail, pages back and keeps loading ───────────
Type "g"
Sleep 1400ms
Type "bbbb"
Sleep 2200ms

# ── What broke, and why ────────────────────────────────────────────────────
# Last, because it needs the run to have finished failing: pressed earlier it
# found nothing, since MULTIQC had not fallen over yet. e jumps to the next
# failed task and leads with Nextflow's own report — cause, exit status, and
# the .command.sh that ran.
Type "e"
Sleep 4500ms

Type "Q"
Sleep 1500ms
