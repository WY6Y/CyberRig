"""CyberRig control panel — fixed 1080x534 instrument face.

Geometry frozen 2026-08-24 — 1080x534, sized for a 1920x1200 display:

    Window          1080 x 534   fixed; 6 px body padding, 5 px gaps
    Header strip    1068 x  26   rig, CAT port, rigctld, WPM, power, UTC
    Row A            h 132       Band 196 | RF 104 | VFO A/B 340 | Meters 413
    Row B            h 130       Mode 196 | CW 104 | Filter 200 | Memory 150 |
                                 Clarifier 398
    Row C            h 186       Receive 196 | Transmit 309 | DSP 553 | Ops 96
    Footer          1068 x  32   Status | Macros | Mems | EQ | PTT 200
    Button cell      h  20       one constant, 3 px grid gap

The left rail is load-bearing: all three rows start a panel at x=6 with width
196, so Band / Mode / Receive stack into one column, and rows A and B share a
second rail 104 px wide. The DSP block on row C is the only region with slack,
so that is where a future panel grows into.

Why fixed and not merely defaulted: the old window called _size_to_screen(),
which took 72% of screen width and 80% of height — 1200x820 on this display,
2.6x the pixel area — and then the docks expanded to fill whatever it took.
A control panel is an instrument face, not a workspace; it should leave room
for WSJT-X, a logger and a browser around it.
"""

import logging
import os
import subprocess
import sys
import time

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow, QSizePolicy,
    QVBoxLayout, QWidget,
)

from cyberrig.cat import meter_cal
from cyberrig.cat.ftdx10 import (
    BAND_EDGES, FTdx10, MODES, next_att, next_preamp,
)
from cyberrig.macros.engine import MacroRunner
from cyberrig.memories.model import MemoryChannel
from cyberrig.panel import theme as T
from cyberrig.panel.popouts import (
    EqPopout, MacrosPopout, MemoriesPopout, MetersPopout,
)
from cyberrig.panel.widgets import (
    BarMeter, FreqDisplay, Group, HSlider, SwappableMeter, TuneStrip,
    VSlider, button, button_grid,
)
from cyberrig.settings import Settings
from cyberrig.ui_legacy.rig_adapter import RigProxy

log = logging.getLogger(__name__)

BANDS = ["160m", "80m", "60m", "40m", "30m", "20m",
         "17m", "15m", "12m", "10m", "6m", "GEN"]

PANEL_MODES = ["USB", "LSB", "CW-U", "CW-L", "AM", "FM",
               "FM-N", "RTTY-L", "RTTY-U", "DATA-L", "DATA-U", "PSK"]

#: Who we are in FTdx10's meter-request table.
METER_OWNER = "panel"


def _hline(parent=None):
    f = QFrame(parent)
    f.setFixedWidth(1)
    f.setStyleSheet(f"background:{T.RULE};")
    return f


