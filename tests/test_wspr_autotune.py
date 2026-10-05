"""Prove the WSPR interlock before it ever keys the radio overnight.

Same posture as tests/test_ft8_tx_guard.py: the one thing you cannot debug live
is the mechanism that is supposed to stop a runaway, so every refusal and every
trip is demonstrated here against synthetic telemetry with no radio attached.

Tests speak in REAL units (an SWR of 5.0) and convert to the raw 0-255 RM counts
the radio actually publishes, so they drive the same calibration path the code
does. Feeding ratios straight in would bypass the exact conversion that was
wrong on 2026-09-01.
"""

from __future__ import annotations

import time
from collections import deque

import pytest

from cyberrig import wspr as wsprmod
from cyberrig.bands import BANDS
from cyberrig.cat import meter_cal
from cyberrig.wspr import Phase, TripReason, WsprAutoTune, tune_freq_for
from tests.fakes import FakeRig


def raw_swr(ratio):
    return meter_cal.SWR.to_raw(ratio)


# Standard WSPR USB dial frequencies, the ones JTDX hops between.
WSPR_DIALS = {
    "160m": 1_836_600, "80m": 3_568_600, "40m": 7_038_600, "30m": 10_138_700,
    "20m": 14_095_600, "17m": 18_104_600, "15m": 21_094_600, "12m": 24_924_600,
    "10m": 28_124_600, "6m": 50_293_000,
}


