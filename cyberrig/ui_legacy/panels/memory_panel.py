"""Memory channels panel — store / recall / rename / delete frequency memories.

Backed by `cyberrig.memories` (the same `memories.json` the web UI uses), so a
channel stored here shows up there and vice versa.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget,
    QListWidgetItem, QLabel, QInputDialog, QMessageBox, QAbstractItemView,
)
from PySide6.QtCore import Qt, Slot

from cyberrig.memories.model import MemoryChannel
from cyberrig.memories.ops import recall as recall_channel, snapshot
from cyberrig.memories.store import load_memories, save_memories


def _describe(ch: MemoryChannel) -> str:
    return f"{ch.name}  —  {ch.freq / 1_000_000:.6f} MHz  {ch.mode}  {ch.power}W"


class MemoryPanel(QWidget):
    """List of stored channels with store/recall/rename/delete."""

    def __init__(self, rig, parent=None):
        super().__init__(parent)
        self.rig = rig
        self.memories: list[MemoryChannel] = load_memories()
        self._build()
        self._refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        # Double-click is the fast path operators actually use.
        self.list.itemDoubleClicked.connect(lambda _: self._recall())
        root.addWidget(self.list)

        btns = QHBoxLayout()
        btns.setSpacing(3)
        self._btn_store = QPushButton("Store Current")
        self._btn_recall = QPushButton("Recall")
        self._btn_rename = QPushButton("Rename")
        self._btn_delete = QPushButton("Delete")
        self._btn_store.clicked.connect(self._store)
        self._btn_recall.clicked.connect(self._recall)
        self._btn_rename.clicked.connect(self._rename)
        self._btn_delete.clicked.connect(self._delete)
        for b in (self._btn_store, self._btn_recall, self._btn_rename, self._btn_delete):
            btns.addWidget(b)
        root.addLayout(btns)

        self._status = QLabel("")
        self._status.setStyleSheet("color:#00aa66; font-size:10px;")
        root.addWidget(self._status)

    # ── helpers ───────────────────────────────────────────────────────────

    def _refresh(self):
        row = self.list.currentRow()
        self.list.clear()
        for ch in self.memories:
            self.list.addItem(QListWidgetItem(_describe(ch)))
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)

    def _selected(self) -> int:
        return self.list.currentRow()

    def _persist(self):
        save_memories(self.memories)
        self._refresh()

    def _require_rig(self) -> bool:
        if not self.rig.is_connected:
            self._status.setText("Radio not connected")
            return False
        return True

    # ── actions ───────────────────────────────────────────────────────────

    @Slot()
    def _store(self):
        if not self._require_rig():
            return
        name, ok = QInputDialog.getText(
            self, "Store Memory", "Name:", text=f"CH{len(self.memories) + 1:02d}"
        )
        if not ok:
            return
        ch = snapshot(self.rig.state, name)
        self.memories.append(ch)
        self._persist()
        self.list.setCurrentRow(len(self.memories) - 1)
        self._status.setText(f"Stored {_describe(ch)}")

    @Slot()
    def _recall(self):
        idx = self._selected()
        if idx < 0:
            return
        if not self._require_rig():
            return
        ch = self.memories[idx]
        recall_channel(self.rig, ch)
        self._status.setText(f"Recalled {ch.name}")

    @Slot()
    def _rename(self):
        idx = self._selected()
        if idx < 0:
            return
        ch = self.memories[idx]
        name, ok = QInputDialog.getText(self, "Rename Memory", "Name:", text=ch.name)
        if ok and name.strip():
            ch.name = name.strip()
            self._persist()

    @Slot()
    def _delete(self):
        idx = self._selected()
        if idx < 0:
            return
        ch = self.memories[idx]
        if QMessageBox.question(
            self, "Delete Memory", f"Delete “{ch.name}”?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        del self.memories[idx]
        self._persist()
        self._status.setText(f"Deleted {ch.name}")

    # ── shared with the hypermemory bar ───────────────────────────────────

    def reload(self):
        """Re-read from disk (after the hypermemory bar changes a slot)."""
        self.memories = load_memories()
        self._refresh()
