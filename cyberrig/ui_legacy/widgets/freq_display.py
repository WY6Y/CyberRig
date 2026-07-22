"""Large segmented-style frequency display with per-digit mouse-wheel tuning."""

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, Signal, QRect
from PySide6.QtGui import QPainter, QColor, QFont, QFontMetrics, QMouseEvent, QWheelEvent


# Step sizes per digit group (Hz)
_DIGIT_STEPS = [
    100_000_000,  # 100 MHz
    10_000_000,   # 10 MHz
    1_000_000,    # 1 MHz
    100_000,      # 100 kHz
    10_000,       # 10 kHz
    1_000,        # 1 kHz
    100,          # 100 Hz
    10,           # 10 Hz
    1,            # 1 Hz
]

# Separator positions (before digit index): after pos 2 and pos 5
_SEPS = {3, 6}

COLOR_ON   = QColor("#00ff88")
COLOR_DIM  = QColor("#0a2a18")
COLOR_SEP  = QColor("#00cc66")
COLOR_BG   = QColor("#050f09")
COLOR_HZ   = QColor("#00aa55")   # slightly dimmer for Hz digits


class FrequencyDisplay(QWidget):
    """Clickable frequency display; scroll wheel tunes per digit group."""

    freq_changed = Signal(int)   # emitted when user scrolls/types new freq

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hz = 14_200_000
        self._hovered_digit = -1
        self.setMouseTracking(True)
        self.setMinimumSize(480, 90)
        self.setCursor(Qt.SizeVerCursor)

    def set_freq(self, hz: int):
        if hz != self._hz:
            self._hz = max(0, int(hz))
            self.update()

    def freq(self) -> int:
        return self._hz

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), COLOR_BG)

        w, h = self.width(), self.height()

        # Build digit string (9 chars, zero-padded)
        digits = f"{self._hz:09d}"

        # Decide font size from available height
        font_size = max(12, int(h * 0.72))
        font = QFont("Consolas", font_size, QFont.Bold)
        if not QFontMetrics(font).boundingRect("0").isValid():
            font = QFont("Courier New", font_size, QFont.Bold)
        p.setFont(font)
        fm = QFontMetrics(font)
        char_w = fm.horizontalAdvance("0")
        char_h = fm.ascent()

        sep_w = max(6, char_w // 3)
        n_seps = 2
        total_w = 9 * char_w + n_seps * sep_w
        x0 = (w - total_w) // 2
        y0 = (h + char_h) // 2 - fm.descent()

        self._digit_rects = []
        draw_x = x0

        for i, ch in enumerate(digits):
            if i in _SEPS:
                p.setPen(COLOR_SEP)
                p.drawText(draw_x, y0, ".")
                draw_x += sep_w

            rect = QRect(draw_x, 0, char_w, h)
            self._digit_rects.append(rect)

            # Colour: MHz digits brighter, Hz digits dimmer
            if i < 3:
                color = COLOR_ON if ch != "0" else COLOR_DIM
            elif i < 6:
                color = COLOR_ON
            else:
                color = COLOR_HZ

            # Highlight on hover
            if i == self._hovered_digit:
                p.fillRect(rect.adjusted(1, 4, -1, -4), QColor("#0d3d20"))
                color = QColor("#80ffcc")

            p.setPen(color)
            p.drawText(draw_x, y0, ch)
            draw_x += char_w

        # Band label
        band = _hz_to_band(self._hz)
        if band:
            lbl_font = QFont("Consolas", max(8, font_size // 4))
            p.setFont(lbl_font)
            p.setPen(COLOR_HZ)
            p.drawText(self.rect().adjusted(6, 4, -6, -4),
                       Qt.AlignTop | Qt.AlignRight, band)

    # ------------------------------------------------------------------ #
    # Mouse interaction
    # ------------------------------------------------------------------ #

    def _digit_at(self, x: int) -> int:
        for i, r in enumerate(self._digit_rects):
            if r.contains(x, self.height() // 2):
                return i
        return -1

    def mouseMoveEvent(self, event: QMouseEvent):
        d = self._digit_at(event.x())
        if d != self._hovered_digit:
            self._hovered_digit = d
            self.update()

    def leaveEvent(self, event):
        self._hovered_digit = -1
        self.update()

    def wheelEvent(self, event: QWheelEvent):
        d = self._digit_at(event.position().x())
        if d < 0:
            d = 5  # default: 1 kHz digit
        step = _DIGIT_STEPS[d]
        delta = 1 if event.angleDelta().y() > 0 else -1
        new_hz = max(100_000, self._hz + delta * step)
        self._hz = new_hz
        self.update()
        self.freq_changed.emit(new_hz)


def _hz_to_band(hz: int) -> str:
    bands = [
        (1_800_000, 2_000_000, "160m"),
        (3_500_000, 4_000_000, "80m"),
        (5_330_500, 5_405_000, "60m"),
        (7_000_000, 7_300_000, "40m"),
        (10_100_000, 10_150_000, "30m"),
        (14_000_000, 14_350_000, "20m"),
        (18_068_000, 18_168_000, "17m"),
        (21_000_000, 21_450_000, "15m"),
        (24_890_000, 24_990_000, "12m"),
        (28_000_000, 29_700_000, "10m"),
        (50_000_000, 54_000_000, "6m"),
    ]
    for lo, hi, name in bands:
        if lo <= hz <= hi:
            return name
    return ""
