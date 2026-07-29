#!/usr/bin/env python3
"""Retime a screen recording by how much is actually happening on screen.

A terminal recording is mostly still. Playing it at one speed wastes the
viewer's time on the quiet stretches; cutting them, or switching speed at a
fixed timestamp, jumps. This measures per-frame change and varies the playback
rate continuously — fast where nothing moves, real time where it does — then
smooths the rate curve so the speed eases in and out instead of snapping.

    python smooth_retime.py demo_raw.gif demo.gif

Tunables are environment variables (see below). Frame delays stay uniform in
the output and the speed comes from *which* input frames are sampled, because
very short GIF delays are unreliable across viewers.
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageChops

FPS = float(os.environ.get("FPS", 12))          # output frame rate
MAX_SPEED = float(os.environ.get("MAX_SPEED", 14))   # rate over a still screen
MIN_SPEED = float(os.environ.get("MIN_SPEED", 1))    # rate when it's busy
BUSY = float(os.environ.get("BUSY", 0))         # >0 enables the activity signal
OPEN_UNTIL = float(os.environ.get("OPEN_UNTIL", -1))  # <0 = detect from the picture
RAMP = float(os.environ.get("RAMP", 2.5))       # seconds spent easing back to 1x
SMOOTH = int(os.environ.get("SMOOTH", 26))      # frames averaged over: the ease
WIDTH = int(os.environ.get("WIDTH", 1300))
# vhs drops frames when it cannot render as fast as it captures, then writes what
# it kept at a nominal rate — so the raw clip is shorter than the wall time it
# represents. Give the real duration and the timeline is corrected first, which
# is what makes OPEN_UNTIL an honest number of seconds.
REAL_SECS = float(os.environ.get("REAL_SECS", 0))


def load(path: str):
    """Frames (RGB) and each one's real duration in seconds."""
    im = Image.open(path)
    frames, durations = [], []
    try:
        while True:
            frames.append(im.convert("RGB"))
            durations.append(im.info.get("duration", 40) / 1000)
            im.seek(im.tell() + 1)
    except EOFError:
        pass
    return frames, durations


def activity(frames) -> list[float]:
    """Per frame, the fraction of pixels that changed since the one before.

    Compared on a heavily downscaled copy: the question is "did the screen
    change", and shrinking makes it both fast and immune to single-pixel noise
    like a blinking cursor.
    """
    small = [f.resize((160, 90)) for f in frames]
    out = [0.0]
    for prev, cur in zip(small, small[1:]):
        diff = ImageChops.difference(prev, cur).convert("L")
        changed = sum(1 for p in diff.getdata() if p > 24)
        out.append(changed / (160 * 90))
    return out


def first_task_frame(frames) -> int | None:
    """Index of the first frame showing a task in the tree, or None.

    Found from the picture rather than configured, because the boot's length is
    not knowable in advance and hand-tuning it went wrong twice: the wall clock
    (~33s) is not the same as the boot's share of a recording whose frames were
    dropped unevenly. A task row carries a coloured status — green COMPLETED,
    orange RUNNING — and the boot never does, so the first coloured row in the
    upper pane marks the end of the wait.
    """
    for i, f in enumerate(frames):
        top = f.crop((0, int(f.height * 0.04), f.width, int(f.height * 0.45)))
        small = top.resize((240, 90))
        hits = 0
        for r, g, b in small.getdata():
            if g > 130 and r < 130 and b < 130:              # green: COMPLETED
                hits += 1
            elif r > 180 and 90 < g < 190 and b < 90:        # orange: RUNNING
                hits += 1
            if hits >= 12:
                return i
    return None


