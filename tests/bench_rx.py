#!/usr/bin/env python3
"""What fraction of a received picture is actually locked to the sender.

Run this before and after any change to the receiver and compare the totals.
It reads real off-air recordings, because a synthetic test card cannot tell
you anything about either failure it measures.

    python3 tests/bench_rx.py [corpus_dir]

Corpus: ~/sstv-corpus (see its README). 198 MB, deliberately not in git.

THREE numbers, and the order matters.

LINE LOCK is the headline: of every line the decoder rendered, how many took
their position from their own 1200 Hz sync pulse rather than free-running on
a remembered phase. A free-running line is not necessarily wrong, but it is
unverified, and a run of them after a fade is exactly how a picture ends up
clean on top and garbage below.

TRAIN COHERENCE is the guard on LINE LOCK, and it is not optional. Line lock
counts a line as locked when it snapped to SOME pulse inside the window — so
a detector that emits spurious pulses scores HIGHER, because the tracker has
more junk to snap to. Measured 2026-08-31: two candidate replacements for
`find_sync_pulses` cut the pulse count on the worst recordings from 320 and
330 to 252 and 233 (the true line count is 256) and raised coherence from
0.29 to 0.36 and 0.30 to 0.43 — genuinely better detection — while line lock
FELL from 75.5% to 74.1%. Read the two together or you will optimise toward
a noisier detector.

Coherence asks a question no tracker is involved in: is the gap between
consecutive pulses a whole number of the sender's lines? It is, for real
pulses, however many were dropped in between.

MODE AGREEMENT is reported separately, and separately for a reason. A file whose
mode is misidentified scores near-zero LINE LOCK, which reads as a tracking
failure and is nothing of the sort — it is an ACQUISITION failure, one stage
earlier, and averaging it into line lock mislabels the finding and drags a
number that is measuring something else. So a disagreeing file is named, and
held out of the line-lock total rather than silently deflating it.

The mode is still RE-DERIVED here, never taken from the sidecar. Pinning it
would make the bench unable to see a misdetection at all — the exact failure
`bench_vis` had one stage up. The sidecar is the reference to compare against,
not a shortcut past the measurement.

HEADER LOCK is the old `bench_vis` measurement, kept as a floor: how weak a
signal can get before no picture starts at all. It is a real guard and it
must not regress — but it was the ONLY number here until 2026-08-31, and
optimising it alone is how the decoder came to score better on every commit
while two thirds of every picture's lines were free-running. A metric that
stops at "a picture started" cannot see the picture.
"""
import glob
import json
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cyberrig.sstv.decode import (  # noqa: E402
    Reception, demodulate, find_sync_pulses, find_vis, lead_skip,
    line_origins_ms,
)

# dB of signal-to-added-noise for the header ladder. Positive is the easy end.
LADDER = [12, 9, 6, 3, 0, -3, -6, -9, -12]
SECONDS = 2.2          # the header lives in the first half second


def _read(path, seconds=None):
    with wave.open(path, "rb") as f:
        rate = f.getframerate()
        n = f.getnframes() if seconds is None else int(seconds * rate)
        a = (np.frombuffer(f.readframes(n), dtype="<i2")
             .astype(np.float32) / 32768.0)
    return a, rate


def sidecar_mode(path):
    """The mode the LIVE receiver recorded for this capture, if it left one.

    The live path reads the VIS as it arrives, in time order, so where it and
    a whole-file re-decode disagree the live answer is the better reference:
    it saw the header before there was any later video to outrank it.
    """
    j = path[:-4] + ".json"
    if not os.path.exists(j):
        return None
    try:
        with open(j) as f:
            return json.load(f).get("mode") or None
    except Exception:
        return None


def train_coherence(pulses, mode):
    """Fraction of pulse gaps that are a whole number of the sender's lines.

    Tracker-free, and it does not reward a detector for emitting more pulses.
    """
    if pulses.size < 6:
        return float("nan")
    k = np.diff(pulses) / mode.line_ms
    whole = (np.abs(k - np.round(k)) < 0.02) & (k > 0.5) & (k < 4.5)
    return float(whole.mean())


