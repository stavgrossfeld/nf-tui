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

# ── Prefix any nextflow command to launch it and watch it live ────────────
Type "nf-tui nextflow run nf-core/sarek -profile test,docker --outdir out -resume"
Sleep 800ms
Enter

# The run starts and the tree fills in. smooth_retime runs everything up to the
# first task at 14x, so this costs a moment rather than the real wait.
Sleep 30s

# ── Scroll the tree ────────────────────────────────────────────────────────
# One key at a time with a gap: `Down 4` sends four presses back to back and
# Textual does not register them all.
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
Sleep 700ms

# ── Sort by peak memory: the heaviest process floats to the top ────────────
# Also how the demo finds mark duplicates on a *live* run without searching.
# Counting scroll steps cannot work here: the tree follows the order Nextflow
# announced its tasks, which differs run to run. Sorting does not care — mark
# duplicates peaks at 4.4 GB, several times anything else.
Type "s"
Sleep 1400ms
Type "s"
Sleep 1800ms
Up
Sleep 190ms
Up
Sleep 190ms
Up
Sleep 190ms
Up
Sleep 190ms
Up
Sleep 190ms
Up
Sleep 190ms
Up
Sleep 190ms
Up
Sleep 190ms
Up
Sleep 190ms
Up
Sleep 190ms
Sleep 500ms
Down
Sleep 900ms

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
