"""TX guards: watch the radio while it transmits, and stop it on the fastest path.

This module exists so that nothing else in the FT8 build has to think about when
to stop. It owns one question -- "should this transmission still be happening?"
-- and it answers it from MEASURED quantities, ten times a second.

the operator's stated posture (2026-08-31), and it shapes everything here:

    no power ceiling -- he will run 90 W for rare DX, deliberately
    ALC and PO are DISPLAYED, never policed
    runaway SWR is the one hard guard

So there is no wattage trip in this file, on purpose. Do not add one "to be
safe": a limit on a setting is not a limit on the thing, and the 2026-08-29
FreeDV work already established that guarding the measured quantity is the only
version that works.

THE ABORT LADDER, fastest first. Order matters more than it looks:

  1. `POST /api/ptt {tx:false}` -> CAT `TX0;`. CyberRig owns the physical serial
     port; this is milliseconds and does not depend on JTDX being healthy.
  2. `HaltTx(8)` over UDP to JTDX. Stops it re-keying on the next cycle. Useless
     as step 1 -- a UDP round-trip through a possibly-wedged decoder is the wrong
     thing in the path of a runaway.
  3. Surface it. A trip that nobody sees is a trip that happens again.

The HA plug (`switch.yaesu`) is deliberately NOT in this ladder. Its telemetry
updates every 5 minutes (measured 2026-09-01), it kills CAT and therefore this
guard along with it, and it does not stop JTDX from re-arming when power returns.
It is the operator's physical last resort, not an automatic step.

WHY THERE IS A SETTLE WINDOW: SWR is meaningless for the first fraction of a
second after key-up while the PA ramps and, on this station, while the
MFJ-994BRT is still deciding. Tripping on that transient would abort every
single transmission. Meters are ignored until `SETTLE_S` has passed, and the
trip needs `DWELL_S` of continuous badness after that.

WHY HIGH SWR IS NOT ALWAYS AN EMERGENCY: the station feeds a 110' doublet
through an MFJ-994BRT with the radio's own ATU bypassed, so the tuner is the only
matching element. Its known firmware quirk is failing to find a match at one
specific frequency while a nudge of a few kHz matches instantly -- that is not an
antenna fault and must not be alarmed as one. See
plan_cyberrig_autotune_and_safety_watchdog. This module reports the
classification; it still stops transmitting either way.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from cyberrig.cat import meter_cal

log = logging.getLogger("cyberrig.ft8.tx")

# --- units ------------------------------------------------------------------
#
# `/api/state` exposes swr_meter / power_meter / alc_meter as RAW RM COUNTS
# (0-255), NOT as SWR ratios, watts or percent. `cyberrig.cat.meter_cal` exists
# to convert them and its docstring spells out the trap; the first version of
# this module ignored it and compared a raw count against an SWR of 3.0.
#
# Raw 39 is SWR 1.35 and raw 52 is SWR 1.50 -- both fine matches -- so the guard
# tripped on EVERY transmission, and on 2026-09-01 it aborted four of the operator's
# remote TUNEs and reported his healthy antenna as SWR 52. Convert first. Always.
def swr_of(raw) -> Optional[float]:
    return None if raw is None else meter_cal.SWR.to_value(float(raw))


def watts_of(raw) -> Optional[float]:
    return None if raw is None else meter_cal.PO.to_value(float(raw))


def alc_of(raw) -> Optional[float]:
    return None if raw is None else meter_cal.ALC.to_value(float(raw))


# --- trip thresholds (REAL units, not raw counts) ---------------------------
SWR_TRIP = 3.0            # sustained SWR RATIO above this aborts (raw 126)
SWR_SOFT = 2.0            # above this is worth reporting, not aborting
DWELL_S = 0.30            # continuous badness required before acting
SETTLE_S = 0.35           # meters ignored this long after key-up (PA ramp)
POLL_S = 0.10             # how often we look

# An FT8 transmission is 12.64 s. Anything past this is not an FT8 over.
MAX_TX_S = 15.0
MAX_TUNE_S = 6.0          # TUNE is a steady carrier: shorter leash

# Dead-man: if the operator's browser is gone this long, finish what is in
# flight and then stop. Never START something new unattended.
DEADMAN_S = 45.0

# A single tap must not become an all-night CQ machine.
DEFAULT_CYCLE_CAP = 6


class TripReason:
    SWR = "swr"
    STUCK_TX = "stuck_tx"
    CYCLE_CAP = "cycle_cap"
    DEADMAN = "deadman"
    OPERATOR = "operator"


@dataclass
class Trip:
    reason: str
    detail: str
    urgent: bool                 # False = the soft MFJ-quirk class
    at: float = field(default_factory=time.time)
    swr: Optional[float] = None
    watts: Optional[float] = None
    alc: Optional[float] = None
    tx_seconds: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "reason": self.reason, "detail": self.detail, "urgent": self.urgent,
            "at": self.at, "swr": self.swr, "watts": self.watts,
            "alc": self.alc, "tx_seconds": self.tx_seconds,
        }


class TxGuard:
    """Polls rig state during TX and runs the abort ladder when required.

    Deliberately takes its inputs as callables so the whole thing can be tested
    against synthetic telemetry with no radio attached -- which is how the trips
    get proven before they are ever needed. See tests/test_ft8_tx_guard.py.
    """

    def __init__(
        self,
        read_state: Callable[[], dict],          # -> CyberRig /api/state
        drop_ptt: Callable[[], None],            # -> POST /api/ptt {tx:false}
        halt_jtdx: Optional[Callable[[], None]] = None,   # -> HaltTx(8)
        on_trip: Optional[Callable[[Trip], None]] = None,
        cycle_cap: int = DEFAULT_CYCLE_CAP,
    ):
        self._read_state = read_state
        self._drop_ptt = drop_ptt
        self._halt_jtdx = halt_jtdx
        self._on_trip = on_trip
        self._cycle_cap = cycle_cap

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._tx_since: Optional[float] = None
        self._bad_since: Optional[float] = None
        self._cycles = 0
        self._last_beat = time.time()
        self._armed = False
        self._max_tx_s = MAX_TX_S
        self.last_trip: Optional[Trip] = None
        self.last_state: dict = {}

    # ------------------------------------------------------------- lifecycle

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ft8-txguard", daemon=True)
        self._thread.start()
        log.info("TX guard running: SWR>%.1f for %.2fs aborts, stuck-TX at %.1fs",
                 SWR_TRIP, DWELL_S, MAX_TX_S)

    def stop(self):
        self._stop.set()

    # ---------------------------------------------------------------- arming

    def arm(self, cycles: Optional[int] = None, tune: bool = False) -> bool:
        """Permit transmission. Refuses if the operator is not there.

        Returns False rather than raising: a refusal is a normal outcome the UI
        has to render, not an exception path.
        """
        with self._lock:
            if self._deadman_expired():
                log.warning("arm refused: dead-man expired (%.0fs since heartbeat)",
                            time.time() - self._last_beat)
                return False
            self._armed = True
            self._cycles = 0
            self._max_tx_s = MAX_TUNE_S if tune else MAX_TX_S
            if cycles is not None:
                self._cycle_cap = cycles
            return True

    def disarm(self, reason: str = "operator"):
        with self._lock:
            self._armed = False
            self._tx_since = None
            self._bad_since = None
        log.info("TX disarmed (%s)", reason)

    def heartbeat(self):
        """Called on every client message. The dead-man's only input."""
        self._last_beat = time.time()

    def _deadman_expired(self) -> bool:
        return (time.time() - self._last_beat) > DEADMAN_S

    @property
    def armed(self) -> bool:
        return self._armed

    # ------------------------------------------------------------------ loop

    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("guard tick failed")
                # A guard that dies quietly is worse than no guard. If we cannot
                # read the radio we cannot supervise a transmission, so stop it.
                if self._tx_since is not None:
                    self._abort(Trip(TripReason.STUCK_TX,
                                     "guard lost telemetry while transmitting",
                                     urgent=True))
            self._stop.wait(POLL_S)

    def _tick(self):
        st = self._read_state() or {}
        self.last_state = st
        now = time.time()
        is_tx = bool(st.get("is_tx"))

        if not is_tx:
            # Falling edge: one completed transmission.
            if self._tx_since is not None:
                with self._lock:
                    self._cycles += 1
                    cycles, cap = self._cycles, self._cycle_cap
                self._tx_since = None
                self._bad_since = None
                if cycles >= cap:
                    self._trip(Trip(TripReason.CYCLE_CAP,
                                    f"{cycles} transmissions since arming (cap {cap})",
                                    urgent=False), stop_tx=False)
                elif self._deadman_expired():
                    self._trip(Trip(TripReason.DEADMAN,
                                    "operator gone; finished the transmission in flight",
                                    urgent=False), stop_tx=False)
            return

        # --- transmitting ---
        #
        # ONLY supervise transmissions this guard armed. The operator drives the
        # radio from the CyberRig panel too -- remote TUNE, manual PTT, SSTV --
        # and those are not ours to police. On 2026-09-01 this guard, sitting
        # disarmed, killed four of the operator's remote TUNEs from the panel: it saw
        # is_tx and applied its own limits to someone else's transmission.
        # Observe and report always; abort only what we started.
        if not self._armed:
            self._tx_since = None
            self._bad_since = None
            return

        if self._tx_since is None:
            self._tx_since = now
            self._bad_since = None
            return

        tx_for = now - self._tx_since

        if tx_for > self._max_tx_s:
            self._abort(Trip(TripReason.STUCK_TX,
                             f"keyed {tx_for:.1f}s (limit {self._max_tx_s:.0f}s)",
                             urgent=True, tx_seconds=tx_for,
                             swr=swr_of(_f(st.get("swr_meter"))),
                             watts=watts_of(_f(st.get("power_meter")))))
            return

        # Meters are not trustworthy while the PA ramps and the MFJ settles.
        if tx_for < SETTLE_S:
            return

        swr = swr_of(_f(st.get("swr_meter")))
        if swr is None:
            return

        if swr >= SWR_TRIP:
            if self._bad_since is None:
                self._bad_since = now
            elif (now - self._bad_since) >= DWELL_S:
                watts = watts_of(_f(st.get("power_meter")))
                # Classification only -- we stop either way. High SWR at low
                # output during the first seconds is the MFJ still hunting, which
                # is its known quirk rather than an antenna fault.
                soft = bool(watts is not None and watts < 10 and tx_for < 4.0)
                self._abort(Trip(
                    TripReason.SWR,
                    (f"SWR {swr:.1f} for {now - self._bad_since:.2f}s"
                     + (" at low output — likely the MFJ-994BRT match quirk,"
                        " try nudging a few kHz" if soft else "")),
                    urgent=not soft, swr=swr, watts=watts,
                    alc=alc_of(_f(st.get("alc_meter"))), tx_seconds=tx_for))
        else:
            self._bad_since = None

    # ---------------------------------------------------------------- aborts

    def _abort(self, trip: Trip):
        """The ladder. Step 1 is CAT because it does not depend on JTDX."""
        log.error("TX ABORT (%s): %s", trip.reason, trip.detail)
        try:
            self._drop_ptt()
        except Exception:
            log.exception("CAT PTT drop FAILED — falling through to JTDX halt")
        if self._halt_jtdx is not None:
            try:
                self._halt_jtdx()
            except Exception:
                log.exception("JTDX HaltTx failed")
        self._tx_since = None
        self._bad_since = None
        self._trip(trip, stop_tx=False)

    def _trip(self, trip: Trip, stop_tx: bool = True):
        with self._lock:
            self._armed = False
        self.last_trip = trip
        if self._on_trip is not None:
            try:
                self._on_trip(trip)
            except Exception:
                log.exception("trip callback failed")

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        st = self.last_state or {}
        return {
            "armed": self._armed,
            "transmitting": bool(st.get("is_tx")),
            "cycles": self._cycles,
            "cycle_cap": self._cycle_cap,
            "deadman_ok": not self._deadman_expired(),
            "seconds_since_heartbeat": round(time.time() - self._last_beat, 1),
            "swr": swr_of(_f(st.get("swr_meter"))),
            "watts": watts_of(_f(st.get("power_meter"))),
            "alc": alc_of(_f(st.get("alc_meter"))),
            "rf_gain": st.get("rf_gain"),
            "power_setting": st.get("power"),
            "last_trip": self.last_trip.as_dict() if self.last_trip else None,
            "thresholds": {
                "swr_trip": SWR_TRIP, "swr_soft": SWR_SOFT,
                "max_tx_s": MAX_TX_S, "max_tune_s": MAX_TUNE_S,
                "deadman_s": DEADMAN_S,
            },
        }


def _f(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
