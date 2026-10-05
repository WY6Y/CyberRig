"""Prove every TX trip fires, against synthetic telemetry, with no radio.

This is Phase 0 of the FT8 TX plan: the guards have to be demonstrated BEFORE
anything keys a transmitter, because the one thing you cannot debug live is the
mechanism that is supposed to stop a runaway.

Each test drives `TxGuard` from a scripted sequence of `/api/state` dicts and
asserts on what the abort ladder actually did -- specifically that CAT PTT is
dropped FIRST, since that is the step that does not depend on JTDX being alive.

The guard polls on a thread in production; here it is stepped synchronously via
`_tick()` so the tests are deterministic and take no wall-clock time.
"""

from __future__ import annotations

import time

import pytest

from cyberrig.cat import meter_cal
from cyberrig.ft8 import tx as txmod
from cyberrig.ft8.tx import TripReason, TxGuard


def raw_swr(ratio):   return meter_cal.SWR.to_raw(ratio)
def raw_watts(w):     return meter_cal.PO.to_raw(w)


class Harness:
    """Records the order of abort actions, which is the thing that matters.

    Tests speak in REAL units (an SWR of 5.0, 40 watts) and this harness converts
    them to the raw 0-255 RM counts `/api/state` actually publishes, so every
    test drives the same calibration path the guard uses. Feeding ratios straight
    in — as the first version did — bypasses the exact conversion that was wrong.
    """

    def __init__(self):
        self.state: dict = {"is_tx": False, "swr_meter": raw_swr(1.2),
                            "power_meter": 0, "alc_meter": 0, "power": 100}
        self.calls: list[str] = []
        self.trips: list = []

    def read_state(self):
        return dict(self.state)

    def drop_ptt(self):
        self.calls.append("cat_tx0")
        self.state["is_tx"] = False

    def halt_jtdx(self):
        self.calls.append("jtdx_halt")

    def on_trip(self, trip):
        self.trips.append(trip)

    def guard(self, **kw) -> TxGuard:
        g = TxGuard(self.read_state, self.drop_ptt,
                    halt_jtdx=self.halt_jtdx, on_trip=self.on_trip, **kw)
        g.heartbeat()
        return g


def _key(h: Harness, guard: TxGuard, seconds: float, *, swr=1.2, watts=40.0,
         step=0.05, start_at=None):
    """Simulate `seconds` of transmit by advancing the guard's clock."""
    t0 = start_at if start_at is not None else time.time()
    h.state.update(is_tx=True, swr_meter=raw_swr(swr), power_meter=raw_watts(watts))
    n = int(seconds / step)
    for i in range(n):
        now = t0 + i * step
        _tick_at(guard, now)
        if not h.state["is_tx"]:
            return now
    return t0 + n * step


def _tick_at(guard: TxGuard, now: float, monkey=None):
    """Run one tick with time pinned, so tests do not sleep."""
    real = time.time
    time.time = lambda: now  # type: ignore[assignment]
    try:
        guard._tick()
    finally:
        time.time = real  # type: ignore[assignment]


# --------------------------------------------------------------------- SWR

def test_sustained_high_swr_aborts_and_cat_comes_first():
    h = Harness()
    g = h.guard()
    assert g.arm()
    _key(h, g, seconds=3.0, swr=5.0, watts=40.0)

    assert h.calls, "guard never aborted on a 5.0 SWR"
    assert h.calls[0] == "cat_tx0", (
        "CAT PTT must be dropped before asking JTDX — a UDP round-trip through a "
        f"wedged decoder is the wrong first step. Got order: {h.calls}")
    assert "jtdx_halt" in h.calls, "JTDX must also be halted so it stops re-keying"

    trip = h.trips[-1]
    assert trip.reason == TripReason.SWR
    assert trip.urgent is True
    assert trip.swr == pytest.approx(5.0)
    assert not g.armed, "a trip must disarm; the next TX has to be a deliberate act"


def test_swr_spike_shorter_than_dwell_does_not_abort():
    """A transient must not kill a good transmission."""
    h = Harness()
    g = h.guard()
    g.arm()
    t0 = time.time()
    # settle window, then a spike far shorter than DWELL_S, then clean again
    _key(h, g, seconds=0.5, swr=1.2, start_at=t0)
    h.state["swr_meter"] = raw_swr(6.0)
    _tick_at(g, t0 + 0.55)
    _tick_at(g, t0 + 0.60)          # 0.05 s of badness, DWELL_S is 0.30
    h.state["swr_meter"] = raw_swr(1.2)
    _tick_at(g, t0 + 0.70)
    assert h.calls == [], "a 50 ms SWR transient must not abort"
    assert g.armed


