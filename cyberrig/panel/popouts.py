"""Memories and Macros as separate small windows.

These are the long-tail surfaces. Docking them into the main panel is what
made the old window grow: each QDockWidget carried a title bar, each could be
dragged into the layout, and _default_dock_sizes() existed purely to stop them
eating the centre column. As popouts they open when needed, close when not,
and never negotiate for space with the panel.

Sizes are frozen with the panel spec: memories 535x524, macros 433x262,
meters 360x220.
"""

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QSlider, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from cyberrig.cat import meter_cal
from cyberrig.cat.ftdx10 import EQ_BAND_RANGE, EQ_FREQ_OPTIONS, MODES
from cyberrig.memories.model import MemoryChannel
from cyberrig.memories.store import load_memories, save_memories
from cyberrig.macros.store import load_macros
from cyberrig.panel import theme as T
from cyberrig.panel.widgets import BarMeter, Group, button

log = logging.getLogger(__name__)


def _fmt_freq(hz: int) -> str:
    """Same 10.131.000 grouping the panel's own readout uses."""
    mhz, rest = divmod(max(0, int(hz)), 1_000_000)
    khz, h = divmod(rest, 1000)
    return f"{mhz}.{khz:03d}.{h:03d}"


class _Popout(QDialog):
    """Common shell: fixed size, panel styling, closes rather than hides."""

    def __init__(self, title, w, h, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedSize(w, h)
        self.setStyleSheet(T.STYLE)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)


