"""CyberRig main window — full dock-based layout."""

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QFrame, QDialog, QFormLayout,
    QComboBox, QSpinBox, QCheckBox, QDialogButtonBox,
    QGroupBox, QStatusBar, QDockWidget, QTabWidget,
    QSizePolicy, QScrollArea, QMenu, QInputDialog,
)
from PySide6.QtCore import Qt, QTimer, Slot, QSize, QByteArray
from PySide6.QtGui import QFont, QColor, QIcon, QCursor, QGuiApplication

from cyberrig.cat.ftdx10 import FTdx10, MODES, sh_hz
from cyberrig.ui_legacy.rig_adapter import RigProxy
from cyberrig.ui_legacy.widgets.freq_display import FrequencyDisplay
from cyberrig.ui_legacy.widgets.smeter import SMeter
from cyberrig.ui_legacy.widgets.meters import TXMeter
from cyberrig.ui_legacy.widgets.flow_layout import FlowLayout
from cyberrig.ui_legacy.panels.rx_panel import RXPanel
from cyberrig.ui_legacy.panels.filter_panel import FilterPanel
from cyberrig.ui_legacy.panels.tx_panel import TXPanel
from cyberrig.ui_legacy.panels.split_panel import SplitPanel
from cyberrig.ui_legacy.panels.cw_panel import CWPanel
from cyberrig.ui_legacy.panels.eq_panel import EQMenuPanel
from cyberrig.ui_legacy.panels.macro_panel import MacroPanel
from cyberrig.ui_legacy.panels.memory_panel import MemoryPanel
from cyberrig.ui_legacy.dialogs.macro_editor import MacroEditorDialog
from cyberrig.server.rigctl import RigctlServer
from cyberrig.settings import Settings
from cyberrig.memories.ops import recall as recall_channel, snapshot
from cyberrig.memories.store import load_memories, save_memories
from cyberrig.memories.model import MemoryChannel

import serial.tools.list_ports

# ── Stylesheet ────────────────────────────────────────────────────────────────

STYLE = """
QMainWindow, QWidget {
    background-color: #0a0f0a;
    color: #00ff88;
    font-family: Consolas, "Courier New", monospace;
}
QDockWidget {
    titlebar-close-icon: none;
    font-family: Consolas, monospace;
    font-size: 11px;
    color: #00ff88;
}
QDockWidget::title {
    background: #0d2a18;
    padding-left: 8px;
    border: 1px solid #1a4a2a;
}
QTabWidget::pane { border: 1px solid #1a4a2a; }
QTabBar::tab {
    background: #0d2018;
    color: #00aa55;
    padding: 4px 10px;
    font-size: 10px;
    border: 1px solid #1a4a2a;
}
QTabBar::tab:selected {
    background: #1a4a28;
    color: #00ff88;
}
QGroupBox {
    border: 1px solid #1a4a2a;
    border-radius: 3px;
    margin-top: 8px;
    padding-top: 6px;
    font-size: 9px;
    color: #00aa55;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}
QPushButton {
    background-color: #0d2a18;
    color: #00ff88;
    border: 1px solid #1a4a2a;
    border-radius: 3px;
    padding: 3px 8px;
    font-family: Consolas, monospace;
    font-size: 10px;
    min-height: 22px;
}
QPushButton:hover  { background-color: #1a4a28; border-color: #00ff88; }
QPushButton:pressed { background-color: #004420; }
QPushButton:checked {
    background-color: #004d22;
    border: 1px solid #00ff88;
    color: #80ffcc;
}
QPushButton#pttBtn {
    background-color: #1a0808;
    color: #ff4444;
    border: 2px solid #550000;
    font-size: 14px;
    font-weight: bold;
    min-height: 44px;
}
QPushButton#pttBtn:checked {
    background-color: #cc0000;
    color: #ffffff;
    border: 2px solid #ff4444;
}
QPushButton#modeBtn:checked {
    background-color: #005533;
    border: 1px solid #00ff88;
    color: #00ff88;
    font-weight: bold;
}
QLabel { color: #00ff88; font-family: Consolas, monospace; }
QStatusBar {
    background-color: #060f08;
    color: #00aa55;
    font-size: 10px;
    border-top: 1px solid #1a4a2a;
}
QComboBox, QSpinBox, QLineEdit {
    background-color: #0d2018;
    color: #00ff88;
    border: 1px solid #1a4a2a;
    border-radius: 2px;
    padding: 2px 4px;
    font-size: 10px;
}
QComboBox QAbstractItemView {
    background-color: #0d2018;
    color: #00ff88;
    selection-background-color: #1a4a28;
}
QSlider::groove:horizontal {
    background: #0a2018;
    height: 4px;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #00ff88;
    width: 12px;
    height: 12px;
    border-radius: 6px;
    margin: -4px 0;
}
QSlider::sub-page:horizontal { background: #00aa55; border-radius: 2px; }
QScrollArea { border: none; }
QTreeWidget {
    background: #050f09;
    color: #00ff88;
    border: 1px solid #1a4a2a;
    font-size: 10px;
}
QTreeWidget::item:selected { background: #1a4a28; }
"""


