"""P120 / P121: DNR name is an on/off switch; VFO digits click and wheel."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from cyberrig.panel.widgets import (
    DEFAULT_VFO_STEP,
    FREQ_MAX_HZ,
    FREQ_MIN_HZ,
    FreqDisplay,
    HSlider,
    VSlider,
    digit_steps_for_text,
    format_freq_text,
    nudge_freq,
)


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_format_freq_unpads_mhz():
    assert format_freq_text(7_074_000) == "7.074.000"
    assert format_freq_text(21_074_000) == "21.074.000"
    assert format_freq_text(14_200_000) == "14.200.000"


def test_digit_steps_from_the_right():
    # 21.074.000 → 10 MHz … 1 Hz
    assert digit_steps_for_text("21.074.000") == [
        10_000_000, 1_000_000, 100_000, 10_000, 1_000, 100, 10, 1,
    ]
    # 7.074.000 has no 10 MHz place
    assert digit_steps_for_text("7.074.000") == [
        1_000_000, 100_000, 10_000, 1_000, 100, 10, 1,
    ]


def test_nudge_clamps_to_radio_range():
    assert nudge_freq(21_074_000, 1_000, 1) == 21_075_000
    assert nudge_freq(FREQ_MIN_HZ, 1_000, -1) == FREQ_MIN_HZ
    assert nudge_freq(FREQ_MAX_HZ, 1_000_000, 1) == FREQ_MAX_HZ


def test_dnr_name_toggles_without_moving_the_slider():
    _app()
    s = HSlider("DNR", 1, 15, 6, toggleable=True)
    seen = []
    s.toggled.connect(seen.append)
    moved = []
    s.valueChanged.connect(moved.append)

    assert s.is_on() is False
    QTest.mouseClick(s.name, Qt.LeftButton)
    assert s.is_on() is True
    assert seen == [True]
    assert moved == []
    assert s.slider.value() == 6

    s.set_on_silent(False)
    assert s.is_on() is False
    assert seen == [True]  # silent path does not re-emit


def test_plain_slider_name_is_not_a_switch():
    _app()
    s = HSlider("NB LVL", 0, 10, 5)
    assert not hasattr(s.name, "clicked")
    s.set_on_silent(True)
    assert s.is_on() is False


def test_cont_chip_matches_dnr():
    _app()
    s = HSlider("CONT", -40, 20, 0, toggleable=True)
    seen = []
    s.toggled.connect(seen.append)
    moved = []
    s.valueChanged.connect(moved.append)

    QTest.mouseClick(s.name, Qt.LeftButton)
    assert s.is_on() is True
    assert seen == [True]
    assert moved == []
    assert s.slider.value() == 0
    assert s.height() == 18


def test_toggleable_chip_is_taller_than_plain_row():
    _app()
    plain = HSlider("C WID", 1, 11, 6)
    chip = HSlider("CONT", -40, 20, 0, toggleable=True)
    assert plain.height() == 15
    assert chip.height() == 18


def test_notch_caption_toggles_without_moving_the_slider():
    _app()
    s = VSlider("NOTCH", 10, 3200, 1500, toggleable=True)
    seen = []
    s.toggled.connect(seen.append)
    moved = []
    s.valueChanged.connect(moved.append)

    assert s.is_on() is False
    QTest.mouseClick(s.cap, Qt.LeftButton)
    assert s.is_on() is True
    assert seen == [True]
    assert moved == []
    assert s.slider.value() == 1500

    s.set_on_silent(False)
    assert s.is_on() is False
    assert seen == [True]


def _show_freq(hz=21_074_000, size=34, w=328, h=44) -> FreqDisplay:
    _app()
    d = FreqDisplay(size=size, primary=True)
    d.resize(w, h)
    d.set_freq(hz)
    d.show()
    d.grab()  # force paintEvent so _hits is populated
    return d


def _click(widget, x, y):
    QTest.mouseClick(widget, Qt.LeftButton, Qt.NoModifier, QPoint(int(x), int(y)))


def _wheel(widget, x, y, up=True):
    pos = QPointF(x, y)
    ev = QWheelEvent(
        pos,
        widget.mapToGlobal(pos),
        QPoint(0, 0),
        QPoint(0, 120 if up else -120),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    QApplication.sendEvent(widget, ev)


def _hit(display: FreqDisplay, step: int):
    matches = [rect for rect, s in display._hits if s == step]
    assert matches, f"no digit with step {step} in {display._hits}"
    return matches[0]


def test_vfo_click_top_increases_that_digit():
    d = _show_freq(21_074_000)
    heard = []
    d.tuned.connect(heard.append)
    rect = _hit(d, 1_000)  # the '4' of 074
    _click(d, rect.center().x(), rect.top() + rect.height() // 4)
    assert heard == [21_075_000]
    assert d.freq() == 21_075_000
    assert d.text() == "21.075.000"


def test_vfo_click_bottom_decreases_that_digit():
    d = _show_freq(21_074_000)
    heard = []
    d.tuned.connect(heard.append)
    rect = _hit(d, 1_000_000)  # the '1' of 21
    _click(d, rect.center().x(), rect.top() + 3 * rect.height() // 4)
    assert heard == [20_074_000]


def test_vfo_wheel_on_hovered_digit():
    d = _show_freq(21_074_000)
    heard = []
    d.tuned.connect(heard.append)
    rect = _hit(d, 100)  # 100 Hz
    _wheel(d, rect.center().x(), rect.center().y(), up=True)
    assert heard == [21_074_100]
    _wheel(d, rect.center().x(), rect.center().y(), up=False)
    assert heard == [21_074_100, 21_074_000]


def test_vfo_wheel_after_click_keeps_that_place():
    """Click a digit, then wheel even off-glyph — still that place."""
    d = _show_freq(21_074_000)
    heard = []
    d.tuned.connect(heard.append)
    rect = _hit(d, 10_000)  # 10 kHz, the '7'
    _click(d, rect.center().x(), rect.top() + 2)
    assert heard == [21_084_000]
    # Wheel in the left margin, not on a digit.
    _wheel(d, 2, d.height() // 2, up=False)
    assert heard[-1] == 21_074_000


def test_set_freq_from_radio_does_not_emit_tuned():
    d = _show_freq(7_000_000)
    heard = []
    d.tuned.connect(heard.append)
    d.set_freq(14_074_000)
    d.grab()
    assert heard == []
    assert d.text() == "14.074.000"


def test_default_wheel_step_is_one_khz():
    assert DEFAULT_VFO_STEP == 1_000


# ── VFO A/B, A>B and FINE (wired 2026-08-26; they were dead buttons) ──────

class _VfoRig:
    """Minimal stand-in for the VFO button path."""

    def __init__(self):
        self.state = type("S", (), {"split": False, "fine": False})()
        self.calls = []
        self._handlers = {}
        self.is_connected = False

    def on(self, event, fn):
        self._handlers.setdefault(event, []).append(fn)

    def _fire(self, event, *args):
        for fn in self._handlers.get(event, []):
            fn(*args)

    def swap_vfo(self):
        self.calls.append("SV")

    def vfo_a_to_b(self):
        self.calls.append("AB")

    def set_fine(self, on):
        self.calls.append(("FN", bool(on)))
        self.state.fine = bool(on)
        self._fire("fine_changed", bool(on))

    def set_split(self, on):
        self.calls.append(("SPLIT", bool(on)))

    def request_meters(self, owner, wanted):
        return None


def _panel():
    from cyberrig.panel.window import PanelWindow
    from cyberrig.ui_legacy.rig_adapter import RigProxy
    _app()
    inner = _VfoRig()
    w = PanelWindow(rig=RigProxy(inner), owns_rig=False)
    w._inner = inner
    return w


def test_ab_and_a_to_b_are_actions_not_latching_toggles():
    """A checkable A/B lights up after a click and reads as a mode.

    That is exactly how these three sat here unwired and looked like they
    worked: clicking latched the highlight and sent nothing.
    """
    w = _panel()
    assert w.vfo_btns["A/B"].isCheckable() is False
    assert w.vfo_btns["A>B"].isCheckable() is False
    assert w.vfo_btns["FINE"].isCheckable() is True   # a real radio state
    w.close()


def test_ab_button_swaps_the_vfos():
    w = _panel()
    QTest.mouseClick(w.vfo_btns["A/B"], Qt.LeftButton)
    assert w._inner.calls == ["SV"]
    w.close()


def test_a_to_b_button_copies_a_into_b():
    w = _panel()
    QTest.mouseClick(w.vfo_btns["A>B"], Qt.LeftButton)
    assert w._inner.calls == ["AB"]
    w.close()


def test_fine_button_sends_fn_and_follows_the_radio():
    w = _panel()
    QTest.mouseClick(w.vfo_btns["FINE"], Qt.LeftButton)
    assert w._inner.calls == [("FN", True)]
    assert w.vfo_btns["FINE"].isChecked() is True
    # The radio reporting its own state back must drive the button too —
    # FINE can be switched on the front panel.
    w._inner._fire("fine_changed", False)
    assert w.vfo_btns["FINE"].isChecked() is False
    w.close()
