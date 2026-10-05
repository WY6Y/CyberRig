"""SSTV transmit: the interlocks, the guards, and getting off the air.

Every test here is really asking one question — under this failure, does the
radio stop transmitting? A test that only checks the happy path would pass on
code that keys the rig and never unkeys it.
"""

import threading
import time

import numpy as np
import pytest

from cyberrig.sstv import tx as tx_mod
from cyberrig.sstv.modes import SSTVMode
from cyberrig.sstv.session import SSTVSession
from cyberrig.sstv.tx import SSTVTransmitter
from fakes import FakeCodec, FakeRig

# A four-line mode so a whole "transmission" is a fraction of a second. The
# 910 ms VIS header is fixed by the spec and dominates it.
TINY = SSTVMode("Tiny", 0x3C, 16, 4, "martin", 2.0, 1.0, 0.5)


def img(mode=TINY):
    return np.full((mode.height, mode.width, 3), 128, dtype=np.uint8)


def make(entered=True, **rig_kw):
    rig = FakeRig(**rig_kw)
    codec = FakeCodec()
    session = SSTVSession(rig, codec, watts=30)
    if entered:
        assert session.enter()["ok"]
    t = SSTVTransmitter(rig, codec, session, settings=None)
    return rig, codec, session, t


def run_to_completion(codec, t, timeout=10.0):
    """Pump the fake sound card until the transmitter says it is done."""
    deadline = time.time() + timeout
    while t.active and time.time() < deadline:
        codec.pump(blocks=50)
        time.sleep(0.005)
    return not t.active


# ── refusing to start ────────────────────────────────────────────────────

def test_refuses_when_sstv_is_not_entered():
    rig, codec, _s, t = make(entered=False)
    r = t.start(img(), TINY)
    assert not r["ok"]
    assert "not entered" in r["error"]
    assert rig.ptt_log == []


def test_refuses_when_the_radio_is_already_transmitting():
    rig, codec, _s, t = make()
    rig.state.is_tx = True
    r = t.start(img(), TINY)
    assert not r["ok"]
    assert "already transmitting" in r["error"]


def test_refuses_to_key_blind_with_no_cat():
    rig, codec, _s, t = make()
    rig.is_connected = False
    r = t.start(img(), TINY)
    assert not r["ok"]
    assert "CAT" in r["error"]
    assert rig.ptt_log == []


def test_refuses_a_second_transmission_while_one_is_running():
    rig, codec, _s, t = make()
    assert t.start(img(), TINY)["ok"]
    try:
        r = t.start(img(), TINY)
        assert not r["ok"]
        assert "already" in r["error"]
    finally:
        t.abort()


def test_an_odd_sized_image_is_fitted_rather_than_refused():
    """prepare_image exists so the caller never has to match the mode geometry."""
    rig, codec, _s, t = make()
    assert t.start(np.zeros((3, 3, 3), np.uint8), TINY)["ok"]
    assert run_to_completion(codec, t)
    assert t.result == "sent"


def test_a_bad_image_fails_before_the_radio_is_keyed():
    rig, codec, _s, t = make()
    r = t.start("/nonexistent/not-a-picture.png", TINY)
    assert not r["ok"]
    assert "encode failed" in r["error"]
    assert rig.ptt_log == [], "must not key on an image we cannot send"
    assert not t.active


# ── the happy path ───────────────────────────────────────────────────────

def test_a_transmission_keys_sends_and_unkeys():
    rig, codec, _s, t = make()
    assert t.start(img(), TINY)["ok"]
    assert run_to_completion(codec, t), "transmission never finished"
    assert t.result == "sent"
    assert t.error is None
    assert rig.ptt_log == [True, False]
    assert rig.state.is_tx is False
    assert codec.output_handler is None, "must release the codec output"


def test_progress_reaches_the_end():
    rig, codec, _s, t = make()
    t.start(img(), TINY)
    assert run_to_completion(codec, t)
    # Status reports zeroes once idle, so sample the counters directly.
    assert t._played >= t._total


