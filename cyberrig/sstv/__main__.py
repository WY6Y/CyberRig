"""CLI: render an image (or a test card) to a WAV for off-air verification.

    python -m cyberrig.sstv --list
    python -m cyberrig.sstv --card "Scottie 1" -o /tmp/s1.wav
    python -m cyberrig.sstv photo.jpg --mode PD120 -o /tmp/pd120.wav

Decode the result with something that is NOT CyberRig's own decoder before
trusting a mode on the air.
"""

from __future__ import annotations

import argparse
import sys

from cyberrig.sstv.encode import encode_to_wav
from cyberrig.sstv.modes import UI_ORDER, get_mode, mode_list
from cyberrig.sstv.testcard import test_card


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cyberrig.sstv")
    p.add_argument("image", nargs="?", help="image file; omit with --card")
    p.add_argument("--mode", "-m", default="Scottie 1")
    p.add_argument("--card", action="store_true", help="use the synthetic test card")
    p.add_argument("--out", "-o", default=None)
    p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--amplitude", "-a", type=float, default=0.8)
    p.add_argument("--fit", default="cover", choices=("cover", "contain", "stretch"))
    p.add_argument("--list", action="store_true", help="list modes and exit")
    a = p.parse_args(argv)

    if a.list:
        for r in mode_list():
            print(f"{r['name']:12} VIS 0x{r['vis']:02X}  "
                  f"{r['width']}x{r['height']:<4}  {r['seconds']:6.1f} s")
        return 0

    if not a.card and not a.image:
        p.error("give an image path or --card")

    m = get_mode(a.mode)
    src = test_card(m.width, m.height, f"CyberRig {m.name}") if a.card else a.image
    out = a.out or f"sstv-{m.name.lower().replace(' ', '')}.wav"
    r = encode_to_wav(out, src, m, rate=a.rate, amplitude=a.amplitude, fit=a.fit)
    print(f"{r['path']}  {r['mode']}  {r['seconds']:.1f} s  {r['samples']} samples")
    return 0


if __name__ == "__main__":
    sys.exit(main())