class FakeSettings(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakeTuner:
    """Stands in for RemoteTuner. Records the carrier, and can be told what the
    MFJ ends up finding — a good match, or the known no-match quirk."""

    def __init__(self, rig, settled_swr=1.3, fail_to_start=False):
        self._rig = rig
        self.settled_swr = settled_swr
        self.fail_to_start = fail_to_start
        self.active = False
        self.freq_when_keyed = None
        self.starts = 0

    def start(self, watts=None):
        if self.fail_to_start:
            return {"ok": False, "error": "Not connected to radio"}
        self.starts += 1
        self.active = True
        self.freq_when_keyed = self._rig.state.freq_a
        self._rig.state.is_tx = True
        self._rig.state.swr_meter = raw_swr(self.settled_swr)
        return {"ok": True, "tune_active": True}

    def stop(self, restore=True):
        self.active = False
        self._rig.state.is_tx = False
        self._rig.state.swr_meter = 0
        return {"ok": True}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Collapse the real-world waits. The logic under test is the ordering and
    the thresholds, not the clock."""
    monkeypatch.setattr(wsprmod, "POLL_S", 0.005)
    monkeypatch.setattr(wsprmod, "TUNE_SETTLE_S", 0.0)
    monkeypatch.setattr(wsprmod, "TUNE_HOLD_S", 0.02)
    monkeypatch.setattr(wsprmod, "QSY_SETTLE_S", 0.0)


def build(dial="40m", settled_swr=1.3, fail_to_start=False, **settings):
    rig = FakeRig(freq=WSPR_DIALS[dial], mode="DATA-U")
    tuner = FakeTuner(rig, settled_swr=settled_swr, fail_to_start=fail_to_start)
    st = FakeSettings({"wspr_tune_min_s": 0, "wspr_tune_max_s": 0.3,
                       "tune_watts": 20, **settings})
    w = WsprAutoTune(rig, st, tuner)
    # The watchdog thread would race the tests' own tick() calls. Every test
    # here steps it synchronously instead, which is also what makes them
    # deterministic; the thread is just a caller of the same tick().
    w._start_thread = lambda: None
    return rig, tuner, w


# ── the tune frequency: never inside the WSPR window ────────────────────────

def test_tune_frequency_clears_every_wspr_receiver_passband():
    """What the offset has to clear is a WSPR receiver's audio passband -- the
    dial up to about dial+2.6 kHz -- not some arbitrary spectrum distance. A
    carrier inside it wipes that slot out for everyone hearing us. Asserting a
    big number instead (this said >=10 kHz until 2026-09-03) hid the real
    requirement and let a 30 kHz default look correct while it was matching the
    tuner at a frequency the station does not operate on."""
    for band, dial in WSPR_DIALS.items():
        hz = tune_freq_for(band, dial)
        assert hz is not None, band
        assert hz <= dial - 4_000 or hz >= dial + 7_000, \
            f"{band} tunes at {hz}, inside a WSPR receiver's passband around {dial}"


def test_tune_frequency_stays_near_the_dial():
    """The other half, and the one that bit us live: a match found far away is
    a different match. 20 m at dial+30 kHz landed on 14.1256, a known hard spot
    on this MFJ, and failed twice at SWR 4.11."""
    for band, dial in WSPR_DIALS.items():
        assert abs(tune_freq_for(band, dial) - dial) <= 15_000, band


def test_no_tune_frequency_lands_on_an_ncdxf_beacon():
    """14.100 and 18.110 carry the international beacon chain. Going UP 5 kHz
    from the WSPR dial would have parked a 20 W carrier on both."""
    for beacon in (14_100_000, 18_110_000, 21_150_000):
        for band, dial in WSPR_DIALS.items():
            assert abs(tune_freq_for(band, dial) - beacon) > 2_000, (band, beacon)


def test_tune_frequency_stays_inside_the_band():
    for band, dial in WSPR_DIALS.items():
        lo, hi = {b: (l, h) for l, h, b in BANDS}[band]
        hz = tune_freq_for(band, dial)
        assert lo < hz < hi, f"{band}: {hz} outside {lo}-{hi}"


def test_offset_flips_rather_than_leaving_the_band():
    """30 m is 50 kHz wide and WSPR sits at 10.1387, so a +30 kHz offset would
    be out of band. It has to flip, not clamp onto the band edge."""
    hz = tune_freq_for("30m", WSPR_DIALS["30m"], offset_hz=30_000)
    assert hz == 10_138_700 - 30_000


def test_sixty_metres_is_refused_not_improvised():
    """60 m is five fixed channels. There is nowhere to move a tuning carrier
    to, so the answer is None and the caller must hold."""
    assert tune_freq_for("60m", 5_287_200) is None


def test_a_per_band_override_wins_over_the_offset():
    """Operator knowledge the offset cannot derive: a spot the MFJ is known to
    match well on a given band."""
    hz = tune_freq_for("20m", WSPR_DIALS["20m"], override={"20m": 14_090_000})
    assert hz == 14_090_000


def test_six_metres_avoids_the_ft8_calling_frequencies():
    hz = tune_freq_for("6m", WSPR_DIALS["6m"])
    assert abs(hz - 50_313_000) > 5_000 and abs(hz - 50_323_000) > 5_000


# ── the trigger: a band hop, and only a band hop ────────────────────────────

def test_band_hop_triggers_a_tune_and_an_in_band_move_does_not():
    rig, tuner, w = build("40m")
    started = []
    w._begin_tune = lambda hz, band, why: started.append((hz, band))
    w.arm(tune_now=False)

    w.note_set_freq(7_040_000)              # same band, JTDX nudging the dial
    assert started == []

    w.note_set_freq(WSPR_DIALS["20m"])      # band hop
    assert started == [(WSPR_DIALS["20m"], "20m")]


def test_disarmed_is_completely_out_of_the_way():
    """Nothing here may touch the radio until the operator arms it."""
    rig, tuner, w = build("40m")
    started = []
    w._begin_tune = lambda *a: started.append(a)
    w.note_set_freq(WSPR_DIALS["20m"])
    assert started == []
    assert w.allow_ptt(True) is True
    assert rig.calls == []


# ── the veto: skip a beacon rather than rush the tune ───────────────────────

def test_ptt_is_refused_while_tuning():
    """A 10 s tune does not fit the ~9 s gap between WSPR slots, so the beacon
    is skipped. Refusing is the whole reason this sits in the proxy."""
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    w._phase = Phase.TUNING
    assert w.allow_ptt(True) is False
    assert w.status()["skipped_beacons"] == 1


def test_unkeying_is_never_refused():
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    w._phase = Phase.TUNING
    assert w.allow_ptt(False) is True


def test_ptt_is_refused_with_no_cat_link():
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    rig.is_connected = False
    assert w.allow_ptt(True) is False


def test_hold_refuses_this_band_and_a_good_tune_clears_it():
    """One bad band costs one band, not the night: the hold is lifted by the
    next successful tune, which is what the next band hop performs."""
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    w._hold_band(TripReason.NO_MATCH, "no match", urgent=False)
    assert w.allow_ptt(True) is False

    tuner.settled_swr = 1.4
    w._run_tune(WSPR_DIALS["20m"], "20m", "test")
    assert w.status()["phase"] == Phase.IDLE
    assert w.allow_ptt(True) is True


# ── the tune sequence itself ───────────────────────────────────────────────

def test_tune_keys_away_from_the_window_and_puts_the_dial_back():
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    w._run_tune(WSPR_DIALS["40m"], "40m", "test")

    assert tuner.starts == 1
    keyed = tuner.freq_when_keyed
    assert keyed <= WSPR_DIALS["40m"] - 4_000 or keyed >= WSPR_DIALS["40m"] + 7_000
    # Being left 30 kHz off frequency means beaconing into empty spectrum all
    # night while JTDX believes it is working.
    assert rig.state.freq_a == WSPR_DIALS["40m"]
    assert w.status()["last_tune"]["converged"] is True


def test_a_no_match_holds_tx_and_names_the_mfj_quirk_rather_than_the_antenna():
    """Conflating the known MFJ firmware quirk with a real antenna fault would
    train the operator to distrust every page. It is a soft alarm, and it says so."""
    rig, tuner, w = build("40m", settled_swr=6.0)
    w.arm(tune_now=False)
    w._run_tune(WSPR_DIALS["40m"], "40m", "test")

    st = w.status()
    assert st["phase"] == Phase.HOLD
    assert st["hold"]["reason"] == TripReason.NO_MATCH
    assert st["hold"]["urgent"] is False
    assert "MFJ" in st["hold"]["detail"]
    assert rig.state.freq_a == WSPR_DIALS["40m"]   # still put back
    assert w.allow_ptt(True) is False


def test_twelve_metres_at_two_and_a_half_counts_as_a_match():
    """the operator operates 12 m happily at about 2.5:1 through this doublet. A
    tuner that refuses to accept that refuses a band that works."""
    rig, tuner, w = build("12m", settled_swr=2.5)
    w.arm(tune_now=False)
    w._run_tune(WSPR_DIALS["12m"], "12m", "test")
    assert w.status()["last_tune"]["converged"] is True
    assert w.allow_ptt(True) is True


def test_tune_is_not_started_while_the_radio_is_still_keyed():
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    rig.state.is_tx = True
    w._run_tune(WSPR_DIALS["40m"], "40m", "test")
    assert tuner.starts == 0
    assert w.status()["phase"] == Phase.HOLD


def test_channelised_band_holds_instead_of_tuning():
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    w._run_tune(5_287_200, "60m", "test")
    assert tuner.starts == 0
    assert w.status()["hold"]["reason"] == TripReason.CHANNELISED


# ── the watchdog ───────────────────────────────────────────────────────────

def test_stuck_tx_drops_ptt():
    """A WSPR over is 110.6 s. Anything past the ceiling is a stuck key."""
    rig, tuner, w = build("40m", wspr_max_tx_s=10)
    w.arm(tune_now=False)
    rig.set_ptt(True)
    rig.calls.clear()
    w.note_ptt(True)
    w._tx_since = time.time() - 11
    w.tick()

    assert ("set_ptt", False) in rig.calls
    assert w.status()["hold"]["reason"] == TripReason.STUCK_TX
    assert w.status()["hold"]["urgent"] is True


def test_runaway_swr_drops_ptt_after_the_dwell():
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    rig.set_ptt(True)
    rig.state.swr_meter = raw_swr(5.0)
    rig.calls.clear()
    w.note_ptt(True)
    w._tx_since = time.time() - 5          # past the settle window

    w.tick()                                # first bad sample: starts the dwell
    assert ("set_ptt", False) not in rig.calls
    w._bad_since = time.time() - (wsprmod.DWELL_S + 0.1)
    w.tick()

    assert ("set_ptt", False) in rig.calls
    assert w.status()["hold"]["reason"] == TripReason.SWR
    assert w.status()["hold"]["swr"] == pytest.approx(5.0, abs=0.2)


def test_a_healthy_antenna_is_not_tripped_by_its_raw_meter_count():
    """The 2026-09-01 bug: raw 52 is SWR 1.50, a fine match. Compared as a
    number against a threshold of 3.2 it aborts every single transmission."""
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    rig.set_ptt(True)
    rig.state.swr_meter = 52
    rig.calls.clear()
    w.note_ptt(True)
    w._tx_since = time.time() - 5
    for _ in range(20):
        w.tick()
    assert ("set_ptt", False) not in rig.calls
    assert w.status()["phase"] != Phase.HOLD


def test_the_watchdog_ignores_transmissions_it_did_not_permit():
    """The FT8 guard once killed four of the operator's own panel TUNEs by policing
    someone else's TX. Only PTT that came through the proxy is ours."""
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    rig.set_ptt(True)                       # operator, from the panel
    rig.state.swr_meter = raw_swr(9.0)
    rig.calls.clear()
    for _ in range(20):
        w.tick()
    assert rig.calls == []


def test_repeated_trips_hard_stop_the_session():
    rig, tuner, w = build("40m", wspr_max_trips=2, wspr_max_tx_s=10)
    w.arm(tune_now=False)
    for _ in range(2):
        w.clear_hold()
        rig.set_ptt(True)
        w.note_ptt(True)
        w._tx_since = time.time() - 11
        w.tick()

    st = w.status()
    assert st["phase"] == Phase.STOPPED
    assert st["hold"]["reason"] == TripReason.TRIP_LIMIT
    assert w.allow_ptt(True) is False
    # A hard stop is not cleared by the everyday "carry on" button.
    w.clear_hold()
    assert w.allow_ptt(True) is False
    w.resume()
    assert w.allow_ptt(True) is True


def test_session_time_limit_hard_stops():
    rig, tuner, w = build("40m", wspr_session_max_hours=1)
    w.arm(tune_now=False)
    w._armed_at = time.time() - 3601
    assert w.allow_ptt(True) is False
    assert w.status()["phase"] == Phase.STOPPED


def test_disarm_lets_the_radio_go():
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    w.disarm()
    assert w.allow_ptt(True) is True
    assert w.status()["armed"] is False


# ── the mask: what a CAT client is told while we hold the VFO ───────────────
#
# The bug this prevents, live on 2026-09-03: the tune QSYs the shared VFO, JTDX
# polls the rig, sees the tune frequency, and does what a CAT client is
# supposed to do -- follows it. It then beaconed on 18.0996 instead of
# 18.1046, five kHz below the WSPR window where nothing was listening. We are
# the proxy, so the client only knows what we tell it.

def test_freq_and_mode_are_masked_while_tuning():
    rig, tuner, w = build("17m")
    w.arm(tune_now=False)
    w.note_set_freq(WSPR_DIALS["17m"])       # JTDX sets the dial
    w._begin_tune = lambda *a: None          # drive the sequence by hand below

    # Simulate what _begin_tune sets up, then the QSY itself.
    w._phase = Phase.TUNING
    w._tune_dial_hz = WSPR_DIALS["17m"]
    w._tune_mode = "DATA-U"
    rig.set_freq(18_099_600)
    rig.set_mode("AM")

    assert w.mask_freq(rig.state.freq_a) == WSPR_DIALS["17m"]
    assert w.mask_mode(rig.state.mode) == "DATA-U"


def test_the_mask_lifts_the_moment_the_tune_ends():
    rig, tuner, w = build("17m")
    w.arm(tune_now=False)
    w._run_tune(WSPR_DIALS["17m"], "17m", "test")
    assert w.mask_freq(rig.state.freq_a) == rig.state.freq_a == WSPR_DIALS["17m"]
    assert w.mask_mode("DATA-U") == "DATA-U"


def test_the_mask_lifts_even_when_the_tune_fails():
    rig, tuner, w = build("17m", settled_swr=6.0)
    w.arm(tune_now=False)
    w._run_tune(WSPR_DIALS["17m"], "17m", "test")
    assert w.status()["phase"] == Phase.HOLD
    assert w.mask_freq(12_345) == 12_345      # truthful again


def test_nothing_is_masked_when_no_tune_is_running():
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    assert w.mask_freq(7_040_000) == 7_040_000
    assert w.mask_mode("USB") == "USB"


def test_a_set_freq_landing_mid_tune_is_absorbed_and_applied_after():
    """JTDX can move the dial during the six seconds we have the VFO. Writing
    it through would fight the carrier, and our restore would then put the OLD
    dial back -- leaving JTDX beaconing somewhere it never asked for."""
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    w._phase = Phase.TUNING
    w._tune_dial_hz = WSPR_DIALS["40m"]
    assert w.absorb_set_freq(WSPR_DIALS["30m"]) is True
    assert w.mask_freq(0) == WSPR_DIALS["30m"]   # reported as the new intent
    w._phase = Phase.IDLE
    assert w.absorb_set_freq(7_000_000) is False  # not tuning: caller writes it


def test_absorbed_dial_is_where_the_tune_returns_to():
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)

    real_start = tuner.start
    def start_then_retune(watts=None):
        res = real_start(watts)
        w.absorb_set_freq(WSPR_DIALS["30m"])   # JTDX hops again mid-tune
        return res
    tuner.start = start_then_retune

    w._run_tune(WSPR_DIALS["40m"], "40m", "test")
    assert rig.state.freq_a == WSPR_DIALS["30m"]


