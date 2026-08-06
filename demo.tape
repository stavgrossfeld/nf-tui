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
Type "export PATH=$HOME/.local/bin:$PATH; rm -rf ~/scratch/demo_sarek; mkdir -p ~/scratch/demo_sarek; cd ~/scratch/demo_sarek; for i in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep -i samtools); do docker run --rm $i true >/dev/null 2>&1; done; clear"
Enter
Sleep 2s
Show

# ── Prefix any nextflow command to launch it and watch it live ────────────
Type "nf-tui nextflow run nf-core/sarek -profile test,docker --outdir out"
Sleep 800ms
Enter

# A genuinely fresh run — nothing cached, so every task is really executing and
# the tree fills with ✓ COMPLETED and real durations rather than ⟲ CACHED.
# That takes a few minutes, which is what OPEN_UNTIL is for: smooth_retime runs
# this whole stretch at 14x, so the viewer watches it fill in seconds.
Sleep 210s

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
Sleep 260ms
Sleep 700ms

# ── Sort by peak memory: the heaviest process floats to the top ────────────
# Also how the demo reaches mark duplicates on a *live* run without searching.
# Counting scroll steps cannot work here: the tree follows the order Nextflow
# announced its tasks, which differs run to run. Sorting does not care — mark
# duplicates peaks near 5 GB, several times anything else.
#
# Nothing may move the cursor before this. `e` (jump to a failure) used to run
# here and threw it tens of lines down the tree, which the Ups below could not
# climb back from — the clip then opened a process group and showed an empty
# file list.
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
Sleep 3500ms

# ── Its outputs, and the BAM ───────────────────────────────────────────────
Type "d"
Sleep 1600ms
Down
Sleep 900ms
Down
Sleep 1s
Enter
# Generous on purpose: a real `docker run` + samtools decode, and a cold start
# under vhs's render load takes seconds. Overshooting costs nothing — the
# retimer squeezes whatever ends up standing still. (At 9s the pane was still
# reading "… loading …" when L took over.)
Sleep 15s

# ── The same BAM in less, full screen ──────────────────────────────────────
# L hands it to the pager: samtools decoding into less, paged lazily.
Type "L"
Sleep 8s
Space
Sleep 2s
Space
Sleep 2500ms
Type "q"
Sleep 1800ms

Type "Q"
Sleep 1500ms
