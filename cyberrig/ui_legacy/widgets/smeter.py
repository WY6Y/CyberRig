"""Arc-style analog S-meter widget."""

import math
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QConicalGradient


# Raw SM0 value → display scale.
#
# `FTdx10.get_smeter()` returns the raw 3-digit SM0 field. This widget
# originally assumed that field topped out at 30 with S9 near 9, but the scale
# actually in use (and validated on air through the web UI, which renders the
# same value) is 0–50 with S9 at 18 and S9+60 at full scale. Keeping the old
# assumption pinned the needle at maximum on any real signal.
#
# Below S9 the raw value is ~2 per S-unit; above it, the remaining 32 raw
# counts cover 60 dB.
_SM_MAX = 50    # full scale (S9+60)
_SM_S9 = 18     # raw value at S9

# Labelled ticks. Bare digits below S9 and +dB above it, the way a real Yaesu
# meter is printed -- "S1".."S7" spelled out do not fit at the low end, where
# the S-units are only ~2 raw counts (about 5 deg) apart.
_MAJOR_TICKS = [
    (2, "1"), (6, None), (10, "5"), (14, None), (18, "9"),
    (29, "+20"), (39, "+40"), (50, "+60"),
]

# Unlabelled ticks in between, so needle position stays readable even where
# there is no room to print a number.
_MINOR_TICKS = [4, 8, 12, 16, 24, 34, 45]

# Radii as fractions of the widget's outer extent. Labels sit *outside*
# the coloured arc: there is ~50% more arc length to write on out there,
# and the needle stays clear of the text.
_R_NEEDLE = 0.62
_R_ARC = 0.70
_R_TICK_MINOR = 0.78
_R_TICK_MAJOR = 0.82
_R_LABEL = 0.92

_ARC_START  = 150   # deg CCW from East; pivot is bottom-centre so the arc
                    # sweeps over the top, 150 (upper-left) -> 30 (upper-right)
_ARC_SPAN   = 120   # total sweep degrees


class SMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw = 0           # raw SM0 field, 0–_SM_MAX
        self._displayed = 0.0   # smoothed for animation
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(30)   # ~33 fps
        self.setMinimumSize(170, 90)

    def set_value(self, raw: int):
        self._raw = max(0, min(_SM_MAX, raw))

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
        extent = min(w * 0.48, h * 0.90)
        radius = extent * _R_ARC          # the coloured scale arc itself
        cx = w / 2
        cy = h - 10
        arc_rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        # --- Draw scale arc ---
        pen_w = max(2, int(radius * 0.045))

        # Green zone S0–S9
        self._draw_arc(p, arc_rect, cx, cy, radius, 0, _SM_S9, QColor("#00aa44"), pen_w)
        # Yellow zone S9–S9+40
        self._draw_arc(p, arc_rect, cx, cy, radius, _SM_S9, 39, QColor("#ccaa00"), pen_w)
        # Red zone S9+40–S9+60
        self._draw_arc(p, arc_rect, cx, cy, radius, 39, _SM_MAX, QColor("#cc2200"), pen_w)

        # --- Tick marks & labels ---
        font_size = max(7, int(radius * 0.11))
        p.setFont(QFont("Consolas", font_size))
        fm = p.fontMetrics()

        def tick_at(raw_val, r_outer, r_inner, pen):
            a = math.radians(_raw_to_angle(raw_val))
            cos_a, sin_a = math.cos(a), math.sin(a)
            p.setPen(pen)
            p.drawLine(
                int(cx + r_outer * cos_a), int(cy - r_outer * sin_a),
                int(cx + r_inner * cos_a), int(cy - r_inner * sin_a),
            )
            return cos_a, sin_a

        minor_pen = QPen(QColor("#00aa66"), max(1, pen_w // 3))
        for raw_val in _MINOR_TICKS:
            tick_at(raw_val, extent * _R_TICK_MINOR, radius, minor_pen)

        major_pen = QPen(QColor("#00ff88"), max(1, pen_w // 2))
        for raw_val, label in _MAJOR_TICKS:
            cos_a, sin_a = tick_at(raw_val, extent * _R_TICK_MAJOR, radius, major_pen)
            if label is None:
                continue  # ticked but not numbered: no room at the low end

            # Centre the text on the tick's own radial line; the previous fixed
            # offset assumed every label was the same width.
            r_lbl = extent * _R_LABEL
            tw = fm.horizontalAdvance(label)
            lx = cx + r_lbl * cos_a - tw / 2.0
            ly = cy - r_lbl * sin_a + fm.capHeight() / 2.0
            p.setPen(QColor("#ffcc00") if raw_val >= _SM_S9 else QColor("#00ff88"))
            p.drawText(int(lx), int(ly), label)

        # --- Needle ---
        needle_angle_rad = math.radians(_raw_to_angle(self._displayed))
        cos_n = math.cos(needle_angle_rad)
        sin_n = math.sin(needle_angle_rad)
        nx = cx + extent * _R_NEEDLE * cos_n
        ny = cy - extent * _R_NEEDLE * sin_n
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
        # Bottom-right, not centred: the pivot dot sits at bottom-centre and the
        # two overlap there.
        p.drawText(self.rect().adjusted(0, 0, -6, -3), Qt.AlignBottom | Qt.AlignRight, s_str)

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
    """Convert raw SM value (0–_SM_MAX) to drawing angle in degrees.

    0 raw = leftmost (_ARC_START from East), _SM_MAX raw = rightmost.
    Returns angle measured counterclockwise from East (standard math convention).
    """
    frac = max(0.0, min(1.0, raw / float(_SM_MAX)))
    # Sweeps 150° -> 30°, an arc over the top of the bottom-centre pivot.
    # _ARC_START was 210°, which put half the arc below the pivot and off-widget;
    # latent, because this widget had never actually been run.
    return _ARC_START - frac * _ARC_SPAN


def _raw_to_label(raw: float) -> str:
    if raw < 2:
        return "S0"
    if raw <= _SM_S9:
        return f"S{int(round(raw / 2.0))}"
    # Remaining raw counts above S9 span 60 dB.
    above = int(round((raw - _SM_S9) / float(_SM_MAX - _SM_S9) * 60))
    above = int(round(above / 10.0)) * 10  # nearest tick, so readout matches the dial
    return f"S9+{above}"
