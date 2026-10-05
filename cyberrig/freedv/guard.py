"""FreeDV transmit guards: watch PO/ALC/SWR while keyed, drop RF when they go bad.

Ported from `cyberrig.sstv.tx` on 2026-08-29. SSTV grew these guards because
it is 100% duty for minutes at a time with nothing coming back to tell the
operator whether the transmission worked. FreeDV is the same shape — RADE is a
continuous, constant-envelope-ish waveform held for the length of an over —
and until now it had none of them: no power ceiling, no SWR trip, no measured
telemetry, and a hard-coded audio drive nobody could see.

The guard is deliberately a separate object from FreeDVEngine. The engine
knows about audio; the interlocks are about RF, and the thing that must be
able to drop PTT should not be buried in a modem worker. It is driven from
the PTT transitions in the web layer: `start()` when the radio is keyed,
`stop()` when it is unkeyed, and `on_trip` when it must be unkeyed NOW.

Not a copy for its own sake: the thresholds, and the settings keys they come
from, are FreeDV's. `cyberrig/sstv/tx.py` stays untouched — it is proven on
the air and its tests monkeypatch its module constants.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from cyberrig.cat.meter_cal import ALC, PO, SWR

log = logging.getLogger("cyberrig.freedv")

# Ignore the SWR and PO meters for this long after keying, and below this raw
# PO reading. A still-settling radio reports nonsense, and a guard that trips
# on nonsense is one the operator learns to switch off.
SWR_SETTLE_S = 1.0
SWR_MIN_PO_RAW = 3

# Watchdog rate, and how often to sample the meters for the over's summary.
#
# SSTV samples once a second because a picture is minutes long. An over is
# often five seconds, and the first two on the air came back "1 sample" — an
# average and a peak that were the same number, which is no summary at all.
POLL_S = 0.05
METER_SAMPLE_S = 0.25

# Drop PTT if the radio is actually RADIATING more than this. Not the same
# thing as the power setting, and it is the one that matters: the setting is
# where the ALC threshold sits, this watches the antenna. Overridable via
# `freedv_max_watts_out`. 70 W leaves room above the 31-40 W this station
# actually runs (measured 2026-08-29), whose peaks reach 58-60 W.
MAX_WATTS_OUT = 70.0

# ...but only once it has STAYED there this long.
#
# The SSTV version of this guard trips on a single reading, and that is right
# for SSTV: the waveform is constant-envelope at 100% duty, so an instant
# reading and a sustained one are the same number. RADE driven by speech is
# not — measured on the air 2026-08-29, a good over ran 23 W average with 33 W
# peaks, and the first over past the target tripped on a 56 W peak while
# averaging 34.9 W with the ALC flat at zero. Cutting a correct transmission
# on one peak is how a guard gets switched off.
#
# What the finals have to survive is heat, which is average power over time,
# so the trip needs the excess to persist. Peaks still go in the summary.
PO_TRIP_DWELL_S = 1.0

# Drop PTT above this SWR. Overridable via `freedv_max_swr`.
MAX_SWR = 3.0

# A held PTT this long is a stuck one. The websocket handler already unkeys on
# disconnect, but a wedged connection never disconnects, and nothing else in
# the FreeDV path has a clock. Overridable via `freedv_max_over_s`.
MAX_OVER_S = 300.0

# Below this peak PO a "transmission" put no meaningful RF out — worth saying
# so, because FreeDV gives an operator no other evidence when nobody answers.
QUIET_TX_WATTS = 2.0

# Warn above this much ALC deflection.
#
# The number CyberRig is shooting for on FreeDV is ZERO. FreeDV's waveform has
# a high peak-to-average ratio, so any ALC action at all is the radio clipping
# peaks — which spreads IMD across the band and costs SNR at the far end. The
# power setting therefore sits well above the target, the audio drive sets the
# output, and a correct over shows no ALC deflection at all. A few percent is
# meter noise; more than this means the drive is too high and the radio is
# limiting instead of us.
MAX_ALC_PERCENT = 10.0


class FreeDVTxGuard:
    """Watches one over. Inject a fake rig in tests."""

    def __init__(self, rig, settings=None,
                 on_trip: Optional[Callable[[str], None]] = None,
                 on_change: Optional[Callable[[], None]] = None):
        self._rig = rig
        self._settings = settings
        self._on_trip = on_trip
        self._on_change = on_change

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watchdog: Optional[threading.Thread] = None
        self._active = False
        self._started = 0.0

        self._m_next = 0.0
        self._m_n = 0
        self._m_po_sum = 0.0
        self._m_po_peak = 0.0
        self._m_alc_peak = 0.0
        self._m_swr_peak = 0.0
        self._po_over_since: Optional[float] = None

        self.trip: Optional[str] = None      # reason the last over was cut short
        self.meters: Optional[dict] = None   # summary of the last over

    # ── state ────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    def status(self) -> dict:
        return {
            "tx_guard": self._active,
            "tx_elapsed": round(time.time() - self._started, 1) if self._active else 0.0,
            "tx_trip": self.trip,
            "tx_meters": self.meters,
            "max_watts_out": self._max_watts_out(),
            "max_swr": self._max_swr(),
            "max_over_s": self._max_over_s(),
        }

    def _changed(self):
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("freedv guard on_change")

    # ── the over ─────────────────────────────────────────────────────────

    def start(self):
        """Called once the radio is keyed."""
        with self._lock:
            if self._active:
                return
            self._stop.clear()
            self._active = True
            self._started = time.time()
            self.trip = None
            self.meters = None
            self._m_next = 0.0
            self._m_n = 0
            self._m_po_sum = 0.0
            self._m_po_peak = 0.0
            self._m_alc_peak = 0.0
            self._m_swr_peak = 0.0
            self._po_over_since = None
            self._watchdog = threading.Thread(
                target=self._watch, daemon=True, name="freedv-watchdog")
            self._watchdog.start()

    def stop(self):
        """Called once the radio is unkeyed. Logs the over's meter summary."""
        with self._lock:
            if not self._active:
                return
            self._stop.set()
            self._active = False
        self.meters = self._meter_summary()
        self._log_summary()
        self._changed()

    def _watch(self):
        while self._active and not self._stop.is_set():
            elapsed = time.time() - self._started
            self._sample_meters(elapsed)
            trip = (self._over_trip(elapsed) or self._swr_trip(elapsed)
                    or self._power_trip(elapsed))
            if trip:
                self._tripped(trip)
                return
            time.sleep(POLL_S)

    def _tripped(self, reason: str):
        """Drop RF first, bookkeeping after. Same order as SSTV's `_finish`."""
        with self._lock:
            if not self._active:
                return
            self._stop.set()
            self._active = False
            self.trip = reason
        log.warning("FreeDV TX tripped: %s", reason)
        if self._on_trip is not None:
            try:
                self._on_trip(reason)
            except Exception:
                log.exception("freedv guard on_trip")
        self.meters = self._meter_summary()
        self._log_summary()
        self._changed()

    # ── meters ───────────────────────────────────────────────────────────

    def _sample_meters(self, elapsed: float):
        """Record PO/ALC/SWR while on the air.

        Read them together: PO is what went out, and ALC near zero means the
        drive set it rather than the radio's limiter.
        """
        if elapsed < SWR_SETTLE_S:
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
        self._m_n += 1
        self._m_po_sum += PO.to_value(po_raw)
        self._m_po_peak = max(self._m_po_peak, PO.to_value(po_raw))
        self._m_alc_peak = max(self._m_alc_peak, ALC.to_value(alc_raw))
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

    def _log_summary(self):
        m = self.meters
        if m is None:
            log.info("FreeDV TX over ended (%s) — no meter samples",
                     self.trip or "complete")
            return
        log.info(
            "FreeDV TX over ended (%s) — PO avg %.1f W peak %.1f W, "
            "ALC peak %.0f%%, SWR peak %.1f over %d samples",
            self.trip or "complete", m["po_avg"], m["po_peak"],
            m["alc_peak"], m["swr_peak"], m["samples"],
        )
        if m["po_peak"] < QUIET_TX_WATTS:
            log.warning(
                "FreeDV TX produced no measurable RF (peak PO %.1f W). The mic "
                "audio was modulated and PTT was keyed, so check the TX audio "
                "path: DATA MOD SOURCE, REAR SELECT, and the DATA port drive "
                "gain in the radio's menu.", m["po_peak"])
        elif m["alc_peak"] > MAX_ALC_PERCENT:
            log.warning(
                "FreeDV TX ran at %.0f%% ALC — the radio is limiting, not us. "
                "Lower the TX DRIVE until the ALC sits at zero; above the knee "
                "the drive control does almost nothing and the clipped peaks "
                "become IMD the far end has to decode through.", m["alc_peak"])

    # ── trips ────────────────────────────────────────────────────────────

    def _swr_trip(self, elapsed: float) -> Optional[str]:
        if elapsed < SWR_SETTLE_S:
            return None
        st = getattr(self._rig, "state", None)
        if st is None:
            return None
        if (getattr(st, "power_meter", 0) or 0) < SWR_MIN_PO_RAW:
            return None
        swr = SWR.to_value(getattr(st, "swr_meter", 0) or 0)
        if swr >= self._max_swr():
            return f"SWR {swr:.1f} — transmission stopped"
        return None

    def _power_trip(self, elapsed: float) -> Optional[str]:
        """Stop an over that is radiating more than it should, and stays there.

        This is the thermal guard, and it is on the MEASURED quantity. A low
        power setting is not one: it only moves the ALC threshold down onto
        the operating point, which puts the station in permanent limiting
        while the antenna still sees whatever the drive produces.

        The dwell is not a softening of the guard. Heat is average power over
        time, so a single speech peak over the line is not the thing being
        protected against — and tripping on one cuts correct transmissions.
        """
        if elapsed < SWR_SETTLE_S:
            return None
        st = getattr(self._rig, "state", None)
        if st is None:
            self._po_over_since = None
            return None
        raw = getattr(st, "power_meter", 0) or 0
        if raw < SWR_MIN_PO_RAW:
            self._po_over_since = None
            return None
        w = PO.to_value(raw)
        if w <= self._max_watts_out():
            self._po_over_since = None
            return None
        now = time.time()
        if self._po_over_since is None:
            self._po_over_since = now
            return None
        held = now - self._po_over_since
        if held < PO_TRIP_DWELL_S:
            return None
        return (f"{w:.0f} W out for {held:.1f} s — over the "
                f"{self._max_watts_out():.0f} W limit")

    def _over_trip(self, elapsed: float) -> Optional[str]:
        limit = self._max_over_s()
        if limit and elapsed > limit:
            return f"PTT held {elapsed:.0f} s — forced off"
        return None

    # ── settings ─────────────────────────────────────────────────────────

    def _num(self, key: str, default: float) -> float:
        if self._settings is None:
            return default
        try:
            return float(self._settings.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    def _max_watts_out(self) -> float:
        return self._num("freedv_max_watts_out", MAX_WATTS_OUT)

    def _max_swr(self) -> float:
        return self._num("freedv_max_swr", MAX_SWR)

    def _max_over_s(self) -> float:
        return self._num("freedv_max_over_s", MAX_OVER_S)