def test_a_real_mode_round_trips():
    """Robot 36 is 36 s on the air; the pump runs it in a fraction of that."""
    rig, codec, _s, t = make()
    from cyberrig.sstv.modes import get_mode
    m = get_mode("Robot 36")
    assert t.start(img(m), m)["ok"]
    assert run_to_completion(codec, t, timeout=30)
    assert t.result == "sent"
    assert rig.ptt_log == [True, False]


def test_audio_actually_reaches_the_sound_card():
    rig, codec, _s, t = make()
    heard = []
    real = None

    def spy(outdata, frames):
        real(outdata, frames)
        heard.append(float(np.abs(outdata).max()))

    t.start(img(), TINY)
    real = codec.output_handler
    codec.output_handler = spy
    run_to_completion(codec, t)
    assert max(heard) > 0.1, "the transmitter sent silence"


# ── stopping ─────────────────────────────────────────────────────────────

def test_abort_drops_ptt_immediately():
    rig, codec, _s, t = make()
    t.start(img(), TINY)
    # Let the watchdog key it.
    for _ in range(200):
        if rig.state.is_tx:
            break
        time.sleep(0.005)
    assert rig.state.is_tx, "never keyed"
    t.abort()
    assert rig.state.is_tx is False
    assert t.result == "aborted"
    assert not t.active
    assert codec.output_handler is None


def test_abort_before_anything_is_running_is_harmless():
    _rig, _codec, _s, t = make()
    r = t.abort()
    assert r["ok"] and r.get("already")


def test_aborting_silences_the_output_even_if_the_card_keeps_calling():
    """The codec is detached on abort, but a callback already in flight on the
    PortAudio thread must still come out silent rather than mid-picture."""
    rig, codec, _s, t = make()
    t.start(img(), TINY)
    handler = codec.output_handler
    t.abort()
    assert codec.output_handler is None
    out = np.ones((256, 2), dtype=np.float32)
    handler(out, 256)
    assert float(np.abs(out).max()) == 0.0


# ── guards ───────────────────────────────────────────────────────────────

def test_high_swr_stops_the_transmission(monkeypatch):
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    rig, codec, _s, t = make()
    rig.tx_swr_raw = 150          # SWR 3.5 on the calibration curve
    t.start(img(), TINY)
    assert run_to_completion(codec, t)
    assert t.result == "tripped"
    assert "SWR" in t.error
    assert rig.state.is_tx is False


def test_swr_guard_ignores_an_unkeyed_radio(monkeypatch):
    """A radio that is not making RF reports nonsense; tripping on it would
    teach the operator to switch the guard off."""
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    rig, codec, _s, t = make()
    rig.tx_swr_raw = 200          # would trip...
    rig.tx_po_raw = 0             # ...but there is no RF
    t.start(img(), TINY)
    assert run_to_completion(codec, t)
    assert t.result == "sent"


def test_swr_guard_waits_out_the_settling_window():
    rig, codec, _s, t = make()
    rig.tx_swr_raw = 200
    t.start(img(), TINY)
    # TINY is under a second, all of it inside the 1 s settle window.
    assert run_to_completion(codec, t)
    assert t.result == "sent"


def test_swr_threshold_comes_from_settings(monkeypatch):
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    rig = FakeRig()
    codec = FakeCodec()
    session = SSTVSession(rig, codec, watts=30)
    session.enter()
    rig.tx_swr_raw = 100          # SWR 2.3 — under the default 3.0
    t = SSTVTransmitter(rig, codec, session, settings={"sstv_max_swr": 2.0})
    t.start(img(), TINY)
    assert run_to_completion(codec, t)
    assert t.result == "tripped"


def test_a_stalled_transmission_is_forced_off_the_air(monkeypatch):
    """Nothing drains the audio, so it can never finish on its own."""
    monkeypatch.setattr(tx_mod, "OVERRUN_GRACE_S", 0.05)
    rig, codec, _s, t = make()
    t.start(img(), TINY)
    deadline = time.time() + 5
    while t.active and time.time() < deadline:
        time.sleep(0.01)
    assert not t.active, "the watchdog never fired"
    assert t.result == "tripped"
    assert "ran past" in t.error
    assert rig.state.is_tx is False


