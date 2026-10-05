"""FreeDV enter/exit is an exclusive radio preset, not a wrapper around freedv-gui."""

from cyberrig.freedv.session import (
    EX_DATA_BPF,
    EX_DATA_HCUT,
    EX_DATA_LCUT,
    EX_DATA_MOD,
    EX_DATA_REAR,
    FreeDVSession,
    SH_WIDE,
    data_mode_for_hz,
)
from fakes import FakeCodec, FakeRig, FakeState  # noqa: F401

import pytest

from cyberrig.modes import session as mode_session


@pytest.fixture(autouse=True)
def _preset_off(monkeypatch):
    """Most of this file predates the radio's [PRESET] and asserts the four
    registers it owns. Default those tests to PRESET-off, which is the
    behaviour they were written against; the PRESET-on case is its own test
    below and in test_data_menu_preset.py."""
    monkeypatch.setattr(mode_session, "_preset_active", lambda: False)


def test_sideband_follows_ssb_convention():
    assert data_mode_for_hz(7_177_000) == "DATA-L"
    assert data_mode_for_hz(3_625_000) == "DATA-L"
    assert data_mode_for_hz(10_130_000) == "DATA-U"
    assert data_mode_for_hz(14_236_000) == "DATA-U"
    assert data_mode_for_hz(28_330_000) == "DATA-U"


def test_enter_uses_data_u_on_20m_and_kills_processor():
    rig = FakeRig(freq=14_236_000, mode="USB")
    codec = FakeCodec()
    s = FreeDVSession(rig, codec)
    r = s.enter()
    assert r["ok"], r
    assert r["active"]
    assert rig.mode == "DATA-U"
    assert rig.compressor is False
    assert rig.nr is False
    assert rig.nb is False
    assert rig.notch is False
    assert rig.contour is False
    assert rig.monitor is False
    assert rig.sh == SH_WIDE
    assert rig.ex[EX_DATA_LCUT] == "00"
    assert rig.ex[EX_DATA_HCUT] == "00"
    assert rig.ex[EX_DATA_BPF] == "0"
    assert rig.ex[EX_DATA_MOD] == "1"
    assert rig.ex[EX_DATA_REAR] == "1"
    assert "freedv" in codec.rx_holders
    assert "freedv" in codec.tx_holders


def test_enter_leaves_the_radios_own_preset_alone(monkeypatch):
    """With the FTDX10's [PRESET] enabled -- which is how this station runs --
    the four registers it owns are its business, not ours. Entering FreeDV
    still sets the mode, the processor and DATA MOD SOURCE, because none of
    those are among PRESET's 14 items."""
    monkeypatch.setattr(mode_session, "_preset_active", lambda: True)
    rig = FakeRig(freq=14_236_000, mode="USB")
    before = dict(rig.ex)

    s = FreeDVSession(rig, FakeCodec())
    assert s.enter()["ok"]

    assert rig.mode == "DATA-U"
    assert rig.compressor is False
    assert rig.ex[EX_DATA_MOD] == "1"
    for key in (EX_DATA_LCUT, EX_DATA_HCUT, EX_DATA_BPF, EX_DATA_REAR):
        assert rig.ex.get(key) == before.get(key), f"{key} was written under PRESET"


def test_enter_uses_data_l_on_40m():
    rig = FakeRig(freq=7_177_000, mode="LSB")
    s = FreeDVSession(rig, FakeCodec())
    r = s.enter()
    assert r["ok"], r
    assert rig.mode == "DATA-L"


def test_exit_restores_snapshot():
    rig = FakeRig(freq=14_236_000, mode="USB")
    rig.compressor = True
    rig.nr = True
    rig.sh = 13
    s = FreeDVSession(rig, FakeCodec())
    assert s.enter()["ok"]
    r = s.exit()
    assert r["ok"], r
    assert not r["active"]
    assert rig.mode == "USB"
    assert rig.compressor is True
    assert rig.nr is True
    assert rig.monitor is True
    assert rig.sh == 13
    assert rig.ex[EX_DATA_MOD] == "0"


def test_refuses_when_remote_tx_holds_the_codec():
    rig = FakeRig()
    codec = FakeCodec(tx_holders=["tx"])
    s = FreeDVSession(rig, codec)
    r = s.enter()
    assert not r["ok"]
    assert "busy" in (r.get("error") or "").lower()
    assert rig.mode == "USB"  # did not touch the radio


def test_codec_open_failure_restores_radio():
    rig = FakeRig()
    codec = FakeCodec(fail=True)
    s = FreeDVSession(rig, codec)
    r = s.enter()
    assert not r["ok"]
    assert rig.mode == "USB"
    assert not s.active


def test_enter_twice_is_idempotent():
    s = FreeDVSession(FakeRig(), FakeCodec())
    assert s.enter()["ok"]
    r = s.enter()
    assert r["ok"] and r.get("already")