# ── the silence: armed with nobody attached ────────────────────────────────
#
# 2026-09-04. JTDX dropped the rigctld socket at 22:04 and no beacon could be
# keyed for the next 8h51m. Nothing in the stack noticed, because an unkeyed
# transmitter and a dead band produce the identical evidence: no spots.


def test_absent_cat_client_is_reported_once_armed():
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    w.note_rigctl_clients(1)
    w.note_rigctl_clients(0)

    w.tick()                                     # just left, still inside grace
    assert not [e for e in w.events if "no CAT client" in e["text"]]

    w._clients_zero_since = time.time() - (wsprmod.CLIENT_GONE_S + 1)
    w.tick()
    alerts = [e for e in w.events if "no CAT client" in e["text"]]
    assert len(alerts) == 1
    assert alerts[0]["level"] == "urgent"

    w.tick()                                     # must not repeat every poll
    assert len([e for e in w.events if "no CAT client" in e["text"]]) == 1


def test_client_returning_rearms_the_alert():
    """A reconnect (the operator clicking Retry) resets it, so a SECOND
    disconnection later that night is reported too."""
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    w.note_rigctl_clients(0)
    w._clients_zero_since = time.time() - (wsprmod.CLIENT_GONE_S + 1)
    w.tick()
    assert len([e for e in w.events if "no CAT client" in e["text"]]) == 1

    w.note_rigctl_clients(1)
    assert w.status()["client_gone_s"] == 0
    w.note_rigctl_clients(0)
    w._clients_zero_since = time.time() - (wsprmod.CLIENT_GONE_S + 1)
    w.tick()
    assert len([e for e in w.events if "no CAT client" in e["text"]]) == 2


def test_absent_client_is_silent_while_disarmed():
    """Nothing is expected to be attached when we are not beaconing."""
    rig, tuner, w = build("40m")
    w.note_rigctl_clients(0)
    w._clients_zero_since = time.time() - (wsprmod.CLIENT_GONE_S + 1)
    w.tick()
    assert w.events == deque() or not [e for e in w.events if "no CAT client" in e["text"]]


def test_absent_client_does_not_stop_or_hold_anything():
    """There is nothing to stop -- it is an alert, not a trip."""
    rig, tuner, w = build("40m")
    w.arm(tune_now=False)
    w.note_rigctl_clients(0)
    w._clients_zero_since = time.time() - (wsprmod.CLIENT_GONE_S + 1)
    w.tick()
    assert w.status()["phase"] != Phase.STOPPED
    assert w.status()["hold"] is None
    assert w.allow_ptt(True) is True
