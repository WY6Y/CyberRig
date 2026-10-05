"""FreeDV TX guards: under this failure, does the radio stop transmitting?

The same question test_sstv_tx.py asks, for the mode that until now had no
guards at all. FreeDV is held-PTT rather than a fixed-length transmission, so
the trips are the SSTV ones plus a stuck-key watchdog, and the meter summary
covers an over instead of a picture.
"""

import time

import pytest

from cyberrig.freedv import guard as guard_mod
from cyberrig.freedv.engine import FreeDVEngine
from cyberrig.freedv.guard import FreeDVTxGuard
from cyberrig.freedv.session import (
    DEFAULT_WATTS,
    FREEDV_MAX_WATTS,
    FreeDVSession,
    clamp_watts,
)
from fakes import FakeCodec, FakeRig


def make(settings=None, **rig_kw):
    """A keyed rig and a guard watching it. Trips are recorded, not acted on."""
    rig = FakeRig(**rig_kw)
    trips = []
    g = FreeDVTxGuard(rig, settings, on_trip=trips.append)
    return rig, g, trips


def key(rig, on=True):
    rig.set_ptt(on)


def wait(g, timeout=2.0):
    """Wait for the watchdog to finish the over. True if it stopped itself."""
    return wait_for(lambda: not g.active, timeout)


def wait_for(cond, timeout=2.0):
    """The guard clears `active` before it calls `on_trip` — dropping RF is
    the callback's job and the flag only stops a second trip — so a test that
    waits on `active` alone can look at `trips` a hair too early."""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ── trips ────────────────────────────────────────────────────────────────

def test_high_swr_drops_the_over(monkeypatch):
    monkeypatch.setattr(guard_mod, "SWR_SETTLE_S", 0.02)
    rig, g, trips = make()
    rig.tx_swr_raw = 150          # SWR 3.5 on the calibration curve
    key(rig)
    g.start()
    assert wait(g)
    assert g.trip and "SWR" in g.trip
    assert wait_for(lambda: trips) and "SWR" in trips[0]


def test_swr_guard_ignores_an_unkeyed_radio(monkeypatch):
    """A radio making no RF reports nonsense. Tripping on nonsense is how a
    guard teaches an operator to switch it off."""
    monkeypatch.setattr(guard_mod, "SWR_SETTLE_S", 0.02)
    rig, g, trips = make()
    rig.tx_swr_raw = 200          # would trip...
    rig.tx_po_raw = 0             # ...but there is no RF
    key(rig)
    g.start()
    time.sleep(0.15)
    assert g.active
    assert not trips
    g.stop()


def test_swr_guard_waits_out_the_settling_window():
    rig, g, trips = make()
    rig.tx_swr_raw = 200
    key(rig)
    g.start()
    time.sleep(0.2)               # all of it inside the 1 s settle window
    assert g.active
    assert not trips
    g.stop()


def test_swr_threshold_comes_from_settings(monkeypatch):
    monkeypatch.setattr(guard_mod, "SWR_SETTLE_S", 0.02)
    rig, g, trips = make(settings={"freedv_max_swr": 2.0})
    rig.tx_swr_raw = 100          # SWR 2.3 — under the default 3.0
    key(rig)
    g.start()
    assert wait(g)
    assert "SWR" in g.trip


def test_a_single_speech_peak_does_not_cut_a_correct_over(monkeypatch):
    """Measured on the air 2026-08-29: a good over ran 23 W average with 33 W
    peaks, and the first one past the target tripped on a 56 W peak while
    averaging 34.9 W with the ALC flat at zero. Heat is average power over
    time, so the excess has to persist before this guard fires."""
    monkeypatch.setattr(guard_mod, "SWR_SETTLE_S", 0.0)
    rig, g, trips = make(settings={"freedv_max_watts_out": 20})
    rig.tx_po_raw = 94            # 25 W
    key(rig)
    g.start()
    time.sleep(0.1)
    rig.state.power_meter = 147   # one 50 W peak, well inside the dwell
    time.sleep(0.1)
    rig.state.power_meter = 94
    time.sleep(0.3)
    assert g.active
    assert not trips
    g.stop()


def test_sustained_overpower_still_trips(monkeypatch):
    monkeypatch.setattr(guard_mod, "SWR_SETTLE_S", 0.0)
    monkeypatch.setattr(guard_mod, "PO_TRIP_DWELL_S", 0.1)
    rig, g, trips = make(settings={"freedv_max_watts_out": 20})
    rig.tx_po_raw = 147           # 50 W and staying there
    key(rig)
    g.start()
    assert wait(g)
    assert "W out for" in g.trip
    assert wait_for(lambda: trips)


