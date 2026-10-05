"""Filter controls — bandwidth (SH/Table 3), IF shift, contour, notch, DNF."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QSlider, QPushButton, QGroupBox, QComboBox,
    QSpinBox,
)
from PySide6.QtCore import Qt, Slot
from cyberrig.cat.ftdx10 import FTdx10, sh_bw_table, sh_hz, sh_max_code, SH_BW_SSB


def _lbl(t, right=False):
    l = QLabel(t)
    l.setStyleSheet("font-size:10px; color:#00aa55;")
    if right:
        l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return l


class FilterPanel(QWidget):
    def __init__(self, rig: FTdx10, parent=None):
        super().__init__(parent)
        self.rig = rig
        self._mode = "USB"
        self._build()
        self._connect_rig()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(4, 4, 4, 4)

        # ── Bandwidth (SH — Table 3) ───────────────────────────────────
        width_box = QGroupBox("Filter Width (SH)")
        wg = QGridLayout(width_box)
        wg.setSpacing(4)

        wg.addWidget(_lbl("Width"), 0, 0)
        self.sh_slider = QSlider(Qt.Horizontal)
        self.sh_slider.setRange(0, 23)   # max for SSB; updated per mode
        self.sh_slider.setValue(13)       # ≈2400Hz for SSB
        self.sh_hz_lbl = _lbl("2400 Hz", right=True)
        wg.addWidget(self.sh_slider, 0, 1)
        wg.addWidget(self.sh_hz_lbl, 0, 2)

        self.bw_lbl = QLabel("BW: 2400 Hz")
        self.bw_lbl.setStyleSheet("font-size:11px; color:#00ff88; font-weight:bold;")
        wg.addWidget(self.bw_lbl, 1, 0, 1, 3, Qt.AlignCenter)

        # Quick presets (SSB-friendly defaults)
        preset_row = QHBoxLayout()
        for label, code in [("Narrow", 5), ("Mid", 10), ("Wide", 16), ("Full", 23)]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.clicked.connect(lambda c, v=code: self.sh_slider.setValue(v))
            preset_row.addWidget(b)
        wg.addLayout(preset_row, 2, 0, 1, 3)

        self.sh_slider.valueChanged.connect(self._on_sh)
        root.addWidget(width_box)

        # ── IF Shift ──────────────────────────────────────────────────
        shift_box = QGroupBox("IF Shift  (IS)")
        sg = QGridLayout(shift_box)
        sg.setSpacing(4)

        self.shift_slider = QSlider(Qt.Horizontal)
        self.shift_slider.setRange(-1200, 1200)
        self.shift_slider.setSingleStep(20)
        self.shift_slider.setPageStep(100)
        self.shift_slider.setValue(0)
        self.shift_val_lbl = _lbl("0 Hz", right=True)
        btn_zero = QPushButton("Zero")
        btn_zero.setFixedWidth(48)
        btn_zero.clicked.connect(self._zero_shift)

        sg.addWidget(self.shift_slider, 0, 0)
        sg.addWidget(self.shift_val_lbl, 0, 1)
        sg.addWidget(btn_zero, 0, 2)
        self.shift_slider.valueChanged.connect(self._on_shift)
        root.addWidget(shift_box)

        # ── DSP toggles ───────────────────────────────────────────────
        dsp_box = QGroupBox("DSP")
        dg = QGridLayout(dsp_box)
        dg.setSpacing(4)

        self.dnf_btn = QPushButton("Auto Notch (DNF)")
        self.dnf_btn.setCheckable(True)
        self.dnf_btn.setFixedHeight(26)
        self.dnf_btn.clicked.connect(lambda c: self.rig.set_dnf(c))
        dg.addWidget(self.dnf_btn, 0, 0, 1, 2)

        self.notch_btn = QPushButton("Manual NOTCH")
        self.notch_btn.setCheckable(True)
        self.notch_btn.setFixedHeight(26)
        dg.addWidget(self.notch_btn, 1, 0)

        self.notch_spin = QSpinBox()
        self.notch_spin.setRange(10, 3200)
        self.notch_spin.setSingleStep(10)
        self.notch_spin.setValue(1000)
        self.notch_spin.setSuffix(" Hz")
        dg.addWidget(self.notch_spin, 1, 1)

        self.notch_btn.clicked.connect(self._on_notch)
        self.notch_spin.valueChanged.connect(self._on_notch_pos)

        self.cont_btn = QPushButton("CONTOUR")
        self.cont_btn.setCheckable(True)
        self.cont_btn.setFixedHeight(26)
        dg.addWidget(self.cont_btn, 2, 0)

        dg.addWidget(_lbl("Freq Hz"), 3, 0)
        self.cont_freq = QSpinBox()
        self.cont_freq.setRange(10, 3200)
        self.cont_freq.setSingleStep(10)
        self.cont_freq.setValue(1000)
        self.cont_freq.setSuffix(" Hz")
        dg.addWidget(self.cont_freq, 3, 1)

        self.cont_btn.clicked.connect(self._on_contour)
        self.cont_freq.valueChanged.connect(self._on_contour_freq)

        # APF (Audio Peak Filter for CW)
        self.apf_btn = QPushButton("APF (CW)")
        self.apf_btn.setCheckable(True)
        self.apf_btn.setFixedHeight(26)
        self.apf_btn.clicked.connect(lambda c: self.rig.set_apf(c))
        dg.addWidget(self.apf_btn, 4, 0, 1, 2)

        root.addWidget(dsp_box)
        root.addStretch()

    # ── Handlers ─────────────────────────────────────────────────────────

    def _on_sh(self, code: int):
        hz = sh_hz(code, self._mode)
        self.sh_hz_lbl.setText(f"{hz} Hz")
        self.bw_lbl.setText(f"BW: {hz} Hz" if hz else "BW: Default")
        self.rig.set_sh(code)

    def _on_shift(self, v: int):
        # Snap to 20Hz grid
        snapped = round(v / 20) * 20
        self.shift_val_lbl.setText(f"{snapped:+d} Hz")
        self.rig.set_if_shift(snapped)

    def _zero_shift(self):
        self.shift_slider.setValue(0)

    def _on_notch(self):
        on = self.notch_btn.isChecked()
        pos = self.notch_spin.value()
        self.rig.set_notch(on, pos)

    def _on_notch_pos(self, hz: int):
        if self.notch_btn.isChecked():
            self.rig.set_notch_freq(hz)

    def _on_contour(self):
        self.rig.set_contour(self.cont_btn.isChecked())

    def _on_contour_freq(self, hz: int):
        self.rig.set_contour_freq(hz)

    # ── Rig → UI ─────────────────────────────────────────────────────────

    def _connect_rig(self):
        self.rig.filter_changed.connect(self._rig_filter)
        self.rig.mode_changed.connect(self._rig_mode)
        self.rig.dnf_changed.connect(self._rig_dnf)
        self.rig.notch_changed.connect(self._rig_notch)
        self.rig.contour_changed.connect(self._rig_contour)

    @Slot(int, int)
    def _rig_filter(self, bw_hz, ifs):
        # Find SH code from Hz
        t = sh_bw_table(self._mode)
        try:
            code = t.index(bw_hz)
            self.sh_slider.blockSignals(True)
            self.sh_slider.setValue(code)
            self.sh_hz_lbl.setText(f"{bw_hz} Hz")
            self.bw_lbl.setText(f"BW: {bw_hz} Hz")
            self.sh_slider.blockSignals(False)
        except ValueError:
            pass
        self.shift_slider.blockSignals(True)
        self.shift_slider.setValue(ifs)
        self.shift_val_lbl.setText(f"{ifs:+d} Hz")
        self.shift_slider.blockSignals(False)

    @Slot(str)
    def _rig_mode(self, mode):
        self._mode = mode
        max_c = sh_max_code(mode)
        self.sh_slider.blockSignals(True)
        self.sh_slider.setRange(0, max_c)
        self.sh_slider.blockSignals(False)

    @Slot(bool)
    def _rig_dnf(self, on):
        self.dnf_btn.setChecked(on)

    @Slot(bool, int)
    def _rig_notch(self, on, pos):
        self.notch_btn.setChecked(on)
        self.notch_spin.blockSignals(True)
        self.notch_spin.setValue(pos)
        self.notch_spin.blockSignals(False)

    @Slot(bool, int)
    def _rig_contour(self, on, freq):
        self.cont_btn.setChecked(on)
        self.cont_freq.blockSignals(True)
        self.cont_freq.setValue(freq)
        self.cont_freq.blockSignals(False)