class PanelWindow(QMainWindow):
    """The whole rig on one non-resizable face."""

    def __init__(self, rig=None, owns_rig=True, tuner=None, wspr=None,
                 parent=None):
        super().__init__(parent)
        self.settings = Settings()
        if rig is None:
            rig = RigProxy(FTdx10())
        self.rig = rig
        self.owns_rig = owns_rig
        self._last_cmd_ms = 0
        # Remote-tuner carrier (OPS TUNE). Shared with the web server when one
        # is running, so both UIs see the same TUNE state and interlocks; see
        # cyberrig/tune.py. None means no tuner is available and the button
        # says so rather than silently doing nothing.
        self.tuner = tuner
        # WSPR auto-tune, the rigctld server's gate. Shared with the web UI for
        # the same reason as the tuner: the panel must show the phase the thing
        # actually sitting in the transmit path is in, not a second guess at it.
        # None when there is no web server, since without rigctld there is no
        # band hop to react to.
        self.wspr = wspr

        self.setWindowTitle("CyberRig — FTDX10")
        self.setStyleSheet(T.STYLE)
        # Fixed, not merely defaulted. Nothing here reflows, so there is no
        # layout to restore and no saved geometry to go stale.
        self.setFixedSize(T.WIN_W, T.WIN_H)

        self._build()
        self._connect_signals()
        self._centre_on_screen()

        self.runner = MacroRunner(self.rig, callsign=self.settings.get("callsign", ""))

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick_clock)
        self._clock.start(1000)
        self._tick_clock()

        QTimer.singleShot(300, self._connect_rig)

    # ── layout ───────────────────────────────────────────────────────────

    def _centre_on_screen(self):
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        a = screen.availableGeometry()
        self.move(a.x() + (a.width() - T.WIN_W) // 2,
                  a.y() + (a.height() - T.WIN_H) // 2)

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        body = QWidget()
        grid = QVBoxLayout(body)
        grid.setContentsMargins(T.PAD, T.PAD, T.PAD, 0)
        grid.setSpacing(T.GAP)
        grid.addWidget(self._build_row_a())
        grid.addWidget(self._build_row_b())
        grid.addWidget(self._build_row_c(), 1)
        outer.addWidget(body, 1)

        outer.addWidget(self._build_footer())

    # -- header -----------------------------------------------------------

    def _build_header(self):
        w = QWidget()
        w.setFixedHeight(T.HEAD_H)
        w.setStyleSheet(
            f"background:{T.PANEL_2};border-bottom:1px solid {T.RULE};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(0)

        def key(text):
            l = QLabel(text)
            l.setStyleSheet(f"color:{T.DIMMER};font-size:10px;")
            return l

        def val(text):
            l = QLabel(text)
            l.setStyleSheet(f"color:{T.INK};font-size:10px;")
            return l

        brand = QLabel("CYBERRIG")
        brand.setStyleSheet(f"color:{T.PHOS};font-size:10px;font-weight:bold;")
        lay.addWidget(brand)
        lay.addSpacing(10); lay.addWidget(_hline(w)); lay.addSpacing(10)

        lay.addWidget(key("FTDX10"))
        lay.addSpacing(10); lay.addWidget(_hline(w)); lay.addSpacing(10)

        self.hdr_link = QLabel("●")
        self.hdr_link.setStyleSheet(f"color:{T.DIMMER};font-size:10px;")
        lay.addWidget(self.hdr_link)
        lay.addSpacing(5)
        lay.addWidget(key("CAT"))
        lay.addSpacing(5)
        self.hdr_port = val("—")
        lay.addWidget(self.hdr_port)
        lay.addSpacing(10); lay.addWidget(_hline(w)); lay.addSpacing(10)

        lay.addWidget(key("RIGCTLD"))
        lay.addSpacing(5)
        lay.addWidget(val(f":{self.settings.get('rigctl_port', 4532)}"))

        lay.addStretch(1)

        lay.addWidget(key("WPM"))
        lay.addSpacing(5)
        self.hdr_wpm = val("—")
        lay.addWidget(self.hdr_wpm)
        lay.addSpacing(10); lay.addWidget(_hline(w)); lay.addSpacing(10)

        lay.addWidget(key("PWR"))
        lay.addSpacing(5)
        self.hdr_pwr = val("—")
        lay.addWidget(self.hdr_pwr)
        lay.addSpacing(10); lay.addWidget(_hline(w)); lay.addSpacing(10)

        self.hdr_utc = val("--:--Z")
        lay.addWidget(self.hdr_utc)
        return w

    # -- row A ------------------------------------------------------------

    def _build_row_a(self):
        w = QWidget()
        w.setFixedHeight(T.ROW_A_H)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(T.GAP)

        # BAND
        g = Group("BAND")
        g.setFixedWidth(196)
        bw, self.band_btns = button_grid(BANDS, 4, checkable=True, exclusive=True)
        g.body.addWidget(bw)
        for name, b in self.band_btns.items():
            b.clicked.connect(lambda _=False, n=name: self._on_band(n))
        lay.addWidget(g)

        # RF
        g = Group("RF")
        g.setFixedWidth(104)
        rw, self.rf_btns = button_grid(
            ["ANT1", "ANT2", "ATT", "IPO", "TUNE", "ATU", "SPOT", "T-OFF"],
            2, checkable=True)
        g.body.addWidget(rw)
        # RF/TUNE runs the radio's own internal ATU cycle (CAT AC002) — it
        # keys briefly. Distinct from OPS/TUNE, which is the external remote
        # tuner's carrier.
        self.rf_btns["TUNE"].setToolTip(
            "Internal ATU: run a tuning cycle now (keys the radio briefly)")
        self.rf_btns["TUNE"].clicked.connect(self._on_atu_tune)
        self.rf_btns["ATT"].setToolTip(
            "Cycle attenuator: OFF → 6 dB → 12 dB → 18 dB → OFF")
        self.rf_btns["ATT"].clicked.connect(self._on_att_clicked)
        self.rf_btns["IPO"].setToolTip(
            "Cycle preamp: IPO → AMP1 → AMP2 → IPO")
        self.rf_btns["IPO"].clicked.connect(self._on_preamp_clicked)
        self.rf_btns["ATU"].clicked.connect(
            lambda on: self._safe(self.rig.set_atu, on))
        self.rf_btns["ANT1"].clicked.connect(
            lambda _=False: self._safe(self.rig.set_antenna, 1))
        self.rf_btns["ANT2"].clicked.connect(
            lambda _=False: self._safe(self.rig.set_antenna, 2))
        lay.addWidget(g)

        # VFO A / B
        g = Group("VFO A / B")
        g.setFixedWidth(340)
        self.freq_a = FreqDisplay(size=34, primary=True)
        self.freq_a.setFixedHeight(44)
        self.freq_a.tuned.connect(lambda hz: self._safe(self.rig.set_freq, hz))
        g.body.addWidget(self.freq_a)
        g.body.addWidget(TuneStrip())

        sub = QHBoxLayout()
        sub.setSpacing(5)
        self.freq_b = FreqDisplay(size=16, primary=False)
        self.freq_b.setFixedHeight(22)
        self.freq_b.tuned.connect(lambda hz: self._safe(self.rig.set_freq_b, hz))
        sub.addWidget(self.freq_b, 1)
        vw, self.vfo_btns = button_grid(["A/B", "A>B", "FINE", "SPLIT"], 2,
                                        checkable=True)
        vw.setFixedWidth(96)
        sub.addWidget(vw)
        g.body.addLayout(sub)
        self.vfo_btns["SPLIT"].clicked.connect(
            lambda on: self._safe(self.rig.set_split, on))
        # A/B and A>B are actions, not states — a checkable button latches
        # lit after the click and reads as "swapped mode is on", which is
        # exactly how these three sat here unwired and looked like they worked.
        for name in ("A/B", "A>B"):
            self.vfo_btns[name].setCheckable(False)
        self.vfo_btns["A/B"].clicked.connect(
            lambda: self._safe(self.rig.swap_vfo))
        self.vfo_btns["A>B"].clicked.connect(
            lambda: self._safe(self.rig.vfo_a_to_b))
        # FINE is a real radio state (CAT FN) — the MAIN dial's step, x10 finer.
        self.vfo_btns["FINE"].clicked.connect(
            lambda on: self._safe(self.rig.set_fine, on))
        lay.addWidget(g)

        # METERS
        g = Group("METERS")
        self.smeter = BarMeter(
            meter_cal.SMETER,
            label_fn=meter_cal.smeter_tick_label,
            reading_fn=meter_cal.smeter_reading,
        )
        g.body.addWidget(self.smeter, 1)

        row = QHBoxLayout()
        row.setSpacing(5)
        self.meter_slots = []
        for i, default in enumerate(("SWR", "PO")):
            key = self.settings.get(f"panel_meter_{i}", default)
            m = SwappableMeter(key)
            m.setFixedHeight(44)
            m.sourceChanged.connect(
                lambda k, idx=i: self._on_meter_source(idx, k))
            row.addWidget(m, 1)
            self.meter_slots.append(m)
        g.body.addLayout(row)
        lay.addWidget(g, 1)
        return w

    # -- row B ------------------------------------------------------------

    def _build_row_b(self):
        w = QWidget()
        w.setFixedHeight(T.ROW_B_H)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(T.GAP)

        # MODE
        g = Group("MODE")
        g.setFixedWidth(196)
        mw, self.mode_btns = button_grid(PANEL_MODES, 3, checkable=True,
                                         exclusive=True)
        g.body.addWidget(mw)
        for name, b in self.mode_btns.items():
            b.clicked.connect(lambda _=False, n=name: self._safe(self.rig.set_mode, n))
        lay.addWidget(g)

        # CW
        g = Group("CW")
        g.setFixedWidth(104)
        inner = QHBoxLayout()
        inner.setSpacing(5)
        cw_w, self.cw_btns = button_grid(["ZIN", "KEYER", "BK-IN", "MSG"], 1,
                                         checkable=True)
        inner.addWidget(cw_w, 1)
        self.cw_speed = VSlider("", 4, 60, 20)
        self.cw_speed.setFixedWidth(26)
        inner.addWidget(self.cw_speed)
        g.body.addLayout(inner)
        self.cw_btns["BK-IN"].clicked.connect(
            lambda on: self._safe(self.rig.set_cw_breakin, on))
        self.cw_speed.valueChanged.connect(
            lambda v: self._safe(self.rig.set_cw_speed, v))
        lay.addWidget(g)

        # FILTER
        g = Group("FILTER")
        g.setFixedWidth(200)
        inner = QHBoxLayout()
        inner.setSpacing(5)
        sl = QHBoxLayout()
        sl.setSpacing(0)
        self.f_shift = VSlider("SHIFT", -1200, 1200, 0)
        self.f_width = VSlider("WIDTH", 0, 18, 13)
        self.f_notch = VSlider("NOTCH", 10, 3200, 1500, toggleable=True)
        self.f_cont = VSlider("CFRQ", 10, 3200, 1000)
        for s in (self.f_shift, self.f_width, self.f_notch, self.f_cont):
            sl.addWidget(s, 1)
        inner.addLayout(sl, 1)
        rw, self.roof_btns = button_grid(["3K", "12K", "500", "300"], 1,
                                         checkable=True, exclusive=True)
        rw.setFixedWidth(46)
        inner.addWidget(rw)
        g.body.addLayout(inner)
        self.f_shift.valueChanged.connect(
            lambda v: self._safe(self.rig.set_if_shift, v))
        self.f_width.valueChanged.connect(
            lambda v: self._safe(self.rig.set_sh, v))
        self.f_notch.valueChanged.connect(
            lambda v: self._safe(self.rig.set_notch_freq, v))
        self.f_notch.toggled.connect(
            lambda on: self._safe(self.rig.set_notch, on))
        self.f_cont.valueChanged.connect(
            lambda v: self._safe(self.rig.set_contour_freq, v))
        for name, b in self.roof_btns.items():
            b.clicked.connect(
                lambda _=False, n=name: self._safe(self.rig.set_roofing, n))
        lay.addWidget(g)

        # MEMORY
        g = Group("MEMORY")
        g.setFixedWidth(150)
        mw2, self.mem_btns = button_grid(["V/M", "UP", "DN", "STO", "RCL", "SCAN"], 3)
        g.body.addWidget(mw2)
        qw, self.quick_btns = button_grid([str(i) for i in range(1, 6)], 5,
                                          checkable=True, exclusive=True)
        g.body.addWidget(qw)
        self.mem_label = QLabel("—")
        self.mem_label.setObjectName("dim")
        g.body.addWidget(self.mem_label)
        self.mem_btns["RCL"].clicked.connect(lambda: self.show_memories())
        self.mem_btns["STO"].clicked.connect(self._store_current)
        for i, b in enumerate(self.quick_btns.values()):
            b.clicked.connect(lambda _=False, idx=i: self._recall_index(idx))
        lay.addWidget(g)

        # CLARIFIER / AUX
        g = Group("CLARIFIER / AUX")
        top = QHBoxLayout()
        top.setSpacing(6)
        self.clar_rx = button("RX", checkable=True, width=32)
        self.clar_val = QLabel("+0.00")
        self.clar_val.setFixedHeight(20)
        self.clar_val.setAlignment(Qt.AlignCenter)
        self.clar_val.setStyleSheet(
            f"background:{T.METER_BG};border:1px solid {T.RULE};"
            f"color:{T.PHOS};font-size:14px;")
        self.clar_tx = button("TX", checkable=True, width=32)
        self.clar_clr = button("CLR", width=36)
        top.addWidget(self.clar_rx)
        top.addWidget(self.clar_val, 1)
        top.addWidget(self.clar_tx)
        top.addWidget(self.clar_clr)
        g.body.addLayout(top)

        self.clar_slider = HSlider("CLAR", -9990, 9990, 0, name_w=44, val_w=52)
        self.vc_slider = HSlider("VC TUNE", 0, 100, 50, name_w=44, val_w=52)
        g.body.addWidget(self.clar_slider)
        g.body.addWidget(self.vc_slider)

        chips = QHBoxLayout()
        chips.setSpacing(3)
        self.aux_btns = {}
        for name in ("TONE", "MONI", "PROC", "VOX", "TXW"):
            b = button(name, checkable=True, height=18)
            chips.addWidget(b)
            self.aux_btns[name] = b
        chips.addStretch(1)
        g.body.addLayout(chips)

        self.clar_rx.clicked.connect(lambda on: self._safe(self.rig.set_rit, on))
        self.clar_tx.clicked.connect(lambda on: self._safe(self.rig.set_xit, on))
        self.clar_slider.valueChanged.connect(self._on_clar_offset)
        self.clar_clr.clicked.connect(lambda: self.clar_slider.slider.setValue(0))
        self.aux_btns["MONI"].clicked.connect(
            lambda on: self._safe(self.rig.set_monitor, on))
        self.aux_btns["PROC"].clicked.connect(
            lambda on: self._safe(self.rig.set_compressor, on))
        self.aux_btns["VOX"].clicked.connect(
            lambda on: self._safe(self.rig.set_vox, on))
        lay.addWidget(g, 1)
        return w

    # -- row C ------------------------------------------------------------

    def _build_row_c(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(T.GAP)

        # RECEIVE
        g = Group("RECEIVE")
        g.setFixedWidth(196)
        sl = QHBoxLayout()
        sl.setSpacing(0)
        self.af = VSlider("AF", 0, 255, 60)
        self.rf = VSlider("RF", 0, 255, 255)
        for s in (self.af, self.rf):
            sl.addWidget(s, 1)
        g.body.addLayout(sl, 1)
        agc = QHBoxLayout()
        agc.setSpacing(3)
        self.agc_btns = {}
        for name in ("OFF", "AUTO", "FAST", "MID", "SLOW"):
            b = button(name, checkable=True, height=18)
            b.setAutoExclusive(True)
            agc.addWidget(b)
            self.agc_btns[name] = b
        g.body.addLayout(agc)
        self.af.valueChanged.connect(lambda v: self._safe(self.rig.set_af_gain, v))
        self.rf.valueChanged.connect(lambda v: self._safe(self.rig.set_rf_gain, v))
        for name, b in self.agc_btns.items():
            b.clicked.connect(lambda _=False, n=name: self._safe(self.rig.set_agc, n))
        lay.addWidget(g)

        # TRANSMIT
        g = Group("TRANSMIT")
        g.setFixedWidth(309)
        sl = QHBoxLayout()
        sl.setSpacing(0)
        self.tx_pwr = VSlider("RF PWR", 5, 100, 100)
        self.tx_mic = VSlider("MIC", 0, 100, 35)
        self.tx_proc = VSlider("PROC", 0, 100, 20)
        self.tx_vox = VSlider("VOX G", 0, 100, 40)
        self.tx_vdly = VSlider("V DLY", 0, 3000, 500)
        for s in (self.tx_pwr, self.tx_mic, self.tx_proc, self.tx_vox, self.tx_vdly):
            sl.addWidget(s, 1)
        g.body.addLayout(sl, 1)
        chips = QHBoxLayout()
        chips.setSpacing(3)
        self.tx_btns = {}
        for name in ("MOX", "M-EQ", "TUNE", "CAL"):
            b = button(name, checkable=True, height=18)
            chips.addWidget(b)
            self.tx_btns[name] = b
        g.body.addLayout(chips)
        self.tx_pwr.valueChanged.connect(lambda v: self._safe(self.rig.set_power, v))
        self.tx_mic.valueChanged.connect(lambda v: self._safe(self.rig.set_mic_gain, v))
        self.tx_proc.valueChanged.connect(lambda v: self._safe(self.rig.set_comp_level, v))
        self.tx_vox.valueChanged.connect(lambda v: self._safe(self.rig.set_vox_gain, v))
        self.tx_vdly.valueChanged.connect(lambda v: self._safe(self.rig.set_vox_delay, v))
        self.tx_btns["MOX"].clicked.connect(lambda on: self._safe(self.rig.set_ptt, on))
        lay.addWidget(g)

        # DSP — the only region with slack; a future panel grows into here.
        g = Group("DSP")
        self.nb = HSlider("NB LVL", 0, 10, 5, name_w=52)
        self.dnr = HSlider("DNR", 1, 15, 6, name_w=52, toggleable=True)
        self.dnf = HSlider("DNF", 0, 1, 0, name_w=52)
        self.notch_w = HSlider("NOTCH W", 0, 1, 1, name_w=52)
        self.cont_lvl = HSlider("CONT", -40, 20, 0, name_w=52, toggleable=True)
        self.cont_wid = HSlider("C WID", 1, 11, 6, name_w=52)
        self.apf = HSlider("APF", -250, 250, 0, name_w=52)
        for s in (self.nb, self.dnr, self.dnf, self.notch_w,
                  self.cont_lvl, self.cont_wid, self.apf):
            g.body.addWidget(s)
        g.body.addStretch(1)
        self.nb.valueChanged.connect(lambda v: self._safe(self.rig.set_nb_level, v))
        self.dnr.valueChanged.connect(lambda v: self._safe(self.rig.set_nr_level, v))
        self.dnr.toggled.connect(lambda on: self._safe(self.rig.set_nr, on))
        self.dnf.valueChanged.connect(lambda v: self._safe(self.rig.set_dnf, v >= 1))
        self.notch_w.valueChanged.connect(
            lambda v: self._safe(self.rig.set_notch_width, v >= 1))
        self.cont_lvl.toggled.connect(lambda on: self._safe(self.rig.set_contour, on))
        self.cont_lvl.valueChanged.connect(
            lambda v: self._safe(self.rig.set_contour_level, v))
        self.cont_wid.valueChanged.connect(
            lambda v: self._safe(self.rig.set_contour_width, v))
        self.apf.valueChanged.connect(lambda v: self._safe(self.rig.set_apf_freq, v))
        lay.addWidget(g, 1)

        # OPS — deliberately unresolved. Four slots on the right-hand edge,
        # reachable without crossing the panel. Let operating decide what
        # earns the space.
        g = Group("OPS")
        g.setFixedWidth(96)
        ow, self.ops_btns = button_grid(
            ["TUNE", "WSPR", "SWAP", "LOCK", "MENU"], 1, checkable=True)
        g.body.addWidget(ow)
        g.body.addStretch(1)
        self.ops_btns["LOCK"].clicked.connect(
            lambda on: self._safe(self.rig.set_lock, on))
        # TUNE = the external remote tuner's carrier (MFJ-994BRT), NOT the
        # radio's internal ATU — that one is the RF group's ATU/TUNE pair.
        # Reachable at the right-hand edge without crossing the panel, since
        # it keys the transmitter and may need stopping in a hurry.
        tune_btn = self.ops_btns["TUNE"]
        tune_btn.setToolTip(
            "Remote tuner: keys a continuous {mode} carrier at {w} W for an "
            "external autotuner, then restores your mode and power. "
            "Auto-stops after {t} s.".format(
                mode=self.settings.get("tune_mode", "AM"),
                w=self.settings.get("tune_watts", 20),
                t=self.settings.get("tune_timeout_sec", 90)))
        tune_btn.clicked.connect(self._on_tune_clicked)
        # The tuner can also be started, stopped or safety-timed-out from the
        # web UI, so the button follows the tuner rather than its own state.
        self._tune_poll = QTimer(self)
        self._tune_poll.timeout.connect(self._sync_tune_button)
        self._tune_poll.start(300)

        # WSPR = arm the unattended beacon: JTDX's band hops tune the MFJ by
        # themselves, and the watchdog supervises what gets keyed. Next to
        # TUNE because it is the same antenna-matching job, one step further
        # from the operator's hand.
        wspr_btn = self.ops_btns["WSPR"]
        wspr_btn.setToolTip(
            "WSPR mode: a JTDX band hop QSYs ~30 kHz off the WSPR window, runs "
            "the remote tuner, checks the SWR actually came down and returns to "
            "the dial. A beacon key arriving mid-tune is refused — one slot "
            "skipped beats transmitting into an unmatched antenna. Stuck TX or "
            "runaway SWR drops PTT over CAT.\n\n"
            "Held on a band? Toggle off and on to re-tune it.")
        wspr_btn.clicked.connect(self._on_wspr_clicked)
        self._wspr_poll = QTimer(self)
        self._wspr_poll.timeout.connect(self._sync_wspr_button)
        self._wspr_poll.start(500)
        self._sync_wspr_button()

        lay.addWidget(g)
        return w

    # -- footer -----------------------------------------------------------

    def _build_footer(self):
        w = QWidget()
        w.setFixedHeight(T.FOOT_H + T.PAD)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(T.PAD, 0, T.PAD, T.PAD)
        lay.setSpacing(T.GAP)

        status = QFrame()
        status.setObjectName("group")
        status.setFixedHeight(T.FOOT_H)
        sl = QHBoxLayout(status)
        sl.setContentsMargins(8, 0, 8, 0)
        sl.setSpacing(9)
        self.st_link = QLabel("CAT OFFLINE")
        self.st_link.setStyleSheet(f"color:{T.DIMMER};font-size:9px;")
        self.st_extra = QLabel("")
        self.st_extra.setObjectName("dim")
        sl.addWidget(self.st_link)
        sl.addWidget(self.st_extra, 1)
        lay.addWidget(status, 1)

        self.macros_btn = button("MACROS", width=90, height=T.FOOT_H)
        self.mems_btn = button("MEMS", width=90, height=T.FOOT_H)
        self.eq_btn = button("EQ", width=96, height=T.FOOT_H)
        self.meters_btn = button("METERS", width=90, height=T.FOOT_H)
        self.meters_btn.setToolTip(
            "PO / ALC / SWR and the remote tuner in a small always-on-top "
            "window, for when the panel itself will not fit the screen layout.")
        self.large_btn = button("LARGE", checkable=True, width=90, height=T.FOOT_H)
        self.large_btn.setToolTip(
            "Bigger panel for VNC (1.33×). Restarts the app — CAT blips.")
        self.large_btn.setChecked(bool(self.settings.get("panel_large_ui", True)))
        for b in (self.macros_btn, self.mems_btn, self.eq_btn,
                  self.meters_btn, self.large_btn):
            b.setObjectName("popout")
            lay.addWidget(b)

        self.ptt = button("PTT", checkable=True, width=200, height=T.FOOT_H)
        self.ptt.setObjectName("ptt")
        lay.addWidget(self.ptt)

        self.macros_btn.clicked.connect(self.show_macros)
        self.mems_btn.clicked.connect(self.show_memories)
        self.eq_btn.clicked.connect(self._show_eq)
        self.meters_btn.clicked.connect(self.show_meters)
        self.large_btn.clicked.connect(self._on_large_toggled)
        self.ptt.clicked.connect(lambda on: self._safe(self.rig.set_ptt, on))
        return w

    # ── TUNE ─────────────────────────────────────────────────────────────

    def _on_tune_clicked(self, on):
        if self.tuner is None:
            self.ops_btns["TUNE"].setChecked(False)
            self.st_extra.setText("no tuner")
            return
        result = self.tuner.start() if on else self.tuner.stop()
        if not result.get("ok"):
            log.warning("remote tune: %s", result.get("error"))
            self.st_extra.setText(str(result.get("error", "tune failed")))
        self._sync_tune_button()

    def _sync_tune_button(self):
        active = bool(self.tuner and self.tuner.active)
        btn = self.ops_btns["TUNE"]
        if btn.isChecked() != active:
            btn.setChecked(active)
        text = "● TUNING" if active else "TUNE"
        if btn.text() != text:
            btn.setText(text)

    # ── WSPR mode ────────────────────────────────────────────────────────

    def _on_wspr_clicked(self, on):
        if self.wspr is None:
            self.ops_btns["WSPR"].setChecked(False)
            self.st_extra.setText("WSPR needs the web server (rigctld)")
            return
        st = self.wspr.arm(True) if on else self.wspr.disarm("panel")
        hold = st.get("hold") or {}
        if hold.get("detail"):
            self.st_extra.setText(str(hold["detail"]))
        self._sync_wspr_button()

    def _sync_wspr_button(self):
        """Follow the WsprAutoTune object, never a local flag — it arms, holds
        and hard-stops on its own, and from the web UI as well."""
        btn = self.ops_btns["WSPR"]
        if self.wspr is None:
            btn.setEnabled(False)
            return
        st = self.wspr.status()
        phase, armed = st.get("phase"), bool(st.get("armed"))
        if btn.isChecked() != armed:
            btn.setChecked(armed)
        text = {"tuning": "◐ TUNING", "hold": "! HELD", "stopped": "!! STOP"}.get(
            phase, "● WSPR" if armed else "WSPR")
        if btn.text() != text:
            btn.setText(text)
        # A hold or a hard stop is the whole reason to look at the panel, so it
        # says why in the status line rather than only in the tooltip.
        if armed and phase in ("hold", "stopped"):
            detail = (st.get("hold") or {}).get("detail") or ""
            if detail and self.st_extra.text() != detail:
                self.st_extra.setText(detail)

    @Slot(object, object)
    def _on_atu(self, atu, tuning):
        self.rf_btns["ATU"].setChecked(bool(atu))
        # The radio owns the cycle and clears atu_tuning when it finishes, so
        # the button un-latches on its own rather than staying stuck on.
        btn = self.rf_btns["TUNE"]
        btn.setChecked(bool(tuning))
        btn.setText("● TUNING" if tuning else "TUNE")

    def _on_atu_tune(self, _on=False):
        # Momentary in effect: the radio owns the cycle, and atu_changed
        # drives the button's checked state from the rig's own atu_tuning.
        self._safe(self.rig.atu_tune)

    # ── rig plumbing ─────────────────────────────────────────────────────

    def _safe(self, fn, *args):
        """Call a CAT setter without letting a serial fault reach the UI."""
        t0 = time.monotonic()
        try:
            fn(*args)
        except Exception:
            log.exception("CAT command failed: %s%r", getattr(fn, "__name__", fn), args)
            return
        self._last_cmd_ms = int((time.monotonic() - t0) * 1000)

    def _connect_signals(self):
        r = self.rig
        r.connected_changed.connect(self._on_connected)
        r.freq_changed.connect(self._on_freq_a)
        r.freq_b_changed.connect(self.freq_b.set_freq)
        r.mode_changed.connect(self._on_mode)
        r.ptt_changed.connect(self._on_ptt)
        r.smeter_update.connect(self.smeter.set_raw)
        r.meters_update.connect(self._on_meters)
        r.power_changed.connect(
            lambda v: (self.tx_pwr.set_value_silent(v), self.hdr_pwr.setText(f"{int(v)}W")))
        r.af_changed.connect(self.af.set_value_silent)
        r.rf_changed.connect(self.rf.set_value_silent)
        r.mic_changed.connect(self.tx_mic.set_value_silent)
        r.cw_changed.connect(self._on_cw)
        r.split_changed.connect(
            lambda on: self.vfo_btns["SPLIT"].setChecked(bool(on)))
        r.fine_changed.connect(
            lambda on: self.vfo_btns["FINE"].setChecked(bool(on)))
        r.lock_changed.connect(
            lambda on: self.ops_btns["LOCK"].setChecked(bool(on)))
        r.atu_changed.connect(self._on_atu)
        r.rit_changed.connect(self._on_rit)
        r.xit_changed.connect(lambda on: self.clar_tx.setChecked(bool(on)))
        r.agc_changed.connect(self._on_agc)
        r.filter_changed.connect(self._on_filter)
        r.notch_changed.connect(self._on_notch)
        r.contour_changed.connect(self._on_contour)
        r.roofing_changed.connect(self._on_roofing)
        r.apf_changed.connect(self._on_apf)
        r.dnf_changed.connect(self._on_dnf)
        r.nb_changed.connect(self._on_nb)
        r.preamp_changed.connect(self._on_preamp)
        r.att_changed.connect(self._on_att)
        r.antenna_changed.connect(self._on_antenna)
        r.comp_changed.connect(
            lambda on, lvl: (self.aux_btns["PROC"].setChecked(bool(on)),
                             self.tx_proc.set_value_silent(lvl or 0)))
        r.monitor_changed.connect(
            lambda on, lvl: self.aux_btns["MONI"].setChecked(bool(on)))
        r.vox_changed.connect(self._on_vox)
        r.nr_changed.connect(self._on_nr)

    def _connect_rig(self):
        port = self.settings.get("cat_port")
        baud = self.settings.get("cat_baud", 38400)
        self.hdr_port.setText(f"{port} · {baud}")
        if not self.owns_rig:
            # The web server opened the port and owns the poll loop; just
            # register our meter interest and let its events arrive.
            #
            # Seed the link indicator from the rig itself: connected_changed
            # fired while the server was starting, before this window existed,
            # so waiting for the next one left the footer reading CAT OFFLINE
            # for the whole session while the panel was plainly working.
            self._on_connected(bool(getattr(self.rig, "is_connected", False)))
            self._sync_from_state()
            self._push_meter_sources()
            return
        try:
            ok = self.rig.cat_connect(port, baud)
        except Exception:
            log.exception("CAT connect failed on %s", port)
            ok = False
        if ok:
            self._sync_from_state()
            self._push_meter_sources()

    def _push_meter_sources(self):
        """Tell the poll loop which RM types the two slots need.

        DEFAULT_METER_SOURCES (PO/ALC/SWR) stays polled regardless — the web
        UI status payload and the ATU tune peak-hold both read those — so this
        widens the set rather than replacing it.
        """
        wanted = {m.spec.rm for m in self.meter_slots}
        try:
            self.rig.request_meters(METER_OWNER, wanted)
        except Exception:
            log.exception("could not register meter sources %r", wanted)

    # ── slots ────────────────────────────────────────────────────────────

    @Slot(object)
    def _on_connected(self, ok):
        if ok:
            self.hdr_link.setStyleSheet(f"color:{T.PHOS};font-size:10px;")
            self.st_link.setText("CAT LINKED")
            self.st_link.setStyleSheet(f"color:{T.PHOS};font-size:9px;")
            self._sync_from_state()
        else:
            self.hdr_link.setStyleSheet(f"color:{T.DIMMER};font-size:10px;")
            self.st_link.setText("CAT OFFLINE")
            self.st_link.setStyleSheet(f"color:{T.RED};font-size:9px;")

    def _sync_from_state(self):
        """Seed widgets the poll loop already filled before this window existed."""
        st = getattr(self.rig, "state", None)
        if st is None:
            return
        self._on_freq_a(getattr(st, "freq_a", 0) or 0)
        self.freq_b.set_freq(getattr(st, "freq_b", 0) or 0)
        self._on_mode(getattr(st, "mode", None))
        self._on_agc(getattr(st, "agc", None))
        self._on_filter(None, getattr(st, "if_shift", None))
        self._on_notch(getattr(st, "notch", False), getattr(st, "notch_pos", None))
        self._on_contour(getattr(st, "contour", False), getattr(st, "contour_freq", None))
        self._on_roofing(getattr(st, "roofing", None))
        self._on_apf(getattr(st, "apf", False), getattr(st, "apf_freq", None))
        self._on_dnf(getattr(st, "dnf", False))
        self._on_nb(getattr(st, "nb", False), getattr(st, "nb_level", None))
        self._on_nr(getattr(st, "nr", False), getattr(st, "nr_level", None))
        self._on_preamp(getattr(st, "preamp", None))
        self._on_att(getattr(st, "att", None))
        self._on_antenna(getattr(st, "antenna", None))
        self.af.set_value_silent(getattr(st, "af_gain", 0) or 0)
        self.rf.set_value_silent(getattr(st, "rf_gain", 0) or 0)
        self.tx_mic.set_value_silent(getattr(st, "mic_gain", 0) or 0)
        self.tx_pwr.set_value_silent(getattr(st, "power", 0) or 0)
        self.vfo_btns["SPLIT"].setChecked(bool(getattr(st, "split", False)))
        self.vfo_btns["FINE"].setChecked(bool(getattr(st, "fine", False)))
        self.aux_btns["PROC"].setChecked(bool(getattr(st, "compressor", False)))
        self.aux_btns["VOX"].setChecked(bool(getattr(st, "vox", False)))
        self.aux_btns["MONI"].setChecked(bool(getattr(st, "monitor", False)))
        self.tx_proc.set_value_silent(getattr(st, "comp_level", 0) or 0)
        self.tx_vox.set_value_silent(getattr(st, "vox_gain", 0) or 0)

    @Slot(object, object)
    def _on_nr(self, on, level):
        self.dnr.set_on_silent(bool(on))
        if level is not None:
            self.dnr.set_value_silent(int(level))

    @Slot(object)
    def _on_freq_a(self, hz):
        self.freq_a.set_freq(hz)
        try:
            hz = int(hz or 0)
        except (TypeError, ValueError):
            return
        band = None
        for name, (lo, hi) in BAND_EDGES.items():
            if lo <= hz <= hi:
                band = name
                break
        for name, b in self.band_btns.items():
            b.setChecked(name == band)

    @Slot(object, object)
    def _on_filter(self, bw, if_shift):
        st = getattr(self.rig, "state", None)
        if st is not None and getattr(st, "sh", None) is not None:
            self.f_width.set_value_silent(int(st.sh))
        if if_shift is not None:
            self.f_shift.set_value_silent(int(if_shift))

    @Slot(object, object)
    def _on_notch(self, on, pos):
        self.f_notch.set_on_silent(bool(on))
        if pos is not None:
            self.f_notch.set_value_silent(int(pos))
        st = getattr(self.rig, "state", None)
        if st is not None and getattr(st, "notch_width", None):
            self.notch_w.set_value_silent(1 if st.notch_width == "WIDE" else 0)

    @Slot(object, object)
    def _on_contour(self, on, hz):
        self.cont_lvl.set_on_silent(bool(on))
        if hz is not None:
            self.f_cont.set_value_silent(int(hz))
        st = getattr(self.rig, "state", None)
        if st is not None and getattr(st, "contour_level", None) is not None:
            self.cont_lvl.set_value_silent(int(st.contour_level))
        if st is not None and getattr(st, "contour_width", None) is not None:
            self.cont_wid.set_value_silent(int(st.contour_width))

    @Slot(object)
    def _on_roofing(self, name):
        if not name:
            return
        key = str(name).upper()
        for n, b in self.roof_btns.items():
            b.setChecked(n == key)

    @Slot(object, object)
    def _on_apf(self, on, hz):
        if hz is not None:
            self.apf.set_value_silent(int(hz))

    @Slot(object)
    def _on_dnf(self, on):
        self.dnf.set_value_silent(1 if on else 0)

    @Slot(object, object)
    def _on_nb(self, on, level):
        if level is not None:
            self.nb.set_value_silent(int(level))

    def _on_att_clicked(self, _on=False):
        # Ignore Qt's checkable flip — ATT is a cycle, not a binary latch.
        cur = getattr(self.rig.state, "att", "OFF") or "OFF"
        nxt = next_att(cur)
        self._safe(self.rig.set_att, nxt)
        self._on_att(getattr(self.rig.state, "att", cur) or cur)

    def _on_preamp_clicked(self, _on=False):
        # Ignore Qt's checkable flip — IPO/AMP is a three-step cycle.
        cur = getattr(self.rig.state, "preamp", "IPO") or "IPO"
        nxt = next_preamp(cur)
        self._safe(self.rig.set_preamp, nxt)
        self._on_preamp(getattr(self.rig.state, "preamp", cur) or cur)

    @Slot(object)
    def _on_preamp(self, mode):
        label = mode if mode in ("IPO", "AMP1", "AMP2") else "IPO"
        btn = self.rf_btns["IPO"]
        if btn.text() != label:
            btn.setText(label)
        # Lit when a preamp is engaged (AMP1/AMP2); IPO is the quiet default.
        btn.setChecked(label in ("AMP1", "AMP2"))

    @Slot(object)
    def _on_att(self, mode):
        # Keep the cell labeled ATT when off so the RF grid still reads
        # cleanly; show the level once attenuation is actually on.
        label = "ATT" if (not mode or mode == "OFF") else str(mode)
        btn = self.rf_btns["ATT"]
        if btn.text() != label:
            btn.setText(label)
        btn.setChecked(bool(mode) and mode != "OFF")

    @Slot(object)
    def _on_antenna(self, ant):
        try:
            a = int(ant)
        except (TypeError, ValueError):
            return
        self.rf_btns["ANT1"].setChecked(a == 1)
        self.rf_btns["ANT2"].setChecked(a == 2)

    @Slot(object, object, object)
    def _on_vox(self, on, gain, delay):
        self.aux_btns["VOX"].setChecked(bool(on))
        if gain is not None:
            self.tx_vox.set_value_silent(int(gain))
        if delay is not None:
            self.tx_vdly.set_value_silent(int(delay))

    @Slot(object)
    def _on_mode(self, mode):
        for name, b in self.mode_btns.items():
            b.setChecked(name == mode)

    @Slot(object)
    def _on_ptt(self, tx):
        self.ptt.setChecked(bool(tx))
        self.tx_btns["MOX"].setChecked(bool(tx))

    @Slot(object)
    def _on_meters(self, meters):
        for slot in self.meter_slots:
            slot.feed(meters)
        if self._last_cmd_ms:
            self.st_extra.setText(f"last cmd {self._last_cmd_ms} ms")

    @Slot(object, object, object, object)
    def _on_cw(self, speed, pitch, breakin, delay):
        if speed is not None:
            self.cw_speed.set_value_silent(speed)
            self.hdr_wpm.setText(str(int(speed)))
        if breakin is not None:
            self.cw_btns["BK-IN"].setChecked(bool(breakin))

    @Slot(object)
    def _on_agc(self, mode):
        """Light the button the radio is actually on.

        The FTDX10 never answers plain "AUTO": GT's Set parameter takes 4 for
        AUTO, but its Answer parameter reports 4/5/6 as AUTO-FAST/AUTO-MID/
        AUTO-SLOW (two different code tables, Yaesu CAT reference). Matching
        the answer against the button captions therefore matched nothing
        whenever AGC was on auto, and every AGC button went dark — the panel
        showed no AGC state at all. The web UI has always handled this; this
        did not. Found 2026-08-26 with the radio sitting on AUTO-MID.
        """
        m = str(mode or "")
        want = "AUTO" if m.startswith("AUTO") else m
        for name, b in self.agc_btns.items():
            b.setChecked(name == want)

    def _on_clar_offset(self, hz):
        self.clar_val.setText(f"{hz / 1000:+.2f}")
        self._safe(self.rig.set_rit_offset, int(hz))

    @Slot(object, object)
    def _on_rit(self, on, offset):
        self.clar_rx.setChecked(bool(on))
        if offset is not None:
            self.clar_slider.set_value_silent(int(offset))
            self.clar_val.setText(f"{int(offset) / 1000:+.2f}")

    def _on_band(self, band):
        if band == "GEN":
            return
        self._safe(self.rig.go_band, band)

    def _on_meter_source(self, idx, key):
        # Settings writes through __setattr__ and only persists on save().
        setattr(self.settings, f"panel_meter_{idx}", key)
        try:
            self.settings.save()
        except Exception:
            log.exception("could not persist meter slot %d", idx)
        self._push_meter_sources()

    def _tick_clock(self):
        self.hdr_utc.setText(time.strftime("%H:%MZ", time.gmtime()))

    # ── popouts ──────────────────────────────────────────────────────────

    def show_memories(self):
        if not hasattr(self, "_mem_pop"):
            self._mem_pop = MemoriesPopout(self)
            self._mem_pop.recall_requested.connect(self._recall_channel)
            self._mem_pop.store_requested.connect(self._store_into)
        self._mem_pop.show()
        self._mem_pop.raise_()

    def show_macros(self):
        if not hasattr(self, "_mac_pop"):
            self._mac_pop = MacrosPopout(self)
            self._mac_pop.macro_requested.connect(self._run_macro)
            self._mac_pop.stop_requested.connect(lambda: self.runner.stop())
            self._mac_pop.edit_requested.connect(self._edit_macros)
        self._mac_pop.show()
        self._mac_pop.raise_()

    def show_meters(self):
        if not hasattr(self, "_meters_pop"):
            self._meters_pop = MetersPopout(self.rig, self.tuner, self)
        self._meters_pop.show()
        self._meters_pop.raise_()

    def _show_eq(self):
        if not hasattr(self, "_eq_pop"):
            self._eq_pop = EqPopout(self.rig, self)
        self._eq_pop.show()
        self._eq_pop.raise_()
        self._eq_pop.read_all()

    def _on_large_toggled(self, on):
        """Qt can only apply QT_SCALE_FACTOR at process start, so save and restart."""
        on = bool(on)
        if on == bool(self.settings.get("panel_large_ui", True)):
            return
        self.large_btn.setEnabled(False)
        self.settings.panel_large_ui = on
        self.settings.save()
        log.info("UI LARGE=%s — restarting for scale %s", on, T.qt_scale_factor(on))
        if os.environ.get("INVOCATION_ID"):
            subprocess.Popen(
                ["systemctl", "--user", "restart", "cyberrig-panel.service"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        try:
            self.rig.cat_disconnect()
        except Exception:
            log.exception("scale restart: CAT close")
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def _recall_channel(self, channel: MemoryChannel):
        from cyberrig.memories.ops import recall as recall_channel
        try:
            recall_channel(self.rig, channel)
        except Exception:
            log.exception("recall failed for %r", channel)
            return
        self.mem_label.setText(channel.name)

    def _recall_index(self, idx):
        from cyberrig.memories.store import load_memories
        mems = load_memories()
        if idx < len(mems):
            self._recall_channel(mems[idx])

    def _store_current(self):
        self.show_memories()

    def _store_into(self, row):
        from cyberrig.memories.ops import snapshot
        try:
            ch = snapshot(self.rig.state, f"CH{row + 1:03d}")
        except Exception:
            log.exception("snapshot failed")
            return
        self._mem_pop.take_snapshot(row, ch)

    def _run_macro(self, macro):
        try:
            self.runner.run(macro)
        except Exception:
            log.exception("macro failed: %s", getattr(macro, "name", macro))

    def _edit_macros(self):
        from cyberrig.ui_legacy.dialogs.macro_editor import MacroEditorDialog
        dlg = MacroEditorDialog(self)
        dlg.setStyleSheet(T.STYLE)
        dlg.exec()
        self._mac_pop.reload()

    # ── shutdown ─────────────────────────────────────────────────────────

    def closeEvent(self, ev):
        try:
            self.rig.release_meters(METER_OWNER)
        except Exception:
            pass
        if self.owns_rig:
            try:
                self.rig.cat_disconnect()
            except Exception:
                log.exception("CAT disconnect failed on close")
        super().closeEvent(ev)
