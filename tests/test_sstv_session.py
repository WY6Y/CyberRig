"""SSTV shares FreeDV's radio preset and adds a TX power clamp.

The clamp is the reason this mode gets its own session class: Scottie DX is
269 seconds of unbroken key-down, and 100 W of that is a finals problem.
"""

import pytest

from cyberrig.freedv.session import FreeDVSession
from cyberrig.modes.session import SH_WIDE, EX_DATA_MOD, active_session
from cyberrig.sstv.session import DEFAULT_WATTS, SSTV_MAX_WATTS, SSTVSession, clamp_watts
from fakes import FakeCodec, FakeRig


def test_enter_applies_the_same_digital_preset_as_freedv():
    rig = FakeRig(freq=14_230_000, mode="USB")
    codec = FakeCodec()
    assert SSTVSession(rig, codec).enter()["ok"]
    assert rig.mode == "DATA-U"
    assert rig.compressor is False
    assert rig.nr is False
    assert rig.monitor is False
    assert rig.sh == SH_WIDE
    assert rig.ex[EX_DATA_MOD] == "1"
    assert "sstv" in codec.rx_holders
    assert "sstv" in codec.tx_holders


def test_sstv_calling_frequencies_pick_the_right_sideband():
    # 3.845 and 7.171 are LSB by convention; 14.230 / 21.340 / 28.680 USB.
    assert SSTVSession(FakeRig(freq=3_845_000), FakeCodec()).enter()["radio_mode"] == "DATA-L"
    assert SSTVSession(FakeRig(freq=7_171_000), FakeCodec()).enter()["radio_mode"] == "DATA-L"
    assert SSTVSession(FakeRig(freq=14_230_000), FakeCodec()).enter()["radio_mode"] == "DATA-U"
    assert SSTVSession(FakeRig(freq=28_680_000), FakeCodec()).enter()["radio_mode"] == "DATA-U"


def test_enter_clamps_power_and_exit_restores_it():
    rig = FakeRig()
    rig.power = 100
    s = SSTVSession(rig, FakeCodec(), watts=30)
    assert s.enter()["ok"]
    assert rig.power == 30
    assert s.exit()["ok"]
    assert rig.power == 100


def test_power_clamp_has_a_ceiling_the_setting_cannot_beat():
    rig = FakeRig()
    rig.power = 100
    s = SSTVSession(rig, FakeCodec(), watts=100)
    assert s.watts == SSTV_MAX_WATTS
    assert s.enter()["ok"]
    assert rig.power == SSTV_MAX_WATTS


def test_clamp_watts_bounds_and_bad_input():
    assert clamp_watts(0) == 5
    assert clamp_watts(999) == SSTV_MAX_WATTS
    assert clamp_watts("nonsense") == DEFAULT_WATTS
    assert clamp_watts(None) == DEFAULT_WATTS
    assert clamp_watts(25) == 25


def test_set_watts_applies_live_while_entered():
    rig = FakeRig()
    s = SSTVSession(rig, FakeCodec(), watts=30)
    assert s.enter()["ok"]
    assert s.set_watts(20) == 20
    assert rig.power == 20


def test_power_is_restored_even_if_a_later_restore_write_fails():
    rig = FakeRig()
    rig.power = 100
    s = SSTVSession(rig, FakeCodec(), watts=30)
    assert s.enter()["ok"]

    def boom(mode):
        raise RuntimeError("CAT died")

    rig.set_mode = boom
    r = s.exit()
    assert not r["ok"]
    assert rig.power == 100, "the clamp must come off before the SSB profile"


def test_sstv_refuses_while_freedv_is_entered_and_says_so():
    codec = FakeCodec()
    fdv = FreeDVSession(FakeRig(), codec)
    assert fdv.enter()["ok"]

    rig = FakeRig()
    r = SSTVSession(rig, codec).enter()
    assert not r["ok"]
    assert "freedv" in (r["error"] or "").lower()
    assert rig.mode == "USB"       # did not touch the radio
    assert rig.power == 100        # and did not clamp power


def test_freedv_refuses_while_sstv_is_entered():
    codec = FakeCodec()
    assert SSTVSession(FakeRig(), codec, watts=30).enter()["ok"]
    r = FreeDVSession(FakeRig(), codec).enter()
    assert not r["ok"]
    assert "sstv" in (r["error"] or "").lower()


def test_exiting_frees_the_slot_for_the_other_mode():
    codec = FakeCodec()
    s = SSTVSession(FakeRig(), codec, watts=30)
    assert s.enter()["ok"]
    assert s.exit()["ok"]
    assert active_session() is None
    assert FreeDVSession(FakeRig(), codec).enter()["ok"]


def test_refuses_when_varac_or_jtdx_holds_the_codec():
    rig = FakeRig()
    r = SSTVSession(rig, FakeCodec(tx_holders=["tx"])).enter()
    assert not r["ok"]
    assert "busy" in (r["error"] or "").lower()
    assert rig.power == 100


@pytest.mark.parametrize("bad", [None])
def test_works_without_a_codec_injected(bad):
    rig = FakeRig()
    s = SSTVSession(rig, bad, watts=25)
    assert s.enter()["ok"]
    assert rig.power == 25
    assert s.exit()["ok"]
    assert rig.power == 100


def test_the_sstv_drive_default_is_the_level_that_was_measured_clean():
    """0.01 is what this station settled on, and the default has to match.

    `sstv_watts` is the ALC THRESHOLD, not the output — with the ceiling at
    100 W the ALC never engages and the drive alone sets power. That makes a
    half-scale default a very loud accident rather than a merely wrong one:
    measured 2026-08-29, 0.01 gives ~28 W with the ALC flat, 0.05 gave 43 W
    sitting on the ALC. SSTV only; FreeDV and Remote TX carry their own.
    """
    from cyberrig.settings import DEFAULTS
    assert DEFAULTS["sstv_tx_level"] == 0.01
    assert DEFAULTS["sstv_watts"] == 30          # a safe ceiling out of the box
