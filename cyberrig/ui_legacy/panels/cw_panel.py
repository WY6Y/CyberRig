"""CW keyer controls panel."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QSlider, QPushButton, QGroupBox, QSpinBox,
    QLineEdit,
)
from PySide6.QtCore import Qt, Slot
from cyberrig.cat.ftdx10 import FTdx10


def _lbl(t):
    l = QLabel(t)
    l.setStyleSheet("font-size:10px; color:#00aa55;")
    return l


class CWPanel(QWidget):
    def __init__(self, rig: FTdx10, parent=None):
        super().__init__(parent)
        self.rig = rig
        self._callsign = ""
        self._build()
        self._connect_rig()

    def set_callsign(self, call: str):
        self._callsign = call.upper()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(4, 4, 4, 4)

        # ── Speed & Pitch ─────────────────────────────────────────────
        sp_box = QGroupBox("CW Settings")
        sg = QGridLayout(sp_box)
        sg.setSpacing(4)

        sg.addWidget(_lbl("Speed"), 0, 0)
        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(4, 60)
        self.speed_spin.setValue(20)
        self.speed_spin.setSuffix(" WPM")
        sg.addWidget(self.speed_spin, 0, 1)
        self.speed_spin.valueChanged.connect(lambda v: self.rig.set_cw_speed(v))

        sg.addWidget(_lbl("Pitch"), 1, 0)
        self.pitch_slider = QSlider(Qt.Horizontal)
        self.pitch_slider.setRange(300, 1050)
        self.pitch_slider.setSingleStep(10)
        self.pitch_slider.setValue(600)
        self.pitch_lbl = QLabel("600 Hz")
        self.pitch_lbl.setStyleSheet("color:#00ff88; font-size:10px;")
        sg.addWidget(self.pitch_slider, 1, 1)
        sg.addWidget(self.pitch_lbl, 1, 2)
        self.pitch_slider.valueChanged.connect(self._on_pitch)

        # Break-in ON/OFF only (SEMI vs FULL is EX menu EX020113)
        sg.addWidget(_lbl("Break-In"), 2, 0)
        bi_row = QHBoxLayout()
        self._bi_off = QPushButton("OFF")
        self._bi_on  = QPushButton("ON")
        self._bi_off.setCheckable(True)
        self._bi_on.setCheckable(True)
        self._bi_off.setChecked(True)
        self._bi_off.setFixedHeight(24)
        self._bi_on.setFixedHeight(24)
        self._bi_off.clicked.connect(lambda: self.rig.set_cw_breakin(False))
        self._bi_on.clicked.connect(lambda: self.rig.set_cw_breakin(True))
        bi_row.addWidget(self._bi_off)
        bi_row.addWidget(self._bi_on)
        bi_note = _lbl("(SEMI/FULL→Radio Menu)")
        bi_row.addWidget(bi_note)
        sg.addLayout(bi_row, 2, 1, 1, 2)

        # QSK Delay
        sg.addWidget(_lbl("QSK Delay"), 3, 0)
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(30, 3000)
        self.delay_spin.setValue(200)
        self.delay_spin.setSuffix(" ms")
        sg.addWidget(self.delay_spin, 3, 1)
        self.delay_spin.valueChanged.connect(lambda v: self.rig.set_cw_delay(v))
        root.addWidget(sp_box)

        # ── CW Message Send ───────────────────────────────────────────
        msg_box = QGroupBox("CW Keyer Send")
        mg = QVBoxLayout(msg_box)

        self.cw_input = QLineEdit()
        self.cw_input.setPlaceholderText("Type message to send…")
        self.cw_input.setStyleSheet(
            "background:#0d2018; color:#00ff88; border:1px solid #1a4a2a; "
            "font-family:Consolas; font-size:12px; padding:3px;"
        )
        mg.addWidget(self.cw_input)

        send_row = QHBoxLayout()
        self.send_btn = QPushButton("▶ SEND")
        self.send_btn.setFixedHeight(28)
        self.send_btn.clicked.connect(self._send_cw)
        self.cw_input.returnPressed.connect(self._send_cw)
        send_row.addWidget(self.send_btn)
        mg.addLayout(send_row)

        # Quick macros — use self._callsign (set after construction)
        macro_row = QHBoxLayout()
        for label, msg_fn in [
            ("CQ",  lambda: f"CQ CQ CQ DE {self._callsign} {self._callsign} K"),
            ("73",  lambda: f"73 DE {self._callsign}"),
            ("TU",  lambda: f"TU 73 DE {self._callsign}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(24)
            b.clicked.connect(lambda c=False, fn=msg_fn: self._send_macro(fn()))
            macro_row.addWidget(b)
        mg.addLayout(macro_row)
        root.addWidget(msg_box)
        root.addStretch()

    # ── Handlers ─────────────────────────────────────────────────────────

    def _on_pitch(self, hz: int):
        # Snap to 10Hz steps
        snapped = round(hz / 10) * 10
        self.pitch_lbl.setText(f"{snapped} Hz")
        self.rig.set_cw_pitch(snapped)

    def _send_cw(self):
        text = self.cw_input.text().strip()
        if text:
            self.rig.send_cw(text)

    def _send_macro(self, msg: str):
        self.cw_input.setText(msg)
        self.rig.send_cw(msg)

    # ── Rig → UI ─────────────────────────────────────────────────────────

    def _connect_rig(self):
        self.rig.cw_changed.connect(self._rig_cw)

    @Slot(int, int, bool, int)
    def _rig_cw(self, speed, pitch, breakin, delay):
        self.speed_spin.blockSignals(True)
        self.speed_spin.setValue(speed)
        self.speed_spin.blockSignals(False)

        self.pitch_slider.blockSignals(True)
        self.pitch_slider.setValue(pitch)
        self.pitch_lbl.setText(f"{pitch} Hz")
        self.pitch_slider.blockSignals(False)

        self._bi_off.setChecked(not breakin)
        self._bi_on.setChecked(breakin)
