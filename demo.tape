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
#   ZOOM=0 REAL_SECS=84 OUT_W=1300 FF_UNTIL=45 FF_RATE=15 ./make_demo.sh
#
# FF_UNTIL covers Nextflow's boot so the clip reaches the first task in ~4s.
# It is expressed in the corrected timeline, which is longer than the boot's
# wall clock (~33s here): vhs drops more frames while the screen is static, so
# the boot occupies a larger share of the stretched clip than of real time.
# If a re-recording boots slower, raise it — check when tasks appear with:
#   ffmpeg -ss 4 -i demo.gif -frames:v 1 /tmp/f.png
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

# ── The outputs of a task that has finished ────────────────────────────────
# Step onto a completed task first: pressing d on a process group shows an
# empty file list, since a group has no work dir of its own.
Down 2
Sleep 1.5s
Type "d"
Sleep 5s
Down
Sleep 1s
Enter
Sleep 5s

Type "Q"
Sleep 2s
