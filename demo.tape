# nf-tui demo — nf-core/sarek launched and watched from the very beginning.
#
# The pipeline is started from the prompt and the recording watches it submit
# its first tasks and fill in, at the speed it really happens. The one exception
# is Nextflow's own startup — about half a minute before it submits anything —
# which is run fast rather than cut, so you still see the run log streaming
# config and plugin loading without waiting through it.
#
# Render with:
#   vhs demo.tape
#   python smooth_retime.py demo_raw.gif demo.gif
#
# smooth_retime corrects vhs's dropped-frame timing, finds where the first task
# appears (so the boot window needs no tuning), runs the boot fast, and eases
# back to real time over a couple of seconds rather than cutting. It also caps
# how long any one frame may sit, so a still stretch never reads as the clip
# freezing (the previous cut held one frame for nine seconds).
#
# Expects docker running and the nf-core/sarek test data reachable.

Output demo_raw.gif
Set FontSize 16
Set Width 1500
Set Height 820
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

Hide
Type "export PATH=$HOME/.local/bin:$PATH; rm -rf ~/scratch/demo_sarek; mkdir -p ~/scratch/demo_sarek; cd ~/scratch/demo_sarek; clear"
Enter
Sleep 2s
Show

# ── Prefix any nextflow command to launch it and watch it ──────────────────
Type "nf-tui nextflow run nf-core/sarek -profile test,docker --outdir out"
Sleep 800ms
Enter

# Nextflow boots: the tree is empty and the run log streams its startup.
# smooth_retime runs everything up to the first task at 14x, so this wait costs
# the viewer a couple of seconds rather than the minute it really takes.
Sleep 30s

# ── The first tasks appear and start completing ────────────────────────────
Sleep 20s

# ── Straight into them: step down the tree, then open a finished task's files
# No passive staring from here on. A still screen is dead air the retimer will
# squeeze out anyway, so every beat below changes something.
Down
Sleep 600ms
Down
Sleep 600ms
Down
Sleep 600ms
Down
Sleep 800ms

Type "d"
Sleep 1400ms

# Arrow the file list — each move repaints the preview beside it.
Down
Sleep 1100ms
Down
Sleep 1100ms
Down
Sleep 1100ms
Enter
Sleep 1800ms

# ── What is running right now ──────────────────────────────────────────────
Type "p"
Sleep 2500ms
Escape
Sleep 800ms

# ── The run log opens at the tail; page back and it keeps loading ──────────
Type "g"
Sleep 1500ms
Type "bbbb"
Sleep 2500ms

Type "Q"
Sleep 2s
