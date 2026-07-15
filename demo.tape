# nf-tui demo — exploring a real nf-core/sarek run.
# Render with:  vhs demo.tape   (produces demo.gif)

Output demo.gif
Set FontSize 16
Set Width 1500
Set Height 850
Set Padding 14
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms

Hide
Type "export PATH=$HOME/.local/bin:$PATH; clear"
Enter
Show

# Open a real nf-core/sarek run — defaults to the live run log + the task tree.
Type "nf-tui /Users/stav/scratch/.nextflow.log"
Sleep 600ms
Enter
Sleep 4s

# Browse to a GATK task that produced a CRAM.
Down 18
Sleep 1.5s

# Its task log …
Type "t"
Sleep 1.5s

# … then its output files.
Type "d"
Sleep 2s
Down 4                 # -> test.md.cram
Sleep 1s
Enter
Sleep 4s               # samtools decodes the CRAM in the task's own container

# Open the CRAM full in less (lazy, real paging + search).
Type "L"
Sleep 4s
Space
Sleep 1.3s
Space
Sleep 1.3s
Type "G"
Sleep 1.3s
Type "q"
Sleep 1.5s

# Full-screen the content pane, then restore.
Type "z"
Sleep 1.5s
Escape
Sleep 1s

Type "q"
Sleep 1s
