"""SSTV encoder: RGB image in, 48 kHz audio out.

Generates directly at the codec's own sample rate so nothing resamples the TX
path — a resampler between here and the radio is a slant waiting to happen.

Timing is kept honest by rounding *cumulative* time to sample boundaries
rather than rounding each segment's length independently. Rounding per segment
would accumulate: Scottie 1 has ~246,000 segments, and a half-sample of error
each would be seconds of drift by the bottom of the picture.
"""

from __future__ import annotations

import logging
import struct
import wave
from typing import Iterable, Iterator, Optional

import numpy as np

from cyberrig.sstv.modes import SSTVMode, fsk_id_ms, get_mode

log = logging.getLogger("cyberrig.sstv")

DEFAULT_RATE = 48000

# Raised-cosine fade at the very start and end of a transmission. Phase
# continuity handles every segment boundary inside the signal; only the two
# ends are hard edges, and a hard edge is a click and splatter.
FADE_MS = 5.0


class ToneRenderer:
    """Phase-continuous NCO fed (freq_hz, duration_ms) segments in chunks.

    Holds cumulative time and phase across calls, so rendering a transmission
    line-by-line is sample-for-sample identical to rendering it in one go.
    """

    def __init__(self, rate: int = DEFAULT_RATE):
        self.rate = int(rate)
        self._t = 0.0          # cumulative seconds requested
        self._n = 0            # cumulative samples emitted
        self._phase = 0.0      # radians, carried across chunks

    @property
    def samples_emitted(self) -> int:
        return self._n

    def feed(self, segments: Iterable) -> np.ndarray:
        freqs = []
        counts = []
        for hz, ms in segments:
            self._t += float(ms) / 1000.0
            end = int(round(self._t * self.rate))
            n = end - self._n
            if n <= 0:
                # A segment shorter than a sample period. Skip it rather than
                # emitting nothing and silently losing its time — the
                # cumulative clock above already accounted for it.
                continue
            self._n = end
            freqs.append(float(hz))
            counts.append(n)
        if not counts:
            return np.zeros(0, dtype=np.float32)
        f = np.repeat(np.asarray(freqs, dtype=np.float64),
                      np.asarray(counts, dtype=np.int64))
        # Integrate frequency to phase. float64 throughout: after 270 s the
        # phase is ~3e6 rad and float32 would have quantised it to mush.
        #
        # The carry goes INTO the cumsum rather than being added to its result,
        # and the phase is never wrapped to [0, 2pi). Both matter: together
        # they make the running sum associate identically however the caller
        # chunks the input, so streaming a transmission line-by-line is
        # bit-for-bit what rendering it in one call would have produced.
        step = 2.0 * np.pi * f / self.rate
        phase = np.cumsum(np.concatenate(([self._phase], step)))[1:]
        self._phase = float(phase[-1])
        return np.sin(phase).astype(np.float32)


def prepare_image(img, mode: SSTVMode, fit: str = "cover") -> np.ndarray:
    """Any PIL image / path / ndarray to exactly (height, width, 3) uint8 RGB.

    `cover` centre-crops to the mode's aspect then scales — the sensible
    default for a photo. `stretch` ignores aspect. `contain` letterboxes onto
    black. The web UI does its own cropping; this is the fallback for an image
    handed straight to the API.
    """
    from PIL import Image

    if isinstance(img, np.ndarray):
        im = Image.fromarray(np.asarray(img, dtype=np.uint8))
    elif isinstance(img, (str, bytes)) or hasattr(img, "read"):
        im = Image.open(img)
    else:
        im = img
    im = im.convert("RGB")

    tw, th = mode.width, mode.height
    # Crop against the DISPLAYED aspect, not the pixel grid. Martin 2 is a
    # 160x256 mode showing a 4:3 picture; cropping to 160/256 would frame it
    # like a phone photo and then squash it.
    aspect = mode.display_aspect
    if fit == "stretch":
        im = im.resize((tw, th), Image.LANCZOS)
    elif fit == "contain":
        sw, sh = im.size
        if sw / sh > aspect:
            bw, bh = sw, int(round(sw / aspect))
        else:
            bw, bh = int(round(sh * aspect)), sh
        canvas = Image.new("RGB", (bw, bh), (0, 0, 0))
        canvas.paste(im, ((bw - sw) // 2, (bh - sh) // 2))
        im = canvas.resize((tw, th), Image.LANCZOS)
    else:  # cover
        sw, sh = im.size
        if sw / sh > aspect:
            cw, ch = int(round(sh * aspect)), sh
        else:
            cw, ch = sw, int(round(sw / aspect))
        left, top = (sw - cw) // 2, (sh - ch) // 2
        im = im.crop((left, top, left + cw, top + ch)).resize((tw, th), Image.LANCZOS)
    return np.asarray(im, dtype=np.uint8)


def encode_chunks(
    img,
    mode,
    rate: int = DEFAULT_RATE,
    amplitude: float = 1.0,
    fit: str = "cover",
    fsk_id: str = "",
) -> Iterator[np.ndarray]:
    """Stream a transmission as float32 chunks in [-1, 1], one per line.

    Chunk zero is the VIS header. Feeding these straight to the radio as they
    arrive is the point — Scottie DX is 270 seconds of audio and there is no
    reason to hold all of it in memory.
    """
    m = mode if isinstance(mode, SSTVMode) else get_mode(mode)
    arr = prepare_image(img, m, fit=fit)
    r = ToneRenderer(rate)
    amp = float(max(0.0, min(1.0, amplitude)))
    fade = int(round(FADE_MS / 1000.0 * rate))

    first = True
    pending: Optional[np.ndarray] = None
    for seg in m.segments(arr, fsk_id=fsk_id):
        block = r.feed(seg)
        if block.size == 0:
            continue
        if first:
            n = min(fade, block.size)
            ramp = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, n)))
            block[:n] *= ramp.astype(np.float32)
            first = False
        # Hold one chunk back so the last one can be faded out.
        if pending is not None:
            yield pending * amp
        pending = block
    if pending is not None:
        n = min(fade, pending.size)
        ramp = 0.5 * (1.0 + np.cos(np.linspace(0.0, np.pi, n)))
        pending[-n:] *= ramp.astype(np.float32)
        yield pending * amp


def encode(img, mode, rate: int = DEFAULT_RATE, amplitude: float = 1.0,
           fit: str = "cover", fsk_id: str = "") -> np.ndarray:
    """The whole transmission as one float32 array. Convenience over encode_chunks."""
    parts = list(encode_chunks(img, mode, rate=rate, amplitude=amplitude, fit=fit,
                               fsk_id=fsk_id))
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts)


def encode_to_wav(path: str, img, mode, rate: int = DEFAULT_RATE,
                  amplitude: float = 0.8, fit: str = "cover",
                  fsk_id: str = "") -> dict:
    """Write a 16-bit mono WAV. This is how the encoder gets verified off-air.

    Decode the result with something that is NOT our own decoder — Robot36 on
    a phone, or the `sstv` package — before trusting a mode on the air.
    """
    m = mode if isinstance(mode, SSTVMode) else get_mode(mode)
    n = 0
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        for chunk in encode_chunks(img, m, rate=rate, amplitude=amplitude,
                                   fit=fit, fsk_id=fsk_id):
            i16 = np.clip(chunk * 32767.0, -32767, 32767).astype("<i2")
            w.writeframes(i16.tobytes())
            n += i16.size
    return {
        "ok": True,
        "path": str(path),
        "mode": m.name,
        "samples": n,
        "seconds": round(n / float(rate), 3),
        "rate": int(rate),
    }
