"""Audio FFT waterfall — captures FTDX10 USB audio and displays a scrolling spectrum.

The FTDX10 appears as a USB audio device ("USB Audio CODEC" / "FTDX10") at
12000 Hz sample rate.  The audio is the IF output — clicking on the waterfall
QSYs the VFO by the corresponding offset.
"""

import queue
import threading
import numpy as np
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QImage, QColor, QPen, QFont

try:
    import sounddevice as sd
    HAS_SD = True
except ImportError:
    HAS_SD = False

# ── Colormap ─────────────────────────────────────────────────────────────────
# dB → RGBA (black → blue → cyan → green → yellow → red)
_STOPS = [
    (0.00, (0,   0,   20)),
    (0.25, (0,   0,  180)),
    (0.50, (0,  200, 200)),
    (0.70, (0,  220,   0)),
    (0.85, (240, 220,  0)),
    (1.00, (255,  50,  0)),
]

_CMAP = np.zeros((256, 3), dtype=np.uint8)
for i in range(256):
    t = i / 255.0
    for j in range(len(_STOPS) - 1):
        t0, c0 = _STOPS[j]
        t1, c1 = _STOPS[j + 1]
        if t0 <= t <= t1:
            frac = (t - t0) / (t1 - t0)
            _CMAP[i] = [int(c0[k] + frac * (c1[k] - c0[k])) for k in range(3)]
            break


def _db_to_color(db: float, db_min: float = -90, db_max: float = -20) -> tuple:
    t = max(0.0, min(1.0, (db - db_min) / (db_max - db_min)))
    idx = int(t * 255)
    r, g, b = _CMAP[idx]
    return int(r), int(g), int(b)


# ── Main widget ───────────────────────────────────────────────────────────────