def test_qsy_across_10mhz_switches_sideband_while_entered():
    rig = FakeRig(freq=14_236_000, mode="USB")
    s = FreeDVSession(rig, FakeCodec())
    assert s.enter()["ok"]
    assert rig.mode == "DATA-U"
    rig.freq = 7_177_000
    assert s.follow_freq(7_177_000) == "DATA-L"
    assert rig.mode == "DATA-L"
    assert s.radio_mode == "DATA-L"
    # Back up the same way.
    assert s.follow_freq(14_236_000) == "DATA-U"
    assert rig.mode == "DATA-U"


def test_follow_freq_is_quiet_within_one_band():
    rig = FakeRig(freq=14_236_000, mode="USB")
    s = FreeDVSession(rig, FakeCodec())
    assert s.enter()["ok"]
    rig.mode = "SENTINEL"
    assert s.follow_freq(14_240_000) is None
    assert rig.mode == "SENTINEL"   # no CAT write at all


def test_follow_freq_force_reasserts_a_borrowed_mode():
    """The remote tuner puts the radio on AM and hands back what it saved."""
    rig = FakeRig(freq=14_236_000, mode="USB")
    s = FreeDVSession(rig, FakeCodec())
    assert s.enter()["ok"]
    rig.mode = "AM"
    assert s.follow_freq(14_236_000, force=True) is None  # no band change
    assert rig.mode == "DATA-U"                            # but mode restored


def test_follow_freq_does_nothing_when_not_entered():
    rig = FakeRig(freq=14_236_000, mode="USB")
    s = FreeDVSession(rig, FakeCodec())
    assert s.follow_freq(7_177_000) is None
    assert rig.mode == "USB"


# ── FINE TUNING (CAT FN) — verified against Yaesu's FTDX10 CAT reference ──

def test_fine_tuning_parses_the_radios_answer():
    from cyberrig.cat.ftdx10 import FTdx10

    rig = FTdx10.__new__(FTdx10)
    rig._cmd = lambda c: {"FN;": "FN1;"}.get(c)
    assert rig.get_fine() is True
    rig._cmd = lambda c: {"FN;": "FN0;"}.get(c)
    assert rig.get_fine() is False
    rig._cmd = lambda c: None          # wedged link answers nothing
    assert rig.get_fine() is None


def test_set_fine_sends_the_documented_command():
    from cyberrig.cat.ftdx10 import FTdx10

    rig = FTdx10.__new__(FTdx10)
    sent = []
    rig._set = sent.append
    rig._fire = lambda *a: None
    rig.state = type("S", (), {"fine": False})()
    rig.set_fine(True)
    rig.set_fine(False)
    assert sent == ["FN1;", "FN0;"]


# ── VFO swap: SV is documented but inert on this FTDX10 (2026-08-26) ──────

class _SwapRig:
    """Answers FA/FB/MD, records writes, and ignores SV — like the real radio."""

    def __init__(self, a, b, mode_a="USB", mode_b="LSB", honour_sv=False):
        self.a, self.b = a, b
        self.mode_a, self.mode_b = mode_a, mode_b
        self.honour_sv = honour_sv
        self.sent = []
        self.state = type("S", (), {"freq_a": a, "freq_b": b, "mode": mode_a})()

    def _set(self, cmd):
        self.sent.append(cmd)
        if cmd == "SV;" and self.honour_sv:
            self.a, self.b = self.b, self.a
            self.mode_a, self.mode_b = self.mode_b, self.mode_a
        elif cmd.startswith("FA"):
            self.a = int(cmd[2:11])
        elif cmd.startswith("FB"):
            self.b = int(cmd[2:11])
        elif cmd.startswith("MD0") and len(cmd) >= 4:
            from cyberrig.cat.ftdx10 import MODES
            self.mode_a = MODES.get(cmd[3], self.mode_a)
        elif cmd.startswith("MD1") and len(cmd) >= 4:
            from cyberrig.cat.ftdx10 import MODES
            self.mode_b = MODES.get(cmd[3], self.mode_b)

    def _cmd(self, cmd):
        from cyberrig.cat.ftdx10 import MODE_CODES
        if cmd == "MD0;":
            return f"MD0{MODE_CODES.get(self.mode_a, '2')}"
        if cmd == "MD1;":
            return f"MD1{MODE_CODES.get(self.mode_b, '2')}"
        return None

    def _fire(self, *a):
        pass

    def get_freq(self):
        return self.a

    def get_freq_b(self):
        return self.b


