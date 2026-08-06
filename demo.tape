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
Sleep 30s

# ── The first tasks appear and start completing, live ──────────────────────
Sleep 26s

# ── What's actually running right now ──────────────────────────────────────
Type "p"
Sleep 8s

Escape
Sleep 4s

# ── Walk the tree, then the outputs of a finished task ─────────────────────
# Beats here are kept *moving* on purpose. smooth_retime compresses a screen
# that isn't changing (that is how the dead air goes), so a long Sleep on a
# still pane gets squeezed to nothing. Stepping through tasks and files makes
# every frame differ, which is what survives into the cut.
Down
Sleep 700ms
Down
Sleep 700ms
Down
Sleep 700ms
Down
Sleep 900ms

# Step onto a completed task: pressing d on a process group shows an empty
# file list, since a group has no work dir of its own.
Type "d"
Sleep 1600ms

# Arrow down the file list — each move repaints the preview beside it.
Down
Sleep 1300ms
Down
Sleep 1300ms
Down
Sleep 1300ms
Enter
Sleep 2s

# ── The run log opens at the tail; page back and it keeps loading ──────────
Type "g"
Sleep 2500ms
Type "bbbb"
Sleep 3s

Type "Q"
Sleep 2s
