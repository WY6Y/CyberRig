"""Arc-style analog S-meter widget."""

import math
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QConicalGradient


# Raw SM0 value → display scale
# FTDX10 returns 0000–0030; 0009 ≈ S9
_S_LABELS = ["S1", "S3", "S5", "S7", "S9", "+20", "+40", "+60"]
_S_VALUES  = [  1,    3,    5,    7,    9,   14,    20,    30  ]

_ARC_START  = 210   # degrees (clockwise from 3 o'clock in Qt convention)
_ARC_SPAN   = 120   # total sweep degrees


class SMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw = 0           # 0–30 from radio
        self._displayed = 0.0   # smoothed for animation
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(30)   # ~33 fps
        self.setMinimumSize(260, 130)

    def set_value(self, raw: int):
        self._raw = max(0, min(30, raw))

    def _animate(self):
        # Exponential smoothing: fast attack, slow decay
        target = float(self._raw)
        if target > self._displayed:
            self._displayed += (target - self._displayed) * 0.4
        else:
            self._displayed += (target - self._displayed) * 0.12
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#050f09"))

        # Arc geometry — centre at bottom-centre of widget
        radius = min(w * 0.48, h * 0.90)
        cx = w / 2
        cy = h - 10
        arc_rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        # --- Draw scale arc ---
        pen_w = max(2, int(radius * 0.045))

        # Green zone S0–S9 (raw 0–9)
        self._draw_arc(p, arc_rect, cx, cy, radius, 0, 9, QColor("#00aa44"), pen_w)
        # Yellow zone S9–S9+40 (raw 9–20)
        self._draw_arc(p, arc_rect, cx, cy, radius, 9, 20, QColor("#ccaa00"), pen_w)
        # Red zone S9+40–S9+60 (raw 20–30)
        self._draw_arc(p, arc_rect, cx, cy, radius, 20, 30, QColor("#cc2200"), pen_w)

        # --- Tick marks & labels ---
        font_size = max(7, int(radius * 0.12))
        lbl_font = QFont("Consolas", font_size)
        p.setFont(lbl_font)

        for raw_val, label in zip(_S_VALUES, _S_LABELS):
            angle_deg = _raw_to_angle(raw_val)
            angle_rad = math.radians(angle_deg)
            cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)

            # Outer tick
            r1 = radius * 0.88
            r2 = radius * 0.72
            x1 = cx + r1 * cos_a
            y1 = cy - r1 * sin_a
            x2 = cx + r2 * cos_a
            y2 = cy - r2 * sin_a
            p.setPen(QPen(QColor("#00ff88"), max(1, pen_w // 2)))
            p.drawLine(int(x1), int(y1), int(x2), int(y2))

            # Label
            r_lbl = radius * 0.58
            lx = cx + r_lbl * cos_a - font_size * 1.2
            ly = cy - r_lbl * sin_a + font_size * 0.4
            color = QColor("#ffcc00") if raw_val >= 9 else QColor("#00ff88")
            p.setPen(color)
            p.drawText(int(lx), int(ly), label)

        # --- Needle ---
        needle_angle_rad = math.radians(_raw_to_angle(self._displayed))
        cos_n = math.cos(needle_angle_rad)
        sin_n = math.sin(needle_angle_rad)
        nx = cx + radius * 0.80 * cos_n
        ny = cy - radius * 0.80 * sin_n
        p.setPen(QPen(QColor("#ffffff"), max(2, pen_w // 2)))
        p.drawLine(int(cx), int(cy), int(nx), int(ny))

        # Pivot dot
        pivot_r = max(4, pen_w)
        p.setBrush(QColor("#00ff88"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(int(cx - pivot_r), int(cy - pivot_r), pivot_r * 2, pivot_r * 2)

        # Digital readout
        s_str = _raw_to_label(self._displayed)
        p.setPen(QColor("#00ff88"))
        d_font = QFont("Consolas", max(8, int(radius * 0.14)), QFont.Bold)
        p.setFont(d_font)
        p.drawText(self.rect().adjusted(0, 0, 0, -4), Qt.AlignBottom | Qt.AlignHCenter, s_str)

    def _draw_arc(self, p, rect, cx, cy, radius, raw_lo, raw_hi, color, pen_w):
        a_lo = _raw_to_angle(raw_lo)
        a_hi = _raw_to_angle(raw_hi)
        # Qt angles: 0° = 3 o'clock, counterclockwise positive for drawArc
        qt_start = int(a_lo * 16)
        qt_span  = int((a_hi - a_lo) * 16)
        p.setPen(QPen(color, pen_w, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(rect, qt_start, qt_span)


def _raw_to_angle(raw: float) -> float:
    """Convert raw SM value (0–30) to drawing angle in degrees.

    0 raw = leftmost (_ARC_START from East), 30 raw = rightmost.
    Returns angle measured counterclockwise from East (standard math convention).
    """
    frac = max(0.0, min(1.0, raw / 30.0))
    # Start at 210° CCW from East (bottom-left), sweep 120° to 330° (bottom-right)
    return _ARC_START - frac * _ARC_SPAN


def _raw_to_label(raw: float) -> str:
    if raw < 1:
        return "S0"
    if raw <= 9:
        return f"S{int(round(raw))}"
    above = int(round((raw - 9) / 21 * 60))  # map 9–30 → 0–60 dB
    above = (above // 10) * 10
    return f"S9+{above}"
