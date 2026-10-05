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
        self.setMinimumSize(300, 56)
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

        # Reserve room at both ends for the band label ("20m"), which is drawn
        # top-right. The digits are centred, so reserving on both sides keeps
        # them centred while guaranteeing they cannot run under the label.
        band = _hz_to_band(self._hz)
        lbl_size = max(9, int(h * 0.72) // 4)
        lbl_font = QFont("Consolas")
        lbl_font.setPixelSize(lbl_size)
        band_w = (QFontMetrics(lbl_font).horizontalAdvance(band) + 12) if band else 0
        avail_w = max(40, w - 2 * band_w)

        # Size from available height, then clamp to available width. Height alone
        # is not enough: in a short, wide window the digits overflowed the widget
        # and collided with the band label.
        # Size in PIXELS, not points. QFont(family, n) treats n as points, so
        # `int(h * 0.72)` asked for a glyph box ~1.33x the widget height at
        # 96dpi and the digits were clipped at every size, not just small ones.
        font_size = max(10, int(h * 0.72))
        family = "Consolas"
        _probe = QFont(family); _probe.setPixelSize(font_size)
        if not QFontMetrics(_probe).boundingRect("0").isValid():
            family = "Courier New"

        def _metrics(size):
            f = QFont(family)
            f.setPixelSize(size)
            f.setBold(True)
            m = QFontMetrics(f)
            cw = m.horizontalAdvance("0")
            sw = max(6, cw // 3)
            return f, m, cw, sw, 9 * cw + 2 * sw

        font, fm, char_w, sep_w, total_w = _metrics(font_size)
        # Height clamp: the glyph box (ascent+descent) must fit inside the widget.
        while font_size > 10 and (fm.ascent() + fm.descent()) > h:
            font_size -= 2
            font, fm, char_w, sep_w, total_w = _metrics(font_size)
        if total_w > avail_w and total_w > 0:
            # char_w scales ~linearly with point size, so one proportional pass
            # lands within a pixel or two; clamp rather than loop each repaint.
            font_size = max(12, int(font_size * avail_w / total_w))
            font, fm, char_w, sep_w, total_w = _metrics(font_size)

        p.setFont(font)
        char_h = fm.ascent()
        x0 = (w - total_w) // 2
        y0 = (h + fm.ascent() - fm.descent()) // 2

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

        # Band label (space for it was reserved above)
        if band:
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
