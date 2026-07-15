# nf-tui demo — a scripted terminal recording.
# Render with:   vhs demo.tape      (produces demo.gif)

Output demo.gif
# Output demo.mp4        # uncomment for an MP4 instead

Set FontSize 16
Set Width 1500
Set Height 850
Set Padding 14
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms

# --- setup (hidden): make sure nf-tui is on PATH ---
Hide
Type "export PATH=$HOME/.local/bin:$PATH; clear"
Enter
Show

# --- launch: point it at a run directory ---
Type "nf-tui /Users/stav/scratch"
Sleep 600ms
Enter
Sleep 2.5s          # the run picker

# --- pick a run (first is highlighted) ---
Enter
Sleep 2s            # the live task tree, grouped by process

# --- browse tasks; the task log below follows the selection ---
Down 4
Sleep 1.5s
Down 4
Sleep 1.5s

# --- switch log views ---
Type "c"            # container log
Sleep 1.3s
Type "t"            # back to task log
Sleep 1.3s

# --- go to a GATK task that produced a CRAM (its task leaf) ---
Down 10
Sleep 1.5s

# --- files view; move to the BAM and open it (samtools decodes it) ---
Type "d"
Sleep 2s            # files view (first file auto-opens)
Down 2              # -> test-test_L1.sorted.bam
Enter
Sleep 4s            # samtools view decodes it in the task's container

# --- open the BAM full in `less` (lazy, real search/paging) ---
Type "L"
Sleep 4s            # docker + samtools | zless starts
Space               # page forward through the alignments
Sleep 1.3s
Space
Sleep 1.3s
Type "G"            # jump to the end
Sleep 1.3s
Type "q"            # quit less -> back into nf-tui
Sleep 1.5s

# --- full-screen the content pane ---
Tab                 # focus the content pane
Sleep 500ms
Type "z"
Sleep 1.5s
Escape              # restore the split
Sleep 800ms

# --- the whole run log ---
Type "g"
Sleep 2s

# --- quit ---
Type "q"
Sleep 1s
