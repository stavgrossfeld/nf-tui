#!/usr/bin/env bash
# Turn vhs's raw capture into demo.gif: correct the timing, add camera moves.
#
# Two things this fixes/adds:
#
#  1. Real time. vhs drops frames when it can't render as fast as it captures
#     (worse at 2x resolution), then writes what it kept at a nominal 25fps —
#     so the raw capture plays several times too fast. We know how long the
#     tape actually takes, so the clip is stretched back to that wall time.
#
#  2. Camera. vhs has no zoom or pan, so the pushes onto the header, the queue
#     and the failure report are done here with zoompan. Recording at 2x means
#     zooming crops real pixels instead of upscaling mush.
set -euo pipefail

RAW=${1:-demo_raw.gif}
OUT=${2:-demo.gif}
REAL_SECS=${REAL_SECS:-44}        # wall time the tape actually takes
OUT_W=1100
FPS=10
COLORS=${COLORS:-48}   # fewer colours: a terminal needs very few, and it halves the file

[ -f "$RAW" ] || { echo "no $RAW — run: vhs demo.tape" >&2; exit 1; }

raw_secs=$(python3 - "$RAW" <<'PY'
import sys
from PIL import Image
im = Image.open(sys.argv[1]); total = 0
try:
    while True:
        total += im.info.get("duration", 40); im.seek(im.tell() + 1)
except EOFError:
    pass
print(total / 1000)
PY
)
stretch=$(python3 -c "print(f'{$REAL_SECS/$raw_secs:.4f}')")
echo "  raw ${raw_secs}s -> ${REAL_SECS}s (x${stretch}), camera moves, ${OUT_W}px"

# Camera: quick one-second pushes, then hold perfectly still. Continuous
# drifting looks nicer but every frame then differs from the last, which is
# exactly what a GIF cannot compress — snapping and holding keeps the motion
# and cuts the file by more than half. Times are seconds in the corrected clip.
Z="if(lt(it,7),1,\
if(lt(it,8),1+0.22*(it-7),\
if(lt(it,15),1.22,\
if(lt(it,16),1.22-0.22*(it-15),\
if(lt(it,18),1,\
if(lt(it,19),1+0.26*(it-18),\
if(lt(it,27),1.26,\
if(lt(it,28),1.26-0.26*(it-27),\
if(lt(it,31),1,\
if(lt(it,32),1+0.30*(it-31),\
if(lt(it,40),1.30,\
if(lt(it,41),1.30-0.30*(it-40),1))))))))))))"

# Look at the lower pane while the queue and the failure report are on screen.
# Harmless outside a zoom: at z=1 the crop is the whole frame and the offset
# clamps away, so there is no jump when the push starts.
Y="ih/2-(ih/zoom/2)+if(between(it,18,42),ih*0.13,0)"

PAL=$(mktemp -t nftui_pal).png
# Terminal output is left-aligned, so a centred zoom crops the labels off the
# left ("queue:" became "e:"). Hold the camera near the left edge instead.
X="iw*0.02"
FILTER="setpts=${stretch}*PTS,fps=${FPS},zoompan=z='${Z}':x='${X}':y='${Y}':d=1:s=${OUT_W}x586:fps=${FPS}"

ffmpeg -v error -y -i "$RAW" -vf "${FILTER},palettegen=max_colors=${COLORS}:stats_mode=diff" "$PAL"
ffmpeg -v error -y -i "$RAW" -i "$PAL" \
       -lavfi "${FILTER}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5" "$OUT"
rm -f "$PAL"

python3 - "$OUT" <<'PY'
import sys
from PIL import Image
im = Image.open(sys.argv[1]); n = 0; total = 0
try:
    while True:
        total += im.info.get("duration", 0); n += 1; im.seek(im.tell() + 1)
except EOFError:
    pass
import os
print(f"  {sys.argv[1]}: {n} frames, {total/1000:.1f}s, "
      f"{os.path.getsize(sys.argv[1])/1e6:.1f} MB")
PY