def test_a_rig_that_refuses_to_key_does_not_report_transmitting():
    rig, codec, _s, t = make()

    def refuse(on):
        raise RuntimeError("CAT died")

    rig.set_ptt = refuse
    t.start(img(), TINY)
    deadline = time.time() + 3
    while t.active and time.time() < deadline:
        codec.pump(blocks=20)
        time.sleep(0.005)
    assert t._keyed is False


def test_status_is_safe_to_read_before_anything_happens():
    _rig, _codec, _s, t = make()
    st = t.status()
    assert st["tx_active"] is False
    assert st["tx_progress"] == 0.0
    assert st["tx_mode"] is None


def test_the_fsk_callsign_is_not_cut_off_by_the_watchdog():
    """Sizing the transmission on the mode alone would end it mid-word."""
    from cyberrig.sstv.modes import fsk_id_ms

    rig, codec, _s, t = make()
    assert t.start(img(), TINY, fsk_id="WY6Y")["ok"]
    extra = fsk_id_ms("WY6Y") / 1000.0 * 48000
    assert t._total == pytest.approx(
        round(TINY.duration_s * 48000) + extra, abs=3)
    assert run_to_completion(codec, t)
    assert t.result == "sent"
    assert rig.ptt_log == [True, False]


def test_status_reports_the_length_including_the_callsign():
    rig, codec, _s, t = make()
    t.start(img(), TINY, fsk_id="WY6Y")
    try:
        assert t.status()["tx_seconds"] > TINY.duration_s
    finally:
        t.abort()


# ── meter telemetry ──────────────────────────────────────────────────────

def test_a_transmission_records_what_the_radio_actually_did(monkeypatch):
    """"Sent" is not evidence of RF.

    The audio stream and PTT were the only things a completed transmission
    ever proved. After nine on-air pictures and one report, "is TX working?"
    could not be answered from anything on disk. PO/ALC/SWR from the radio's
    own meters answer it.
    """
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    monkeypatch.setattr(tx_mod, "METER_SAMPLE_S", 0.01)
    rig, codec, _s, t = make()
    rig.tx_po_raw = 94            # 25 W on the PO curve
    rig.tx_alc_raw = 32           # 50% ALC
    t.start(img(), TINY)
    assert run_to_completion(codec, t)
    assert t.result == "sent"
    m = t.meters
    assert m is not None and m["samples"] >= 1
    assert m["po_peak"] == pytest.approx(25.0, abs=0.5)
    assert m["alc_peak"] == pytest.approx(50.0, abs=1.0)
    assert m["swr_peak"] == pytest.approx(1.2, abs=0.1)
    assert t.status()["tx_meters"] == m


def test_a_transmission_that_made_no_rf_says_so(monkeypatch, caplog):
    """The failure this whole thing exists to catch: keyed, streamed, silent."""
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    monkeypatch.setattr(tx_mod, "METER_SAMPLE_S", 0.01)
    rig, codec, _s, t = make()
    rig.tx_po_raw = 0
    rig.tx_alc_raw = 0
    with caplog.at_level("WARNING", logger="cyberrig.sstv"):
        t.start(img(), TINY)
        assert run_to_completion(codec, t)
    assert t.result == "sent"
    assert t.meters["po_peak"] == 0.0
    assert any("no measurable RF" in r.message for r in caplog.records)


def test_running_on_the_alc_is_flagged_as_over_driven(monkeypatch, caplog):
    """ALC deflection means the radio is limiting instead of the drive.

    This test used to assert the opposite — that zero ALC at full power was
    the fault — because the theory at the time was under-modulation. With PO
    set well above the target so the drive controls output, zero ALC is what
    a CORRECT transmission looks like, and the old warning fired on every
    good one.
    """
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    monkeypatch.setattr(tx_mod, "METER_SAMPLE_S", 0.01)
    rig, codec, _s, t = make()
    rig.tx_po_raw = 94            # 25 W out...
    rig.tx_alc_raw = 32           # ...with the ALC doing the limiting at 50%
    with caplog.at_level("WARNING", logger="cyberrig.sstv"):
        t.start(img(), TINY)
        assert run_to_completion(codec, t)
    assert any("ALC" in r.message and "limiting" in r.message
               for r in caplog.records)