# ── Settings Dialog ───────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("CyberRig Settings")
        self.setStyleSheet(STYLE)
        self._build()

    def _build(self):
        layout = QFormLayout(self)

        self.port_combo = QComboBox()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo.addItems(ports or ["COM3"])
        idx = self.port_combo.findText(self.settings.cat_port)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)
        layout.addRow("CAT Port:", self.port_combo)

        self.baud_combo = QComboBox()
        for b in ["4800", "9600", "19200", "38400"]:
            self.baud_combo.addItem(b)
        self.baud_combo.setCurrentText(str(self.settings.cat_baud))
        layout.addRow("Baud Rate:", self.baud_combo)

        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(100, 2000)
        self.poll_spin.setSuffix(" ms")
        self.poll_spin.setValue(int(self.settings.poll_interval * 1000))
        layout.addRow("Poll Interval:", self.poll_spin)

        self.callsign_edit = QLineEdit()
        self.callsign_edit.setText(self.settings.callsign)
        layout.addRow("Callsign:", self.callsign_edit)

        self.rigctl_port_spin = QSpinBox()
        self.rigctl_port_spin.setRange(1024, 65535)
        self.rigctl_port_spin.setValue(self.settings.rigctl_port)
        self.rigctl_enabled_cb = QCheckBox("Enable rigctld server")
        self.rigctl_enabled_cb.setChecked(self.settings.rigctl_enabled)
        layout.addRow("rigctld Port:", self.rigctl_port_spin)
        layout.addRow("", self.rigctl_enabled_cb)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _save(self):
        self.settings.cat_port = self.port_combo.currentText()
        self.settings.cat_baud = int(self.baud_combo.currentText())
        self.settings.poll_interval = self.poll_spin.value() / 1000.0
        self.settings.callsign = self.callsign_edit.text().strip().upper()
        self.settings.rigctl_port = self.rigctl_port_spin.value()
        self.settings.rigctl_enabled = self.rigctl_enabled_cb.isChecked()
        self.settings.save()
        self.accept()


# ── Main Window ───────────────────────────────────────────────────────────────