def line_lock(path):
    """(locked, rendered, mode, coherence, pulses) at native SNR."""
    a, rate = _read(path)
    trace = demodulate(a, rate)
    vis = find_vis(trace, rate, audio=a)
    if vis is None:
        return 0, 0, None, float("nan"), 0
    off = float(vis.offset_hz or 0.0)
    if abs(off) >= 1.0:
        trace = trace - np.float32(off)
    start = vis.start + lead_skip(trace, vis.mode, vis.start, rate)
    r = Reception(mode=vis.mode, start=start, rate=rate, trace=trace)
    r.autoslant()
    n = r.lines_complete
    if n <= 0:
        return 0, 0, vis.mode, float("nan"), 0
    _, locked = line_origins_ms(trace, vis.mode, start, rate, n,
                                slant=r.slant, shift_ms=r.shift_ms,
                                with_lock=True)
    p = find_sync_pulses(trace, vis.mode, start, rate)
    return (int(locked.sum()), n, vis.mode, train_coherence(p, vis.mode),
            int(p.size))


def header_lock(path, rng):
    a, rate = _read(path, SECONDS)
    rms = float(np.sqrt(np.mean(a ** 2)))
    got = []
    for db in LADDER:
        x = a + rng.normal(0, rms * 10 ** (-db / 20), a.size).astype(np.float32)
        got.append(find_vis(demodulate(x, rate), rate, audio=x) is not None)
    return got


def main(corpus):
    files = sorted(glob.glob(os.path.join(corpus, "*.wav")))
    if not files:
        print(f"no recordings in {corpus}")
        return 1

    print("LINE LOCK — lines positioned by their own sync pulse, native SNR")
    print("COH — gaps that are a whole number of lines; NPULSE — detected\n")
    tot_lock = tot_lines = 0
    worst = []
    cohs = []
    disagree = []          # (rid, derived, recorded) — acquisition, not tracking
    checked = 0
    for path in files:
        rid = os.path.basename(path)[:-4]
        lock, lines, mode, coh, npulse = line_lock(path)
        if lines == 0:
            print(f"  {rid:26s} {'NO PICTURE':>22s}")
            worst.append((0.0, rid))
            continue
        recorded = sidecar_mode(path)
        if recorded:
            checked += 1
        bad_mode = bool(recorded) and recorded != mode.name
        pct = 100.0 * lock / lines
        bar = "#" * int(pct / 6)
        flag = ""
        if bad_mode:
            disagree.append((rid, mode.name, recorded))
            flag = f"  <== MODE: live said {recorded}; held out"
        else:
            # Only files whose mode we agree on are measuring the tracker.
            tot_lock += lock
            tot_lines += lines
            if coh == coh:
                cohs.append(coh)
            worst.append((pct, rid))
        print(f"  {rid:26s} {mode.name:10s} {lock:4d}/{lines:<4d} {pct:5.1f}%"
              f"  coh {coh:4.2f}  n {npulse:4d}  {bar}{flag}")

    pct = 100.0 * tot_lock / max(1, tot_lines)
    print(f"\nTOTAL LINE LOCK: {tot_lock} / {tot_lines}  ({pct:.1f}%)")
    print(f"MEAN COHERENCE:  {np.mean(cohs):.3f}"
          "   (read WITH line lock — see the module docstring)")
    for pc, rid in sorted(worst)[:3]:
        print(f"  worst: {rid} {pc:.1f}%")

    print(f"\nMODE AGREEMENT: {checked - len(disagree)} / {checked} "
          "checked against the live receiver's sidecar")
    if disagree:
        print("  These are ACQUISITION failures, not tracking failures, and are")
        print("  held out of the line-lock total above so it keeps its meaning:")
        for rid, derived, recorded in disagree:
            print(f"    {rid}: whole-file decode says {derived}, "
                  f"live receiver recorded {recorded}")
    if checked < len(files):
        print(f"  ({len(files) - checked} file(s) have no sidecar — not checkable)")

    print("\n\nHEADER LOCK — does a picture start at all (floor, must not regress)\n")
    # Fixed seed: the point is comparing runs, so the noise must be identical.
    rng = np.random.default_rng(4)
    total = 0
    for path in files:
        rid = os.path.basename(path)[:-4]
        got = header_lock(path, rng)
        total += sum(got)
        weakest = next((f"{LADDER[i]:+d}" for i in range(len(LADDER) - 1, -1, -1)
                        if got[i]), "none")
        print(f"  {rid:26s} {''.join('#' if g else '.' for g in got)}"
              f"  {sum(got)}/{len(LADDER)}  weakest {weakest} dB")
    print(f"\nladder: {' '.join(f'{d:+d}' for d in LADDER)}   (# = header found)")
    print(f"TOTAL HEADER LOCKS: {total} / {len(files) * len(LADDER)}")
    return 0


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/sstv-corpus")
    raise SystemExit(main(d))
