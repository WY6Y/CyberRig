"""Split / RIT / XIT panel."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QSlider, QPushButton, QGroupBox, QSpinBox,
)
from PySide6.QtCore import Qt, Slot
from cyberrig.cat.ftdx10 import FTdx10


def _lbl(t):
    l = QLabel(t)
    l.setStyleSheet("font-size:10px; color:#00aa55;")
    return l


class SplitPanel(QWidget):
    def __init__(self, rig: FTdx10, parent=None):
        super().__init__(parent)
        self.rig = rig
        self._build()
        self._connect_rig()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(4, 4, 4, 4)

        # ── Split ─────────────────────────────────────────────────────
        split_box = QGroupBox("Split Operation")
        sg = QGridLayout(split_box)
        sg.setSpacing(4)

        self.split_btn = QPushButton("SPLIT OFF")
        self.split_btn.setCheckable(True)
        self.split_btn.setFixedHeight(30)
        sg.addWidget(self.split_btn, 0, 0, 1, 2)

        sg.addWidget(_lbl("TX VFO"), 1, 0)
        self.tx_a_btn = QPushButton("VFO A")
        self.tx_b_btn = QPushButton("VFO B")
        self.tx_a_btn.setCheckable(True)
        self.tx_b_btn.setCheckable(True)
        self.tx_b_btn.setChecked(True)
        sg.addWidget(self.tx_a_btn, 1, 1)
        sg.addWidget(self.tx_b_btn, 1, 2)

        sg.addWidget(_lbl("VFO-B:"), 2, 0)
        self.freq_b_lbl = QLabel("14.200.000")
        self.freq_b_lbl.setStyleSheet("color:#00ffcc; font-family:Consolas; font-size:14px;")
        sg.addWidget(self.freq_b_lbl, 2, 1, 1, 2)

        self.split_btn.clicked.connect(self._on_split)
        self.tx_a_btn.clicked.connect(lambda: self.rig.set_split(self.split_btn.isChecked(), "A"))
        self.tx_b_btn.clicked.connect(lambda: self.rig.set_split(self.split_btn.isChecked(), "B"))
        root.addWidget(split_box)

        # ── RIT ───────────────────────────────────────────────────────
        rit_box = QGroupBox("RIT / Clarifier")
        rg = QGridLayout(rit_box)
        rg.setSpacing(4)

        self.rit_btn = QPushButton("RIT OFF")
        self.rit_btn.setCheckable(True)
        self.rit_btn.setFixedHeight(28)
        rg.addWidget(self.rit_btn, 0, 0, 1, 3)

        self.rit_offset_lbl = QLabel("0 Hz")
        self.rit_offset_lbl.setStyleSheet("color:#00ff88; font-family:Consolas; font-size:14px;")
        self.rit_offset_lbl.setAlignment(Qt.AlignCenter)
        rg.addWidget(self.rit_offset_lbl, 1, 0, 1, 3)

        btn_down = QPushButton("◀ -100")
        btn_zero = QPushButton("Zero")
        btn_up   = QPushButton("+100 ▶")
        btn_down.clicked.connect(lambda: self._adj_rit(-100))
        btn_zero.clicked.connect(self._zero_rit)
        btn_up.clicked.connect(lambda: self._adj_rit(100))
        rg.addWidget(btn_down, 2, 0)
        rg.addWidget(btn_zero, 2, 1)
        rg.addWidget(btn_up,   2, 2)

        self.rit_spin = QSpinBox()
        self.rit_spin.setRange(-9999, 9999)
        self.rit_spin.setSuffix(" Hz")
        self.rit_spin.setValue(0)
        rg.addWidget(_lbl("Set offset:"), 3, 0)
        rg.addWidget(self.rit_spin, 3, 1)
        set_btn = QPushButton("Set")
        set_btn.clicked.connect(lambda: self.rig.set_rit_offset(self.rit_spin.value()))
        rg.addWidget(set_btn, 3, 2)

        self.rit_btn.clicked.connect(self._on_rit)
        root.addWidget(rit_box)

        # ── XIT ───────────────────────────────────────────────────────
        xit_box = QGroupBox("XIT (TX Offset)")
        xg = QHBoxLayout(xit_box)
        self.xit_btn = QPushButton("XIT OFF")
        self.xit_btn.setCheckable(True)
        self.xit_btn.setFixedHeight(28)
        xg.addWidget(self.xit_btn)
        self.xit_btn.clicked.connect(self._on_xit)
        root.addWidget(xit_box)

        root.addStretch()

    # ── Handlers ─────────────────────────────────────────────────────────

    def _on_split(self, checked: bool):
        self.split_btn.setText("SPLIT ON" if checked else "SPLIT OFF")
        tx_vfo = "B" if self.tx_b_btn.isChecked() else "A"
        self.rig.set_split(checked, tx_vfo)

    def _on_rit(self, checked: bool):
        self.rit_btn.setText("RIT ON" if checked else "RIT OFF")
        self.rig.set_rit(checked)

    def _on_xit(self, checked: bool):
        self.xit_btn.setText("XIT ON" if checked else "XIT OFF")
        self.rig.set_xit(checked)

    def _adj_rit(self, delta_hz: int):
        self.rig.set_rit_offset(self.rig.state.rit_offset + delta_hz)

    def _zero_rit(self):
        self.rig.clear_rit()

    # ── Rig → UI ─────────────────────────────────────────────────────────

    def _connect_rig(self):
        self.rig.split_changed.connect(self._rig_split)
        self.rig.rit_changed.connect(self._rig_rit)
        self.rig.xit_changed.connect(self._rig_xit)
        self.rig.freq_b_changed.connect(self._rig_freq_b)

    @Slot(bool)
    def _rig_split(self, on):
        self.split_btn.setChecked(on)
        self.split_btn.setText("SPLIT ON" if on else "SPLIT OFF")

    @Slot(bool, int)
    def _rig_rit(self, on, offset):
        self.rit_btn.setChecked(on)
        self.rit_btn.setText("RIT ON" if on else "RIT OFF")
        self.rit_offset_lbl.setText(f"{offset:+d} Hz")

    @Slot(bool)
    def _rig_xit(self, on):
        self.xit_btn.setChecked(on)
        self.xit_btn.setText("XIT ON" if on else "XIT OFF")

    @Slot(int)
    def _rig_freq_b(self, hz):
        mhz = hz / 1_000_000
        s = f"{hz:09d}"
        self.freq_b_lbl.setText(f"{s[0:3]}.{s[3:6]}.{s[6:9]}")