def test_high_swr_during_settle_window_is_ignored():
    """SWR is meaningless while the PA ramps and the MFJ is still hunting.

    Asserts on WHEN the abort lands, not merely that a too-short window produced
    none. An earlier version of this test ran for exactly SETTLE_S and so could
    never have tripped regardless — it passed with the settle window deleted.
    """
    h = Harness()
    g = h.guard()
    g.arm()
    t0 = time.time()
    h.state.update(is_tx=True, swr_meter=raw_swr(9.0), power_meter=raw_watts(2.0))

    first_abort = None
    span = txmod.SETTLE_S + txmod.DWELL_S + 0.5
    for i in range(int(span / 0.05)):
        now = t0 + i * 0.05
        _tick_at(g, now)
        if h.calls and first_abort is None:
            first_abort = now - t0

    assert first_abort is not None, "sustained 9.0 SWR must abort eventually"
    assert first_abort >= (txmod.SETTLE_S + txmod.DWELL_S) - 0.06, (
        f"aborted {first_abort:.2f}s after key-up; must ignore meters for the "
        f"first {txmod.SETTLE_S}s or every transmission dies on the PA ramp")


def test_low_power_high_swr_is_classified_soft_not_urgent():
    """The known MFJ-994BRT quirk: it cannot match here, a nudge fixes it.

    We still stop transmitting — but calling it an antenna fault would send
    the operator up to the attic for a firmware bug.
    """
    h = Harness()
    g = h.guard()
    g.arm()
    _key(h, g, seconds=2.0, swr=8.0, watts=3.0)

    trip = h.trips[-1]
    assert trip.reason == TripReason.SWR
    assert trip.urgent is False, "low output + early = the MFJ quirk class"
    assert "MFJ" in trip.detail
    assert h.calls[0] == "cat_tx0", "soft classification still stops the carrier"


# ---------------------------------------------------------------- stuck TX

def test_stuck_tx_aborts_past_the_ft8_over_length():
    # Bound the limit itself. Scaling the simulated duration off MAX_TX_S (as an
    # earlier version did) means raising the limit also lengthens the test, so it
    # passed with the stuck-TX guard effectively removed.
    assert 12.64 < txmod.MAX_TX_S <= 20.0, (
        "the stuck-TX limit must bracket exactly one FT8 over (12.64 s)")

    h = Harness()
    g = h.guard()
    g.arm()
    _key(h, g, seconds=txmod.MAX_TX_S + 3.0, swr=1.1, watts=40.0, step=0.25)

    assert h.trips, "a transmission past the limit must abort"
    trip = h.trips[-1]
    assert trip.reason == TripReason.STUCK_TX
    assert trip.urgent is True
    assert trip.tx_seconds > txmod.MAX_TX_S
    assert h.calls[0] == "cat_tx0"


def test_a_normal_ft8_over_is_not_aborted():
    """12.64 s at a good match must complete untouched."""
    h = Harness()
    g = h.guard()
    g.arm()
    _key(h, g, seconds=12.64, swr=1.3, watts=40.0, step=0.1)
    assert h.calls == [], "a clean FT8 over must not trip anything"


def test_tune_gets_a_shorter_leash_than_an_ft8_over():
    assert txmod.MAX_TUNE_S < txmod.MAX_TX_S, "TUNE must have the shorter leash"
    h = Harness()
    g = h.guard()
    g.arm(tune=True)
    _key(h, g, seconds=txmod.MAX_TUNE_S + 2.0, swr=1.2, watts=20.0, step=0.25)
    assert h.trips, "TUNE past its limit must abort"
    assert h.trips[-1].reason == TripReason.STUCK_TX, (
        "TUNE is a steady carrier and must not be allowed to run an FT8 length")


# ------------------------------------------------------------- housekeeping

def test_losing_telemetry_mid_transmission_aborts():
    """If we cannot see the radio we cannot supervise it."""
    h = Harness()

    def boom():
        raise OSError("CyberRig unreachable")

    g = TxGuard(boom, h.drop_ptt, halt_jtdx=h.halt_jtdx, on_trip=h.on_trip)
    g.heartbeat()
    g.arm()
    g._tx_since = time.time() - 1.0     # pretend we are mid-over

    # Drive the REAL loop, not a hand-rolled emulation of it: the whole point is
    # that _run()'s except branch aborts rather than letting the thread die
    # silently with the radio still keyed.
    g.start()
    for _ in range(50):
        if h.calls:
            break
        time.sleep(0.02)
    g.stop()

    assert h.calls, "guard thread swallowed the telemetry failure while keyed"
    assert h.calls[0] == "cat_tx0"
    assert h.trips[-1].urgent is True


