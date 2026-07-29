# nf-tui demo — a live pipeline and a finished nf-core/sarek run.
# Render with:  vhs demo.tape   (produces demo.gif)
#
# Expects, under ~/scratch: a pipeline currently running (so the picker shows a
# ● running row and the queue view has something in flight) and the completed
# nf-core/sarek run.

Output demo.gif
Set FontSize 15
Set Width 1500
Set Height 860
Set Padding 14
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

Hide
Type "export PATH=$HOME/.local/bin:$PATH; cd ~/scratch; clear"
Enter
Show

# ── The run picker: every run under a directory, as a table ────────────────
Type "nf-tui"
Sleep 500ms
Enter
Sleep 4.5s

# Move down to the finished sarek run and open it.
Down
Sleep 2s
Enter
Sleep 3.5s

# ── Sort so the slowest process floats to the top ──────────────────────────
Type "s"
Sleep 3.5s

# ── Failure triage: jump to the failure and read why it failed ─────────────
Type "e"
Sleep 4.5s

# ── Filter the tree as you type ────────────────────────────────────────────
Type "/"
Sleep 700ms
Type "FASTQC"
Sleep 2.5s
Escape
Sleep 1.5s

# ── A task's output files, previewed in place ──────────────────────────────
# Clearing the filter rebuilds the tree, so step onto a task leaf first.
Down 2
Sleep 1.2s
Type "d"
Sleep 3.5s

# Step through a couple of outputs; each previews on the right.
Down
Sleep 2s
Down
Sleep 2.5s

# ── The whole run log, paged in less (lazy — instant on a huge file) ────────
Escape
Sleep 800ms
Type "g"
Sleep 2s
Type "L"
Sleep 3s
Space
Sleep 1.5s
Type "q"
Sleep 1.8s

Type "Q"
Sleep 1.2s

# ── Now a pipeline that is still running: progress, throughput, ETA … ──────
Type "nf-tui ~/scratch/demo_live"
Sleep 400ms
Enter
Sleep 4.5s

# ── … and what's actually running right now, squeue-style ──────────────────
Type "p"
Sleep 5s

Type "Q"
Sleep 1.5s
