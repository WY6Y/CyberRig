"""WSPR mode: auto-tune the remote ATU on every band hop, and supervise the beacon.

This is the first thing in CyberRig that keys the transmitter unattended, all
night, with nobody watching. So the tune half and the watchdog half are one
module and one commit: the thing that decides to transmit and the thing that
can stop it must not be separable.

WHY THIS IS AN INTERLOCK AND NOT A RACE
---------------------------------------
JTDX reaches the radio *through* CyberRig -- its `Rig=Hamlib NET rigctl` points
at 127.0.0.1:4532, which is `cyberrig/server/rigctl.py`. So CyberRig sits in the
path of both halves it needs:

    set_freq  -- the band hop arrives here.  The TRIGGER.
    set_ptt   -- the beacon key arrives here. The VETO.

Holding the proxy position means we do not have to detect a band change and
hope we finish tuning before JTDX keys; we can simply refuse the key. Every
refusal costs exactly one WSPR slot.

THE FOUR NON-NEGOTIABLES (plan_cyberrig_autotune_and_safety_watchdog, P139)
---------------------------------------------------------------------------
1. NEVER KEY THE TUNE CARRIER INSIDE THE WSPR WINDOW. Those sub-bands are 200 Hz
   wide and full of -25 dB signals. A 20 W AM carrier parked there for ten
   seconds wipes that slot out for everyone hearing us, worldwide. CyberRig owns
   the dial, so it QSYs ~30 kHz away, tunes, and comes back. The MFJ cannot tell
   the difference; nobody's decode is stepped on.

2. A 10 s TUNE DOES NOT FIT BETWEEN SLOTS. WSPR is 110.6 s of TX in a 120 s
   slot, so the gap is about nine seconds. We decline that one beacon rather
   than rushing the tune. One lost slot per band change costs nothing;
   transmitting into an unmatched antenna costs a lot.

   HOW we decline matters more than the decision. `allow_ptt` returning False
   makes the proxy answer RPRT 0 and quietly not key -- it must NEVER surface
   as an error, because JTDX escalates a failed set_ptt to a fatal CAT fault
   and drops the link until a human clicks Retry. That cost 8h51m on
   2026-09-04. See the long note in server/rigctl.py's set_ptt handler.

3. CONVERT BEFORE COMPARING. `/api/state` publishes swr/po/alc as RAW RM COUNTS
   (0-255), not ratios. On 2026-09-01 a guard compared a raw count against an
   SWR of 3.0, aborted four remote TUNEs, and reported a healthy antenna as
   "SWR 52" (raw 52 is SWR 1.50). Everything here goes through
   `cyberrig.cat.meter_cal`. Full scale is 236/205/64, never 255.

4. THE WATCHDOG SHIPS WITH IT, not after it.

WHAT COUNTS AS A GOOD MATCH IS NOT 1.5
---------------------------------------
the operator operates 12m happily at about 2.5:1 through this doublet, so the accept
threshold is deliberately loose (`wspr_swr_accept`, 2.8). The abort threshold
sits just above it (`wspr_swr_trip`, 3.2) rather than at the FT8 module's 3.0,
because a band that legitimately sits at 2.5 has no headroom under 3.0 and a
watchdog that cries wolf on a working band is a watchdog the operator turns off.

WHAT THIS DOES *NOT* DO
-----------------------
* It does not police transmissions it did not permit. The FT8 guard once killed
  four of the operator's own panel TUNEs by policing someone else's TX; only PTT that
  arrived through the rigctl proxy while armed is supervised here.
* No auto-nudge-retry when the MFJ cannot find a match. the operator's manual fix is
  to nudge the VFO a few kHz; automating it has real edge cases (does it put the
  dial back? how far before giving up?) and it is deliberately deferred. A
  no-match holds TX on that band and clears itself on the next successful tune,
  so a bad band costs one band's worth of the night, not the night.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

from cyberrig.bands import CHANNELISED, band_edges, band_for_hz
from cyberrig.cat import meter_cal

log = logging.getLogger("cyberrig.wspr")


def swr_of(raw) -> Optional[float]:
    """Raw RM6 count -> SWR ratio. Never compare the raw count. See docstring."""
    return None if raw is None else meter_cal.SWR.to_value(float(raw))


def watts_of(raw) -> Optional[float]:
    return None if raw is None else meter_cal.PO.to_value(float(raw))


# ── timing ──────────────────────────────────────────────────────────────────
POLL_S = 0.10             # watchdog cadence
SETTLE_S = 1.0            # meters ignored this long after key-up (PA + MFJ ramp)
DWELL_S = 1.0             # continuous bad SWR required before aborting
CLIENT_GONE_S = 300       # armed with no CAT client this long = say something
TUNE_SETTLE_S = 0.8       # ignore meters this long after the tune carrier keys
TUNE_HOLD_S = 0.8         # SWR must stay good this long to call it converged
TX_CLEAR_WAIT_S = 5.0     # how long to wait for an in-flight beacon before tuning
QSY_SETTLE_S = 0.25       # let the VFO write land before keying

#: A WSPR transmission is 110.6 s. Past this it is not a beacon any more.
DEFAULT_MAX_TX_S = 125

#: Defaults that also appear in cyberrig/settings.py DEFAULTS.
# -5 kHz, NOT the 30 kHz this shipped with on 2026-09-03. Two live 20 m tunes
# at 14.1256 both ended at SWR 4.11 (raw 180, the identical value twice, so the
# MFJ had finished searching rather than being cut off) and the operator identified
# it immediately as one of his tuner's known hard-to-match spots. The lesson is
# more general than that one frequency: this station matches a doublet through
# an external tuner, so a solution found 30 kHz away is a DIFFERENT solution,
# and the further away it is the less it says about the operating frequency.
#
# What the offset actually has to clear is a WSPR receiver's audio passband --
# the dial up to about dial+2.6 kHz -- not 30 kHz of spectrum. Going DOWN 5 kHz
# clears it on every band with room to spare, keeps the match close, and lands
# on no beacon frequency (dial+5 kHz would sit on the NCDXF beacons at 14.100
# and 18.110).
DEFAULT_OFFSET_HZ = -5_000
DEFAULT_TUNE_MAX_S = 12
DEFAULT_TUNE_MIN_S = 3
DEFAULT_SWR_ACCEPT = 2.8
DEFAULT_SWR_TRIP = 3.2
DEFAULT_SESSION_MAX_H = 12
DEFAULT_MAX_TRIPS = 3

#: Per-band tune frequencies, overriding the offset. Empty by default now that
#: the offset is small (the two entries that were here existed only because
#: +30 kHz landed on a 6m FT8 calling frequency and the 3.590 RTTY DX window).
#: This is the place to pin a frequency the MFJ is KNOWN to match well on a
#: band -- operator knowledge the offset cannot derive. Settings key:
#: `wspr_tune_freq`, e.g. {"20m": 14090000}.
BAND_TUNE_OVERRIDE: dict = {}

#: Keep a tuning carrier this far inside the band edge.
EDGE_MARGIN_HZ = 5_000


class Phase:
    IDLE = "idle"
    TUNING = "tuning"
    HOLD = "hold"
    STOPPED = "stopped"       # hard stop: too many trips, or session expired


class TripReason:
    SWR = "swr"
    STUCK_TX = "stuck_tx"
    NO_MATCH = "no_match"
    CHANNELISED = "channelised"
    NO_CAT = "no_cat"
    SESSION_MAX = "session_max"
    TRIP_LIMIT = "trip_limit"


def tune_freq_for(band: str, dial_hz: int, offset_hz: int = DEFAULT_OFFSET_HZ,
                  override: Optional[dict] = None) -> Optional[int]:
    """A frequency on the same band, clear of the WSPR window, to tune against.

    Offset rather than a fixed band-edge frequency on purpose: this station
    feeds a doublet, whose match moves fast with frequency. A match found at
    3.997 tells you nothing about 3.5686 -- and, as 2026-09-03 proved on 20 m,
    neither does one found 30 kHz away. Near enough that the tuner solution
    still applies when we QSY back, far enough that the carrier is outside
    every WSPR receiver's audio passband. See DEFAULT_OFFSET_HZ.

    Returns None for a channelised band (60 m) -- there is nowhere legal to
    move to there, so the caller must hold rather than improvise.
    """
    if band in CHANNELISED:
        return None
    edges = band_edges(band)
    if not edges:
        return None
    lo, hi = edges
    ov = (override or {}).get(band)
    if ov:
        return int(max(lo + EDGE_MARGIN_HZ, min(hi - EDGE_MARGIN_HZ, int(ov))))

    up = int(dial_hz) + int(offset_hz)
    if up <= hi - EDGE_MARGIN_HZ:
        return up
    down = int(dial_hz) - int(offset_hz)
    if down >= lo + EDGE_MARGIN_HZ:
        return down
    # Band too narrow for the offset (30 m is 50 kHz wide): use whichever half
    # of the band the dial is not sitting in.
    mid = (lo + hi) // 2
    return (lo + EDGE_MARGIN_HZ) if int(dial_hz) > mid else (hi - EDGE_MARGIN_HZ)


class WsprAutoTune:
    """Arm this and JTDX's band hopping tunes the MFJ by itself, supervised.

    Takes the rig and the RemoteTuner rather than reimplementing either: the
    tune carrier, its power/mode restore and its own safety timer already exist
    in `cyberrig/tune.py`, and a second routine that keys the transmitter is the
    exact mistake that module's docstring was written to prevent.
    """

    def __init__(self, rig, settings, tuner, on_change: Optional[Callable[[], None]] = None):
        self._rig = rig
        self._settings = settings
        self._tuner = tuner
        self._on_change = on_change

        self._lock = threading.RLock()
        self._armed = False
        self._armed_at = 0.0
        self._phase = Phase.IDLE
        self._band = ""
        self._dial_hz = 0
        self._hold: Optional[dict] = None
        self._last_tune: Optional[dict] = None
        #: What to tell a client the radio is doing WHILE we have it borrowed.
        self._tune_dial_hz: int = 0
        self._tune_mode: str = ""

        self._tx_since: Optional[float] = None
        self._bad_since: Optional[float] = None
        self._trips = 0
        self._tunes = 0
        self._skipped = 0
        #: Live rigctld clients. Armed with zero of these means the thing that
        #: would key the beacon is gone, which is invisible everywhere else.
        self._clients = 0
        self._clients_zero_since: Optional[float] = None
        self._clients_alerted = False
        self.events: deque = deque(maxlen=20)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tune_thread: Optional[threading.Thread] = None

    # ── settings ────────────────────────────────────────────────────────

    def _s(self, key, default):
        try:
            val = self._settings.get(key, default)
        except Exception:
            return default
        return default if val is None else val

    @property
    def max_tx_s(self) -> float:
        return float(self._s("wspr_max_tx_s", DEFAULT_MAX_TX_S))

    @property
    def swr_accept(self) -> float:
        return float(self._s("wspr_swr_accept", DEFAULT_SWR_ACCEPT))

    @property
    def swr_trip(self) -> float:
        return float(self._s("wspr_swr_trip", DEFAULT_SWR_TRIP))

    # ── arming ──────────────────────────────────────────────────────────

    @property
    def armed(self) -> bool:
        return self._armed

    def arm(self, on: bool = True, tune_now: bool = True) -> dict:
        if not on:
            return self.disarm("operator")
        with self._lock:
            if self._armed:
                return self.status()
            self._armed = True
            self._armed_at = time.time()
            self._phase = Phase.IDLE
            self._hold = None
            self._trips = 0
            self._tx_since = None
            self._bad_since = None
            self._band = band_for_hz(getattr(self._rig.state, "freq_a", 0))
            self._dial_hz = int(getattr(self._rig.state, "freq_a", 0) or 0)
        self._note("info", f"WSPR mode ARMED on {self._band or 'an unknown band'}")
        self._start_thread()
        # Tune once for the band we are already sitting on -- otherwise the
        # first beacon of the night goes out on whatever the tuner last did.
        if tune_now and self._band:
            self._begin_tune(self._dial_hz, self._band, "armed")
        self._changed()
        return self.status()

    def disarm(self, reason: str = "operator") -> dict:
        with self._lock:
            was = self._armed
            self._armed = False
            self._phase = Phase.IDLE
            self._hold = None
            self._tx_since = None
            self._bad_since = None
        self._stop.set()
        if was:
            self._note("info", f"WSPR mode disarmed ({reason})")
        self._changed()
        return self.status()

    def clear_hold(self) -> dict:
        """Operator says the band is fine. Does not clear a hard stop."""
        with self._lock:
            if self._phase == Phase.HOLD:
                self._phase = Phase.IDLE
                self._hold = None
        self._changed()
        return self.status()

    def resume(self) -> dict:
        """Clear a hard stop. Deliberately separate from clear_hold: a hard
        stop means the watchdog fired repeatedly and a human should have
        looked at the antenna before this is pressed."""
        with self._lock:
            if self._phase == Phase.STOPPED:
                self._phase = Phase.IDLE
                self._hold = None
                self._trips = 0
                if self._armed:
                    self._armed_at = time.time()
        self._changed()
        return self.status()

    # ── the rigctl gate ─────────────────────────────────────────────────

    def note_set_freq(self, hz: int) -> None:
        """JTDX moved the dial. If that crossed a band edge, tune the ATU."""
        if not self._armed:
            return
        try:
            hz = int(hz)
        except (TypeError, ValueError):
            return
        band = band_for_hz(hz)
        with self._lock:
            self._dial_hz = hz
            if band == self._band:
                return
            old, self._band = self._band, band
        if not band:
            self._hold_band(TripReason.CHANNELISED,
                            f"{hz/1e6:.6f} MHz is outside every ham band", urgent=True)
            return
        log.info("WSPR band hop %s -> %s (%.6f MHz)", old or "?", band, hz / 1e6)
        self._begin_tune(hz, band, f"band hop {old or '?'} -> {band}")

    def allow_ptt(self, on: bool) -> bool:
        """The veto. False means the proxy SWALLOWS this key: it reports
        success and does not touch the radio, so the client loses one slot
        believing it transmitted. That is the whole cost, and it is the cost
        this veto was always meant to have -- returning an error instead
        killed the CAT link for nine hours on 2026-09-04.

        Unkeying is never refused -- a guard that can block "stop transmitting"
        is not a guard.
        """
        if not on or not self._armed:
            return True

        reason = self._refusal()
        if reason:
            with self._lock:
                self._skipped += 1
                n = self._skipped
            log.warning("WSPR beacon SKIPPED (%s) -- %d skipped so far", reason, n)
            self._note("soft", f"beacon skipped: {reason}")
            self._changed()
            return False
        return True

    def _refusal(self) -> Optional[str]:
        if not getattr(self._rig, "is_connected", False):
            return "no CAT link to the radio"
        with self._lock:
            phase, hold = self._phase, self._hold
        if phase == Phase.STOPPED:
            return (hold or {}).get("detail") or "WSPR mode hard-stopped"
        if phase == Phase.TUNING or getattr(self._tuner, "active", False):
            return "auto-tune in progress (a 10s tune does not fit the 9s slot gap)"
        if phase == Phase.HOLD:
            return (hold or {}).get("detail") or "TX held on this band"
        if self._session_expired():
            self._hard_stop(TripReason.SESSION_MAX,
                            f"armed longer than {self._s('wspr_session_max_hours', DEFAULT_SESSION_MAX_H)}h")
            return "session time limit reached"
        return None

    def absorb_set_freq(self, hz: int) -> bool:
        """True if we swallowed this write because a tune is in flight.

        JTDX may move the dial at any moment, including the six seconds we have
        the VFO borrowed. Writing it through would fight the tune carrier, and
        our own restore afterwards would then put the OLD dial back and leave
        JTDX beaconing somewhere it did not ask for. So it becomes the new
        restore target instead, and lands the moment the tuner lets go.
        """
        with self._lock:
            if self._phase != Phase.TUNING:
                return False
            self._tune_dial_hz = int(hz)
            log.info("WSPR: set_freq %.6f arrived mid-tune, applying after", hz / 1e6)
            return True

    def mask_freq(self, actual: int) -> int:
        """What JTDX is told the VFO is. See the class note on masking."""
        with self._lock:
            if self._phase == Phase.TUNING and self._tune_dial_hz:
                return self._tune_dial_hz
        return actual

    def mask_mode(self, actual: str) -> str:
        """Ditto for the mode -- the tune carrier runs in AM."""
        with self._lock:
            if self._phase == Phase.TUNING and self._tune_mode:
                return self._tune_mode
        return actual

    def note_rigctl_clients(self, n: int) -> None:
        """The rigctl server telling us how many CAT clients are attached."""
        with self._lock:
            self._clients = int(n)
            if n:
                self._clients_zero_since = None
                self._clients_alerted = False
            elif self._clients_zero_since is None:
                self._clients_zero_since = time.time()

    def note_ptt(self, on: bool) -> None:
        """Record that WE permitted this transmission, so the watchdog knows
        which TX is its business."""
        if not self._armed:
            return
        with self._lock:
            self._tx_since = time.time() if on else None
            self._bad_since = None

    def _session_expired(self) -> bool:
        hours = float(self._s("wspr_session_max_hours", DEFAULT_SESSION_MAX_H))
        if hours <= 0:
            return False
        return (time.time() - self._armed_at) > hours * 3600.0

    # ── auto-tune ───────────────────────────────────────────────────────

    def _begin_tune(self, dial_hz: int, band: str, why: str) -> None:
        with self._lock:
            if self._phase in (Phase.TUNING, Phase.STOPPED):
                return
        self._enter_tuning(dial_hz)
        self._changed()
        t = threading.Thread(target=self._run_tune, args=(int(dial_hz), band, why),
                             name="wspr-autotune", daemon=True)
        self._tune_thread = t
        t.start()

    def _enter_tuning(self, dial_hz: int) -> None:
        """Phase and mask together, before the VFO moves.

        These two must not be separable. The phase is what refuses PTT and the
        mask is what stops JTDX adopting the tune frequency as its own; a tune
        that started with only one of them in place would either transmit into
        an unmatched antenna or beacon 5 kHz off. Idempotent, and called both
        by `_begin_tune` (before its thread starts, so a PTT arriving in that
        gap is already refused) and at the top of `_run_tune` itself, so the
        sequence is correct however it is entered.
        """
        with self._lock:
            self._phase = Phase.TUNING
            self._hold = None
            self._tune_dial_hz = int(dial_hz)
            self._tune_mode = str(getattr(self._rig.state, "mode", "") or "")

    def _run_tune(self, dial_hz: int, band: str, why: str) -> None:
        started = time.time()
        self._enter_tuning(dial_hz)
        try:
            if not getattr(self._rig, "is_connected", False):
                self._hold_band(TripReason.NO_CAT, "no CAT link -- cannot tune", urgent=True)
                return

            tune_hz = tune_freq_for(
                band, dial_hz,
                int(self._s("wspr_tune_offset_hz", DEFAULT_OFFSET_HZ)),
                dict(self._s("wspr_tune_freq", BAND_TUNE_OVERRIDE) or {}),
            )
            if tune_hz is None:
                self._hold_band(
                    TripReason.CHANNELISED,
                    f"{band} is channelised -- there is nowhere to move a tuning "
                    f"carrier to, so this band is skipped", urgent=False)
                return

            if not self._wait_for_tx_clear():
                self._hold_band(TripReason.STUCK_TX,
                                "radio still keyed when the band hop arrived", urgent=True)
                return

            log.info("WSPR auto-tune (%s): QSY %.6f -> %.6f MHz to tune",
                     why, dial_hz / 1e6, tune_hz / 1e6)
            self._rig.set_freq(tune_hz)
            time.sleep(QSY_SETTLE_S)

            res = self._tuner.start()
            if not res.get("ok"):
                self._restore_dial(dial_hz)
                self._hold_band(TripReason.NO_MATCH,
                                f"tuner would not start: {res.get('error')}", urgent=True)
                return

            swr, converged = self._watch_tune()
            self._tuner.stop(restore=True)
            time.sleep(0.2)
            # Not the dial we captured on entry: absorb_set_freq may have
            # replaced it while we held the VFO.
            with self._lock:
                dial_hz = self._tune_dial_hz or dial_hz
            self._restore_dial(dial_hz)

            elapsed = time.time() - started
            with self._lock:
                self._tunes += 1
                self._last_tune = {
                    "band": band, "why": why, "tune_hz": tune_hz, "dial_hz": dial_hz,
                    "swr": round(swr, 2) if swr is not None else None,
                    "converged": converged, "seconds": round(elapsed, 1),
                    "at": time.time(),
                }
            if converged:
                with self._lock:
                    self._phase = Phase.IDLE
                    self._hold = None
                log.info("WSPR auto-tune OK on %s: SWR %.2f in %.1fs",
                         band, swr or 0.0, elapsed)
                self._note("info", f"{band} tuned, SWR {swr:.2f}" if swr else f"{band} tuned")
                self._changed()
            else:
                shown = f"{swr:.2f}" if swr is not None else "unreadable"
                self._hold_band(
                    TripReason.NO_MATCH,
                    f"auto-tune failed on {band} at {tune_hz/1e6:.4f} MHz "
                    f"(SWR {shown}) -- likely the known MFJ-994BRT match quirk, "
                    f"not an antenna fault. Beacons held on this band; a nudge of "
                    f"a few kHz usually fixes it. Nothing was transmitted.",
                    urgent=False)
        except Exception as e:
            log.exception("WSPR auto-tune failed")
            try:
                self._tuner.stop(restore=True)
            except Exception:
                pass
            try:
                self._rig.set_ptt(False)
            except Exception:
                pass
            self._hold_band(TripReason.NO_MATCH, f"auto-tune error: {e}", urgent=True)
        finally:
            # Belt and braces: the phase leaving TUNING is what actually lifts
            # the mask, but a stuck mask would make the radio lie to JTDX
            # indefinitely, so it is cleared on every path out of here.
            self._clear_mask()

    def _wait_for_tx_clear(self) -> bool:
        deadline = time.time() + TX_CLEAR_WAIT_S
        while time.time() < deadline:
            if not getattr(self._rig.state, "is_tx", False):
                return True
            time.sleep(0.1)
        return not getattr(self._rig.state, "is_tx", False)

    def _clear_mask(self) -> None:
        with self._lock:
            self._tune_dial_hz = 0
            self._tune_mode = ""

    def _restore_dial(self, dial_hz: int) -> None:
        """Back to the WSPR dial. This must happen on every exit path -- being
        left 30 kHz off frequency means JTDX beacons into empty spectrum all
        night and believes it is working."""
        try:
            self._rig.set_freq(int(dial_hz))
        except Exception:
            log.exception("WSPR: could not restore the dial to %s", dial_hz)

    def _watch_tune(self):
        """Key time for the MFJ, then decide whether it actually found a match.

        Returns (swr, converged). The MFJ swings wildly while it searches, so a
        single good sample proves nothing -- SWR has to stay at or under the
        accept threshold for TUNE_HOLD_S before this calls it done.
        """
        t0 = time.time()
        max_s = float(self._s("wspr_tune_max_s", DEFAULT_TUNE_MAX_S))
        min_s = float(self._s("wspr_tune_min_s", DEFAULT_TUNE_MIN_S))
        accept = self.swr_accept
        good_since: Optional[float] = None
        last: Optional[float] = None

        while (time.time() - t0) < max_s:
            time.sleep(POLL_S)
            if self._stop.is_set() or not self._armed:
                break
            elapsed = time.time() - t0
            if elapsed < TUNE_SETTLE_S:
                continue
            swr = swr_of(getattr(self._rig.state, "swr_meter", None))
            if swr is None:
                continue
            last = swr
            if swr <= accept:
                good_since = good_since or time.time()
                if (time.time() - good_since) >= TUNE_HOLD_S and elapsed >= min_s:
                    return swr, True
            else:
                good_since = None
        return last, False

    # ── watchdog ────────────────────────────────────────────────────────

    def _start_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wspr-watchdog", daemon=True)
        self._thread.start()
        log.info("WSPR watchdog running: SWR>%.1f for %.1fs aborts, stuck-TX at %.0fs",
                 self.swr_trip, DWELL_S, self.max_tx_s)

    def _run(self):
        while not self._stop.is_set() and self._armed:
            try:
                self.tick()
            except Exception:
                log.exception("WSPR watchdog tick failed")
                # A supervisor that cannot read the radio cannot supervise a
                # transmission. Get off the air rather than guess.
                if self._tx_since is not None:
                    self._abort(TripReason.STUCK_TX,
                                "watchdog lost telemetry while transmitting", urgent=True)
            self._stop.wait(POLL_S)

    def tick(self) -> None:
        """One watchdog pass. Public so the tests can step it deterministically."""
        if not self._armed:
            return

        self._check_client_present()

        with self._lock:
            tx_since = self._tx_since
        if tx_since is None:
            return

        now = time.time()
        elapsed = now - tx_since

        if not getattr(self._rig.state, "is_tx", False):
            # The radio came back to RX on its own: a normal end-of-beacon that
            # we did not see a set_ptt 0 for. Only trust it after the settle
            # window -- CAT reports TX a beat late.
            if elapsed > SETTLE_S:
                with self._lock:
                    self._tx_since = None
                    self._bad_since = None
            return

        if elapsed > self.max_tx_s:
            self._abort(TripReason.STUCK_TX,
                        f"keyed {elapsed:.0f}s (a WSPR over is 110.6s, limit "
                        f"{self.max_tx_s:.0f}s)", urgent=True)
            return

        if elapsed < SETTLE_S:
            return

        swr = swr_of(getattr(self._rig.state, "swr_meter", None))
        if swr is None:
            return
        if swr >= self.swr_trip:
            with self._lock:
                self._bad_since = self._bad_since or now
                bad_for = now - self._bad_since
            if bad_for >= DWELL_S:
                watts = watts_of(getattr(self._rig.state, "power_meter", None))
                detail = f"SWR {swr:.2f} for {bad_for:.1f}s"
                if watts is not None:
                    detail += f" at {watts:.0f}W"
                self._abort(TripReason.SWR, detail, urgent=True, swr=swr, watts=watts)
        else:
            with self._lock:
                self._bad_since = None

    def _check_client_present(self) -> None:
        """Armed with no CAT client attached is a fault, not a quiet band.

        On 2026-09-04 JTDX dropped the rigctld socket at 22:04 and nothing
        noticed for nearly nine hours: no beacon can be keyed without a client,
        but an unkeyed transmitter looks exactly like poor propagation. This
        does not stop anything -- there is nothing to stop -- it just refuses
        to let the silence pass unremarked.
        """
        with self._lock:
            zero_since, alerted = self._clients_zero_since, self._clients_alerted
            if zero_since is None or alerted:
                return
            gone_for = time.time() - zero_since
            if gone_for < CLIENT_GONE_S:
                return
            self._clients_alerted = True
        detail = (f"no CAT client attached for {gone_for/60:.0f} min -- nothing "
                  f"can key a beacon; check JTDX for a rig control error")
        log.error("WSPR: %s", detail)
        self._note("urgent", detail)
        self._changed()

    # ── trips ───────────────────────────────────────────────────────────

    def _abort(self, reason: str, detail: str, urgent: bool = True,
               swr: Optional[float] = None, watts: Optional[float] = None) -> None:
        """Unkey first, on the shortest path, then reason about it."""
        log.error("WSPR ABORT (%s): %s", reason, detail)
        try:
            self._rig.set_ptt(False)
        except Exception:
            log.exception("WSPR abort: CAT PTT drop FAILED")
        with self._lock:
            self._tx_since = None
            self._bad_since = None
            self._trips += 1
            trips = self._trips
        limit = int(self._s("wspr_max_trips", DEFAULT_MAX_TRIPS))
        if limit and trips >= limit:
            self._hard_stop(TripReason.TRIP_LIMIT,
                            f"{trips} trips this session (last: {detail})")
        else:
            self._hold_band(reason, detail, urgent=urgent, swr=swr, watts=watts)

    def _hold_band(self, reason: str, detail: str, urgent: bool = True,
                   swr: Optional[float] = None, watts: Optional[float] = None) -> None:
        """Refuse to transmit on this band. Cleared by the next successful tune,
        so one bad band costs one band, not the night."""
        with self._lock:
            if self._phase == Phase.STOPPED:
                return
            self._phase = Phase.HOLD
            self._hold = {
                "reason": reason, "detail": detail, "urgent": urgent,
                "band": self._band, "at": time.time(),
                "swr": round(swr, 2) if swr is not None else None,
                "watts": round(watts) if watts is not None else None,
            }
        log.warning("WSPR HOLD on %s (%s): %s", self._band or "?", reason, detail)
        self._note("urgent" if urgent else "soft", detail)
        self._changed()

    def _hard_stop(self, reason: str, detail: str) -> None:
        try:
            self._rig.set_ptt(False)
        except Exception:
            log.exception("WSPR hard stop: CAT PTT drop FAILED")
        with self._lock:
            self._phase = Phase.STOPPED
            self._hold = {"reason": reason, "detail": detail, "urgent": True,
                          "band": self._band, "at": time.time()}
            self._tx_since = None
        log.error("WSPR HARD STOP (%s): %s -- no further transmission until resumed",
                  reason, detail)
        self._note("urgent", f"HARD STOP: {detail}")
        self._changed()

    # ── reporting ───────────────────────────────────────────────────────

    def _note(self, level: str, text: str) -> None:
        self.events.append({"at": time.time(), "level": level, "text": text})

    def _changed(self):
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                log.exception("wspr on_change")

    def status(self) -> dict:
        with self._lock:
            hold = dict(self._hold) if self._hold else None
            return {
                "armed": self._armed,
                "phase": self._phase,
                "band": self._band,
                "dial_hz": self._dial_hz,
                "hold": hold,
                "urgent": bool(hold and hold.get("urgent")),
                "last_tune": dict(self._last_tune) if self._last_tune else None,
                "tunes": self._tunes,
                "skipped_beacons": self._skipped,
                "rigctl_clients": self._clients,
                "client_gone_s": (round(time.time() - self._clients_zero_since)
                                  if self._clients_zero_since else 0),
                "trips": self._trips,
                "armed_seconds": round(time.time() - self._armed_at, 1) if self._armed else 0,
                "swr_now": swr_of(getattr(self._rig.state, "swr_meter", None)),
                "thresholds": {
                    "swr_accept": self.swr_accept,
                    "swr_trip": self.swr_trip,
                    "max_tx_s": self.max_tx_s,
                    "tune_max_s": float(self._s("wspr_tune_max_s", DEFAULT_TUNE_MAX_S)),
                    "session_max_hours": float(self._s("wspr_session_max_hours",
                                                       DEFAULT_SESSION_MAX_H)),
                    "max_trips": int(self._s("wspr_max_trips", DEFAULT_MAX_TRIPS)),
                },
                "events": list(self.events),
            }
