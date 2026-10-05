"""P124 / P125: ATT and IPO panel buttons cycle levels, not binary toggles."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cyberrig.cat.ftdx10 import (
    ATT_CYCLE, PREAMP_CYCLE, next_att, next_preamp,
)
from cyberrig.panel.window import PanelWindow
from cyberrig.ui_legacy.rig_adapter import RigProxy


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


class _InnerRig:
    """Minimal stand-in: state + setters the panel ATT/IPO path needs."""

    def __init__(self):
        self.state = type("S", (), {"att": "OFF", "preamp": "IPO"})()
        self.att_sets = []
        self.preamp_sets = []
        self._handlers = {}
        self.is_connected = False

    def on(self, event, fn):
        self._handlers.setdefault(event, []).append(fn)

    def _fire(self, event, *args):
        for fn in self._handlers.get(event, []):
            fn(*args)

    def set_att(self, mode):
        self.att_sets.append(mode)
        self.state.att = mode
        self._fire("att_changed", mode)

    def set_preamp(self, mode):
        self.preamp_sets.append(mode)
        self.state.preamp = mode
        self._fire("preamp_changed", mode)

    def request_meters(self, owner, wanted):
        return None


def test_next_att_cycles_all_four_then_wraps():
    assert ATT_CYCLE == ["OFF", "6dB", "12dB", "18dB"]
    assert next_att("OFF") == "6dB"
    assert next_att("6dB") == "12dB"
    assert next_att("12dB") == "18dB"
    assert next_att("18dB") == "OFF"
    assert next_att(None) == "6dB"
    assert next_att("bogus") == "6dB"


def test_next_preamp_cycles_ipo_amp1_amp2():
    assert PREAMP_CYCLE == ["IPO", "AMP1", "AMP2"]
    assert next_preamp("IPO") == "AMP1"
    assert next_preamp("AMP1") == "AMP2"
    assert next_preamp("AMP2") == "IPO"
    assert next_preamp(None) == "AMP1"
    assert next_preamp("bogus") == "AMP1"


def _panel():
    _app()
    inner = _InnerRig()
    # owns_rig=False so PanelWindow does not try to open serial / start polls.
    w = PanelWindow(rig=RigProxy(inner), owns_rig=False)
    w._inner = inner
    return w


def test_att_button_cycles_and_relabels():
    w = _panel()
    btn = w.rf_btns["ATT"]
    assert btn.text() == "ATT"
    assert btn.isChecked() is False

    QTest.mouseClick(btn, Qt.LeftButton)
    assert w._inner.att_sets == ["6dB"]
    assert btn.text() == "6dB"
    assert btn.isChecked() is True

    QTest.mouseClick(btn, Qt.LeftButton)
    QTest.mouseClick(btn, Qt.LeftButton)
    QTest.mouseClick(btn, Qt.LeftButton)
    assert w._inner.att_sets == ["6dB", "12dB", "18dB", "OFF"]
    assert btn.text() == "ATT"
    assert btn.isChecked() is False
    w.close()


def test_ipo_button_cycles_and_relabels():
    w = _panel()
    btn = w.rf_btns["IPO"]
    assert btn.text() == "IPO"
    assert btn.isChecked() is False

    QTest.mouseClick(btn, Qt.LeftButton)
    assert w._inner.preamp_sets == ["AMP1"]
    assert btn.text() == "AMP1"
    assert btn.isChecked() is True

    QTest.mouseClick(btn, Qt.LeftButton)
    assert w._inner.preamp_sets == ["AMP1", "AMP2"]
    assert btn.text() == "AMP2"
    assert btn.isChecked() is True

    QTest.mouseClick(btn, Qt.LeftButton)
    assert w._inner.preamp_sets == ["AMP1", "AMP2", "IPO"]
    assert btn.text() == "IPO"
    assert btn.isChecked() is False
    w.close()


def test_radio_push_updates_labels_without_click():
    w = _panel()
    w._on_att("12dB")
    assert w.rf_btns["ATT"].text() == "12dB"
    assert w.rf_btns["ATT"].isChecked() is True
    w._on_preamp("AMP2")
    assert w.rf_btns["IPO"].text() == "AMP2"
    assert w.rf_btns["IPO"].isChecked() is True
    w.close()


# ── AGC auto (2026-08-26): the radio never answers plain "AUTO" ───────────

def _agc_panel():
    """A panel whose fake rig carries the AGC attributes only."""
    _app()
    inner = _InnerRig()
    inner.state.agc = None
    w = PanelWindow(rig=RigProxy(inner), owns_rig=False)
    w._inner = inner
    return w


def test_auto_flavours_all_light_the_auto_button():
    """GT answers 4/5/6 as AUTO-FAST/AUTO-MID/AUTO-SLOW, never "AUTO"."""
    w = _agc_panel()
    for answer in ("AUTO-FAST", "AUTO-MID", "AUTO-SLOW", "AUTO"):
        w._on_agc(answer)
        assert w.agc_btns["AUTO"].isChecked() is True, answer
        assert w.agc_btns["OFF"].isChecked() is False, answer
    w.close()


def test_the_plain_modes_still_match_exactly():
    w = _agc_panel()
    for answer in ("OFF", "FAST", "MID", "SLOW"):
        w._on_agc(answer)
        assert w.agc_btns[answer].isChecked() is True, answer
        assert w.agc_btns["AUTO"].isChecked() is False, answer
    w.close()


def test_no_agc_answer_lights_nothing():
    w = _agc_panel()
    w._on_agc(None)
    assert not any(b.isChecked() for b in w.agc_btns.values())
    w.close()
