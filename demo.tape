# nf-tui demo — a live nf-core/sarek run, then one task in full.
#
# Storyboard:
#   p            what is running vs queued right now, squeue-style
#   /bwamem1_mem the alignment task — it has a BAM and both fastqs
#   t            its .command.sh, then its .command.log
#   d            the files it produced, with sizes
#   Enter        one of them in the pane
#   L            the same file in less
#   q Q          out
#
# Navigation is by filter, not by counting Down presses. On a live run the tree
# follows the order Nextflow announced its tasks, which differs every time, so
# a fixed number of steps lands on a different process in every recording —
# that cost three takes before it was obvious.
#
# Every beat changes the screen. A still pane is dead air that smooth_retime
# squeezes out, so "sleep and admire" does not survive into the cut.
#
# Render with:
#   vhs demo.tape
#   REAL_SECS=<wall time> OPEN_UNTIL=<when the beats start> \
#     WIDTH=1100 MAX_HOLD=2.2 python smooth_retime.py demo_raw.gif demo.gif
#
# Expects docker running.

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

# Nothing is cached, so every task really executes: the tree fills with
# ✓ COMPLETED and real durations. smooth_retime runs this stretch at 14x.
# One wait, not two. The retimer fast-forwards a single opening stretch, so a
# second long sleep later in the tape plays at real speed — that put a full
# minute of dead air in the middle of the last cut. Everything that needs to
# happen before the beats happens here, at 14x.
Sleep 135s

# ── What is running right now, squeue-style ────────────────────────────────
# Nextflow's log cannot tell queued from executing — it records a task only
# when it finishes — so this reads .command.begin in each work dir.
Type "p"
Sleep 5s
Escape
Sleep 1200ms

# ── One task in full: the alignment, which has a BAM and both fastqs ───────
Type "/bwamem1_mem"
Sleep 1400ms
Enter
Sleep 700ms
Down
Sleep 1s

# ── Its .command.sh, then its .command.log ────────────────────────────────
# The task view leads with the script Nextflow generated, with the container
# noise filtered out of the output below it.
Type "t"
Sleep 4s
Type "l"
Sleep 800ms
Space
Sleep 2500ms
Space
Sleep 2500ms

# ── The files it produced, with sizes ─────────────────────────────────────
Type "d"
Sleep 2s
Down
Sleep 1200ms
Down
Sleep 1200ms
Down
Sleep 1400ms

# ── One of them, in the pane ──────────────────────────────────────────────
Enter
Sleep 6s

# ── The same file in less ─────────────────────────────────────────────────
Type "L"
Sleep 8s
Space
Sleep 2s
Type "q"
Sleep 1800ms

Type "Q"
Sleep 1500ms