MODES_ORDERED = [
    "LSB", "USB", "CW-U", "CW-L", "AM", "AM-N", "FM", "FM-N",
    "RTTY-L", "RTTY-U", "DATA-L", "DATA-U", "PSK",
]
BANDS = ["160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]


def _fmt_hz(hz: int) -> str:
    """14074000 -> '14.074.000'."""
    return f"{int(hz):,}".replace(",", ".")


class MainWindow(QMainWindow):
    """CyberRig desktop UI.

    Args:
        rig: a `RigProxy` (see `cyberrig.ui_legacy.rig_adapter`) wrapping an
            `FTdx10`. If omitted, one is built and this window owns it.
        owns_rig: whether this window is responsible for the CAT lifecycle
            (opening the serial port, starting the poll loop, running rigctld).
            Pass ``False`` when the rig is shared with an embedded web server
            that already does those things -- the serial port has exactly one
            owner, and two rigctld instances cannot both bind port 4532.
    """

    def __init__(self, rig=None, owns_rig=True):
        super().__init__()
        self.settings = Settings()
        if rig is None:
            rig = RigProxy(FTdx10())
        self.rig = rig
        self.owns_rig = owns_rig
        self.server = None

        self.setWindowTitle("CyberRig — FTDX10")
        self.setStyleSheet(STYLE)
        self.setMinimumSize(700, 460)
        self.setDockOptions(
            QMainWindow.AllowNestedDocks |
            QMainWindow.AllowTabbedDocks |
            QMainWindow.AnimatedDocks
        )

        self._build_central()
        self._build_docks()
        self._connect_signals()

        self._restore_geometry()

        QTimer.singleShot(300, self._connect_rig)

    # ── Window geometry / dock layout persistence ─────────────────────────

    def _restore_geometry(self):
        """Reopen at the size and dock layout last used.

        `window_geometry` has been in DEFAULTS since the beginning but was never
        actually read or written, so every launch opened at the minimum size.
        """
        geo = self.settings.get("window_geometry")
        state = self.settings.get("window_state")
        restored = False
        if geo:
            try:
                restored = self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
            except Exception:
                restored = False
        if state:
            try:
                self.restoreState(QByteArray.fromBase64(state.encode()),
                                  self.LAYOUT_VERSION)
            except Exception:
                pass
        if not restored:
            self._size_to_screen()
        if not state:
            # No saved layout: give the central column the lion's share.
            # Without this the docks expand to their own maxima (the EQ dock
            # will take 400px) and squeeze the centre down to its minimum.
            # Deferred: resizeDocks() is a no-op before the dock layout is
            # actually realised, which it isn't until after show().
            QTimer.singleShot(0, self._default_dock_sizes)

    def _default_dock_sizes(self):
        from PySide6.QtWidgets import QDockWidget
        docks = {d.objectName(): d for d in self.findChildren(QDockWidget)}
        side = [docks[n] for n in ("RX Controls", "TX Controls") if n in docks]
        if side:
            self.resizeDocks(side, [230] * len(side), Qt.Horizontal)
        # Start with the EQ/menu dock closed. Hiding it in _build_docks does not
        # stick -- QMainWindow.show() re-shows it -- so it has to happen here,
        # after the layout is realised.
        for name in ("EQ & Radio Menu",):
            if name in docks:
                docks[name].hide()

    def _size_to_screen(self):
        """Pick a sensible first-run size that actually fits this display.

        The shack machine is 1360x768, so a fixed default sized for a desktop
        monitor fills the whole screen and leaves no room for other apps.
        """
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1000, 640)
            return
        avail = screen.availableGeometry()
        w = min(1200, max(680, int(avail.width() * 0.72)))
        h = min(820, max(460, int(avail.height() * 0.80)))
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2,
                  avail.y() + (avail.height() - h) // 2)

    def _save_geometry(self):
        try:
            self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
            self.settings.window_state = bytes(
                self.saveState(self.LAYOUT_VERSION).toBase64()).decode()
        except Exception:
            pass

    # ── Hypermemory quick-recall bar ──────────────────────────────────────

    HYPERMEM_SLOTS = 6

    #: Bumped whenever the dock set or central layout changes. restoreState()
    #: matches docks by objectName and will happily apply a layout saved against
    #: a different set of docks, producing a garbled window (seen live: a stale
    #: state let the EQ dock swallow the whole window). A version mismatch makes
    #: Qt reject the old state instead.
    LAYOUT_VERSION = 4  # waterfall dock removed

    def _build_hypermemory_bar(self):
        """Six fixed slots. Click recalls; right-click stores/clears.

        Slots are positional, so `memories.json` is padded with blanks rather
        than compacted -- slot 4 must stay slot 4 even if slot 2 is empty.
        """
        bar = QHBoxLayout()
        bar.setSpacing(3)
        lbl = QLabel("MEM")
        lbl.setStyleSheet("color:#00aa66; font-size:10px; font-weight:bold;")
        bar.addWidget(lbl)

        self._hyper_btns: list[QPushButton] = []
        for i in range(self.HYPERMEM_SLOTS):
            b = QPushButton(f"{i + 1}")
            b.setFixedHeight(22)
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.clicked.connect(lambda _=False, idx=i: self._hyper_recall(idx))
            b.customContextMenuRequested.connect(
                lambda _pos, idx=i: self._hyper_menu(idx)
            )
            self._hyper_btns.append(b)
            bar.addWidget(b)
        bar.addStretch()
        self._refresh_hypermemory()
        return bar

    def _refresh_hypermemory(self):
        mems = load_memories()
        for i, b in enumerate(self._hyper_btns):
            if i < len(mems) and mems[i].freq:
                ch = mems[i]
                b.setText(f"{i + 1}: {ch.name}")
                b.setToolTip(f"{_fmt_hz(ch.freq)}  {ch.mode}  {ch.power}W\nRight-click to re-store or clear")
                b.setEnabled(True)
            else:
                b.setText(f"{i + 1}")
                b.setToolTip("Empty — right-click to store the current VFO here")
                b.setEnabled(True)

    @Slot()
    def _hyper_recall(self, idx: int):
        mems = load_memories()
        if idx >= len(mems) or not mems[idx].freq:
            self._status.showMessage(f"Memory slot {idx + 1} is empty", 3000)
            return
        if not self.rig.is_connected:
            self._status.showMessage("Radio not connected", 3000)
            return
        recall_channel(self.rig, mems[idx])
        self._status.showMessage(f"Recalled {mems[idx].name}", 3000)

    def _hyper_menu(self, idx: int):
        menu = QMenu(self)
        act_store = menu.addAction("Store current VFO here")
        act_clear = menu.addAction("Clear slot")
        chosen = menu.exec(QCursor.pos())
        if chosen == act_store:
            self._hyper_store(idx)
        elif chosen == act_clear:
            self._hyper_clear(idx)

    def _hyper_store(self, idx: int):
        if not self.rig.is_connected:
            self._status.showMessage("Radio not connected", 3000)
            return
        name, ok = QInputDialog.getText(
            self, "Store Memory", f"Name for slot {idx + 1}:", text=f"CH{idx + 1:02d}"
        )
        if not ok:
            return
        mems = load_memories()
        # Pad so slot numbering stays positional.
        while len(mems) <= idx:
            mems.append(MemoryChannel(name=f"CH{len(mems) + 1:02d}", freq=0))
        mems[idx] = snapshot(self.rig.state, name)
        save_memories(mems)
        self._refresh_hypermemory()
        if getattr(self, "_mem_panel", None):
            self._mem_panel.reload()
        self._status.showMessage(f"Stored slot {idx + 1}", 3000)

    def _hyper_clear(self, idx: int):
        mems = load_memories()
        if idx >= len(mems):
            return
        mems[idx] = MemoryChannel(name=f"CH{idx + 1:02d}", freq=0)
        save_memories(mems)
        self._refresh_hypermemory()
        if getattr(self, "_mem_panel", None):
            self._mem_panel.reload()
        self._status.showMessage(f"Cleared slot {idx + 1}", 3000)

    # ── Radio power (CAT PS) ──────────────────────────────────────────────

    @Slot()
    def _on_radio_power(self):
        want_on = self._btn_power.isChecked()
        try:
            self.rig.set_radio_power(want_on)
        except Exception as e:
            self._status.showMessage(f"Radio power failed: {e}", 4000)
            return
        self._status.showMessage("Radio ON" if want_on else "Radio OFF", 3000)

    @Slot(object)
    def _on_radio_power_changed(self, on):
        self._btn_power.blockSignals(True)
        self._btn_power.setChecked(bool(on))
        self._btn_power.blockSignals(False)

    # ── Internal ATU ──────────────────────────────────────────────────────

    @Slot()
    def _on_atu_toggle(self):
        try:
            self.rig.set_atu(self._btn_atu.isChecked())
        except Exception as e:
            self._status.showMessage(f"ATU failed: {e}", 4000)

    @Slot()
    def _on_atu_tune(self):
        try:
            self.rig.atu_tune()
        except Exception as e:
            self._status.showMessage(f"TUNE failed: {e}", 4000)

    @Slot(object, object)
    def _on_atu_changed(self, on, tuning):
        self._btn_atu.blockSignals(True)
        self._btn_atu.setChecked(bool(on))
        self._btn_atu.blockSignals(False)
        self._btn_tune.setText("TUNING…" if tuning else "TUNE")
        self._btn_tune.setStyleSheet(
            "background:#663300; color:#ffcc00; font-weight:bold;" if tuning else ""
        )

    # ── Central widget ────────────────────────────────────────────────────

    def _build_central(self):
        central = QWidget()
        # Wrapped in a scroll area rather than set directly: on a short screen
        # (the shack machine is 1360x768) the central column's minimum height
        # exceeds what's left after the dock rows, and Qt resolves that by
        # clipping children -- which chopped the bottom off the VFO digits.
        # Scrolling degrades gracefully instead of silently cutting content.
        central_scroll = QScrollArea()
        central_scroll.setWidgetResizable(True)
        central_scroll.setFrameShape(QFrame.NoFrame)
        central_scroll.setMinimumSize(420, 300)
        central_scroll.setWidget(central)
        self.setCentralWidget(central_scroll)
        root = QVBoxLayout(central)
        root.setSpacing(4)
        root.setContentsMargins(6, 4, 6, 4)

        # ── Connection header ──
        # FlowLayout so the button cluster wraps instead of pinning a ~426px floor.
        hdr = FlowLayout(margin=0, spacing=4)
        self._led = QLabel("●")
        self._led.setStyleSheet("color:#333; font-size:18px;")
        self._conn_lbl = QLabel("Disconnected")
        self._conn_lbl.setStyleSheet("color:#555; font-size:10px;")
        self._split_ind = QLabel("")
        self._split_ind.setStyleSheet("color:#ffcc00; font-size:10px; font-weight:bold;")
        self._rit_ind   = QLabel("")
        self._rit_ind.setStyleSheet("color:#00ccff; font-size:10px; font-weight:bold;")
        hdr.addWidget(self._led)
        hdr.addWidget(self._conn_lbl)
        hdr.addWidget(self._split_ind)
        hdr.addWidget(self._rit_ind)
        # Internal ATU: enable/bypass, plus a tuning cycle.
        self._btn_atu = QPushButton("ATU")
        self._btn_atu.setCheckable(True)
        self._btn_atu.setToolTip("Enable or bypass the internal ATU")
        self._btn_atu.clicked.connect(self._on_atu_toggle)
        self._btn_tune = QPushButton("TUNE")
        self._btn_tune.setToolTip("Start/stop the ATU tuning cycle (keys the radio)")
        self._btn_tune.clicked.connect(self._on_atu_tune)
        hdr.addWidget(self._btn_atu)
        hdr.addWidget(self._btn_tune)

        # CAT power (PS). Distinct from Connect, which is the serial link.
        self._btn_power = QPushButton("⏻ Radio")
        self._btn_power.setCheckable(True)
        self._btn_power.setToolTip("Switch the radio itself on/off over CAT")
        self._btn_power.clicked.connect(self._on_radio_power)
        hdr.addWidget(self._btn_power)

        self._btn_connect  = QPushButton("Connect")
        self._btn_settings = QPushButton("⚙ Settings")
        self._btn_connect.clicked.connect(self._toggle_connect)
        self._btn_settings.clicked.connect(self._on_settings)
        hdr.addWidget(self._btn_connect)
        hdr.addWidget(self._btn_settings)
        root.addLayout(hdr)

        root.addLayout(self._build_hypermemory_bar())

        # ── Main frequency display ──
        self.freq_display = FrequencyDisplay()
        self.freq_display.setMinimumHeight(70)
        freq_frame = QFrame()
        freq_frame.setStyleSheet(
            "QFrame { background:#050f09; border:1px solid #1a4a2a; border-radius:4px; }"
        )
        ffl = QVBoxLayout(freq_frame)
        ffl.setContentsMargins(4, 4, 4, 2)
        ffl.addWidget(self.freq_display)

        # VFO-B label (shown when split active)
        self._freq_b_lbl = QLabel("")
        self._freq_b_lbl.setStyleSheet(
            "color:#00ccff; font-family:Consolas; font-size:16px; padding-left:8px;"
        )
        ffl.addWidget(self._freq_b_lbl)
        root.addWidget(freq_frame)

        # ── Mode buttons ──
        mode_grp = QGroupBox("Mode")
        # FlowLayout, not QHBoxLayout: 13 buttons on one enforced line gave the
        # central widget a 608px floor and made the dock splitters unusable.
        ml = FlowLayout(mode_grp, margin=0, spacing=3)
        self._mode_btns: dict[str, QPushButton] = {}
        for m in MODES_ORDERED:
            btn = QPushButton(m)
            btn.setObjectName("modeBtn")
            btn.setCheckable(True)
            btn.setFixedHeight(24)
            btn.clicked.connect(lambda c, mode=m: self._on_mode_btn(mode))
            self._mode_btns[m] = btn
            ml.addWidget(btn)
        root.addWidget(mode_grp)

        # ── Band buttons ──
        band_grp = QGroupBox("Band")
        bl = FlowLayout(band_grp, margin=0, spacing=3)
        for band in BANDS:
            btn = QPushButton(band)
            btn.setFixedHeight(24)
            btn.clicked.connect(lambda c, b=band: self.rig.go_band(b))
            bl.addWidget(btn)
        root.addWidget(band_grp)

        # ── Meters row ──
        meter_row = QHBoxLayout()
        meter_row.setSpacing(8)

        # S-meter
        self.smeter = SMeter()
        self.smeter.setMinimumSize(170, 78)
        meter_row.addWidget(self.smeter, stretch=2)

        # TX meters
        self.tx_meter = TXMeter()
        self.tx_meter.setMinimumSize(130, 54)
        meter_row.addWidget(self.tx_meter, stretch=1)

        # Quick control column
        qcol = QVBoxLayout()
        qcol.setSpacing(4)

        # PTT
        self._ptt_btn = QPushButton("PTT")
        self._ptt_btn.setObjectName("pttBtn")
        self._ptt_btn.setCheckable(True)
        self._ptt_btn.clicked.connect(self._on_ptt)
        qcol.addWidget(self._ptt_btn)

        # Quick DSP toggles
        dsp_row1 = QHBoxLayout()
        for lbl, fn in [("DNF", lambda c: self.rig.set_dnf(c)),
                         ("NB",  lambda c: self.rig.set_nb(c))]:
            b = QPushButton(lbl)
            b.setCheckable(True)
            b.setFixedHeight(24)
            b.clicked.connect(fn)
            dsp_row1.addWidget(b)
            setattr(self, f"_quick_{lbl.lower()}", b)
        qcol.addLayout(dsp_row1)

        dsp_row2 = QHBoxLayout()
        for lbl, fn in [("NR", lambda c: self.rig.set_nr(c)),
                         ("LOCK", lambda c: self.rig.set_lock(c))]:
            b = QPushButton(lbl)
            b.setCheckable(True)
            b.setFixedHeight(24)
            b.clicked.connect(fn)
            dsp_row2.addWidget(b)
            setattr(self, f"_quick_{lbl.lower()}", b)
        qcol.addLayout(dsp_row2)

        # Tune step + VFO
        vfo_row = QHBoxLayout()
        btn_swap = QPushButton("A↕B")
        btn_atob = QPushButton("A→B")
        btn_swap.clicked.connect(self.rig.swap_vfo)
        btn_atob.clicked.connect(self.rig.vfo_a_to_b)
        vfo_row.addWidget(btn_swap)
        vfo_row.addWidget(btn_atob)
        qcol.addLayout(vfo_row)

        meter_row.addLayout(qcol)
        root.addLayout(meter_row)

        # ── Filter info strip ──
        self._filter_lbl = QLabel("BW: — Hz  |  Shift: 0 Hz")
        self._filter_lbl.setStyleSheet(
            "color:#00aa55; font-size:10px; background:#050f09; "
            "border:1px solid #1a4a2a; padding:2px 6px;"
        )
        root.addWidget(self._filter_lbl)

        # ── Waterfall ──

        # ── Status bar ──
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._server_lbl = QLabel("")
        self._status.addPermanentWidget(self._server_lbl)

    # ── Dock widgets ──────────────────────────────────────────────────────

    def _build_docks(self):
        def make_dock(title, widget, area):
            dock = QDockWidget(title, self)
            dock.setObjectName(title)
            dock.setAllowedAreas(Qt.AllDockWidgetAreas)
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setWidget(widget)
            # Without an explicit minimum, a resizable QScrollArea reports the
            # inner panel's own minimum as its own — which makes the dock
            # splitters immovable, because the dock can never be dragged
            # narrower than the widest button row inside it. Pin a small
            # minimum and let the panel scroll instead.
            sa.setMinimumWidth(120)
            sa.setMinimumHeight(80)
            dock.setWidget(sa)
            self.addDockWidget(area, dock)
            return dock

        # Left: RX + Filter (tabbed)
        self._rx_panel     = RXPanel(self.rig)
        self._filter_panel = FilterPanel(self.rig)
        rx_dock     = make_dock("RX Controls",  self._rx_panel,     Qt.LeftDockWidgetArea)
        filter_dock = make_dock("Filters",       self._filter_panel, Qt.LeftDockWidgetArea)
        self.tabifyDockWidget(rx_dock, filter_dock)
        rx_dock.raise_()

        # Right: TX + Split + CW (tabbed)
        self._tx_panel     = TXPanel(self.rig)
        self._split_panel  = SplitPanel(self.rig)
        self._cw_panel     = CWPanel(self.rig)
        tx_dock    = make_dock("TX Controls",  self._tx_panel,    Qt.RightDockWidgetArea)
        split_dock = make_dock("Split / RIT",  self._split_panel, Qt.RightDockWidgetArea)
        cw_dock    = make_dock("CW Keyer",     self._cw_panel,    Qt.RightDockWidgetArea)
        self.tabifyDockWidget(tx_dock, split_dock)
        self.tabifyDockWidget(split_dock, cw_dock)
        tx_dock.raise_()

        # Bottom: EQ + Menu access
        self._eq_panel = EQMenuPanel(self.rig)
        eq_dock = make_dock("EQ & Radio Menu", self._eq_panel, Qt.BottomDockWidgetArea)
        eq_dock.setMaximumHeight(260)
        # Hidden by default: it is configuration (parametric EQ + the radio's
        # menu tree), not an operating control, and on a 768px-tall screen it
        # was taking a third of the window and pushing the S-meter and PTT out
        # of view. Toggle it from View. A saved layout re-opens it if the user
        # had it showing, since restoreState() runs after this.

        # Right-bottom: Macro palette (not in scroll area — it has its own)
        self._macro_panel = MacroPanel(self.rig, self.settings.callsign)
        macro_dock = QDockWidget("Macros", self)
        macro_dock.setObjectName("Macros")
        macro_dock.setWidget(self._macro_panel)
        macro_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.addDockWidget(Qt.RightDockWidgetArea, macro_dock)
        self.tabifyDockWidget(cw_dock, macro_dock)
        tx_dock.raise_()
        self._macro_panel.open_editor.connect(self._on_macro_edit)

        # Right: memory channels, tabbed with the macro palette
        self._mem_panel = MemoryPanel(self.rig)
        mem_dock = QDockWidget("Memories", self)
        mem_dock.setObjectName("Memories")
        mem_dock.setWidget(self._mem_panel)
        mem_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.addDockWidget(Qt.RightDockWidgetArea, mem_dock)
        self.tabifyDockWidget(macro_dock, mem_dock)
        tx_dock.raise_()

        # Add dock visibility toggles to View menu
        menu = self.menuBar().addMenu("View")
        for dock in [rx_dock, filter_dock, tx_dock, split_dock, cw_dock,
                     macro_dock, mem_dock, eq_dock]:
            menu.addAction(dock.toggleViewAction())

        menu = self.menuBar().addMenu("Help")
        menu.addAction("About CyberRig", self._about)

    # ── Signal wiring ─────────────────────────────────────────────────────

    def _connect_signals(self):
        self.rig.freq_changed.connect(self._on_freq)
        self.rig.freq_b_changed.connect(self._on_freq_b)
        self.rig.mode_changed.connect(self._on_mode)
        self.rig.smeter_update.connect(self.smeter.set_value)
        self.rig.meter_update.connect(self._on_meters)
        self.rig.ptt_changed.connect(self._on_ptt_rig)
        self.rig.split_changed.connect(self._on_split_rig)
        self.rig.rit_changed.connect(self._on_rit_rig)
        self.rig.filter_changed.connect(self._on_filter)
        self.rig.dnf_changed.connect(lambda on: self._quick_dnf.setChecked(on))
        self.rig.connected_changed.connect(self._on_connected)
        self.rig.radio_power_changed.connect(self._on_radio_power_changed)
        self.rig.atu_changed.connect(self._on_atu_changed)
        self.freq_display.freq_changed.connect(self.rig.set_freq)

    # ── Connection ────────────────────────────────────────────────────────

    def _connect_rig(self):
        if not self.owns_rig:
            # The embedded web server owns connect/polling/rigctld; just
            # reflect whatever state the shared rig is already in.
            if self.rig.is_connected:
                self._on_connected(True)
            return
        port = self.settings.cat_port
        baud = self.settings.cat_baud
        self._status.showMessage(f"Connecting {port} @ {baud}…")
        ok = self.rig.cat_connect(port, baud)
        if ok:
            self.rig.start_polling(self.settings.poll_interval)
            self._start_server()
        else:
            self._status.showMessage(f"Connect failed on {port} — check Settings")

    def _start_server(self):
        if self.settings.rigctl_enabled and self.owns_rig:
            self.server = RigctlServer(self.rig, self.settings.rigctl_port)
            self.server.start()
            self._server_lbl.setText(f"rigctld :{self.settings.rigctl_port}")

    @Slot()
    def _toggle_connect(self):
        if self.rig.is_connected:
            self.rig.cat_disconnect()
            if self.server:
                self.server.stop()
                self.server = None
            self._btn_connect.setText("Connect")
            self._server_lbl.setText("")
        else:
            self._connect_rig()

    @Slot()
    def _on_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            if self.rig.is_connected:
                self.rig.cat_disconnect()
            if self.server:
                self.server.stop()
                self.server = None
            self._connect_rig()

    # ── Rig signal handlers ───────────────────────────────────────────────

    @Slot(bool)
    def _on_connected(self, ok: bool):
        if ok:
            self._led.setStyleSheet("color:#00ff88; font-size:18px;")
            self._conn_lbl.setText(f"{self.settings.cat_port} @ {self.settings.cat_baud}")
            self._conn_lbl.setStyleSheet("color:#00ff88; font-size:10px;")
            self._btn_connect.setText("Disconnect")
            self._status.showMessage("Connected")
        else:
            self._led.setStyleSheet("color:#440000; font-size:18px;")
            self._conn_lbl.setText("Disconnected")
            self._conn_lbl.setStyleSheet("color:#555; font-size:10px;")
            self._btn_connect.setText("Connect")
            self._status.showMessage("Disconnected")

    @Slot(int)
    def _on_freq(self, hz: int):
        self.freq_display.set_freq(hz)

    @Slot(int)
    def _on_freq_b(self, hz: int):
        if self.rig.state.split:
            s = f"{hz:09d}"
            self._freq_b_lbl.setText(f"VFO-B: {s[0:3]}.{s[3:6]}.{s[6:9]}")
        else:
            self._freq_b_lbl.setText("")

    @Slot(str)
    def _on_mode(self, mode: str):
        for m, btn in self._mode_btns.items():
            btn.setChecked(m == mode)

    @Slot(int, int, int)
    def _on_meters(self, pwr, alc, swr):
        self.tx_meter.set_meters(pwr, alc, swr)

    @Slot(bool)
    def _on_ptt_rig(self, tx: bool):
        self._ptt_btn.setChecked(tx)
        self._ptt_btn.setText("● TX" if tx else "PTT")
        self.tx_meter.set_tx(tx)
        if tx:
            self._ptt_btn.setStyleSheet("")  # let stylesheet handle it

    @Slot(bool)
    def _on_split_rig(self, on: bool):
        self._split_ind.setText("SPLIT" if on else "")
        if not on:
            self._freq_b_lbl.setText("")

    @Slot(bool, int)
    def _on_rit_rig(self, on: bool, offset: int):
        if on:
            self._rit_ind.setText(f"RIT {offset:+d}Hz")
        else:
            self._rit_ind.setText("")

    @Slot(int, int)
    def _on_filter(self, bw_hz, ifs):
        lbl = f"BW: {bw_hz} Hz" if bw_hz else "BW: Default"
        self._filter_lbl.setText(f"{lbl}  |  Shift: {ifs:+d} Hz")

    # ── User actions ──────────────────────────────────────────────────────

    def _on_mode_btn(self, mode: str):
        self.rig.set_mode(mode)
        for m, btn in self._mode_btns.items():
            btn.setChecked(m == mode)

    def _on_ptt(self, checked: bool):
        self.rig.set_ptt(checked)


    def _on_macro_edit(self, idx: int):
        macro = self._macro_panel._macros[idx]
        dlg = MacroEditorDialog(macro, self)
        dlg.macro_saved.connect(lambda m: self._macro_panel.update_macro(idx, m))
        dlg.exec()

    def _about(self):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.about(self, "CyberRig",
            "CyberRig — FTDX10 Control\n\n"
            "Full CAT control • hamlib rigctld server\n"
            "Filters • EQ • P-EQ • Split • RIT/XIT • CW\n"
            "Audio FFT Waterfall")

    # ── Close ─────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        # Only tear down CAT if this window owns it. In shared mode the embedded
        # web server owns the port and may still be serving other clients.
        if self.owns_rig:
            self.rig.cat_disconnect()
            if self.server:
                self.server.stop()
        self._save_geometry()
        self.settings.save()
        event.accept()
