# nf-tui demo — a real pipeline, recorded in real time.
#
# Nothing here is sped up or stitched together: the pipeline is launched from
# the prompt and the recording simply watches it run, so the progress bar, the
# task tree, the throughput and the queue all move at the speed they really do.
#
# Render with:  vhs demo.tape && ./make_demo.sh
#   vhs records at 2x into demo_raw.mp4; make_demo.sh adds the camera
#   moves and writes demo.gif. Recording big keeps the zooms sharp.
# Expects ~/scratch/demo_live/main.nf — a 40-task pipeline where task 9 fails.

Output demo_raw.gif
Set FontSize 30
Set Width 3000
Set Height 1600
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

Hide
Type "export PATH=$HOME/.local/bin:$PATH; cd ~/scratch/demo_live; rm -rf work .nextflow* run.out; clear"
Enter
Sleep 2s
Show

# ── Launch it and watch: just prefix your nextflow command ─────────────────
Type "nf-tui nextflow run main.nf -process.maxForks 4"
Sleep 700ms
Enter

# Nextflow starts up, then the tree fills and the header climbs — all live.
Sleep 17s

# ── What's actually running right now (elapsed times tick up) ──────────────
Type "p"
Sleep 8s

# Back to the tree; a task has failed by now (red ✗).
Escape
Sleep 5s

# ── Jump straight to the failure and read why ──────────────────────────────
Type "e"
Sleep 8s

# ── Sort by slowest, still updating as the run continues ───────────────────
Type "s"
Sleep 4s

Type "Q"
Sleep 2s
