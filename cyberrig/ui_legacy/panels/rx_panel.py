"""RX controls panel — AF/RF gain, AGC, preamp/IPO, ATT, NB, NR."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QSlider, QPushButton, QButtonGroup, QGroupBox,
)
from PySide6.QtCore import Qt, Slot
from cyberrig.cat.ftdx10 import FTdx10, PREAMP, ATT


def _label(text: str, align=Qt.AlignLeft) -> QLabel:
    lbl = QLabel(text)
    lbl.setAlignment(align)
    lbl.setStyleSheet("font-size:10px; color:#00aa55;")
    return lbl


def _slider(lo: int, hi: int, val: int, orient=Qt.Horizontal) -> QSlider:
    s = QSlider(orient)
    s.setRange(lo, hi)
    s.setValue(val)
    return s


def _toggle_btn(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setCheckable(True)
    b.setFixedHeight(24)
    return b


class RXPanel(QWidget):
    def __init__(self, rig: FTdx10, parent=None):
        super().__init__(parent)
        self.rig = rig
        self._build()
        self._connect_rig()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(4, 4, 4, 4)

        # ── AF / RF Gain ──────────────────────────────────────────────
        gain_box = QGroupBox("Gain")
        gain_grid = QGridLayout(gain_box)
        gain_grid.setSpacing(4)

        gain_grid.addWidget(_label("AF"), 0, 0)
        self.af_slider = _slider(0, 255, 100)
        self.af_val    = _label("100", Qt.AlignRight)
        gain_grid.addWidget(self.af_slider, 0, 1)
        gain_grid.addWidget(self.af_val, 0, 2)

        gain_grid.addWidget(_label("RF"), 1, 0)
        self.rf_slider = _slider(0, 255, 200)
        self.rf_val    = _label("200", Qt.AlignRight)
        gain_grid.addWidget(self.rf_slider, 1, 1)
        gain_grid.addWidget(self.rf_val, 1, 2)

        self.af_slider.valueChanged.connect(self._on_af)
        self.rf_slider.valueChanged.connect(self._on_rf)
        root.addWidget(gain_box)

        # ── AGC ──────────────────────────────────────────────────────
        agc_box = QGroupBox("AGC")
        agc_row = QHBoxLayout(agc_box)
        agc_row.setSpacing(2)
        self._agc_group = QButtonGroup(self)
        for mode in ["AUTO", "FAST", "MID", "SLOW", "OFF"]:
            btn = _toggle_btn(mode)
            self._agc_group.addButton(btn)
            agc_row.addWidget(btn)
            btn.clicked.connect(lambda c, m=mode: self.rig.set_agc(m))
        root.addWidget(agc_box)

        # ── IPO / AMP / ATT ──────────────────────────────────────────
        front_box = QGroupBox("Front-end")
        front_row = QHBoxLayout(front_box)
        front_row.setSpacing(2)

        self._preamp_group = QButtonGroup(self)
        for mode in ["IPO", "AMP1", "AMP2"]:
            btn = _toggle_btn(mode)
            self._preamp_group.addButton(btn)
            front_row.addWidget(btn)
            btn.clicked.connect(lambda c, m=mode: self.rig.set_preamp(m))

        front_row.addSpacing(8)
        self._att_group = QButtonGroup(self)
        for mode in ["OFF", "6dB", "12dB", "18dB"]:
            btn = _toggle_btn(mode)
            self._att_group.addButton(btn)
            front_row.addWidget(btn)
            btn.clicked.connect(lambda c, m=mode: self.rig.set_att(m))
        root.addWidget(front_box)

        # ── NB ───────────────────────────────────────────────────────
        nb_box = QGroupBox("Noise Blanker")
        nb_row = QHBoxLayout(nb_box)
        nb_row.setSpacing(4)
        self.nb_btn = _toggle_btn("NB ON")
        self.nb_slider = _slider(0, 255, 50)
        self.nb_val = _label("50", Qt.AlignRight)
        nb_row.addWidget(self.nb_btn)
        nb_row.addWidget(_label("Lvl"))
        nb_row.addWidget(self.nb_slider, stretch=1)
        nb_row.addWidget(self.nb_val)
        self.nb_btn.clicked.connect(lambda c: self.rig.set_nb(c))
        self.nb_slider.valueChanged.connect(self._on_nb_level)
        root.addWidget(nb_box)

        # ── NR ───────────────────────────────────────────────────────
        nr_box = QGroupBox("Noise Reduction")
        nr_grid = QGridLayout(nr_box)
        nr_grid.setSpacing(4)

        self._nr_group = QButtonGroup(self)
        for i, lbl in enumerate(["OFF", "NR1", "NR2"]):
            btn = _toggle_btn(lbl)
            self._nr_group.addButton(btn)
            nr_grid.addWidget(btn, 0, i)
            btn.clicked.connect(lambda c, mode=i: self.rig.set_nr(mode))

        nr_grid.addWidget(_label("Level"), 1, 0)
        self.nr_slider = _slider(1, 15, 5)
        self.nr_val = _label("5", Qt.AlignRight)
        nr_grid.addWidget(self.nr_slider, 1, 1, 1, 1)
        nr_grid.addWidget(self.nr_val, 1, 2)
        self.nr_slider.valueChanged.connect(self._on_nr_level)
        root.addWidget(nr_box)

        root.addStretch()

    # ── Local event handlers ──────────────────────────────────────────────

    def _on_af(self, v: int):
        self.af_val.setText(str(v))
        self.rig.set_af_gain(v)

    def _on_rf(self, v: int):
        self.rf_val.setText(str(v))
        self.rig.set_rf_gain(v)

    def _on_nb_level(self, v: int):
        self.nb_val.setText(str(v))
        self.rig.set_nb_level(v)

    def _on_nr_level(self, v: int):
        self.nr_val.setText(str(v))
        self.rig.set_nr_level(v)

    # ── Rig signals → UI ─────────────────────────────────────────────────

    def _connect_rig(self):
        self.rig.af_changed.connect(self._rig_af)
        self.rig.rf_changed.connect(self._rig_rf)
        self.rig.agc_changed.connect(self._rig_agc)
        self.rig.preamp_changed.connect(self._rig_preamp)
        self.rig.att_changed.connect(self._rig_att)
        self.rig.nb_changed.connect(self._rig_nb)
        self.rig.nr_changed.connect(self._rig_nr)

    @Slot(int)
    def _rig_af(self, v):
        self.af_slider.blockSignals(True)
        self.af_slider.setValue(v)
        self.af_val.setText(str(v))
        self.af_slider.blockSignals(False)

    @Slot(int)
    def _rig_rf(self, v):
        self.rf_slider.blockSignals(True)
        self.rf_slider.setValue(v)
        self.rf_val.setText(str(v))
        self.rf_slider.blockSignals(False)

    @Slot(str)
    def _rig_agc(self, mode):
        for btn in self._agc_group.buttons():
            btn.setChecked(btn.text() == mode)

    @Slot(str)
    def _rig_preamp(self, mode):
        for btn in self._preamp_group.buttons():
            btn.setChecked(btn.text() == mode)

    @Slot(str)
    def _rig_att(self, mode):
        for btn in self._att_group.buttons():
            btn.setChecked(btn.text() == mode)

    @Slot(bool, int)
    def _rig_nb(self, on, level):
        self.nb_btn.setChecked(on)
        self.nb_slider.blockSignals(True)
        self.nb_slider.setValue(level)
        self.nb_val.setText(str(level))
        self.nb_slider.blockSignals(False)

    @Slot(int, int)
    def _rig_nr(self, mode, level):
        for btn in self._nr_group.buttons():
            labels = ["OFF", "NR1", "NR2"]
            btn.setChecked(labels.index(btn.text()) == mode if btn.text() in labels else False)
        self.nr_slider.blockSignals(True)
        self.nr_slider.setValue(level)
        self.nr_val.setText(str(level))
        self.nr_slider.blockSignals(False)
