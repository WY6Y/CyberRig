"""Recall ordering: a cross-band channel must land in its stored mode.

Setting mode before frequency looked correct and passed every same-band
check, but a Yaesu restores the target band's last-used mode when the
frequency write crosses bands -- so recalling 3.916 LSB from 40m arrived on
the right frequency in DATA-U (found on the air 2026-08-28).
"""

from cyberrig.memories.model import MemoryChannel
from cyberrig.memories.ops import recall


class _BandStackRig:
    """Rig that reapplies a band's last-used mode on a cross-band tune."""

    def __init__(self, freq, mode, band_stack=None):
        self.freq = freq
        self.mode = mode
        # band (MHz int) -> mode the radio remembers for it
        self.band_stack = band_stack or {}
        self.calls = []

    def _band(self, hz):
        return hz // 1_000_000

    def set_freq(self, hz):
        self.calls.append(("freq", hz))
        if self._band(hz) != self._band(self.freq):
            stacked = self.band_stack.get(self._band(hz))
            if stacked:
                self.mode = stacked
        self.freq = hz

    def set_mode(self, mode):
        self.calls.append(("mode", mode))
        self.mode = mode
        self.band_stack[self._band(self.freq)] = mode

    def set_sh(self, sh):
        self.calls.append(("sh", sh))
        self.sh = sh

    def set_power(self, p):
        self.calls.append(("power", p))
        self.power = p

    def set_att(self, a):
        self.calls.append(("att", a))
        self.att = a

    def set_preamp(self, p):
        self.calls.append(("preamp", p))
        self.preamp = p


def _channel():
    return MemoryChannel(
        name="3916", freq=3_916_000, mode="LSB", power=100,
        sh=18, att="12dB", preamp="IPO",
    )


def test_cross_band_recall_keeps_the_stored_mode():
    # Sitting on 40m FT8; 80m's band-stack remembers DATA-U from earlier.
    rig = _BandStackRig(7_074_000, "DATA-U", band_stack={3: "DATA-U"})
    recall(rig, _channel())
    assert rig.freq == 3_916_000
    assert rig.mode == "LSB", "band-stack overwrote the channel's mode"


def test_mode_is_written_after_frequency():
    rig = _BandStackRig(7_074_000, "DATA-U", band_stack={3: "DATA-U"})
    recall(rig, _channel())
    order = [name for name, _ in rig.calls]
    assert order.index("freq") < order.index("mode"), "freq must precede mode"
    assert order.index("mode") < order.index("sh"), "sh clamps against mode"


def test_same_band_recall_still_works():
    rig = _BandStackRig(3_800_000, "DATA-U")
    recall(rig, _channel())
    assert rig.freq == 3_916_000 and rig.mode == "LSB"