class MemoriesPopout(_Popout):
    """Channel table with recall/store/edit. 535x524."""

    recall_requested = Signal(object)   # MemoryChannel
    store_requested = Signal(int)       # row index to overwrite

    COLS = ("CH", "LABEL", "FREQUENCY", "MODE", "W")

    def __init__(self, parent=None):
        super().__init__("Frequency Memories", 535, 524, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(5)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c, w in ((0, 42), (2, 108), (3, 66), (4, 44)):
            self.table.setColumnWidth(c, w)
            hh.setSectionResizeMode(c, QHeaderView.Fixed)
        self.table.doubleClicked.connect(lambda *_: self._recall())
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.btns = {}
        for name in ("RECALL", "STORE", "EDIT", "DELETE", "RELOAD"):
            b = button(name, height=22)
            row.addWidget(b, 1)
            self.btns[name] = b
        lay.addLayout(row)

        self.btns["RECALL"].clicked.connect(self._recall)
        self.btns["STORE"].clicked.connect(self._store)
        self.btns["EDIT"].clicked.connect(self._edit)
        self.btns["DELETE"].clicked.connect(self._delete)
        self.btns["RELOAD"].clicked.connect(self.reload)

        self.memories: list[MemoryChannel] = []
        self.reload()

    # -- data -------------------------------------------------------------

    def reload(self):
        self.memories = load_memories()
        self.refresh()

    def refresh(self):
        # Always show at least 20 rows so empty channels read as slots
        # waiting to be filled rather than as a broken table.
        n = max(20, len(self.memories))
        self.table.setRowCount(n)
        for i in range(n):
            m = self.memories[i] if i < len(self.memories) else None
            vals = (
                f"{i + 1:03d}",
                m.name if m else "—",
                _fmt_freq(m.freq) if m else "—",
                m.mode if m else "—",
                str(m.power) if m else "—",
            )
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c in (2, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if m is None:
                    item.setForeground(Qt.darkGray)
                elif c == 2:
                    item.setForeground(Qt.green)
                self.table.setItem(i, c, item)
            self.table.setRowHeight(i, 19)

    def _selected(self):
        r = self.table.currentRow()
        return r if 0 <= r < len(self.memories) else -1

    # -- actions ----------------------------------------------------------

    def _recall(self):
        i = self._selected()
        if i < 0:
            return
        self.recall_requested.emit(self.memories[i])

    def _store(self):
        r = self.table.currentRow()
        if r < 0:
            return
        self.store_requested.emit(r)

    def _edit(self):
        from PySide6.QtWidgets import QInputDialog
        i = self._selected()
        if i < 0:
            return
        name, ok = QInputDialog.getText(
            self, "Channel label", f"Label for channel {i + 1:03d}:",
            text=self.memories[i].name)
        if ok:
            self.memories[i].name = name.strip() or "Memory"
            save_memories(self.memories)
            self.refresh()

    def _delete(self):
        i = self._selected()
        if i < 0:
            return
        if QMessageBox.question(
            self, "Delete channel",
            f"Delete channel {i + 1:03d} ({self.memories[i].name})?"
        ) != QMessageBox.Yes:
            return
        del self.memories[i]
        save_memories(self.memories)
        self.refresh()

    def take_snapshot(self, row: int, channel: MemoryChannel):
        """Write `channel` into `row`, padding the list if needed."""
        while len(self.memories) <= row:
            self.memories.append(MemoryChannel(name="Memory"))
        self.memories[row] = channel
        save_memories(self.memories)
        self.refresh()


class MacrosPopout(_Popout):
    """4-wide grid of macro buttons. 433x262."""

    macro_requested = Signal(object)   # Macro
    stop_requested = Signal()
    edit_requested = Signal()

    COLS = 4
    ROWS = 5

    def __init__(self, parent=None):
        super().__init__("Macros", 433, 262, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(5)

        self.grid_w = QGridLayout()
        self.grid_w.setSpacing(4)
        lay.addLayout(self.grid_w, 1)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.edit_btn = button("EDIT…", height=22)
        self.stop_btn = button("STOP", height=22)
        row.addWidget(self.edit_btn, 1)
        row.addWidget(self.stop_btn, 1)
        lay.addLayout(row)

        self.edit_btn.clicked.connect(self.edit_requested)
        self.stop_btn.clicked.connect(self.stop_requested)

        self.macros = []
        self.reload()

    def reload(self):
        while self.grid_w.count():
            item = self.grid_w.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.macros = load_macros()
        slots = self.COLS * self.ROWS
        for i in range(slots):
            m = self.macros[i] if i < len(self.macros) else None
            if m is None:
                b = button(f"M{i + 1}", height=24)
                b.setEnabled(False)
            else:
                label = m.name if len(m.name) <= 14 else m.name[:13] + "…"
                b = button(label, height=24, tip=m.desc or m.name)
                b.clicked.connect(lambda _=False, mm=m: self.macro_requested.emit(mm))
            self.grid_w.addWidget(b, i // self.COLS, i % self.COLS)


class _EqBand(QWidget):
    """One parametric band: freq code, signed dB, bandwidth 1–10."""

    def __init__(self, section, band, rig, parent=None):
        super().__init__(parent)
        self.section = section
        self.band = int(band)
        self.rig = rig
        self._range = EQ_BAND_RANGE[self.band]
        self._writing = False

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._write)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        title = QLabel(f"BAND {self.band + 1}  {self._range.upper()}")
        title.setObjectName("dim")
        lay.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(6)
        freq_l = QLabel("FREQ")
        freq_l.setObjectName("dim")
        freq_l.setFixedWidth(32)
        self.freq = QComboBox()
        for code, label in EQ_FREQ_OPTIONS[self._range].items():
            self.freq.addItem("OFF" if label == "OFF" else f"{label} Hz", int(code))
        row.addWidget(freq_l)
        row.addWidget(self.freq, 1)
        lay.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(6)
        lvl_l = QLabel("LVL")
        lvl_l.setObjectName("dim")
        lvl_l.setFixedWidth(32)
        self.level = QSlider(Qt.Horizontal)
        self.level.setRange(-20, 10)
        self.level.setValue(0)
        self.level_val = QLabel("+0 dB")
        self.level_val.setObjectName("value")
        self.level_val.setFixedWidth(44)
        row.addWidget(lvl_l)
        row.addWidget(self.level, 1)
        row.addWidget(self.level_val)
        lay.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(6)
        bw_l = QLabel("BW")
        bw_l.setObjectName("dim")
        bw_l.setFixedWidth(32)
        self.bw = QSpinBox()
        self.bw.setRange(1, 10)
        self.bw.setValue(5)
        row.addWidget(bw_l)
        row.addWidget(self.bw)
        row.addStretch(1)
        lay.addLayout(row)

        self.freq.currentIndexChanged.connect(self._schedule)
        self.level.valueChanged.connect(self._on_level)
        self.bw.valueChanged.connect(self._schedule)

    def _on_level(self, v):
        self.level_val.setText(f"{v:+d} dB")
        self._schedule()

    def _schedule(self, *_):
        if self._writing:
            return
        self._debounce.start()

    def values(self):
        return {
            "freq": int(self.freq.currentData()),
            "level": int(self.level.value()),
            "bw": int(self.bw.value()),
        }

    def _write(self):
        if self._writing:
            return
        vals = self.values()
        try:
            self.rig.set_eq_band(self.section, self.band,
                                 vals["freq"], vals["level"], vals["bw"])
        except Exception:
            log.exception("eq write %s band %s", self.section, self.band)

    def apply(self, freq, level, bw):
        self._writing = True
        try:
            idx = self.freq.findData(int(freq))
            if idx >= 0:
                self.freq.setCurrentIndex(idx)
            self.level.setValue(int(level))
            self.level_val.setText(f"{int(level):+d} dB")
            self.bw.setValue(int(bw))
        finally:
            self._writing = False

    def read_from_rig(self):
        try:
            val = self.rig.get_eq_band(self.section, self.band)
        except Exception:
            log.exception("eq read %s band %s", self.section, self.band)
            return False
        if not val:
            return False
        self.apply(val["freq"], val["level"], val["bw"])
        return True


class EqPopout(_Popout):
    """TX DSP EQ + Mic P-EQ. Same EX 03 03 bands as the web EQ page.

    Radio-menu browser stays on desktop.py — this is the operating EQ face.
    """

    def __init__(self, rig, parent=None):
        super().__init__("Parametric EQ", 680, 430, parent)
        self.rig = rig
        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)
        self.sections = {}
        for section, title in (("tx", "TX DSP EQ"), ("mic", "MIC P-EQ")):
            g = Group(title)
            bands = [_EqBand(section, i, rig) for i in range(3)]
            for b in bands:
                g.body.addWidget(b)
            read = button("READ FROM RADIO", height=22)
            read.setObjectName("popout")
            read.clicked.connect(lambda _=False, sec=section: self.read_section(sec))
            g.body.addWidget(read)
            g.body.addStretch(1)
            root.addWidget(g, 1)
            self.sections[section] = bands

    def read_section(self, section):
        for b in self.sections[section]:
            b.read_from_rig()

    def read_all(self):
        self.read_section("tx")
        self.read_section("mic")


#: Our own name in the FTdx10 meter-request table. The panel registers as
#: "panel" for whatever its swappable slots happen to be showing; this window
#: always wants PO, ALC and SWR specifically, so it registers its own interest
#: and releases it on close. Sharing the panel's owner id would mean whichever
#: of the two closed first switched the other one's meters off.
METER_OWNER = "meters-popout"


class MetersPopout(_Popout):
    """PO, ALC and SWR with a remote-tuner TUNE button. 360x220.

    The panel is setFixedSize (1436x710 at 1.33) and so cannot be tiled beside
    a waterfall, a decoder and a map on one 1920x1080 screen. Mid-QSO on FT8
    the only rig facts that matter are "am I making power", "is the radio
    limiting", and "is the antenna still matched", plus being able to re-run
    the tuner after a band change -- so this is those four things and nothing
    else, small enough to keep in a corner of an FT8 layout.

    Always-on-top, deliberately: the decoder's Log QSO dialog pops up over
    whatever it likes after every contact, and a meter you cannot see during
    the over after it is not a meter.
    """

    #: Sized in LOGICAL px, because LARGE mode runs the panel at
    #: QT_SCALE_FACTOR 1.33 and setFixedSize is scaled with everything else:
    #: 480 logical came out 638 real and hung off the right of a 1920 screen.
    #: 360x220 lands at ~479x293 scaled, which still fits the layout's 500 px
    #: column at either scale.
    def __init__(self, rig, tuner=None, parent=None):
        super().__init__("CyberRig — PO / ALC / SWR", 360, 220, parent)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.rig = rig
        self.tuner = tuner
        self._registered = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.po = BarMeter(meter_cal.PO, label_fn=self._tick_label)
        self.alc = BarMeter(meter_cal.ALC, label_fn=self._tick_label)
        self.swr = BarMeter(meter_cal.SWR, label_fn=self._tick_label)
        for m in (self.po, self.alc, self.swr):
            m.setFixedHeight(40)
            lay.addWidget(m)

        # TUNE here is the external MFJ-994BRT carrier (cyberrig/tune.py), the
        # same object the panel's OPS/TUNE drives -- NOT the radio's internal
        # ATU cycle. One RemoteTuner, so the interlocks hold no matter which
        # surface started it.
        self.tune_btn = button("TUNE", checkable=True, height=34)
        self.tune_btn.setToolTip(
            "Remote tuner: keys a continuous carrier for the external "
            "autotuner, then restores your mode and power. Same tuner as the "
            "panel's OPS/TUNE and the web UI -- not the radio's internal ATU.")
        self.tune_btn.clicked.connect(self._on_tune_clicked)
        lay.addWidget(self.tune_btn)

        self.status = QLabel("")
        self.status.setStyleSheet(
            f"color:#9db3ac;font-family:{T.MONO};font-size:10px;")
        lay.addWidget(self.status)

        # Follow the tuner object rather than this button's own state: it can
        # be started, stopped or safety-timed-out from the panel or the web UI.
        self._tune_poll = QTimer(self)
        self._tune_poll.timeout.connect(self._sync_tune_button)
        self._tune_poll.start(300)
        self._sync_tune_button()

        if rig is not None:
            rig.meters_update.connect(self._on_meters)

    @staticmethod
    def _tick_label(v):
        return meter_cal.format_number(v, 0 if float(v).is_integer() else 1)

    # -- meters -----------------------------------------------------------

    def _on_meters(self, meters: dict):
        if meter_cal.PO.rm in meters:
            self.po.set_raw(meters[meter_cal.PO.rm])
        if meter_cal.ALC.rm in meters:
            self.alc.set_raw(meters[meter_cal.ALC.rm])
        if meter_cal.SWR.rm in meters:
            self.swr.set_raw(meters[meter_cal.SWR.rm])

    def _register(self, on: bool):
        if self.rig is None or on == self._registered:
            return
        try:
            if on:
                self.rig.request_meters(
                    METER_OWNER,
                    {meter_cal.PO.rm, meter_cal.ALC.rm, meter_cal.SWR.rm})
            else:
                self.rig.release_meters(METER_OWNER)
            self._registered = on
        except Exception:
            log.exception("meters popout: could not %s meter interest",
                          "register" if on else "release")

    def showEvent(self, ev):
        super().showEvent(ev)
        self._register(True)

    def closeEvent(self, ev):
        self._register(False)
        super().closeEvent(ev)

    # -- remote tuner -----------------------------------------------------

    def _on_tune_clicked(self, on):
        if self.tuner is None:
            self.tune_btn.setChecked(False)
            self.status.setText("no tuner")
            return
        result = self.tuner.start() if on else self.tuner.stop()
        if not result.get("ok"):
            log.warning("remote tune: %s", result.get("error"))
            self.status.setText(str(result.get("error", "tune failed")))
        else:
            self.status.setText("")
        self._sync_tune_button()

    def _sync_tune_button(self):
        active = bool(self.tuner and self.tuner.active)
        if self.tune_btn.isChecked() != active:
            self.tune_btn.setChecked(active)
        text = "● TUNING" if active else "TUNE"
        if self.tune_btn.text() != text:
            self.tune_btn.setText(text)
