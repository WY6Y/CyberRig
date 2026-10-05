"""FT8 waterfall: capture-only FFT on the shared codec.

Deliberately NOT `cyberrig.audio.waterfall`. That engine goes through RigCodec,
which opens the codec as a single DUPLEX stream on purpose -- its docstring is
explicit that a second stream on a USB audio-class device is how you hang the
panel. Duplex is right when CyberRig owns the radio. Here JTDX owns TX audio and
we are a passive listener, so claiming the playback side would be both useless and
a way to collide with the decoder we depend on.

So this opens `ftdx10_rx` (the ALSA dsnoop PCM from /etc/asound.conf) as an INPUT
ONLY. dsnoop fans one hardware capture out to many readers; without it the codec
is single-open and whoever starts first locks everyone else out.

Resolution is chosen to match JTDX and then some:

    capture 48000 Hz, FFT 16384  ->  2.93 Hz/bin

which is the same bin width JTDX shows at its default, but we hand the browser
every bin instead of averaging 3 into a pixel. 200-3200 Hz is bins 68..1092 =
1024 columns. An iPhone at devicePixelRatio 3 has 1170 physical pixels across a
390pt portrait screen, so all 1024 columns fit on the glass at one column per
device pixel -- the whole FT8 sub-band, no panning, finer than the desktop.

FFT directly at 48 kHz rather than decimating to 12 kHz: same bin width, one less
resampling stage, and no anti-alias filter to get wrong. The window is 341 ms
either way, which is what JTDX uses too.

Rows are emitted as raw bytes, not JSON. A 1024-element JSON int array is ~4 KB
per row; the same row as uint8 is 1 KB and needs no parse on the phone. At 5
rows/sec that is 40 kbps -- nothing over a VPN, but it is also the difference
between a smooth canvas and a phone burning battery on JSON.parse.

GAIN IS AUTOMATIC AND THAT IS NOT A CONVENIENCE. The ZL1FZ palette crushes the
bottom 37% of its range to pure black, so the mapping from dB to level has a cliff
in it: a few dB too low and the screen is blank, a few too high and it is a green
wash. There is no graceful failure. The transfer curve is therefore calibrated to
an outcome -- hold a fixed small fraction of bins above the green knee -- rather
than to fixed dB constants, so a band opening does not require reaching for a
slider. See the gain mapping note below for the measurement that forced this.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("cyberrig.ft8.waterfall")

try:
    import sounddevice as sd
    HAS_SD = True
except ImportError:  # pragma: no cover - dev machines without portaudio
    sd = None
    HAS_SD = False

DEVICE = "ftdx10_rx"          # the dsnoop PCM; see /etc/asound.conf on the station PC
RATE = 48000
FFT_SIZE = 16384              # 2.93 Hz/bin at 48 kHz -- matches JTDX
HOP_S = 0.2                   # 5 rows/sec
LO_HZ = 200.0
HI_HZ = 3200.0

# ---------------------------------------------------------------- gain mapping
#
# Fixed dB constants do not survive contact with a real band. The first attempt
# here mapped the 30th percentile to level 90 over a 40 dB span; measured on 20m
# at 03:00 UTC that lit 64% of the screen green or brighter and 34% yellow. A
# waterfall that is mostly lit has no contrast left for the weak signal you are
# actually hunting, which is one of the four jobs this display exists to do.
#
# So the mapping is calibrated to an OUTCOME instead: hold a target fraction of
# bins above the palette's black->green knee. Two tracked percentiles define the
# line -- a low one that is the noise, pinned deep in the black, and a high one
# pinned exactly at the knee. Everything above keeps the same slope up to red.
#
# This self-corrects across a dead 40m morning and a packed 20m evening without
# a gain slider, which matters because ZL1FZ crushes its bottom 37% to black and
# therefore fails hard in both directions: a little low is a blank screen, a
# little high is a green wash.
# The curve is in two segments, because one slope cannot serve both halves of
# this palette. A single slope tuned to put 8% of bins into green leaves the
# strongest signals at level ~143 -- measured -- so the yellow plateau and the
# red tip never appear and half the palette is decoration. Splitting at the knee
# lets the black region be set by the noise and the colour region be set by the
# loudest signal actually present.
KNEE_LEVEL = 96               # where ZL1FZ stops being black
FLOOR_LEVEL = 52              # noise sits here: solidly black, not clipped
TOP_LEVEL = 235               # the loudest signal present lands here, into red
GREEN_TARGET_PCT = 8.0        # % of bins allowed above the knee
FLOOR_PCT = 20.0              # percentile taken as "noise"
KNEE_PCT = 100.0 - GREEN_TARGET_PCT
TOP_PCT = 99.9                # "the loudest signal", robust against one hot bin
TAU = 0.12                    # EMA weight; slow enough to ignore one loud burst
MIN_RANGE_DB = 6.0            # guard: a silent input must not become max contrast
MAX_RANGE_DB = 60.0
MIN_TOP_DB = 3.0              # knee->top span floor, same guard on a quiet band


class FT8Waterfall:
    """Background capture + FFT. Push rows to listeners as uint8 bytes."""

    def __init__(self, device: str = DEVICE):
        self._device = device
        self._stream = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._listeners: list[Callable[[bytes, dict], None]] = []
        self._buf = np.zeros(0, dtype=np.float32)
        self._window = np.hanning(FFT_SIZE).astype(np.float32)
        self._floor_db: Optional[float] = None
        self._knee_db: Optional[float] = None
        self._top_db: Optional[float] = None
        self._seq = 0
        self._error: Optional[str] = None
        self._running = False

        freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / RATE)
        self._lo = int(np.searchsorted(freqs, LO_HZ))
        self._hi = self._lo + 1024
        if self._hi > len(freqs):                      # tiny FFT in a test
            self._hi = len(freqs)
        self.bins = self._hi - self._lo
        self.hz_per_bin = RATE / FFT_SIZE
        self.f_lo = float(freqs[self._lo])
        self.f_hi = float(freqs[self._hi - 1])

    # ------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._running

    @property
    def error(self) -> Optional[str]:
        return self._error

    def start(self) -> bool:
        if not HAS_SD:
            self._error = "sounddevice not installed"
            log.error(self._error)
            return False
        if self._running:
            return True
        self._stop.clear()
        try:
            self._stream = sd.InputStream(
                device=self._device,
                channels=1,
                samplerate=RATE,
                dtype="float32",
                blocksize=2048,
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            log.error("cannot open %s: %s", self._device, self._error)
            self._stream = None
            return False

        self._running = True
        self._error = None
        self._thread = threading.Thread(target=self._run, name="ft8-wf", daemon=True)
        self._thread.start()
        log.info("waterfall up on %r: %d bins, %.2f Hz/bin, %.0f-%.0f Hz",
                 self._device, self.bins, self.hz_per_bin, self.f_lo, self.f_hi)
        return True

    def stop(self):
        self._stop.set()
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.exception("error closing stream")
            self._stream = None

    # ----------------------------------------------------------------- audio

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            # Underruns here are the thing to watch: they would mean dsnoop is
            # not keeping up and JTDX may be suffering too.
            log.debug("audio status: %s", status)
        with self._lock:
            self._buf = np.concatenate((self._buf, indata[:, 0].copy()))
            # Never let the buffer grow without bound if listeners stall.
            cap = FFT_SIZE + int(RATE * 2)
            if self._buf.size > cap:
                self._buf = self._buf[-cap:]

    def _run(self):
        hop = int(RATE * HOP_S)
        while not self._stop.is_set():
            with self._lock:
                have = self._buf.size
                if have >= FFT_SIZE:
                    frame = self._buf[-FFT_SIZE:].copy()
                    self._buf = self._buf[-(FFT_SIZE - hop):] if FFT_SIZE > hop else self._buf[:0]
                else:
                    frame = None
            if frame is None:
                self._stop.wait(0.02)
                continue
            try:
                self._emit(frame)
            except Exception:
                log.exception("fft/emit error")
            self._stop.wait(HOP_S)

    def _emit(self, frame: np.ndarray):
        spec = np.fft.rfft(frame * self._window)
        mag = np.abs(spec[self._lo:self._hi])
        db = 20.0 * np.log10(np.maximum(mag, 1e-12))

        # Two tracked percentiles define the transfer curve -- see the gain
        # mapping note above for why this is calibrated to an outcome rather
        # than to fixed dB constants.
        row_floor, row_knee, row_top = np.percentile(db, [FLOOR_PCT, KNEE_PCT, TOP_PCT])
        if self._floor_db is None:
            self._floor_db = float(row_floor)
            self._knee_db = float(row_knee)
            self._top_db = float(row_top)
        else:
            self._floor_db += TAU * (float(row_floor) - self._floor_db)
            self._knee_db += TAU * (float(row_knee) - self._knee_db)
            self._top_db += TAU * (float(row_top) - self._top_db)

        lo_rng = min(MAX_RANGE_DB, max(MIN_RANGE_DB, self._knee_db - self._floor_db))
        hi_rng = min(MAX_RANGE_DB, max(MIN_TOP_DB, self._top_db - self._knee_db))
        lo_scale = (KNEE_LEVEL - FLOOR_LEVEL) / lo_rng
        hi_scale = (TOP_LEVEL - KNEE_LEVEL) / hi_rng

        below = db - self._floor_db
        above = db - self._knee_db
        level = np.where(
            db <= self._knee_db,
            FLOOR_LEVEL + below * lo_scale,
            KNEE_LEVEL + above * hi_scale,
        )
        row = np.clip(level, 0, 255).astype(np.uint8)

        self._seq += 1
        meta = {
            "seq": self._seq,
            "bins": int(row.size),
            "floor_db": round(float(self._floor_db), 1),
            "knee_db": round(float(self._knee_db), 1),
            "top_db": round(float(self._top_db), 1),
            "t": time.time(),
        }
        payload = row.tobytes()
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(payload, meta)
            except Exception:
                log.exception("waterfall listener failed")

    # ------------------------------------------------------------- listeners

    def add_listener(self, cb: Callable[[bytes, dict], None]):
        with self._lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[bytes, dict], None]):
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def config(self) -> dict:
        return {
            "bins": self.bins,
            "hz_per_bin": round(self.hz_per_bin, 4),
            "f_lo": round(self.f_lo, 1),
            "f_hi": round(self.f_hi, 1),
            "rows_per_sec": round(1.0 / HOP_S, 2),
            "device": self._device,
            "running": self._running,
            "error": self._error,
        }
