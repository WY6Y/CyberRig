"""A layout that wraps its items onto additional rows instead of demanding one line.

The Mode row (13 buttons) and Band row (10) were plain `QHBoxLayout`s, which
report the sum of their children as a minimum width -- 608px and 407px
respectively. That gave the central widget a ~620px floor, so on a small screen
there was almost no slack left after the two dock columns, and dragging a dock
splitter would snap straight back: the central widget simply refused to give up
the width. Wrapping removes that floor without hiding any buttons.

Based on Qt's standard FlowLayout example.
"""

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, spacing=3):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(QMargins(margin, margin, margin, margin))
        self.setSpacing(spacing)

    # -- QLayout plumbing ---------------------------------------------------
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    # -- QHBoxLayout compatibility -----------------------------------------
    # These exist so FlowLayout is a drop-in for the QHBoxLayout it replaces.
    # Neither has a meaning here: there is no single line to distribute slack
    # along, and inter-item spacing is uniform (see setSpacing).

    def addStretch(self, stretch: int = 0):
        return

    def addSpacing(self, size: int):
        return

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    # -- height-for-width is what makes wrapping work -----------------------
    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        """Just the widest single item -- not the sum. This is the whole point."""
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # -- placement ----------------------------------------------------------
    def _layout(self, rect, test_only):
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y = effective.x(), effective.y()
        line_height = 0

        for item in self._items:
            wid = item.widget()
            sp = self.spacing()
            if wid is not None:
                sp_x = sp + wid.style().layoutSpacing(
                    QSizePolicy.PushButton, QSizePolicy.PushButton, Qt.Horizontal)
                sp_y = sp + wid.style().layoutSpacing(
                    QSizePolicy.PushButton, QSizePolicy.PushButton, Qt.Vertical)
            else:
                sp_x = sp_y = sp

            next_x = x + item.sizeHint().width() + sp_x
            if next_x - sp_x > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + sp_y
                next_x = x + item.sizeHint().width() + sp_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))

            x = next_x
            line_height = max(line_height, item.sizeHint().height())

        return y + line_height - rect.y() + m.bottom()