def smooth(values: list[float], window: int) -> list[float]:
    """Moving average — this is what turns a step change into an ease."""
    if window < 2:
        return values
    half, n, out = window // 2, len(values), []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def speed_curve(frames, durations, open_until: float) -> list[float]:
    """Playback rate per input frame, from two signals.

    Motion alone is not enough. Nextflow's startup is visually *busy* — the run
    log streams the whole time — while being the least interesting stretch, and
    most frames elsewhere are byte-identical to the one before (the median
    per-frame change measured 0.0). So:

      * an explicit opening window carries the judgement that the boot is
        skippable, and ramps back to real time rather than cutting;
      * everywhere else, genuinely idle stretches speed up on their own, judged
        on smoothed activity so a burst keeps its neighbourhood at full speed.
    """
    starts, t = [], 0.0
    for d in durations:
        starts.append(t)
        t += d

    # Optional, and off by default, because it measured backwards here: over
    # this recording the boot's median per-frame change was 0.0017 against
    # 0.0000 for the live stretch. The streaming log moves more pixels than a
    # task tree does, so an activity-driven rate would hurry the part worth
    # watching and linger on the part that isn't.
    act = smooth(activity(frames), SMOOTH) if BUSY > 0 else [1.0] * len(starts)

    curve = []
    for i, when in enumerate(starts):
        if when < open_until:                   # inside the opening
            rate = MAX_SPEED
        elif when < open_until + RAMP:          # ease out of it, don't cut
            k = (when - open_until) / RAMP
            rate = MAX_SPEED + (MIN_SPEED - MAX_SPEED) * k
        else:
            rate = MIN_SPEED
        if BUSY > 0:
            idle = MIN_SPEED + (MAX_SPEED - MIN_SPEED) * max(0.0, 1 - act[i] / BUSY)
            rate = max(rate, idle)
        curve.append(rate)
    # Averaging the rate is what makes the change gradual: with RAMP alone the
    # ends of the ramp are still corners.
    return smooth(curve, max(2, SMOOTH))


def retime(frames, durations, speeds):
    """Sample input frames at a varying rate; emit them at a constant delay."""
    # cumulative real time of each input frame
    starts, t = [], 0.0
    for d in durations:
        starts.append(t)
        t += d
    total = t

    picked, cursor = [], 0.0
    idx = 0
    step = 1.0 / FPS
    while cursor < total:
        while idx + 1 < len(starts) and starts[idx + 1] <= cursor:
            idx += 1
        picked.append(idx)
        cursor += step * speeds[idx]        # a faster rate skips further ahead
    return picked


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else "demo_raw.gif"
    dst = sys.argv[2] if len(sys.argv) > 2 else "demo.gif"

    frames, durations = load(src)
    raw = sum(durations)
    if REAL_SECS > 0:                           # correct vhs's dropped frames
        durations = [d * REAL_SECS / raw for d in durations]
    real = sum(durations)
    if OPEN_UNTIL >= 0:
        open_until = OPEN_UNTIL
    else:
        hit = first_task_frame(frames)
        open_until = 0.0
        if hit is not None:
            open_until = max(0.0, sum(durations[:hit]) - 1.0)   # stop just before
        print(f"  boot ends where tasks appear: {open_until:.1f}s "
              f"({'detected' if hit is not None else 'none found'})")
    speeds = speed_curve(frames, durations, open_until)
    picked = retime(frames, durations, speeds)

    scale = WIDTH / frames[0].width
    size = (WIDTH, int(frames[0].height * scale) // 2 * 2)
    out = [frames[i].resize(size, Image.LANCZOS) for i in picked]

    # One shared palette, built from frames sampled across the whole clip.
    # Taking it from the first frame alone washed everything out: frame one is
    # the boot screen, which contains none of the greens and oranges the task
    # states are drawn in, so those colours had no entry to map to.
    step = max(1, len(out) // 24)
    sample = out[::step]
    strip = Image.new("RGB", (size[0], size[1] * len(sample)))
    for n, f in enumerate(sample):
        strip.paste(f, (0, size[1] * n))
    pal = strip.quantize(colors=128, method=Image.MEDIANCUT)
    out = [f.quantize(palette=pal, dither=Image.FLOYDSTEINBERG) for f in out]

    out[0].save(dst, save_all=True, append_images=out[1:],
                duration=int(1000 / FPS), loop=0, optimize=True)

    fast = sum(1 for i in picked if speeds[i] > (MIN_SPEED + MAX_SPEED) / 2)
    print(f"  in : {len(frames)} frames, {raw:.0f}s raw -> {real:.0f}s real")
    print(f"  out: {len(out)} frames, {len(out)/FPS:.0f}s "
          f"({os.path.getsize(dst)/1e6:.1f} MB)")
    print(f"  speed ranged {min(speeds):.1f}x–{max(speeds):.1f}x; "
          f"{fast*100//max(1,len(picked))}% of it sped up")


if __name__ == "__main__":
    main()
