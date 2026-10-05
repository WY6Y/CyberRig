"""Macro editor dialog — step-by-step builder."""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget,
    QPushButton, QLabel, QLineEdit, QComboBox, QSpinBox,
    QListWidget, QListWidgetItem, QGroupBox, QFormLayout,
    QColorDialog, QDialogButtonBox, QFrame, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from cyberrig.macros.model import Macro, MacroStep, STEP_TYPES, MACRO_VARS


class StepEditor(QGroupBox):
    """Inline editor pane that shows fields relevant to the selected step type."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Step Editor", parent)
        self._step: MacroStep | None = None
        self._build()

    def _build(self):
        layout = QFormLayout(self)
        layout.setLabelAlignment(Qt.AlignRight)
        layout.setSpacing(6)

        self.type_combo = QComboBox()
        for t in STEP_TYPES:
            self.type_combo.addItem(t)
        layout.addRow("Type:", self.type_combo)

        # CAT
        self.cat_edit = QLineEdit()
        self.cat_edit.setPlaceholderText("e.g.  NR01;")
        layout.addRow("CAT cmd:", self.cat_edit)

        # Delay
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 60000)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setSingleStep(10)
        layout.addRow("Delay:", self.delay_spin)

        # Frequency
        self.freq_spin = QSpinBox()
        self.freq_spin.setRange(1_800_000, 75_000_000)
        self.freq_spin.setSuffix(" Hz")
        self.freq_spin.setSingleStep(1000)
        self.freq_spin.setValue(14_200_000)
        layout.addRow("Frequency:", self.freq_spin)

        # Mode
        self.mode_combo = QComboBox()
        for m in ["LSB","USB","CW-U","CW-L","FM","FM-N","AM","AM-N",
                  "RTTY-L","RTTY-U","DATA-U","DATA-L","PSK"]:
            self.mode_combo.addItem(m)
        layout.addRow("Mode:", self.mode_combo)

        # Power
        self.power_spin = QSpinBox()
        self.power_spin.setRange(5, 100)
        self.power_spin.setSuffix(" W")
        self.power_spin.setValue(100)
        layout.addRow("Power:", self.power_spin)

        # Text (CW/comment)
        self.text_edit = QLineEdit()
        self.text_edit.setPlaceholderText("Text — use $CALL $FREQ_A $MODE")
        layout.addRow("Text:", self.text_edit)

        # Hint about vars
        hint = QLabel("  ".join(f"{k}" for k in MACRO_VARS))
        hint.setStyleSheet("color:#00aa55; font-size:9px;")
        layout.addRow("", hint)

        # Connect changes
        self.type_combo.currentIndexChanged.connect(self._on_type_change)
        self.cat_edit.textChanged.connect(self._save_to_step)
        self.delay_spin.valueChanged.connect(self._save_to_step)
        self.freq_spin.valueChanged.connect(self._save_to_step)
        self.mode_combo.currentIndexChanged.connect(self._save_to_step)
        self.power_spin.valueChanged.connect(self._save_to_step)
        self.text_edit.textChanged.connect(self._save_to_step)

        self._all_rows = [
            ("cat_edit", "CAT cmd:"),
            ("delay_spin", "Delay:"),
            ("freq_spin", "Frequency:"),
            ("mode_combo", "Mode:"),
            ("power_spin", "Power:"),
            ("text_edit", "Text:"),
        ]
        self._on_type_change()

    def _on_type_change(self):
        t = self.type_combo.currentText()
        # Show only relevant fields
        visible = {
            "cat":       ["cat_edit"],
            "delay":     ["delay_spin"],
            "set_freq":  ["freq_spin"],
            "set_mode":  ["mode_combo"],
            "set_power": ["power_spin"],
            "cw":        ["text_edit"],
            "ptt_on":    [],
            "ptt_off":   [],
            "comment":   ["text_edit"],
        }.get(t, [])

        for attr, lbl_text in self._all_rows:
            widget = getattr(self, attr)
            # Find label row in form layout and toggle
            widget.setVisible(attr in visible)
        self._save_to_step()

    def load_step(self, step: MacroStep):
        self._step = step
        self.type_combo.blockSignals(True)
        idx = self.type_combo.findText(step.stype)
        self.type_combo.setCurrentIndex(max(0, idx))
        self.type_combo.blockSignals(False)

        self.cat_edit.blockSignals(True);   self.cat_edit.setText(step.cmd);   self.cat_edit.blockSignals(False)
        self.delay_spin.blockSignals(True); self.delay_spin.setValue(step.ms);  self.delay_spin.blockSignals(False)
        self.freq_spin.blockSignals(True);  self.freq_spin.setValue(step.hz);   self.freq_spin.blockSignals(False)
        self.power_spin.blockSignals(True); self.power_spin.setValue(step.watts); self.power_spin.blockSignals(False)
        self.text_edit.blockSignals(True);  self.text_edit.setText(step.text);  self.text_edit.blockSignals(False)

        mi = self.mode_combo.findText(step.mode)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(max(0, mi))
        self.mode_combo.blockSignals(False)

        self._on_type_change()

    def _save_to_step(self, *_):
        if not self._step:
            return
        t = self.type_combo.currentText()
        self._step.stype  = t
        self._step.cmd    = self.cat_edit.text().strip()
        self._step.ms     = self.delay_spin.value()
        self._step.hz     = self.freq_spin.value()
        self._step.mode   = self.mode_combo.currentText()
        self._step.watts  = self.power_spin.value()
        self._step.text   = self.text_edit.text()
        self.changed.emit()


class MacroEditorDialog(QDialog):
    """Full macro editor: metadata + step list + step detail pane."""

    macro_saved = Signal(Macro)

    def __init__(self, macro: Macro, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit Macro — {macro.name}")
        self.setMinimumSize(680, 520)
        # Deep-copy so cancel works
        import copy
        self._macro = copy.deepcopy(macro)
        self._selected_step: int = -1
        self._build()
        self._load_meta()
        self._refresh_list()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── Metadata ─────────────────────────────────────────────────────────
        meta = QGroupBox("Macro Properties")
        mg = QFormLayout(meta)
        mg.setSpacing(6)

        self.name_edit = QLineEdit()
        mg.addRow("Name:", self.name_edit)

        self.desc_edit = QLineEdit()
        mg.addRow("Description:", self.desc_edit)

        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("F5, Ctrl+1, …")
        self.key_edit.setFixedWidth(100)
        key_row.addWidget(self.key_edit)
        key_row.addStretch()
        mg.addRow("Shortcut key:", key_row)

        color_row = QHBoxLayout()
        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(40, 22)
        self.color_btn.clicked.connect(self._pick_color)
        self.color_lbl = QLabel(self._macro.color)
        self.color_lbl.setStyleSheet("font-size:10px; color:#00aa55;")
        color_row.addWidget(self.color_btn)
        color_row.addWidget(self.color_lbl)
        color_row.addStretch()
        mg.addRow("Button color:", color_row)
        self._apply_color(self._macro.color)
        root.addWidget(meta)

        # ── Step list + editor ────────────────────────────────────────────────
        mid = QHBoxLayout()

        left = QVBoxLayout()
        left_lbl = QLabel("Steps")
        left_lbl.setStyleSheet("color:#00ff88; font-size:10px;")
        left.addWidget(left_lbl)

        self.step_list = QListWidget()
        self.step_list.setMinimumWidth(220)
        self.step_list.currentRowChanged.connect(self._on_step_select)
        left.addWidget(self.step_list)

        btn_row = QHBoxLayout()
        self.add_step_btn  = QPushButton("+ Add")
        self.del_step_btn  = QPushButton("✕ Del")
        self.up_step_btn   = QPushButton("↑")
        self.dn_step_btn   = QPushButton("↓")
        for b in (self.add_step_btn, self.del_step_btn, self.up_step_btn, self.dn_step_btn):
            b.setFixedHeight(24)
            btn_row.addWidget(b)
        left.addLayout(btn_row)
        self.add_step_btn.clicked.connect(self._add_step)
        self.del_step_btn.clicked.connect(self._del_step)
        self.up_step_btn.clicked.connect(lambda: self._move_step(-1))
        self.dn_step_btn.clicked.connect(lambda: self._move_step(+1))

        mid.addLayout(left)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color:#1a4a2a;")
        mid.addWidget(sep)

        self.step_editor = StepEditor()
        self.step_editor.changed.connect(self._on_step_edited)
        mid.addWidget(self.step_editor, stretch=1)
        root.addLayout(mid, stretch=1)

        # ── Dialog buttons ────────────────────────────────────────────────────
        bbox = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bbox.accepted.connect(self._on_save)
        bbox.rejected.connect(self.reject)
        root.addWidget(bbox)

    def _load_meta(self):
        self.name_edit.setText(self._macro.name)
        self.desc_edit.setText(self._macro.desc)
        self.key_edit.setText(self._macro.key)
        self._apply_color(self._macro.color)

    def _apply_color(self, color: str):
        self._macro.color = color
        self.color_btn.setStyleSheet(f"background:{color}; border:1px solid #00ff88;")
        self.color_lbl.setText(color)

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self._macro.color), self)
        if c.isValid():
            self._apply_color(c.name())

    def _refresh_list(self):
        self.step_list.blockSignals(True)
        self.step_list.clear()
        for i, s in enumerate(self._macro.steps):
            item = QListWidgetItem(f"{i+1:02d}. {s.summary()}")
            item.setForeground(QColor("#00ff88"))
            self.step_list.addItem(item)
        self.step_list.blockSignals(False)

    def _on_step_select(self, row: int):
        self._selected_step = row
        if 0 <= row < len(self._macro.steps):
            self.step_editor.load_step(self._macro.steps[row])

    def _on_step_edited(self):
        # Update list label in place
        row = self._selected_step
        if 0 <= row < len(self._macro.steps):
            self.step_list.item(row).setText(f"{row+1:02d}. {self._macro.steps[row].summary()}")

    def _add_step(self):
        row = self._selected_step + 1 if self._selected_step >= 0 else len(self._macro.steps)
        self._macro.steps.insert(row, MacroStep("delay", ms=100))
        self._refresh_list()
        self.step_list.setCurrentRow(row)

    def _del_step(self):
        row = self._selected_step
        if 0 <= row < len(self._macro.steps):
            self._macro.steps.pop(row)
            self._refresh_list()
            new_row = min(row, len(self._macro.steps) - 1)
            self.step_list.setCurrentRow(new_row)

    def _move_step(self, delta: int):
        row = self._selected_step
        dest = row + delta
        if 0 <= dest < len(self._macro.steps):
            self._macro.steps.insert(dest, self._macro.steps.pop(row))
            self._refresh_list()
            self.step_list.setCurrentRow(dest)

    def _on_save(self):
        self._macro.name  = self.name_edit.text().strip() or "Unnamed"
        self._macro.desc  = self.desc_edit.text().strip()
        self._macro.key   = self.key_edit.text().strip()
        self.macro_saved.emit(self._macro)
        self.accept()
