"""TX controls — power, compressor, VOX, monitor, antenna."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QSlider, QPushButton, QGroupBox, QButtonGroup,
    QSpinBox,
)
from PySide6.QtCore import Qt, Slot
from cyberrig.cat.ftdx10 import FTdx10


def _lbl(t, right=False):
    l = QLabel(t)
    l.setStyleSheet("font-size:10px; color:#00aa55;")
    if right:
        l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return l


def _toggle(text: str, h=26) -> QPushButton:
    b = QPushButton(text)
    b.setCheckable(True)
    b.setFixedHeight(h)
    return b


class TXPanel(QWidget):
    def __init__(self, rig: FTdx10, parent=None):
        super().__init__(parent)
        self.rig = rig
        self._build()
        self._connect_rig()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(4, 4, 4, 4)

        # ── TX Power ──────────────────────────────────────────────────
        pwr_box = QGroupBox("TX Power")
        pg = QGridLayout(pwr_box)
        pg.setSpacing(4)

        pg.addWidget(_lbl("Power"), 0, 0)
        self.pwr_slider = QSlider(Qt.Horizontal)
        self.pwr_slider.setRange(5, 200)
        self.pwr_slider.setValue(100)
        self.pwr_val = _lbl("100 W", right=True)
        pg.addWidget(self.pwr_slider, 0, 1)
        pg.addWidget(self.pwr_val, 0, 2)
        self.pwr_slider.valueChanged.connect(self._on_power)

        # Quick power buttons
        qrow = QHBoxLayout()
        for w in [5, 25, 50, 100, 200]:
            b = QPushButton(f"{w}W")
            b.setFixedHeight(22)
            b.clicked.connect(lambda _, ww=w: self._set_power_quick(ww))
            qrow.addWidget(b)
        pg.addLayout(qrow, 1, 0, 1, 3)
        root.addWidget(pwr_box)

        # ── Compressor / Processor ────────────────────────────────────
        comp_box = QGroupBox("Mic Compressor")
        cg = QGridLayout(comp_box)
        cg.setSpacing(4)

        self.comp_btn = _toggle("COMP ON")
        cg.addWidget(self.comp_btn, 0, 0)

        cg.addWidget(_lbl("Level"), 0, 1)
        self.comp_slider = QSlider(Qt.Horizontal)
        self.comp_slider.setRange(0, 100)
        self.comp_slider.setValue(50)
        self.comp_val = _lbl("50", right=True)
        cg.addWidget(self.comp_slider, 0, 2)
        cg.addWidget(self.comp_val, 0, 3)

        self.comp_btn.clicked.connect(lambda c: self.rig.set_compressor(c))
        self.comp_slider.valueChanged.connect(self._on_comp_level)
        root.addWidget(comp_box)

        # ── VOX ───────────────────────────────────────────────────────
        vox_box = QGroupBox("VOX")
        vg = QGridLayout(vox_box)
        vg.setSpacing(4)

        self.vox_btn = _toggle("VOX ON")
        vg.addWidget(self.vox_btn, 0, 0, 1, 1)

        vg.addWidget(_lbl("Gain"), 1, 0)
        self.vox_gain = QSlider(Qt.Horizontal)
        self.vox_gain.setRange(0, 255)
        self.vox_gain.setValue(50)
        self.vox_gain_val = _lbl("50", right=True)
        vg.addWidget(self.vox_gain, 1, 1)
        vg.addWidget(self.vox_gain_val, 1, 2)

        vg.addWidget(_lbl("Delay"), 2, 0)
        self.vox_delay = QSlider(Qt.Horizontal)
        self.vox_delay.setRange(0, 3000)
        self.vox_delay.setValue(100)
        self.vox_delay_val = _lbl("100 ms", right=True)
        vg.addWidget(self.vox_delay, 2, 1)
        vg.addWidget(self.vox_delay_val, 2, 2)

        self.vox_btn.clicked.connect(lambda c: self.rig.set_vox(c))
        self.vox_gain.valueChanged.connect(self._on_vox_gain)
        self.vox_delay.valueChanged.connect(self._on_vox_delay)
        root.addWidget(vox_box)

        # ── Monitor ───────────────────────────────────────────────────
        mon_box = QGroupBox("TX Monitor")
        mg = QHBoxLayout(mon_box)
        self.mon_btn = _toggle("MON ON")
        mg.addWidget(self.mon_btn)
        mg.addWidget(_lbl("Level"))
        self.mon_slider = QSlider(Qt.Horizontal)
        self.mon_slider.setRange(0, 100)
        self.mon_slider.setValue(50)
        self.mon_val = _lbl("50", right=True)
        mg.addWidget(self.mon_slider, stretch=1)
        mg.addWidget(self.mon_val)
        self.mon_btn.clicked.connect(lambda c: self.rig.set_monitor(c))
        self.mon_slider.valueChanged.connect(self._on_mon)
        root.addWidget(mon_box)

        # ── Antenna ───────────────────────────────────────────────────
        ant_box = QGroupBox("Antenna")
        ag = QHBoxLayout(ant_box)
        self._ant_group = QButtonGroup(self)
        for a in [1, 2]:
            btn = _toggle(f"ANT {a}")
            self._ant_group.addButton(btn)
            ag.addWidget(btn)
            btn.clicked.connect(lambda c, ant=a: self.rig.set_antenna(ant))
        root.addWidget(ant_box)

        root.addStretch()

    # ── Handlers ─────────────────────────────────────────────────────────

    def _on_power(self, v: int):
        self.pwr_val.setText(f"{v} W")
        self.rig.set_power(v)

    def _set_power_quick(self, w: int):
        self.pwr_slider.setValue(w)

    def _on_comp_level(self, v: int):
        self.comp_val.setText(str(v))
        self.rig.set_comp_level(v)

    def _on_vox_gain(self, v: int):
        self.vox_gain_val.setText(str(v))
        self.rig.set_vox(self.rig.state.vox, gain=v)

    def _on_vox_delay(self, v: int):
        self.vox_delay_val.setText(f"{v} ms")
        self.rig.set_vox(self.rig.state.vox, delay_ms=v)

    def _on_mon(self, v: int):
        self.mon_val.setText(str(v))
        self.rig.set_monitor(self.rig.state.monitor, level=v)

    # ── Rig → UI ─────────────────────────────────────────────────────────

    def _connect_rig(self):
        self.rig.comp_changed.connect(self._rig_comp)
        self.rig.vox_changed.connect(self._rig_vox)
        self.rig.antenna_changed.connect(self._rig_ant)

    @Slot(bool, int)
    def _rig_comp(self, on, level):
        self.comp_btn.setChecked(on)
        self.comp_slider.blockSignals(True)
        self.comp_slider.setValue(level)
        self.comp_val.setText(str(level))
        self.comp_slider.blockSignals(False)

    @Slot(bool, int, int)
    def _rig_vox(self, on, gain, delay):
        self.vox_btn.setChecked(on)

    @Slot(int)
    def _rig_ant(self, ant):
        for btn in self._ant_group.buttons():
            btn.setChecked(btn.text().endswith(str(ant)))