def _swap(rig):
    """Drive the real swap_vfo against the fake, with the real refresh."""
    from cyberrig.cat.ftdx10 import FTdx10

    rig._refresh_vfos = lambda: FTdx10._refresh_vfos(rig)
    rig._refresh_mode = lambda: FTdx10._refresh_mode(rig)
    rig.get_mode = lambda: FTdx10.get_mode(rig)
    rig.get_mode_b = lambda: FTdx10.get_mode_b(rig)
    # set_freq/set_mode mirror to VFO B when single-VFO mode is on; the swap
    # path is only reachable with it off, so pin it off here explicitly.
    rig.single_vfo = False
    rig._mirror_a_to_b = lambda: FTdx10._mirror_a_to_b(rig)
    rig.set_freq = lambda hz: FTdx10.set_freq(rig, hz)
    rig.set_freq_b = lambda hz: FTdx10.set_freq_b(rig, hz)
    rig.set_mode = lambda m: FTdx10.set_mode(rig, m)
    rig.set_mode_b = lambda m: FTdx10.set_mode_b(rig, m)
    FTdx10.swap_vfo(rig)


def test_swap_falls_back_when_the_radio_ignores_sv():
    rig = _SwapRig(7_200_700, 7_230_000, mode_a="USB", mode_b="LSB", honour_sv=False)
    _swap(rig)
    assert rig.sent[0] == "SV;"                  # documented command first
    assert rig.a == 7_230_000 and rig.b == 7_200_700
    assert "FA007230000;" in rig.sent and "FB007200700;" in rig.sent
    # Modes must travel with the frequencies — FA/FB alone recall band-stack.
    assert "MD12;" in rig.sent                   # B gets old A's USB
    assert "MD01;" in rig.sent                   # A gets old B's LSB
    assert rig.mode_a == "LSB" and rig.mode_b == "USB"


def test_swap_leaves_a_radio_that_honours_sv_alone():
    rig = _SwapRig(7_200_700, 7_230_000, honour_sv=True)
    _swap(rig)
    assert rig.sent == ["SV;"]                   # no fallback writes
    assert rig.a == 7_230_000 and rig.b == 7_200_700


def test_swap_of_two_equal_vfos_writes_nothing_extra():
    rig = _SwapRig(7_200_000, 7_200_000, honour_sv=False)
    _swap(rig)
    assert rig.sent == ["SV;"]


def test_refresh_mode_trusts_md0_not_stale_if():
    """IF P6 can lag MD0 after VFO writes; state.mode must follow MD0."""
    from cyberrig.cat.ftdx10 import FTdx10

    rig = FTdx10.__new__(FTdx10)
    rig.state = type("S", (), {"mode": "USB"})()
    events = []
    rig._fire = lambda ev, *a: events.append((ev, a))
    rig.get_mode = lambda: "DATA-U"
    FTdx10._refresh_mode(rig)
    assert rig.state.mode == "DATA-U"
    assert events == [("mode_changed", ("DATA-U",))]


# ── FT (TX VFO) is asymmetric like GT — set 2/3, answer 0/1 ───────────────

def _ft_rig(answer):
    from cyberrig.cat.ftdx10 import FTdx10

    rig = FTdx10.__new__(FTdx10)
    rig._cmd = lambda c: {"FT;": answer}.get(c)
    return rig


def test_tx_vfo_is_read_with_the_answer_codes_not_the_set_codes():
    from cyberrig.cat.ftdx10 import FTdx10

    assert FTdx10.get_tx_vfo(_ft_rig("FT0;")) == "A"   # MAIN
    assert FTdx10.get_tx_vfo(_ft_rig("FT1;")) == "B"   # SUB
    # The SET codes must NOT be accepted as answers — that is the bug this
    # guards: parsing an answer with the set table silently never matches.
    assert FTdx10.get_tx_vfo(_ft_rig("FT2;")) is None
    assert FTdx10.get_tx_vfo(_ft_rig("FT3;")) is None
    assert FTdx10.get_tx_vfo(_ft_rig(None)) is None


def test_set_split_writes_the_set_codes():
    from cyberrig.cat.ftdx10 import FTdx10

    rig = FTdx10.__new__(FTdx10)
    sent = []
    rig._set = sent.append
    rig._fire = lambda *a: None
    rig.state = type("S", (), {"split": False, "tx_vfo": "A"})()
    FTdx10.set_split(rig, True, "B")
    assert sent == ["ST1;", "FT3;"]
    assert rig.state.tx_vfo == "B"
    sent.clear()
    FTdx10.set_split(rig, True, "A")
    assert sent == ["ST1;", "FT2;"]


def test_split_off_does_not_claim_a_tx_vfo():
    """No FT is written, so the cached value is not ours to keep asserting."""
    from cyberrig.cat.ftdx10 import FTdx10

    rig = FTdx10.__new__(FTdx10)
    sent = []
    rig._set = sent.append
    rig._fire = lambda *a: None
    rig.state = type("S", (), {"split": True, "tx_vfo": "B"})()
    FTdx10.set_split(rig, False)
    assert sent == ["ST0;"]
    assert rig.state.split is False
    assert rig.state.tx_vfo == "B"      # untouched; the poll corrects it