class Waterfall(QWidget):
    """Scrolling audio FFT waterfall display."""

    freq_offset_clicked = Signal(int)   # Hz offset from VFO centre

    FFT_SIZE   = 2048
    SAMPLE_RATE = 12000
    HISTORY    = 300           # rows of history

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._stream = None
        self._img = QImage(self.FFT_SIZE // 2 + 1, self.HISTORY, QImage.Format_RGB888)
        self._img.fill(QColor("#050f09"))
        self._db_min = -90.0
        self._db_max = -20.0
        self._vfo_hz = 14_200_000
        self._running = False
        self._device_name = ""
        self._window = np.hanning(self.FFT_SIZE)

        self.setMinimumHeight(150)
        self.setCursor(Qt.CrossCursor)

        # Refresh timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._consume)
        self._timer.start(50)   # 20 fps

    # ── Audio capture ─────────────────────────────────────────────────────

    @staticmethod
    def list_devices() -> list[str]:
        if not HAS_SD:
            return []
        devs = sd.query_devices()
        return [f"{i}: {d['name']}" for i, d in enumerate(devs) if d["max_input_channels"] > 0]

    def start(self, device_index: int | None = None):
        if not HAS_SD:
            return False
        self.stop()
        try:
            self._stream = sd.InputStream(
                device=device_index,
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=self.FFT_SIZE,
                callback=self._audio_cb,
            )
            self._stream.start()
            self._running = True
            return True
        except Exception as e:
            self._running = False
            return False

    def stop(self):
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _audio_cb(self, indata, frames, time_info, status):
        if not self._queue.full():
            self._queue.put_nowait(indata[:, 0].copy())

    # ── FFT processing ────────────────────────────────────────────────────

    def _consume(self):
        consumed = 0
        while not self._queue.empty() and consumed < 4:
            chunk = self._queue.get_nowait()
            consumed += 1
            if len(chunk) < self.FFT_SIZE:
                continue
            fft_in = chunk[:self.FFT_SIZE] * self._window
            spectrum = np.abs(np.fft.rfft(fft_in))
            spectrum = 20 * np.log10(np.maximum(spectrum, 1e-12))
            self._add_row(spectrum)
        if consumed:
            self.update()

    def _add_row(self, spectrum: np.ndarray):
        """Scroll image up by one pixel and add new row at bottom."""
        w = self._img.width()
        h = self._img.height()
        # Scroll: copy existing image up by 1 row
        self._img.scroll(0, -1, 0, 0, w, h)
        # Paint new row at bottom
        n = min(len(spectrum), w)
        for x in range(n):
            r, g, b = _db_to_color(spectrum[x], self._db_min, self._db_max)
            self._img.setPixelColor(x, h - 1, QColor(r, g, b))

    # ── Painting ─────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        w, h = self.width(), self.height()

        # Scale waterfall image to widget size
        scaled = self._img.scaled(w, h - 20, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        p.drawImage(0, 0, scaled)

        # Frequency axis
        p.fillRect(0, h - 20, w, 20, QColor("#050f09"))
        p.setPen(QColor("#00aa55"))
        font = QFont("Consolas", 8)
        p.setFont(font)

        bins = self.FFT_SIZE // 2 + 1
        hz_per_bin = self.SAMPLE_RATE / 2 / bins
        # Show ±SR/2 relative to VFO (for SSB: 0 to 3kHz)
        for khz in range(0, int(self.SAMPLE_RATE / 2 / 1000) + 1, 1):
            bin_x = int(khz * 1000 / hz_per_bin)
            px = int(bin_x / bins * w)
            if px < w:
                p.drawLine(px, h - 20, px, h - 15)
                p.drawText(px - 10, h - 4, f"{khz}k")

        # Centre / VFO marker
        if not self._running:
            p.setPen(QColor("#ff4400"))
            p.drawText(10, 20, "▲ Waterfall: select audio device and Start")

    # ── Mouse → QSY ──────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            bins = self.FFT_SIZE // 2 + 1
            hz_per_bin = self.SAMPLE_RATE / 2 / bins
            bin_x = int(event.x() / self.width() * bins)
            offset_hz = int(bin_x * hz_per_bin)
            self.freq_offset_clicked.emit(offset_hz)

    def set_vfo(self, hz: int):
        self._vfo_hz = hz

    # ── Range controls ────────────────────────────────────────────────────

    def set_range(self, db_min: float, db_max: float):
        self._db_min = db_min
        self._db_max = db_max


class WaterfallPanel(QWidget):
    """Waterfall + device selector + controls."""

    def __init__(self, rig=None, parent=None):
        super().__init__(parent)
        self.rig = rig
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(2)
        root.setContentsMargins(0, 0, 0, 0)

        # Controls bar
        ctrl = QHBoxLayout()

        self.dev_combo = QComboBox()
        self.dev_combo.setStyleSheet("font-size:10px;")
        self.dev_combo.setMinimumWidth(200)
        self._populate_devices()
        ctrl.addWidget(QLabel("Audio:"))
        ctrl.addWidget(self.dev_combo)

        self.start_btn = QPushButton("▶ Start")
        self.stop_btn  = QPushButton("■ Stop")
        self.start_btn.setFixedHeight(22)
        self.stop_btn.setFixedHeight(22)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        ctrl.addWidget(self.start_btn)
        ctrl.addWidget(self.stop_btn)

        # dB range
        ctrl.addWidget(QLabel("Floor:"))
        self.floor_combo = QComboBox()
        for v in [-110, -100, -90, -80, -70]:
            self.floor_combo.addItem(f"{v} dB", v)
        self.floor_combo.setCurrentIndex(2)  # -90
        ctrl.addWidget(self.floor_combo)

        ctrl.addWidget(QLabel("Ceil:"))
        self.ceil_combo = QComboBox()
        for v in [-40, -30, -20, -10, 0]:
            self.ceil_combo.addItem(f"{v} dB", v)
        self.ceil_combo.setCurrentIndex(2)   # -20
        ctrl.addWidget(self.ceil_combo)

        self.floor_combo.currentIndexChanged.connect(self._update_range)
        self.ceil_combo.currentIndexChanged.connect(self._update_range)

        ctrl.addStretch()
        root.addLayout(ctrl)

        self.wf = Waterfall()
        root.addWidget(self.wf, stretch=1)

        if not HAS_SD:
            lbl = QLabel("⚠  Install sounddevice + numpy for waterfall  (pip install sounddevice numpy scipy)")
            lbl.setStyleSheet("color:#ff8800; font-size:10px; padding:4px;")
            root.addWidget(lbl)

    def _populate_devices(self):
        self.dev_combo.clear()
        self.dev_combo.addItem("Default input", None)
        for dev in Waterfall.list_devices():
            idx = int(dev.split(":")[0])
            name = dev.split(":", 1)[1].strip()
            self.dev_combo.addItem(name, idx)
            if "FTDX10" in name or "USB Audio" in name:
                self.dev_combo.setCurrentIndex(self.dev_combo.count() - 1)

    def _start(self):
        dev = self.dev_combo.currentData()
        self.wf.start(dev)

    def _stop(self):
        self.wf.stop()

    def _update_range(self):
        floor = self.floor_combo.currentData()
        ceil  = self.ceil_combo.currentData()
        self.wf.set_range(floor, ceil)
