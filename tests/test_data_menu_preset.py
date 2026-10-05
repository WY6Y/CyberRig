"""The DATA EX menu group, and the radio's own [PRESET] that owns part of it.

Two bugs met here on 2026-09-12. The addresses were in the wrong mode group
(P2=05, MODE RTTY) so every DATA write silently went nowhere; and the FTDX10's
[PRESET] owns four of the same registers, so correcting the group without
noticing that would have started a fight with a front-panel control instead.
"""

from __future__ import annotations

import pytest

from cyberrig.modes import session as mode_session
from cyberrig.modes.session import (
    EX_DATA_BPF,
    EX_DATA_HCUT,
    EX_DATA_LCUT,
    EX_DATA_MOD,
    EX_DATA_REAR,
    EX_DATA_RPORT,
    PRESET_OWNED,
)


class FakeRig:
    """A radio that ignores writes to addresses it does not have -- which is
    what the real one does, and the reason a bad address stayed invisible."""

    VALID = {(1, 4, p) for p in range(1, 19)} | {(1, 5, p) for p in range(1, 17)}

    def __init__(self):
        self.store: dict[tuple, str] = {}
        self.writes: list[tuple] = []

    def ex_write(self, p1, p2, p3, value):
        self.writes.append((p1, p2, p3))
        if (p1, p2, p3) in self.VALID:
            self.store[(p1, p2, p3)] = value

    def ex_read(self, p1, p2, p3):
        return self.store.get((p1, p2, p3))


class _Session(mode_session.DigitalSession):
    label = "test"

    def __init__(self, rig):
        self.rig = rig


@pytest.fixture
def preset_off(monkeypatch):
    monkeypatch.setattr(mode_session, "_preset_active", lambda: False)


@pytest.fixture
def preset_on(monkeypatch):
    monkeypatch.setattr(mode_session, "_preset_active", lambda: True)


# ── the group ───────────────────────────────────────────────────────────────

def test_the_data_registers_are_in_the_psk_data_group():
    """P2=04 per the CAT manual and a live sweep of both groups. P2=05 is
    MODE RTTY, whose SSB-shaped P3 numbers are what made 05 look right."""
    for key in (EX_DATA_LCUT, EX_DATA_HCUT, EX_DATA_BPF,
                EX_DATA_MOD, EX_DATA_REAR, EX_DATA_RPORT):
        assert key[:2] == (1, 4), f"{key} is not in the PSK/DATA group"


def test_rport_gain_is_the_address_that_only_exists_in_this_group():
    """1/5/17 reads None because that group ends at 16 -- the observation that
    made the address look dead when it was only misfiled."""
    assert EX_DATA_RPORT == (1, 4, 17)


# ── verification ────────────────────────────────────────────────────────────

def test_a_write_to_a_missing_address_is_reported_not_swallowed(preset_off):
    rig = FakeRig()
    s = _Session(rig)

    assert s._ex_write_verified((1, 5, 17), "1") is False


def test_a_write_that_lands_verifies(preset_off):
    rig = FakeRig()
    s = _Session(rig)

    assert s._ex_write_verified(EX_DATA_MOD, "1") is True
    assert rig.store[EX_DATA_MOD] == "1"


def test_zero_padded_read_back_still_counts_as_a_match(preset_off):
    """The radio pads to its table's field width, so "050" answers a "50"."""
    rig = FakeRig()
    rig.ex_write = lambda p1, p2, p3, value: rig.store.__setitem__(
        (p1, p2, p3), "050")
    s = _Session(rig)

    assert s._ex_write_verified(EX_DATA_RPORT, "50") is True


# ── PRESET ──────────────────────────────────────────────────────────────────

def test_preset_owned_registers_are_not_written_while_preset_is_on(preset_on):
    rig = FakeRig()
    s = _Session(rig)

    for key in PRESET_OWNED:
        assert s._ex_write_verified(key, "0") is True

    assert rig.writes == [], "wrote underneath a live PRESET"


def test_data_mod_source_is_ours_even_with_preset_on(preset_on):
    """DATA MOD SOURCE is not one of PRESET's 14 items, so it stays ours."""
    rig = FakeRig()
    s = _Session(rig)

    assert EX_DATA_MOD not in PRESET_OWNED
    assert s._ex_write_verified(EX_DATA_MOD, "1") is True
    assert rig.writes == [EX_DATA_MOD]


def test_preset_off_writes_everything(preset_off):
    rig = FakeRig()
    s = _Session(rig)

    for key in PRESET_OWNED:
        s._ex_write_verified(key, "0")

    assert len(rig.writes) == len(PRESET_OWNED)


def test_the_preset_flag_defaults_to_on_when_unreadable(monkeypatch):
    """Defaults true: leaving four registers alone is the cheaper mistake."""
    import cyberrig.settings as settings_mod

    def boom():
        raise RuntimeError("no settings")

    monkeypatch.setattr(settings_mod, "Settings", boom)
    assert mode_session._preset_active() is True
