"""SSTV mode timing tables.

Values follow JL Barber N7CXI's "Proposal for SSTV Mode Specifications" (2000),
which is what every decoder in the wild actually implements. Times are in
milliseconds; frequencies in Hz.

DO NOT "clean up" these numbers to rounder ones. 138.240 ms is not 138 ms, and
a decoder that trusts the spec will show a slanted picture if we drift.

Cross-checked tone-for-tone against PySSTV (see tests/test_sstv_pysstv.py).
Two deliberate divergences, both cases where PySSTV departs from the spec:

  * Scottie separator placement. The spec lists one 1.5 ms separator before each
    channel followed by a full 138.240 ms scan, which is what we emit. PySSTV
    puts a separator both before AND after each channel and shortens the scan to
    136.74 ms to compensate. Both total the published 428.22 ms line, so there is
    no slant either way — but the pixel rate differs by about 1%, and ours is the
    literal reading of the spec.

  * Scottie's leading sync. In Scottie the sync pulse sits at the END of a line
    (after blue, before red), so the spec puts a lone 9 ms pulse in front of the
    first line to anchor it. PySSTV omits it. We emit it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

import numpy as np

# The universal SSTV tone map.
FREQ_BLACK = 1500.0
FREQ_WHITE = 2300.0
FREQ_SYNC = 1200.0
FREQ_LEADER = 1900.0
FREQ_VIS_1 = 1100.0
FREQ_VIS_0 = 1300.0

# VIS header, same for every mode.
VIS_LEADER_MS = 300.0
VIS_BREAK_MS = 10.0
VIS_BIT_MS = 30.0


def level_to_freq(v) -> float:
    """0-255 sample level to its tone. 1500 Hz black, 2300 Hz white."""
    return FREQ_BLACK + (float(v) / 255.0) * (FREQ_WHITE - FREQ_BLACK)


# Full-range (JPEG/PIL) YCbCr, NOT the studio-swing 16-235 formula the N7CXI
# paper prints for the Robot modes. The two disagree by about 8% of contrast.
#
# This was a real fork in the road. The spec paper says studio swing; PySSTV —
# which is what a lot of the SSTV images on the air are actually made with —
# uses PIL's full-range conversion. Studio swing would also mean a white pixel
# transmits at 2237 Hz in Robot 36 while transmitting at 2300 Hz in Martin 1,
# so the same picture would come out dimmer in one mode than the other.
# Matching the widely-deployed implementation wins over matching the paper.
#
# UNVERIFIED ON AIR. If a decoded Robot 36 or PD image comes back looking
# washed out or crushed, this constant is the first thing to revisit.
YCC_FULL_RANGE = True


def rgb_to_ycrcb(rgb: np.ndarray) -> np.ndarray:
    """RGB to [Y, R-Y, B-Y] on a 0-255 scale, for the Robot and PD modes."""
    r = rgb[..., 0].astype(np.float64)
    g = rgb[..., 1].astype(np.float64)
    b = rgb[..., 2].astype(np.float64)
    if YCC_FULL_RANGE:
        y = 0.299 * r + 0.587 * g + 0.114 * b
        cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b
        cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
    else:
        y = 16.0 + 0.003906 * (65.738 * r + 129.057 * g + 25.064 * b)
        cr = 128.0 + 0.003906 * (112.439 * r - 94.154 * g - 18.285 * b)
        cb = 128.0 + 0.003906 * (-37.945 * r - 74.494 * g + 112.439 * b)
    return np.clip(np.stack([y, cr, cb], axis=-1), 0.0, 255.0)


@dataclass(frozen=True)
class SSTVMode:
    """One SSTV mode: its VIS code, geometry, and how a line is laid out."""

    name: str
    vis_code: int
    width: int
    height: int
    family: str                 # "scottie" | "martin" | "robot36" | "pd"
    scan_ms: float              # per-channel scan time for one line
    sync_ms: float
    sep_ms: float = 0.0         # separator / porch between channels
    # Robot 36 only
    chroma_scan_ms: float = 0.0
    porch_ms: float = 0.0
    # Every one of these modes is displayed 4:3 regardless of its pixel count,
    # so the pixels are not square — Martin 2 is 160 wide showing the same
    # picture as Martin 1's 320. Crop source images to THIS, never to
    # width/height, or the framing comes out wrong.
    display_aspect: float = 4.0 / 3.0
    aliases: tuple = field(default_factory=tuple)

    # -- timing ----------------------------------------------------------

    @property
    def line_ms(self) -> float:
        if self.family == "scottie":
            # sep G sep B sync sep R
            return 3 * (self.sep_ms + self.scan_ms) + self.sync_ms
        if self.family == "martin":
            # sync sep G sep B sep R sep
            return self.sync_ms + 4 * self.sep_ms + 3 * self.scan_ms
        if self.family == "robot36":
            # sync porch Y sep porch C
            return (
                self.sync_ms + self.porch_ms + self.scan_ms
                + self.sep_ms + 1.5 + self.chroma_scan_ms
            )
        if self.family == "pd":
            # sync porch Y(odd) R-Y B-Y Y(even)
            return self.sync_ms + self.porch_ms + 4 * self.scan_ms
        raise ValueError(self.family)

    @property
    def lines_sent(self) -> int:
        """Transmitted lines. PD sends two image lines per transmitted line."""
        return self.height // 2 if self.family == "pd" else self.height

    @property
    def vis_ms(self) -> float:
        return 2 * VIS_LEADER_MS + VIS_BREAK_MS + 10 * VIS_BIT_MS

    @property
    def duration_s(self) -> float:
        """Total on-air time including the VIS header. This is what the UI shows."""
        lead = self.sync_ms if self.family == "scottie" else 0.0
        return (self.vis_ms + lead + self.lines_sent * self.line_ms) / 1000.0

    @property
    def pixel_ms(self) -> float:
        return self.scan_ms / self.width

    def __str__(self) -> str:
        return f"{self.name} ({self.width}x{self.height}, {self.duration_s:.0f} s)"

    # -- signal ----------------------------------------------------------

    def segments(self, img: np.ndarray, fsk_id: str = "") -> Iterator[list]:
        """Yield the image as a list of (freq_hz, duration_ms) per chunk.

        One chunk per transmitted line, with the VIS header as chunk zero, so
        a caller can stream to the radio without rendering minutes of audio up
        front. `img` must already be (height, width, 3) uint8 RGB.
        """
        if img.shape[:2] != (self.height, self.width):
            raise ValueError(
                f"{self.name} needs {self.width}x{self.height}, got "
                f"{img.shape[1]}x{img.shape[0]}"
            )
        yield vis_header(self.vis_code)
        if self.family == "scottie":
            # Scottie's sync pulse lives at the END of a line, so the very
            # first line needs one in front of it.
            yield [(FREQ_SYNC, self.sync_ms)]
            yield from self._scottie(img)
        elif self.family == "martin":
            yield from self._martin(img)
        elif self.family == "robot36":
            yield from self._robot36(img)
        elif self.family == "pd":
            yield from self._pd(img)
        else:
            raise ValueError(self.family)
        tail = fsk_id_segments(fsk_id)
        if tail:
            yield tail

    def _scan(self, row: np.ndarray) -> list:
        """One channel of one line: width pixels at pixel_ms each."""
        px = self.pixel_ms
        return [(level_to_freq(v), px) for v in row]

    def _scottie(self, img):
        sep = (FREQ_BLACK, self.sep_ms)
        sync = (FREQ_SYNC, self.sync_ms)
        for line in img:
            out = [sep]
            out += self._scan(line[:, 1])          # green
            out.append(sep)
            out += self._scan(line[:, 2])          # blue
            out.append(sync)
            out.append(sep)
            out += self._scan(line[:, 0])          # red
            yield out

    def _martin(self, img):
        sep = (FREQ_BLACK, self.sep_ms)
        for line in img:
            out = [(FREQ_SYNC, self.sync_ms), sep]
            out += self._scan(line[:, 1])          # green
            out.append(sep)
            out += self._scan(line[:, 2])          # blue
            out.append(sep)
            out += self._scan(line[:, 0])          # red
            out.append(sep)
            yield out

    def _robot36(self, img):
        ycc = rgb_to_ycrcb(img)
        cpx = self.chroma_scan_ms / self.width
        for i in range(self.height):
            y = ycc[i, :, 0]
            # Chroma is sent at half vertical resolution: R-Y on even lines,
            # B-Y on odd, each averaged over the line pair it covers.
            base = i - (i % 2)
            chan = 1 if i % 2 == 0 else 2
            chroma = ycc[base:base + 2, :, chan].mean(axis=0)
            out = [(FREQ_SYNC, self.sync_ms), (FREQ_BLACK, self.porch_ms)]
            out += [(level_to_freq(v), self.pixel_ms) for v in y]
            # The separator tone is what tells the decoder which chroma this is.
            out.append((FREQ_BLACK if i % 2 == 0 else FREQ_WHITE, self.sep_ms))
            out.append((FREQ_LEADER, 1.5))
            out += [(level_to_freq(v), cpx) for v in chroma]
            yield out

    def _pd(self, img):
        ycc = rgb_to_ycrcb(img)
        px = self.pixel_ms
        for i in range(self.lines_sent):
            odd = ycc[2 * i]
            even = ycc[2 * i + 1]
            cr = (odd[:, 1] + even[:, 1]) / 2.0
            cb = (odd[:, 2] + even[:, 2]) / 2.0
            out = [(FREQ_SYNC, self.sync_ms), (FREQ_BLACK, self.porch_ms)]
            for row in (odd[:, 0], cr, cb, even[:, 0]):
                out += [(level_to_freq(v), px) for v in row]
            yield out


def vis_header(code: int) -> list:
    """Leader, break, leader, start bit, 7 data bits LSB first, parity, stop."""
    bits = [(code >> i) & 1 for i in range(7)]
    bits.append(sum(bits) % 2)          # even parity
    out = [
        (FREQ_LEADER, VIS_LEADER_MS),
        (FREQ_SYNC, VIS_BREAK_MS),
        (FREQ_LEADER, VIS_LEADER_MS),
        (FREQ_SYNC, VIS_BIT_MS),        # start bit
    ]
    out += [(FREQ_VIS_1 if b else FREQ_VIS_0, VIS_BIT_MS) for b in bits]
    out.append((FREQ_SYNC, VIS_BIT_MS))  # stop bit
    return out


# ── FSK ID ───────────────────────────────────────────────────────────────
# The callsign sent in FSK after the picture, which decoders show as text.
# Note the polarity is INVERTED relative to VIS: here a 1 bit is 1900 Hz.
FREQ_FSK_1 = 1900.0
FREQ_FSK_0 = 2100.0
FSK_BIT_MS = 22.0

# Six bits per character, offset so that a space is zero. The 0x20 0x2A
# preamble and 0x01 terminator are part of the format, not decoration.
FSK_PREAMBLE = (0x20, 0x2A)
FSK_TERMINATOR = 0x01
FSK_MAX_CHARS = 16


def clean_fsk_text(text: str) -> str:
    """Only what the 6-bit alphabet can carry, and not too much of it."""
    out = []
    for c in str(text or "").upper():
        if 0x20 <= ord(c) <= 0x5F:
            out.append(c)
    return "".join(out)[:FSK_MAX_CHARS]


def fsk_id_segments(text: str) -> list:
    txt = clean_fsk_text(text)
    if not txt:
        return []
    payload = list(FSK_PREAMBLE) + [ord(c) - 0x20 for c in txt] + [FSK_TERMINATOR]
    out = []
    for byte in payload:
        for i in range(6):
            bit = (byte >> i) & 1
            out.append((FREQ_FSK_1 if bit else FREQ_FSK_0, FSK_BIT_MS))
    return out


def fsk_id_ms(text: str) -> float:
    txt = clean_fsk_text(text)
    if not txt:
        return 0.0
    return (len(FSK_PREAMBLE) + len(txt) + 1) * 6 * FSK_BIT_MS


MODES: dict[str, SSTVMode] = {}


def _add(m: SSTVMode):
    MODES[m.name.lower()] = m
    for a in m.aliases:
        MODES[a.lower()] = m


_add(SSTVMode("Scottie 1", 0x3C, 320, 256, "scottie", 138.240, 9.0, 1.5,
              aliases=("s1", "scottie1")))
_add(SSTVMode("Scottie 2", 0x38, 160, 256, "scottie", 88.064, 9.0, 1.5,
              aliases=("s2", "scottie2")))
_add(SSTVMode("Scottie DX", 0x4C, 320, 256, "scottie", 345.600, 9.0, 1.5,
              aliases=("sdx", "scottiedx")))
_add(SSTVMode("Martin 1", 0x2C, 320, 256, "martin", 146.432, 4.862, 0.572,
              aliases=("m1", "martin1")))
_add(SSTVMode("Martin 2", 0x28, 160, 256, "martin", 73.216, 4.862, 0.572,
              aliases=("m2", "martin2")))
_add(SSTVMode("Robot 36", 0x08, 320, 240, "robot36", 88.0, 9.0, 4.5,
              chroma_scan_ms=44.0, porch_ms=3.0, aliases=("r36", "robot36")))
_add(SSTVMode("PD90", 0x63, 320, 256, "pd", 170.240, 20.0, porch_ms=2.08))
_add(SSTVMode("PD120", 0x5F, 640, 496, "pd", 121.600, 20.0, porch_ms=2.08))
_add(SSTVMode("PD180", 0x60, 640, 496, "pd", 183.040, 20.0, porch_ms=2.08))


# Order the UI offers them in: fastest-useful first, DX last.
UI_ORDER = ["Scottie 1", "Scottie 2", "Scottie DX", "Martin 1", "Martin 2",
            "Robot 36", "PD90", "PD120", "PD180"]


def get_mode(name: str) -> SSTVMode:
    m = MODES.get(str(name or "").strip().lower())
    if m is None:
        raise KeyError(f"unknown SSTV mode {name!r}")
    return m


def mode_list() -> list[dict]:
    """What the mode selector needs, duration included so a 269 s pick is never a surprise."""
    out = []
    for name in UI_ORDER:
        m = MODES[name.lower()]
        out.append({
            "name": m.name,
            "vis": m.vis_code,
            "width": m.width,
            "height": m.height,
            "seconds": round(m.duration_s, 1),
            "family": m.family,
        })
    return out
