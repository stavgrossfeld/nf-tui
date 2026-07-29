# nf-tui demo — browsing an nf-core/sarek run's output files.
#
# Recorded against a real nf-core/sarek run. No camera moves; make_demo.sh only
# corrects the timing, because vhs drops frames when it can't render as fast as
# it captures and the raw capture otherwise plays too fast.
#
# Render with:  vhs demo.tape && ZOOM=0 ./make_demo.sh
# Expects the completed sarek run at ~/scratch/.nextflow.log.

Output demo_raw.gif
Set FontSize 16
Set Width 1500
Set Height 820
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

Hide
Type "export PATH=$HOME/.local/bin:$PATH; cd ~/scratch; clear"
Enter
Sleep 1s
Show

# ── Open the run: task tree with duration and peak memory, run log below ───
Type "nf-tui .nextflow.log"
Sleep 600ms
Enter
Sleep 5s

# ── Filter to one task. Its hash is unique; a process name would not be —
#    "MOSDEPTH" also matches SAMTOOLS_STATS, whose subworkflow path contains it.
Type "/"
Sleep 600ms
Type "1f/8e46a2"
Sleep 2.5s
Enter
Sleep 1.2s

# Step onto the task itself.
Down
Sleep 1.5s

# ── Its work directory: every output, with sizes and symlinks marked ───────
Type "d"
Sleep 4s

# A 44 MB CRAM — decoded by samtools from this task's own container, so the
# reference resolves exactly as it did for the task.
Down 2
Sleep 1s
Enter
Sleep 7s

# A plain text summary, read straight off the host.
Down 4
Sleep 800ms
Enter
Sleep 4s

# And a gzip, decompressed on the fly.
Down
Sleep 800ms
Enter
Sleep 4s

# ── Page it in less — lazy, so even a huge file opens at once ──────────────
Type "L"
Sleep 3.5s
Space
Sleep 1.5s
Type "q"
Sleep 2s

Type "Q"
Sleep 1.5s