def test_cycle_cap_disarms_after_the_configured_number_of_overs():
    h = Harness()
    g = h.guard(cycle_cap=3)
    g.arm(cycles=3)
    t = time.time()
    for _ in range(3):
        t = _key(h, g, seconds=1.0, swr=1.2, start_at=t) + 0.1
        h.state["is_tx"] = False
        _tick_at(g, t)                  # falling edge counts the cycle
        t += 0.1
    assert not g.armed, "a single tap must not become an all-night CQ machine"
    assert h.trips[-1].reason == TripReason.CYCLE_CAP
    assert h.trips[-1].urgent is False


def test_deadman_refuses_to_arm_when_the_operator_is_gone():
    h = Harness()
    g = h.guard()
    g._last_beat = time.time() - (txmod.DEADMAN_S + 5)
    assert g.arm() is False, "must not start a new transmission unattended"


def test_deadman_lets_the_transmission_in_flight_finish():
    """Cutting a carrier mid-over is worse than letting 12 s complete."""
    h = Harness()
    g = h.guard()
    g.arm()
    t0 = time.time()
    _key(h, g, seconds=2.0, swr=1.2, start_at=t0)
    g._last_beat = t0 - (txmod.DEADMAN_S + 5)      # browser dies mid-over
    _key(h, g, seconds=2.0, swr=1.2, start_at=t0 + 2.0)
    assert h.calls == [], "must not abort a healthy over just because the UI closed"

    h.state["is_tx"] = False
    _tick_at(g, t0 + 5.0)
    assert h.trips[-1].reason == TripReason.DEADMAN
    assert not g.armed, "but it must not be allowed to start another"


def test_status_reports_thresholds_and_last_trip():
    h = Harness()
    g = h.guard()
    g.arm()
    _key(h, g, seconds=3.0, swr=5.0)
    st = g.status()
    assert st["armed"] is False
    assert st["last_trip"]["reason"] == TripReason.SWR
    assert st["thresholds"]["swr_trip"] == txmod.SWR_TRIP
    assert st["thresholds"]["max_tx_s"] == txmod.MAX_TX_S


def test_no_wattage_trip_exists():
    """Guard against a future 'helpful' power ceiling.

    the operator runs 90 W for rare DX on purpose. PO and ALC are displayed, never
    policed — see the module docstring and the 2026-08-29 finding that a limit
    on a SETTING is not a limit on the thing.
    """
    h = Harness()
    g = h.guard()
    g.arm()
    _key(h, g, seconds=12.0, swr=1.2, watts=95.0, step=0.2)
    assert h.calls == [], "95 W at a good match is a deliberate operator choice"


# ------------------------------------------------- 2026-09-01 live regressions

def test_raw_meter_counts_are_converted_not_compared_directly():
    """Raw 39 is SWR 1.35, not 39.

    `/api/state` publishes RM counts, not ratios. The first version of this guard
    compared the raw count against SWR_TRIP=3.0, so it tripped on every single
    transmission and aborted four of the operator's remote TUNEs while reporting his
    healthy antenna as "SWR 52".
    """
    assert txmod.swr_of(39) == pytest.approx(1.35, abs=0.02)
    assert txmod.swr_of(52) == pytest.approx(1.50, abs=0.02)
    assert txmod.swr_of(126) == pytest.approx(3.00, abs=0.02)
    # PO and ALC are equally not raw: full scale is 205 and 64, not 255.
    assert txmod.watts_of(205) == pytest.approx(100, abs=1)
    assert txmod.alc_of(64) == pytest.approx(100, abs=1)


def test_a_good_match_at_full_power_never_trips():
    """Raw 52 = SWR 1.5. This is a NORMAL transmission and must be left alone."""
    h = Harness()
    g = h.guard()
    g.arm()
    _key(h, g, seconds=12.64, swr=1.5, watts=90.0, step=0.1)
    assert h.calls == [], "SWR 1.5 at 90 W is a fine transmission"


def test_a_disarmed_guard_never_aborts_someone_elses_transmission():
    """The operator also drives the radio from the CyberRig panel.

    Remote TUNE, manual PTT and SSTV are not this guard's to police. Disarmed, it
    watched `is_tx`, applied its own limits, and killed four remote TUNEs on
    2026-09-01.
    """
    h = Harness()
    g = h.guard()                      # deliberately NOT armed
    _key(h, g, seconds=30.0, swr=6.0, watts=90.0, step=0.25)
    assert h.calls == [], (
        "a disarmed guard aborted a transmission it did not start")
    assert h.trips == []


def test_arming_resumes_supervision():
    """Scoping must not turn the guard off permanently."""
    h = Harness()
    g = h.guard()
    _key(h, g, seconds=5.0, swr=6.0, step=0.25)
    assert h.calls == []
    g.arm()
    _key(h, g, seconds=3.0, swr=6.0, step=0.05)
    assert h.calls and h.calls[0] == "cat_tx0"