def test_measured_watts_out_is_the_thermal_guard(monkeypatch):
    """The power SETTING only moves the ALC threshold. What the finals have to
    survive is what the antenna sees, so that is what trips."""
    monkeypatch.setattr(guard_mod, "SWR_SETTLE_S", 0.02)
    monkeypatch.setattr(guard_mod, "PO_TRIP_DWELL_S", 0.05)
    rig, g, trips = make(settings={"freedv_max_watts_out": 20})
    rig.tx_po_raw = 147           # 50 W on the PO curve
    key(rig)
    g.start()
    assert wait(g)
    assert "W out" in g.trip
    assert wait_for(lambda: trips)


def test_ordinary_power_does_not_trip(monkeypatch):
    monkeypatch.setattr(guard_mod, "SWR_SETTLE_S", 0.02)
    rig, g, trips = make()
    rig.tx_po_raw = 94            # 25 W — the FreeDV target
    key(rig)
    g.start()
    time.sleep(0.2)
    assert g.active
    assert not trips
    g.stop()


def test_a_stuck_ptt_is_forced_off_the_air(monkeypatch):
    """The websocket handler unkeys on disconnect, but a wedged connection
    never disconnects and nothing else in the FreeDV path has a clock."""
    monkeypatch.setattr(guard_mod, "SWR_SETTLE_S", 0.02)
    rig, g, trips = make(settings={"freedv_max_over_s": 0.1})
    key(rig)
    g.start()
    assert wait(g)
    assert "PTT held" in g.trip
    assert wait_for(lambda: trips)


# ── telemetry ────────────────────────────────────────────────────────────

def test_a_clean_over_records_what_the_radio_actually_did(monkeypatch):
    monkeypatch.setattr(guard_mod, "SWR_SETTLE_S", 0.0)
    monkeypatch.setattr(guard_mod, "METER_SAMPLE_S", 0.0)
    rig, g, trips = make()
    rig.tx_po_raw = 94            # 25 W
    rig.tx_alc_raw = 0            # what a correct FreeDV over looks like
    key(rig)
    g.start()
    time.sleep(0.15)
    g.stop()
    assert not trips
    assert g.trip is None
    m = g.meters
    assert m and m["samples"] > 0
    assert m["po_peak"] == pytest.approx(25, abs=1)
    assert m["alc_peak"] == 0


def test_alc_deflection_is_recorded_not_swallowed(monkeypatch):
    """Zero is the target. A number above meter noise means the radio took the
    drive control away from us, and the operator has to be able to see it."""
    monkeypatch.setattr(guard_mod, "SWR_SETTLE_S", 0.0)
    monkeypatch.setattr(guard_mod, "METER_SAMPLE_S", 0.0)
    rig, g, _t = make()
    rig.tx_alc_raw = 32           # 50% of the ALC scale (full scale is 64)
    key(rig)
    g.start()
    time.sleep(0.15)
    g.stop()
    assert g.meters["alc_peak"] == pytest.approx(50, abs=1)
    assert guard_mod.MAX_ALC_PERCENT == 10.0


def test_no_samples_means_no_summary_rather_than_a_fake_one():
    rig, g, _t = make()
    key(rig)
    g.start()
    g.stop()                      # inside the settle window, nothing sampled
    assert g.meters is None


def test_status_exposes_the_live_thresholds():
    _rig, g, _t = make(settings={"freedv_max_watts_out": 40})
    st = g.status()
    assert st["max_watts_out"] == 40
    assert st["max_swr"] == 3.0
    assert st["tx_guard"] is False


# ── the power model ──────────────────────────────────────────────────────

def test_entering_freedv_clamps_power_and_exit_restores_it():
    rig = FakeRig()
    rig.power = 45
    s = FreeDVSession(rig, FakeCodec(), watts=100)
    assert s.enter()["ok"]
    assert rig.power == 100
    assert s.exit()["ok"]
    assert rig.power == 45


def test_the_clamp_sits_high_on_purpose():
    """It is the ALC THRESHOLD, not the output. Putting it on the target makes
    the ALC the limiter — the mistake SSTV spent weeks in."""
    assert DEFAULT_WATTS == 100
    assert FREEDV_MAX_WATTS == 100
    assert clamp_watts(500) == 100
    assert clamp_watts(1) == 5
    assert clamp_watts("nonsense") == DEFAULT_WATTS


def test_changing_watts_while_entered_applies_immediately():
    rig = FakeRig()
    s = FreeDVSession(rig, FakeCodec(), watts=100)
    s.enter()
    assert s.set_watts(60) == 60
    assert rig.power == 60


def test_drive_floor_is_low_enough_to_get_clear_of_the_alc():
    """A few dB is the whole difference between sitting on the ALC and being
    clear of it, and reads as nothing in linear amplitude. The old 0.05 floor
    left an operator aiming lower with nowhere to go."""
    e = FreeDVEngine(codec=object())
    assert e.set_tx_level(0.001) == 0.005
    assert e.set_tx_level(0.01) == pytest.approx(0.01)
    assert e.set_tx_level(5) == 1.0
    assert e.status()["tx_level"] == 1.0
