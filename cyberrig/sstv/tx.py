"""SSTV transmit: render, key, stream, and stop — with the interlocks.

Modelled on RemoteTuner rather than on the FreeDV engine, and for the same
reason: anything that keys the transmitter must be ONE object that every UI
looks at, or the interlocks are decorative. Threading, not asyncio, so the Qt
panel can drive it as safely as a request handler.

Unlike FreeDV, the browser is not in the TX loop. An SSTV transmission is a
rendered file, so the server keys the radio, streams the audio, and owns the
clock. A dropped websocket cannot leave the radio transmitting.

Three things stop a transmission: it finishes, the operator aborts it, or a
guard trips. The guards are not optional and there is no unattended path —
nothing here starts a transmission on its own.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

from cyberrig.cat.meter_cal import ALC, PO, SWR
from cyberrig.sstv.encode import DEFAULT_RATE, encode_chunks
from cyberrig.sstv.modes import SSTVMode, fsk_id_ms, get_mode

log = logging.getLogger("cyberrig.sstv")

# Keep this much audio queued ahead of the sound card. SSTV is unforgiving of
# gaps — a buffer underrun is a horizontal tear in somebody's picture.
BUFFER_TARGET_S = 1.5

# Buffer this much before keying, so the leader tone starts the instant RF does.
PREROLL_S = 0.25

# A transmission may overrun its computed length by this much before the
# watchdog kills it. Covers scheduling jitter, not a stuck loop.
OVERRUN_GRACE_S = 5.0

# Ignore the SWR meter for this long after keying, and below this PO reading.
# An unkeyed or still-settling radio reports nonsense, and tripping on nonsense
# would make the guard something the operator learns to switch off.
SWR_SETTLE_S = 1.0
SWR_MIN_PO_RAW = 3

# How often to sample PO/ALC/SWR while transmitting, for the post-transmission
# summary. The watchdog already runs at 20 Hz; the meters do not move that fast
# and a picture is minutes long, so one sample a second is plenty.
METER_SAMPLE_S = 1.0

# Abort if the radio is actually RADIATING more than this. Not the same thing
# as the power clamp, and it is the one that matters: the clamp sets the ALC
# threshold, this watches the antenna. SSTV is up to 270 s at 100% duty, so
# what the finals have to survive is measured output, not a menu setting.
# Overridable via `sstv_max_watts_out`; the default leaves 3 dB of room above
# a 30 W target so ordinary operating never sees it.
MAX_WATTS_OUT = 60.0

# Below this many watts on the peak PO reading, a "sent" transmission did not
# put meaningful RF out. Worth a warning in the log: SSTV has no ACK, so the
# only other evidence that a transmission worked is somebody choosing to send
# a report, and until now nothing recorded whether the radio radiated at all.
QUIET_TX_WATTS = 2.0

# Warn above this much ALC deflection.
#
# This started life the other way up — a warning when the ALC did NOT move,
# written while the working theory was under-modulation. Under the operating
# model this station actually uses that is exactly backwards: PO is set well
# above the target so the ALC threshold is nowhere near, output is set by the
# audio drive, and a correct transmission therefore shows ZERO ALC. The old
# warning fired on every good transmission, which is how a guard teaches an
# operator to ignore it.
#
# ALC deflection now means what it always meant here: the drive is too high
# and the radio is limiting instead of us. A few percent is meter noise.
MAX_ALC_PERCENT = 10.0


class SSTVTransmitter:
    """One transmission at a time. Inject a fake rig + codec in tests."""

    def __init__(self, rig, codec, session=None, settings=None,
                 on_change: Optional[Callable[[], None]] = None):
        self._rig = rig
        self._codec = codec
        self._session = session
        self._settings = settings
        self._on_change = on_change

        self._lock = threading.RLock()
        self._ring_lock = threading.Lock()
        self._ring = np.zeros(0, dtype=np.float32)

        self._active = False
        self._mode: Optional[SSTVMode] = None
        self._rate = DEFAULT_RATE
        self._total = 0            # samples in the whole transmission
        self._played = 0           # samples handed to the sound card
        self._produced = 0         # samples the encoder has emitted
        self._done_encoding = False
        self._started = 0.0
        self._keyed = False
        self._id_ms = 0.0

        self._stop = threading.Event()
        self._producer: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None

        self.result: Optional[str] = None      # "sent" | "aborted" | "tripped"
        self.error: Optional[str] = None

        # Meter telemetry for the transmission in progress, and the summary of
        # the last one. `_finish` logs it; `status()` hands it to the UI.
        self._m_next = 0.0
        self._m_n = 0
        self._m_po_sum = 0.0
        self._m_po_peak = 0.0
        self._m_alc_peak = 0.0
        self._m_swr_peak = 0.0
        self.meters: Optional[dict] = None

    # ── state ────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    def status(self) -> dict:
        m = self._mode
        total = self._total or 1
        return {
            "tx_active": self._active,
            "tx_mode": m.name if m is not None else None,
            "tx_seconds": (round(m.duration_s + self._id_ms / 1000.0, 1)
                           if m is not None else None),
            "tx_progress": round(min(1.0, self._played / total), 4) if self._active else 0.0,
            "tx_elapsed": round(time.time() - self._started, 1) if self._active else 0.0,
            "tx_remaining": (
                round(max(0.0, (self._total - self._played) / float(self._rate)), 1)
                if self._active else 0.0
            ),
            "tx_result": self.result,
            "tx_error": self.error,
            "tx_meters": self.meters,
        }

    def _changed(self):
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("sstv on_change")

    # ── start ────────────────────────────────────────────────────────────

    def start(self, image, mode, amplitude: float = 0.8, fit: str = "cover",
              fsk_id: str = "") -> dict:
        with self._lock:
            if self._active:
                return {"ok": False, "error": "already transmitting", **self.status()}

            m = mode if isinstance(mode, SSTVMode) else get_mode(mode)
            blocked = self._blocked_reason()
            if blocked:
                return {"ok": False, "error": blocked, **self.status()}

            rate = int(getattr(self._codec, "sample_rate", None) or DEFAULT_RATE)

            # Render the first chunk before keying. If the image is bad we want
            # the exception here, with the radio still receiving.
            try:
                gen = encode_chunks(image, m, rate=rate, amplitude=amplitude,
                                    fit=fit, fsk_id=fsk_id)
                first = next(gen)
            except Exception as e:
                self.error = f"encode failed: {e}"
                log.exception("SSTV encode")
                return {"ok": False, "error": self.error, **self.status()}

            self._mode = m
            self._rate = rate
            # The FSK ID is part of the transmission, so it is part of the
            # length. Sizing on the mode alone would have the watchdog decide
            # the picture had finished and cut the callsign off mid-word.
            self._id_ms = fsk_id_ms(fsk_id)
            self._total = round((m.duration_s * 1000.0 + self._id_ms) / 1000.0 * rate)
            self._played = 0
            self._produced = 0
            self._done_encoding = False
            self._ring = np.zeros(0, dtype=np.float32)
            self._stop.clear()
            self.result = None
            self.error = None
            self._active = True
            self._keyed = False
            self._started = time.time()
            self._m_next = 0.0
            self._m_n = 0
            self._m_po_sum = 0.0
            self._m_po_peak = 0.0
            self._m_alc_peak = 0.0
            self._m_swr_peak = 0.0
            self.meters = None

            self._append(first)
            self._codec.set_output_handler(self._on_output)

            self._producer = threading.Thread(
                target=self._produce, args=(gen,), daemon=True, name="sstv-encode")
            self._producer.start()
            self._watchdog = threading.Thread(
                target=self._watch, daemon=True, name="sstv-watchdog")
            self._watchdog.start()

        log.info("SSTV TX start %s %.1f s", m.name, m.duration_s)
        self._changed()
        return {"ok": True, **self.status()}

    def _blocked_reason(self) -> Optional[str]:
        if self._session is not None and not getattr(self._session, "active", False):
            return "SSTV is not entered"
        rig = self._rig
        if getattr(getattr(rig, "state", None), "is_tx", False):
            return "the radio is already transmitting"
        if not getattr(rig, "is_connected", True):
            return "no CAT connection — refusing to key blind"
        return None

    # ── audio ────────────────────────────────────────────────────────────

    def _append(self, block: np.ndarray):
        with self._ring_lock:
            self._ring = np.concatenate([self._ring, block])
        self._produced += block.size

    def _buffered_s(self) -> float:
        with self._ring_lock:
            return len(self._ring) / float(self._rate)

    def _produce(self, gen):
        try:
            for block in gen:
                if self._stop.is_set():
                    return
                self._append(block)
                # Back off once far enough ahead, so a 270 s transmission does
                # not sit in memory all at once.
                while (not self._stop.is_set()
                       and self._buffered_s() > BUFFER_TARGET_S):
                    time.sleep(0.02)
        except Exception as e:
            self.error = f"encode failed mid-transmission: {e}"
            log.exception("SSTV producer")
            self.abort("encoder failed")
            return
        self._done_encoding = True

    def _on_output(self, outdata, frames):
        """PortAudio thread. Fill the buffer, never raise, never block."""
        ch = outdata.shape[1] if getattr(outdata, "ndim", 1) > 1 else 1
        mono = np.zeros(frames, dtype=np.float32)
        if self._active and not self._stop.is_set():
            with self._ring_lock:
                n = min(frames, len(self._ring))
                if n:
                    mono[:n] = self._ring[:n]
                    self._ring = self._ring[n:]
            self._played += n
        if ch >= 2:
            outdata[:, 0] = mono
            outdata[:, 1] = mono
        else:
            outdata[:, 0] = mono

    # ── guards ───────────────────────────────────────────────────────────

    def _watch(self):
        limit = self._mode.duration_s + self._id_ms / 1000.0 + OVERRUN_GRACE_S
        while self._active and not self._stop.is_set():
            # Key once there is audio ready, so the leader tone and the RF
            # start together rather than transmitting a silent carrier.
            if not self._keyed and self._buffered_s() >= PREROLL_S:
                self._key(True)

            elapsed = time.time() - self._started
            if elapsed > limit:
                self._finish("tripped", f"ran past {limit:.0f} s — forced off")
                return
            self._sample_meters(elapsed)
            trip = self._swr_trip(elapsed) or self._power_trip(elapsed)
            if trip:
                self._finish("tripped", trip)
                return
            if self._done_encoding and self._played >= self._total:
                self._finish("sent", None)
                return
            time.sleep(0.05)

    def _sample_meters(self, elapsed: float):
        """Record PO/ALC/SWR while on the air.

        SSTV is the one thing this radio transmits where nothing comes back:
        no ACK, no decode, no report unless another operator volunteers one.
        "SSTV TX sent (complete)" only ever meant the audio was streamed and
        PTT was dropped — it said nothing about whether the transmitter was
        modulated or even producing power, which left "is TX working?" an
        unanswerable question after nine transmissions. These three numbers
        answer it from the radio's own meters.

        Read them together: PO is what went out, and ALC near zero means the
        drive set it rather than the radio's limiter.
        """
        if not self._keyed or elapsed < SWR_SETTLE_S:
            return
        now = time.time()
        if now < self._m_next:
            return
        self._m_next = now + METER_SAMPLE_S
        st = getattr(self._rig, "state", None)
        if st is None:
            return
        po_raw = int(getattr(st, "power_meter", 0) or 0)
        alc_raw = int(getattr(st, "alc_meter", 0) or 0)
        swr_raw = int(getattr(st, "swr_meter", 0) or 0)
        po = PO.to_value(po_raw)
        alc = ALC.to_value(alc_raw)
        self._m_n += 1
        self._m_po_sum += po
        self._m_po_peak = max(self._m_po_peak, po)
        self._m_alc_peak = max(self._m_alc_peak, alc)
        # SWR is only meaningful with power behind it — the same threshold the
        # trip guard uses, for the same reason.
        if po_raw >= SWR_MIN_PO_RAW:
            self._m_swr_peak = max(self._m_swr_peak, SWR.to_value(swr_raw))

    def _meter_summary(self) -> Optional[dict]:
        if self._m_n <= 0:
            return None
        return {
            "samples": self._m_n,
            "po_avg": round(self._m_po_sum / self._m_n, 1),
            "po_peak": round(self._m_po_peak, 1),
            "alc_peak": round(self._m_alc_peak, 1),
            "swr_peak": round(self._m_swr_peak, 1),
        }

    def _swr_trip(self, elapsed: float) -> Optional[str]:
        if not self._keyed or elapsed < SWR_SETTLE_S:
            return None
        st = getattr(self._rig, "state", None)
        if st is None:
            return None
        po_raw = getattr(st, "power_meter", 0) or 0
        if po_raw < SWR_MIN_PO_RAW:
            return None
        swr = SWR.to_value(getattr(st, "swr_meter", 0) or 0)
        if swr >= self._max_swr():
            return f"SWR {swr:.1f} — transmission stopped"
        return None

    def _power_trip(self, elapsed: float) -> Optional[str]:
        """Stop a transmission that is radiating more than it should.

        This replaces a low PO clamp as the thermal guard. Clamping PO to the
        target made the ALC the limiter and left the station permanently in
        limiting; the ceiling now sits high so the drive controls output, and
        the protection moved to the quantity it was always really about.
        """
        if not self._keyed or elapsed < SWR_SETTLE_S:
            return None
        st = getattr(self._rig, "state", None)
        if st is None:
            return None
        raw = getattr(st, "power_meter", 0) or 0
        if raw < SWR_MIN_PO_RAW:
            return None
        w = PO.to_value(raw)
        if w > self._max_watts_out():
            return f"{w:.0f} W out — over the {self._max_watts_out():.0f} W limit"
        return None

    def _max_watts_out(self) -> float:
        if self._settings is None:
            return MAX_WATTS_OUT
        try:
            return float(self._settings.get("sstv_max_watts_out", MAX_WATTS_OUT)
                         or MAX_WATTS_OUT)
        except (TypeError, ValueError):
            return MAX_WATTS_OUT

    def _max_swr(self) -> float:
        if self._settings is None:
            return 3.0
        try:
            return float(self._settings.get("sstv_max_swr", 3.0) or 3.0)
        except (TypeError, ValueError):
            return 3.0

    def watts_out(self) -> Optional[int]:
        st = getattr(self._rig, "state", None)
        if st is None or not self._keyed:
            return None
        raw = getattr(st, "power_meter", 0) or 0
        return int(round(PO.to_value(raw))) if raw >= SWR_MIN_PO_RAW else 0

    # ── stop ─────────────────────────────────────────────────────────────

    def abort(self, reason: str = "aborted by operator") -> dict:
        if not self._active:
            return {"ok": True, "already": True, **self.status()}
        log.warning("SSTV TX abort: %s", reason)
        self._finish("aborted", reason)
        return {"ok": True, **self.status()}

    def _finish(self, result: str, message: Optional[str]):
        with self._lock:
            if not self._active:
                return
            self._stop.set()
            # Drop RF before anything else. Everything below is bookkeeping;
            # this is the part that matters.
            self._key(False)
            with self._ring_lock:
                self._ring = np.zeros(0, dtype=np.float32)
            try:
                self._codec.set_output_handler(None)
            except Exception:
                log.exception("SSTV release output handler")
            self._active = False
            self.result = result
            if message:
                self.error = message
        self.meters = self._meter_summary()
        if self.meters is None:
            log.info("SSTV TX %s (%s) — no meter samples", result,
                     message or "complete")
        else:
            m = self.meters
            log.info(
                "SSTV TX %s (%s) — PO avg %.1f W peak %.1f W, ALC peak %.0f%%, "
                "SWR peak %.1f over %d samples",
                result, message or "complete", m["po_avg"], m["po_peak"],
                m["alc_peak"], m["swr_peak"], m["samples"],
            )
            if m["po_peak"] < QUIET_TX_WATTS:
                log.warning(
                    "SSTV TX produced no measurable RF (peak PO %.1f W). The "
                    "audio was streamed and PTT was keyed, so check the TX "
                    "audio path: DATA MOD SOURCE, REAR SELECT, and the DATA "
                    "port drive gain in the radio's menu.", m["po_peak"])
            elif m["alc_peak"] > MAX_ALC_PERCENT:
                log.warning(
                    "SSTV TX ran at %.0f%% ALC — the radio is limiting, not "
                    "us. Lower the TX level until the ALC sits at zero; above "
                    "the knee the drive control does almost nothing and the "
                    "leader transient lands in the VIS header.",
                    m["alc_peak"])
        self._changed()

    def _key(self, on: bool):
        try:
            self._rig.set_ptt(bool(on))
            self._keyed = bool(on)
        except Exception:
            log.exception("SSTV PTT %s", on)
            if on:
                # Could not key — do not pretend we are on the air.
                self._keyed = False
                self._stop.set()
