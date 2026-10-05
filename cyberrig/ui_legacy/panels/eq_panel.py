"""Parametric EQ panel + full radio menu access via EX command.

EQ structure per CAT manual Table 2:
  TX DSP EQ  (PRMTRC EQ):  EX 03 03 02-10
  MIC P-EQ   (P PRMTRC EQ): EX 03 03 11-19
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget,
    QLabel, QSlider, QPushButton, QGroupBox, QComboBox,
    QSpinBox, QTreeWidget, QTreeWidgetItem, QLineEdit, QSplitter,
    QScrollArea,
)
from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QFont
from cyberrig.cat.ftdx10 import FTdx10
from cyberrig.cat.menus import MENU_GROUPS, MENU_BY_KEY, MenuItem


def _lbl(t):
    l = QLabel(t)
    l.setStyleSheet("font-size:10px; color:#00aa55;")
    return l


# TX DSP EQ: bands 1-3 use P3 offsets 02,05,08
# MIC P-EQ:  bands 1-3 use P3 offsets 11,14,17
# Each band: (freq_p3, level_p3, bw_p3)

_EQ_FREQ_OPTIONS = {
    "low": {0:"OFF",1:"100",2:"200",3:"300",4:"400",5:"500",6:"600",7:"700"},
    "mid": {0:"OFF",1:"700",2:"800",3:"900",4:"1000",5:"1100",6:"1200",7:"1300",8:"1400",9:"1500"},
    "high": {0:"OFF",1:"1500",2:"1600",3:"1700",4:"1800",5:"1900",6:"2000",
             7:"2100",8:"2200",9:"2300",10:"2400",11:"2500",12:"2600",
             13:"2700",14:"2800",15:"2900",16:"3000",17:"3100",18:"3200"},
}

_BAND_FREQ_RANGE = ["low", "mid", "high"]


class EQBand(QWidget):
    """One parametric EQ band: Freq / Level / BW selectors."""

    def __init__(self, band_idx: int, p3_base: int, rig: FTdx10, parent=None):
        """band_idx: 0/1/2 → bands 1/2/3; p3_base: starting P3 for this band."""
        super().__init__(parent)
        self._p3_freq  = p3_base
        self._p3_level = p3_base + 1
        self._p3_bw    = p3_base + 2
        self._freq_range = _BAND_FREQ_RANGE[band_idx]
        self.rig = rig
        self._build(band_idx + 1)

    def _build(self, band_num: int):
        g = QGroupBox(f"Band {band_num}")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(g)
        grid = QGridLayout(g)
        grid.setSpacing(4)

        grid.addWidget(_lbl("Freq"), 0, 0)
        self.freq_combo = QComboBox()
        for k, v in _EQ_FREQ_OPTIONS[self._freq_range].items():
            self.freq_combo.addItem(v, k)
        grid.addWidget(self.freq_combo, 0, 1)

        grid.addWidget(_lbl("Level"), 1, 0)
        self.level_slider = QSlider(Qt.Horizontal)
        self.level_slider.setRange(-20, 10)
        self.level_slider.setValue(0)
        self.level_lbl = QLabel("0 dB")
        self.level_lbl.setStyleSheet("color:#00ff88; font-size:10px;")
        grid.addWidget(self.level_slider, 1, 1)
        grid.addWidget(self.level_lbl, 1, 2)

        grid.addWidget(_lbl("BW"), 2, 0)
        self.bw_spin = QSpinBox()
        self.bw_spin.setRange(1, 10)
        self.bw_spin.setValue(5)
        grid.addWidget(self.bw_spin, 2, 1)

        self.freq_combo.currentIndexChanged.connect(self._write)
        self.level_slider.valueChanged.connect(self._on_level)
        self.bw_spin.valueChanged.connect(self._write)

    def _on_level(self, v: int):
        self.level_lbl.setText(f"{v:+d} dB")
        self._write()

    def _write(self):
        if not self.rig.is_connected:
            return
        fc = self.freq_combo.currentData()
        lv = self.level_slider.value()
        bw = self.bw_spin.value()
        # Level stored as offset: range -20..+10, transmitted as 3-digit signed
        # The radio represents level as 3-digit value where the actual dB is decoded
        # by sign + magnitude. We send the raw signed int as 3-char string.
        lv_val = lv + 20  # shift to 000-030 range
        self.rig.ex_write(3, 3, self._p3_freq,  f"{fc:02d}")
        self.rig.ex_write(3, 3, self._p3_level, f"{lv_val:03d}")
        self.rig.ex_write(3, 3, self._p3_bw,    f"{bw:02d}")

    def read_from_rig(self):
        if not self.rig.is_connected:
            return
        fc  = self.rig.ex_read(3, 3, self._p3_freq)
        lv  = self.rig.ex_read(3, 3, self._p3_level)
        bw  = self.rig.ex_read(3, 3, self._p3_bw)
        if fc is not None:
            idx = self.freq_combo.findData(int(fc))
            if idx >= 0:
                self.freq_combo.blockSignals(True)
                self.freq_combo.setCurrentIndex(idx)
                self.freq_combo.blockSignals(False)
        if lv is not None:
            val = int(lv) - 20
            self.level_slider.blockSignals(True)
            self.level_slider.setValue(val)
            self.level_lbl.setText(f"{val:+d} dB")
            self.level_slider.blockSignals(False)
        if bw is not None:
            self.bw_spin.blockSignals(True)
            self.bw_spin.setValue(int(bw))
            self.bw_spin.blockSignals(False)


class EQSection(QWidget):
    """Three-band EQ section."""

    def __init__(self, title: str, p3_bases: list[int], rig: FTdx10, parent=None):
        """p3_bases: list of 3 P3 base values for each band."""
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(4, 4, 4, 4)
        root.addWidget(_lbl(title))

        self.bands = [EQBand(i, p3_bases[i], rig) for i in range(3)]
        for b in self.bands:
            root.addWidget(b)

        read_btn = QPushButton("↓ Read from Radio")
        read_btn.clicked.connect(self._read_all)
        root.addWidget(read_btn)
        root.addStretch()

    def _read_all(self):
        for b in self.bands:
            b.read_from_rig()


class MenuPanel(QWidget):
    """Full EX menu browser."""

    def __init__(self, rig: FTdx10, parent=None):
        super().__init__(parent)
        self.rig = rig
        self._current: MenuItem | None = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(4, 4, 4, 4)
        root.addWidget(_lbl("FTDX10 Radio Menu (EX command)"))

        splitter = QSplitter(Qt.Horizontal)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Menu Items")
        self.tree.setStyleSheet("background:#050f09; color:#00ff88; font-size:10px;")
        for group, items in MENU_GROUPS.items():
            g_item = QTreeWidgetItem([group])
            g_item.setFont(0, QFont("Consolas", 9, QFont.Bold))
            self.tree.addTopLevelItem(g_item)
            for m in items:
                child = QTreeWidgetItem([f"EX{m.p1:02d}{m.p2:02d}{m.p3:02d}  {m.label}"])
                child.setData(0, Qt.UserRole, m.key)
                g_item.addChild(child)
        self.tree.expandAll()
        self.tree.currentItemChanged.connect(self._on_select)
        splitter.addWidget(self.tree)

        right = QWidget()
        rg = QVBoxLayout(right)
        rg.setSpacing(6)

        self.item_lbl = QLabel("Select an item →")
        self.item_lbl.setStyleSheet("color:#00ff88; font-family:Consolas; font-size:11px;")
        self.item_lbl.setWordWrap(True)
        rg.addWidget(self.item_lbl)

        self.raw_lbl = QLabel("Raw value: —")
        self.raw_lbl.setStyleSheet("color:#00aa55; font-size:10px;")
        rg.addWidget(self.raw_lbl)

        self.val_spin = QSpinBox()
        self.val_spin.setRange(-999, 9999)
        rg.addWidget(self.val_spin)

        self.val_combo = QComboBox()
        self.val_combo.hide()
        rg.addWidget(self.val_combo)

        btn_row = QHBoxLayout()
        self.read_btn  = QPushButton("↓ Read")
        self.write_btn = QPushButton("↑ Write")
        self.read_btn.clicked.connect(self._do_read)
        self.write_btn.clicked.connect(self._do_write)
        btn_row.addWidget(self.read_btn)
        btn_row.addWidget(self.write_btn)
        rg.addLayout(btn_row)

        rg.addWidget(_lbl("Direct EX: EX p1p2p3 value"))
        raw_row = QHBoxLayout()
        self.raw_p1 = QSpinBox(); self.raw_p1.setRange(1,4); self.raw_p1.setPrefix("P1:")
        self.raw_p2 = QSpinBox(); self.raw_p2.setRange(1,7); self.raw_p2.setPrefix("P2:")
        self.raw_p3 = QSpinBox(); self.raw_p3.setRange(1,23); self.raw_p3.setPrefix("P3:")
        self.raw_val = QLineEdit(); self.raw_val.setPlaceholderText("value")
        self.raw_val.setStyleSheet("background:#0d2018; color:#00ff88; border:1px solid #1a4a2a;")
        raw_go_r = QPushButton("Read"); raw_go_r.clicked.connect(self._raw_read)
        raw_go_w = QPushButton("Write"); raw_go_w.clicked.connect(self._raw_write)
        raw_row.addWidget(self.raw_p1)
        raw_row.addWidget(self.raw_p2)
        raw_row.addWidget(self.raw_p3)
        raw_row.addWidget(self.raw_val)
        raw_row.addWidget(raw_go_r)
        raw_row.addWidget(raw_go_w)
        rg.addLayout(raw_row)
        rg.addStretch()

        splitter.addWidget(right)
        splitter.setSizes([260, 240])
        root.addWidget(splitter)

    def _on_select(self, current, _prev):
        if not current:
            return
        key = current.data(0, Qt.UserRole)
        if key is None:
            return
        item = MENU_BY_KEY.get(key)
        if not item:
            return
        self._current = item
        self.item_lbl.setText(f"EX{item.p1:02d}{item.p2:02d}{item.p3:02d} — {item.label}\n{item.unit}")
        self.raw_lbl.setText("Raw value: —")

        if item.vtype == "select" and item.options:
            self.val_spin.hide()
            self.val_combo.show()
            self.val_combo.clear()
            for k, v in item.options.items():
                self.val_combo.addItem(str(v), k)
        else:
            self.val_combo.hide()
            self.val_spin.show()
            self.val_spin.setRange(item.vmin, item.vmax)
            self.val_spin.setSuffix(f" {item.unit}" if item.unit else "")

    def _do_read(self):
        if not self._current or not self.rig.is_connected:
            return
        m = self._current
        raw = self.rig.ex_read(m.p1, m.p2, m.p3)
        if raw is None:
            self.raw_lbl.setText("Raw value: (no response)")
            return
        self.raw_lbl.setText(f"Raw value: {raw}")
        try:
            v = int(raw)
            if m.vtype == "select" and m.options:
                idx = self.val_combo.findData(v)
                if idx >= 0:
                    self.val_combo.setCurrentIndex(idx)
            else:
                self.val_spin.setValue(v)
        except ValueError:
            pass

    def _do_write(self):
        if not self._current or not self.rig.is_connected:
            return
        m = self._current
        if m.vtype == "select":
            v = self.val_combo.currentData()
        else:
            v = self.val_spin.value()
        fmt = f"{{:0{m.digits}d}}"
        self.rig.ex_write(m.p1, m.p2, m.p3, fmt.format(v))

    def _raw_read(self):
        if not self.rig.is_connected:
            return
        raw = self.rig.ex_read(self.raw_p1.value(), self.raw_p2.value(), self.raw_p3.value())
        self.raw_val.setText(raw or "(no response)")

    def _raw_write(self):
        if not self.rig.is_connected:
            return
        self.rig.ex_write(self.raw_p1.value(), self.raw_p2.value(),
                          self.raw_p3.value(), self.raw_val.text())


class EQMenuPanel(QWidget):
    """Tab widget: TX DSP EQ | MIC P-EQ | Radio Menu."""

    def __init__(self, rig: FTdx10, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()

        # TX DSP EQ: P3 bases 2,5,8
        tx_scroll = QScrollArea()
        tx_scroll.setWidgetResizable(True)
        tx_scroll.setWidget(EQSection("TX DSP Parametric EQ", [2, 5, 8], rig))
        tabs.addTab(tx_scroll, "TX DSP EQ")

        # MIC P-EQ: P3 bases 11,14,17
        mic_scroll = QScrollArea()
        mic_scroll.setWidgetResizable(True)
        mic_scroll.setWidget(EQSection("Parametric MIC EQ", [11, 14, 17], rig))
        tabs.addTab(mic_scroll, "MIC P-EQ")

        tabs.addTab(MenuPanel(rig), "Radio Menu")
        root.addWidget(tabs)
