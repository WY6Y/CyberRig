"""Macro palette panel — grid of macro buttons with run/stop control."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QScrollArea, QFrame,
)
from PySide6.QtCore import Qt, Slot, QTimer, Signal
from PySide6.QtGui import QColor, QPalette, QKeySequence, QShortcut
from cyberrig.cat.ftdx10 import FTdx10
from cyberrig.macros.model import Macro, MacroStep
from cyberrig.macros.engine import MacroRunner
from cyberrig.macros.store import load_macros, save_macros


class MacroButton(QPushButton):
    """Single macro button with active/idle styling."""

    def __init__(self, macro: Macro, parent=None):
        super().__init__(parent)
        self.macro = macro
        self._active = False
        self._refresh()

    def _refresh(self):
        name = self.macro.name
        key  = f"  [{self.macro.key}]" if self.macro.key else ""
        self.setText(f"{name}{key}")
        self.setToolTip(self.macro.desc or self.macro.name)
        if self._active:
            self.setStyleSheet(
                "QPushButton {"
                f"  background: #ff6600;"
                "  color: #000; font-weight:bold; font-size:11px;"
                "  border: 2px solid #ff9900; padding: 4px 8px;"
                "  border-radius: 3px;"
                "}"
            )
        else:
            col = self.macro.color or "#1a4a2a"
            self.setStyleSheet(
                "QPushButton {"
                f"  background: {col};"
                "  color: #00ff88; font-size:11px;"
                "  border: 1px solid #2a7a4a; padding: 4px 8px;"
                "  border-radius: 3px;"
                "}"
                "QPushButton:hover {"
                "  border-color: #00ff88;"
                "}"
            )

    def set_active(self, active: bool):
        self._active = active
        self._refresh()

    def update_macro(self, macro: Macro):
        self.macro = macro
        self._refresh()


class MacroPanel(QWidget):
    """Palette of macro buttons + status bar."""

    open_editor = Signal(int)   # emitted with macro index to edit

    def __init__(self, rig: FTdx10, callsign: str = "", parent=None):
        super().__init__(parent)
        self.rig = rig
        self.callsign = callsign
        self._macros: list[Macro] = []
        self._runner = MacroRunner(rig, callsign)
        self._active_idx: int = -1
        self._buttons: list[MacroButton] = []
        self._shortcuts: list[QShortcut] = []
        self._build()
        self.reload()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(4, 4, 4, 4)

        # Header row
        hdr = QHBoxLayout()
        title = QLabel("MACROS")
        title.setStyleSheet("color:#00ff88; font-family:Consolas; font-weight:bold; font-size:12px;")
        hdr.addWidget(title)
        hdr.addStretch()
        self.stop_btn = QPushButton("■ STOP")
        self.stop_btn.setFixedWidth(80)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet(
            "QPushButton { background:#3a0000; color:#ff4444; border:1px solid #aa0000; "
            "font-weight:bold; } QPushButton:hover { border-color:#ff0000; }"
        )
        self.stop_btn.clicked.connect(self._on_stop)
        hdr.addWidget(self.stop_btn)
        root.addLayout(hdr)

        # Status label
        self.status_lbl = QLabel("Ready")
        self.status_lbl.setStyleSheet("color:#00aa55; font-size:10px; font-family:Consolas;")
        root.addWidget(self.status_lbl)

        # Scroll area for button grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.grid_widget = QWidget()
        self.grid = QGridLayout(self.grid_widget)
        self.grid.setSpacing(4)
        scroll.setWidget(self.grid_widget)
        root.addWidget(scroll)

        # Bottom controls
        ctrl = QHBoxLayout()
        add_btn = QPushButton("+ New")
        add_btn.clicked.connect(self._on_new)
        edit_btn = QPushButton("✎ Edit")
        edit_btn.clicked.connect(self._on_edit_selected)
        reload_btn = QPushButton("↺ Reload")
        reload_btn.clicked.connect(self.reload)
        for b in (add_btn, edit_btn, reload_btn):
            b.setFixedHeight(24)
            ctrl.addWidget(b)
        root.addLayout(ctrl)

    # ── Macro management ─────────────────────────────────────────────────────

    def reload(self):
        from cyberrig.macros.store import load_macros
        self._macros = load_macros()
        self._rebuild_grid()
        self._rebuild_shortcuts()

    def _rebuild_grid(self):
        # Clear existing buttons
        for b in self._buttons:
            b.deleteLater()
        self._buttons.clear()
        for i in reversed(range(self.grid.count())):
            item = self.grid.takeAt(i)
            if item.widget():
                item.widget().deleteLater()

        cols = 3
        for idx, macro in enumerate(self._macros):
            btn = MacroButton(macro)
            btn.clicked.connect(lambda c=False, i=idx: self._on_run(i))
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, i=idx: self._ctx_menu(i)
            )
            self._buttons.append(btn)
            self.grid.addWidget(btn, idx // cols, idx % cols)

    def _rebuild_shortcuts(self):
        for sc in self._shortcuts:
            sc.deleteLater()
        self._shortcuts.clear()
        for idx, macro in enumerate(self._macros):
            if macro.key:
                try:
                    sc = QShortcut(QKeySequence(macro.key), self)
                    sc.activated.connect(lambda i=idx: self._on_run(i))
                    self._shortcuts.append(sc)
                except Exception:
                    pass

    # ── Run / Stop ───────────────────────────────────────────────────────────

    def _on_run(self, idx: int):
        if self._runner.is_running():
            return
        macro = self._macros[idx]
        self._active_idx = idx
        for b in self._buttons:
            b.set_active(False)
        self._buttons[idx].set_active(True)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText(f"Running: {macro.name}")
        self._runner = MacroRunner(self.rig, self.callsign)
        self._runner.run(
            macro,
            on_step=self._on_step,
            on_done=self._on_done,
        )

    def _on_stop(self):
        self._runner.stop()
        self.status_lbl.setText("Stopped")

    def _on_step(self, step_idx: int, step: MacroStep):
        # Called from worker thread — use Qt-safe label update via timer trick
        text = f"Step {step_idx + 1}: {step.summary()}"
        QTimer.singleShot(0, lambda: self.status_lbl.setText(text))

    def _on_done(self, completed: bool):
        def _ui():
            for b in self._buttons:
                b.set_active(False)
            self.stop_btn.setEnabled(False)
            self.status_lbl.setText("Done" if completed else "Stopped")
            self._active_idx = -1
        QTimer.singleShot(0, _ui)

    # ── New / Edit ────────────────────────────────────────────────────────────

    def _on_new(self):
        from cyberrig.macros.model import Macro
        self._macros.append(Macro(name=f"Macro {len(self._macros)+1}"))
        save_macros(self._macros)
        self._rebuild_grid()
        self._rebuild_shortcuts()
        self.open_editor.emit(len(self._macros) - 1)

    def _on_edit_selected(self):
        if self._active_idx >= 0:
            self.open_editor.emit(self._active_idx)

    def _ctx_menu(self, idx: int):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction("Edit",   lambda: self.open_editor.emit(idx))
        menu.addAction("Move Up",   lambda: self._move(idx, -1))
        menu.addAction("Move Down", lambda: self._move(idx, +1))
        menu.addSeparator()
        menu.addAction("Delete", lambda: self._delete(idx))
        menu.exec(self._buttons[idx].mapToGlobal(self._buttons[idx].rect().center()))

    def _move(self, idx: int, delta: int):
        dest = idx + delta
        if 0 <= dest < len(self._macros):
            self._macros.insert(dest, self._macros.pop(idx))
            save_macros(self._macros)
            self._rebuild_grid()
            self._rebuild_shortcuts()

    def _delete(self, idx: int):
        self._macros.pop(idx)
        save_macros(self._macros)
        self._rebuild_grid()
        self._rebuild_shortcuts()

    def update_macro(self, idx: int, macro: Macro):
        if 0 <= idx < len(self._macros):
            self._macros[idx] = macro
            save_macros(self._macros)
            self._buttons[idx].update_macro(macro)
            self._rebuild_shortcuts()
