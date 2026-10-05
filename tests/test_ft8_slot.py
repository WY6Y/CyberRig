"""XIT slot control: the offset must be verified, and it must never be left on.

A stale XIT offset is the nastiest failure available here. Receive is unaffected,
so the operator hears nothing wrong; the radio simply transmits somewhere nobody
asked for, on every over, until someone notices. So these tests care about two
things above all: that a set is confirmed against the radio's own IF register
rather than against our own write, and that every path which ends a transmission
clears it.
"""

from __future__ import annotations

import pytest

from cyberrig.ft8.rig import (SLOT_MAX_HZ, SLOT_MIN_HZ, XIT_MAX_HZ,
                              FT8_BAND_HZ, SlotController, SlotError)


class FakeRig:
    """A radio that actually applies what it is told, and can be made to lie."""

    def __init__(self):
        self.xit = False
        self.clarifier = 0
        self.calls: list[str] = []
        self.lie = False          # report something other than what was set
        self.readback_fails = False

    def set_xit(self, on):
        self.calls.append(f"xit={on}")
        self.xit = bool(on)
        return {"ok": True}

    def set_clarifier(self, hz):
        self.calls.append(f"clar={hz}")
        self.clarifier = int(hz)
        return {"ok": True}

    def clear_clarifier(self):
        self.calls.append("clar_clear")
        self.clarifier = 0
        return {"ok": True}

    def if_register(self):
        if self.readback_fails:
            raise OSError("panel unreachable")
        if self.lie:
            return {"clarifier": self.clarifier + 17, "xit": self.xit}
        return {"clarifier": self.clarifier, "xit": self.xit}


@pytest.fixture
def rig():
    return FakeRig()


@pytest.fixture
def slot(rig):
    return SlotController(rig)


# ------------------------------------------------------------------- placement

def test_offset_is_target_minus_jtdx_slot(slot, rig):
    slot.set_slot(1500, tx_df=1813)
    assert rig.xit is True
    assert rig.clarifier == -313, "emission = tx_df + offset, so offset = target - tx_df"
    st = slot.status()
    assert st["target_slot"] == 1500 and st["offset_hz"] == -313
    assert st["active"] is True and st["verified"] is True


def test_slot_above_jtdx_gives_a_positive_offset(slot, rig):
    slot.set_slot(2400, tx_df=1813)
    assert rig.clarifier == 587


@pytest.mark.parametrize("bad", [0, 100, SLOT_MIN_HZ - 1, SLOT_MAX_HZ + 1, 4000])
def test_slot_outside_the_sub_band_is_refused_not_clamped(slot, bad):
    """A slot quietly moved is worse than a refusal — the operator would not know."""
    with pytest.raises(SlotError):
        slot.set_slot(bad, tx_df=1813)


def test_offset_beyond_clarifier_range_is_refused(slot):
    with pytest.raises(SlotError):
        slot.set_slot(2900, tx_df=2900 - (XIT_MAX_HZ + 100))


def test_matching_slot_clears_rather_than_leaving_xit_on(slot, rig):
    """Zero offset must actually turn XIT off, not merely believe it is zero.

    The precondition matters: an earlier version of this test asked for a
    matching slot on a fresh controller, where XIT was already off, so it passed
    with the clear() removed entirely. Arm XIT first, then ask for a slot that
    needs no offset.
    """
    slot.set_slot(1500, tx_df=1813)
    assert rig.xit is True and rig.clarifier == -313, "precondition: XIT armed"

    slot.set_slot(1813, tx_df=1813)
    assert rig.xit is False, "a zero-offset slot must actively disarm XIT"
    assert rig.clarifier == 0
    assert slot.status()["offset_hz"] == 0
    assert slot.status()["active"] is False


# ---------------------------------------------------------------- verification

def test_a_radio_that_reports_a_different_offset_is_not_verified(slot, rig):
    """The VS0 lesson: the write returning ok is not evidence it took."""
    rig.lie = True
    slot.set_slot(1500, tx_df=1813)
    st = slot.status()
    assert st["verified"] is False
    assert "wanted -313" in st["error"]

    ok, why = slot.safe_to_transmit()
    assert ok is False and why, "an unverified offset must block transmit"


def test_readback_failure_blocks_transmit(slot, rig):
    rig.readback_fails = True
    slot.set_slot(1500, tx_df=1813)
    assert slot.status()["verified"] is False
    assert slot.safe_to_transmit()[0] is False


def test_zero_offset_is_always_safe_to_transmit(slot):
    assert slot.safe_to_transmit() == (True, "")


def test_verified_offset_is_safe_to_transmit(slot):
    slot.set_slot(1500, tx_df=1813)
    assert slot.safe_to_transmit()[0] is True


# --------------------------------------------------------------------- clearing

@pytest.mark.parametrize("hook", ["on_band_change", "on_halt", "on_deadman",
                                  "on_shutdown"])
def test_every_ending_path_clears_the_offset(slot, rig, hook):
    slot.set_slot(1500, tx_df=1813)
    assert rig.xit is True
    getattr(slot, hook)()
    assert rig.xit is False, f"{hook} left XIT applied — silent QRM"
    assert rig.clarifier == 0
    assert slot.status()["offset_hz"] == 0
    assert slot.status()["active"] is False


def test_clear_never_raises_even_when_the_radio_is_gone(slot, rig):
    """clear() runs on abort paths; throwing there would mask the real trip."""
    slot.set_slot(1500, tx_df=1813)

    def boom(*a, **k):
        raise OSError("panel gone")

    rig.set_xit = boom
    rig.clear_clarifier = boom
    slot.clear(reason="abort")          # must not raise
    assert slot.status()["offset_hz"] == 0


def test_clear_turns_xit_off_as_well_as_zeroing_the_offset(slot, rig):
    """Zeroing the clarifier alone leaves XIT enabled and armed for the next set."""
    slot.set_slot(1500, tx_df=1813)
    slot.clear()
    assert "clar_clear" in rig.calls
    assert "xit=False" in rig.calls


# ------------------------------------------------------------------ band table

def test_ft8_band_table_targets_watering_holes_not_ssb_calling_frequencies():
    """CyberRig's own go_band() sends 20m to 14.200, which is not FT8."""
    assert FT8_BAND_HZ["20m"] == 14_074_000
    assert FT8_BAND_HZ["40m"] == 7_074_000
    assert FT8_BAND_HZ["15m"] == 21_074_000
    for band, hz in FT8_BAND_HZ.items():
        assert 1_800_000 <= hz <= 54_000_000, f"{band} out of HF/6m range"