def test_a_clean_transmission_warns_about_nothing(monkeypatch, caplog):
    """The shape of a good one: power out, ALC flat. Must be silent."""
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    monkeypatch.setattr(tx_mod, "METER_SAMPLE_S", 0.01)
    rig, codec, _s, t = make()
    rig.tx_po_raw = 103           # ~29 W, the level this station settled on
    rig.tx_alc_raw = 0            # ALC never moves — this is CORRECT
    with caplog.at_level("WARNING", logger="cyberrig.sstv"):
        t.start(img(), TINY)
        assert run_to_completion(codec, t)
    assert t.result == "sent"
    assert t.meters["alc_peak"] == 0.0
    assert [r.message for r in caplog.records] == []


def test_meters_are_not_sampled_before_the_radio_is_keyed():
    """An unkeyed radio reports nonsense — the same reason the SWR guard waits."""
    rig, codec, _s, t = make()
    rig.tx_po_raw = 94
    t._sample_meters(elapsed=0.0)          # not keyed yet
    assert t._meter_summary() is None


def test_each_transmission_starts_its_meters_from_scratch(monkeypatch):
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    monkeypatch.setattr(tx_mod, "METER_SAMPLE_S", 0.01)
    rig, codec, _s, t = make()
    rig.tx_po_raw = 176           # 75 W
    t.start(img(), TINY)
    assert run_to_completion(codec, t)
    assert t.meters["po_peak"] == pytest.approx(75.0, abs=1.0)
    rig.tx_po_raw = 27            # 5 W on the next one
    t.start(img(), TINY)
    assert run_to_completion(codec, t)
    assert t.meters["po_peak"] == pytest.approx(5.0, abs=0.5)


# ── measured-output trip ─────────────────────────────────────────────────
#
# The power CLAMP sets the ALC threshold; it is not what the radio radiates.
# Clamping it to the target made the ALC the limiter and left this station
# transmitting at 30% ALC every time. The ceiling now sits high so the audio
# drive sets output, which means the thermal guard has to watch the antenna
# instead of a menu setting — SSTV is up to 270 s at 100% duty.

def test_radiating_over_the_limit_stops_the_transmission(monkeypatch):
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    rig, codec, _s, t = make()
    rig.tx_po_raw = 205           # 100 W on the PO curve
    t.start(img(), TINY)
    assert run_to_completion(codec, t)
    assert t.result == "tripped"
    assert "over the" in t.error
    assert rig.state.is_tx is False


def test_normal_operating_power_does_not_trip(monkeypatch):
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    rig, codec, _s, t = make()
    rig.tx_po_raw = 94            # 25 W — a normal SSTV transmission
    t.start(img(), TINY)
    assert run_to_completion(codec, t)
    assert t.result == "sent"


def test_the_output_limit_comes_from_settings(monkeypatch):
    monkeypatch.setattr(tx_mod, "SWR_SETTLE_S", 0.02)
    rig = FakeRig()
    codec = FakeCodec()
    session = SSTVSession(rig, codec, watts=100)
    session.enter()
    rig.tx_po_raw = 94            # 25 W — under the default 60 W limit...
    t = SSTVTransmitter(rig, codec, session,
                        settings={"sstv_max_watts_out": 20})   # ...over this one
    t.start(img(), TINY)
    assert run_to_completion(codec, t)
    assert t.result == "tripped"


def test_the_output_trip_ignores_an_unkeyed_radio():
    """Same reason the SWR guard does: an unkeyed radio reports nonsense."""
    rig, codec, _s, t = make()
    rig.tx_po_raw = 205
    assert t._power_trip(elapsed=99.0) is None      # never keyed


def test_the_power_clamp_can_now_reach_a_hundred_watts():
    """The ceiling is where the ALC threshold goes, not what we radiate."""
    from cyberrig.sstv.session import SSTV_MAX_WATTS, clamp_watts
    assert SSTV_MAX_WATTS == 100
    assert clamp_watts(100) == 100
    assert clamp_watts(150) == 100
    assert clamp_watts(1) == 5
