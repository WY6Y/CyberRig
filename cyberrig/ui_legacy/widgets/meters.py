"""TX meter bar — shows PWR / ALC / SWR during transmit."""

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import QTimer
from PySide6.QtGui import QPainter, QColor, QFont


class TXMeter(QWidget):
    """Horizontal bar meters for power, ALC, SWR."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pwr = 0    # 0-255
        self._alc = 0
        self._swr = 0
        self._disp_pwr = 0.0
        self._disp_alc = 0.0
        self._disp_swr = 0.0
        self._is_tx = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(30)
        self.setMinimumSize(200, 70)

    def set_meters(self, pwr: int, alc: int, swr: int):
        self._pwr = pwr
        self._alc = alc
        self._swr = swr

    def set_tx(self, tx: bool):
        self._is_tx = tx
        if not tx:
            self._pwr = 0
            self._alc = 0
            self._swr = 0

    def _animate(self):
        def smooth(disp, target, up, dn):
            d = target - disp
            rate = up if d > 0 else dn
            return disp + d * rate

        self._disp_pwr = smooth(self._disp_pwr, self._pwr, 0.5, 0.15)
        self._disp_alc = smooth(self._disp_alc, self._alc, 0.5, 0.15)
        self._disp_swr = smooth(self._disp_swr, self._swr, 0.5, 0.15)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#050f09"))

        labels = ["PWR", "ALC", "SWR"]
        vals   = [self._disp_pwr, self._disp_alc, self._disp_swr]
        colors = [QColor("#00ff88"), QColor("#ffcc00"), QColor("#ff4400")]

        row_h = (h - 8) // 3
        font  = QFont("Consolas", max(7, row_h // 3))
        p.setFont(font)

        lbl_w = 32
        bar_x = lbl_w + 4
        bar_w = w - bar_x - 4

        for i, (lbl, val, col) in enumerate(zip(labels, vals, colors)):
            y = 4 + i * row_h
            bar_h = max(6, row_h - 6)
            bar_y = y + (row_h - bar_h) // 2

            # Label
            p.setPen(QColor("#00aa55"))
            p.drawText(2, bar_y + bar_h - 2, lbl)

            # Background
            p.fillRect(bar_x, bar_y, bar_w, bar_h, QColor("#0a1a0d"))

            # Fill
            fill = int(val / 255 * bar_w)
            if fill > 0:
                # Red zone past 80% for PWR, past 60% for ALC/SWR
                threshold = int(bar_w * (0.80 if i == 0 else 0.60))
                green_w = min(fill, threshold)
                p.fillRect(bar_x, bar_y, green_w, bar_h, col)
                if fill > threshold:
                    p.fillRect(bar_x + threshold, bar_y, fill - threshold, bar_h, QColor("#ff3300"))

            # Peak tick
            p.setPen(QColor("#ffffff"))
            p.drawRect(bar_x, bar_y, bar_w, bar_h)
