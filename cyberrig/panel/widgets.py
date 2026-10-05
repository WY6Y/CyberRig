"""Panel widgets — the pieces the fixed layout is assembled from.

Everything here is sized in absolute pixels on purpose. At a fixed window size
nothing reflows, so a widget that negotiates for space is a widget that can
surprise you.
"""

from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

# Match FTdx10.set_freq clamp. A click that would walk off the radio's
# range stays put rather than sending a CAT value the rig will reject.
FREQ_MIN_HZ = 30_000
FREQ_MAX_HZ = 75_000_000
DEFAULT_VFO_STEP = 1_000

from cyberrig.cat import meter_cal
from cyberrig.panel import theme as T


# ── group frame ──────────────────────────────────────────────────────────

class Group(QFrame):
    """Bordered panel with its name painted into the bottom-right corner.

    QGroupBox puts its title on top, which costs margin-top plus padding-top
    before a single control is drawn — about 14 px, times fourteen groups.
    Painting the label into the bottom-right corner instead costs nothing, and
    a corner label is the usual convention on dense instrument panels.
    """

    LABEL_H = 13

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("group")
        self._title = title
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(6, 5, 7, self.LABEL_H)
        self.body.setSpacing(4)

    def set_title(self, title: str):
        self._title = title
        self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if not self._title:
            return
        p = QPainter(self)
        f = QFont(self.font())
        f.setPointSizeF(6.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(T.AMBER))
        r = QRect(0, self.height() - self.LABEL_H - 1, self.width() - 6, self.LABEL_H)
        p.drawText(r, Qt.AlignRight | Qt.AlignVCenter, self._title)
        p.end()


# ── buttons ──────────────────────────────────────────────────────────────

def button(text: str, checkable=False, tip="", width=None, height=T.BTN_H) -> QPushButton:
    """One button height everywhere. That constant is T.BTN_H and it is 20."""
    b = QPushButton(text)
    b.setCheckable(checkable)
    b.setFixedHeight(height)
    if width:
        b.setFixedWidth(width)
    if tip:
        b.setToolTip(tip)
    b.setFocusPolicy(Qt.NoFocus)
    return b


def button_grid(names, cols, checkable=True, exclusive=False):
    """A tight grid of identical buttons. Returns (widget, {name: button})."""
    w = QWidget()
    g = QGridLayout(w)
    g.setContentsMargins(0, 0, 0, 0)
    g.setSpacing(3)
    made = {}
    for i, name in enumerate(names):
        b = button(name, checkable=checkable)
        g.addWidget(b, i // cols, i % cols)
        made[name] = b
    if exclusive:
        for b in made.values():
            b.setAutoExclusive(True)
    return w, made


# ── sliders ──────────────────────────────────────────────────────────────

class _TickSlider(QSlider):
    """Slider that paints its own tick rail beside the groove."""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setFocusPolicy(Qt.NoFocus)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setPen(QPen(QColor("#26302e"), 1))
        if self.orientation() == Qt.Vertical:
            x0, x1 = self.width() - 6, self.width() - 1
            n = 9
            for i in range(n):
                y = int(2 + (self.height() - 4) * i / (n - 1))
                p.drawLine(x0, y, x1, y)
        else:
            y0, y1 = self.height() - 5, self.height() - 1
            n = 11
            for i in range(n):
                x = int(2 + (self.width() - 4) * i / (n - 1))
                p.drawLine(x, y0, x, y1)
        p.end()
        super().paintEvent(ev)


class VSlider(QWidget):
    """Vertical slider + caption + live value, in a 26 px column.

    Eight of these fit in the height a single labelled spinbox row would take,
    and the thumb position is readable from across the room.

    Pass toggleable=True when the caption is also an on/off switch (NOTCH):
    it becomes a checkable chip like DNR/CONT, and `toggled` fires independently
    of `valueChanged` so the frequency survives off.
    """

    valueChanged = Signal(int)
    toggled = Signal(bool)

    def __init__(self, caption: str, lo=0, hi=100, value=0, fmt="{}",
                 toggleable=False, parent=None):
        super().__init__(parent)
        self._fmt = fmt
        self._toggleable = bool(toggleable)
        self._on = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self.slider = _TickSlider(Qt.Vertical)
        self.slider.setRange(lo, hi)
        self.slider.setValue(value)
        self.slider.setFixedWidth(16)

        if self._toggleable:
            self.cap = button(caption, checkable=True, height=18)
            self.cap.setToolTip(f"{caption} on/off — slider is the frequency")
            self.cap.toggled.connect(self._on_cap_toggled)
        else:
            self.cap = QLabel(caption)
            self.cap.setObjectName("dim")
            self.cap.setAlignment(Qt.AlignHCenter)

        self.val = QLabel(fmt.format(value))
        self.val.setObjectName("value")
        self.val.setAlignment(Qt.AlignHCenter)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(self.slider)
        row.addStretch(1)
        lay.addLayout(row, 1)
        lay.addWidget(self.cap)
        lay.addWidget(self.val)

        self.slider.valueChanged.connect(self._on_change)

    def _on_change(self, v):
        self.val.setText(self._fmt.format(v))
        self.valueChanged.emit(v)

    def _on_cap_toggled(self, on):
        self._on = bool(on)
        self.toggled.emit(self._on)

    def set_on_silent(self, on):
        if not self._toggleable:
            return
        self._on = bool(on)
        self.cap.blockSignals(True)
        self.cap.setChecked(self._on)
        self.cap.blockSignals(False)

    def is_on(self):
        return bool(self._on)

    def set_value_silent(self, v):
        """Update from the radio without echoing a command back at it."""
        self.slider.blockSignals(True)
        self.slider.setValue(int(v))
        self.slider.blockSignals(False)
        self.val.setText(self._fmt.format(int(v)))


class HSlider(QWidget):
    """Horizontal slider with a fixed-width name and value either side.

    Pass toggleable=True when the name is also an on/off switch (DNR):
    the name becomes a checkable chip like PROC/VOX, and `toggled` fires
    independently of `valueChanged` so the level survives off.
    """

    valueChanged = Signal(int)
    toggled = Signal(bool)

    def __init__(self, name, lo=0, hi=100, value=0, fmt="{}",
                 name_w=46, val_w=48, toggleable=False, parent=None):
        super().__init__(parent)
        self._fmt = fmt
        self._toggleable = bool(toggleable)
        self._on = False
        # Toggle chips (DNR, CONT) match PROC/VOX at 18px so they read as
        # buttons, not dim captions. Plain level rows stay 15px.
        row_h = 18 if self._toggleable else 15
        self.setFixedHeight(row_h)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        if self._toggleable:
            self.name = button(name, checkable=True, width=name_w, height=row_h)
            self.name.setToolTip(f"{name} on/off — slider is the level")
            self.name.toggled.connect(self._on_name_toggled)
        else:
            self.name = QLabel(name)
            self.name.setObjectName("dim")
            self.name.setFixedWidth(name_w)

        self.slider = _TickSlider(Qt.Horizontal)
        self.slider.setRange(lo, hi)
        self.slider.setValue(value)

        self.val = QLabel(fmt.format(value))
        self.val.setObjectName("value")
        self.val.setFixedWidth(val_w)
        self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        lay.addWidget(self.name)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.val)
        self.slider.valueChanged.connect(self._on_change)

    def _on_change(self, v):
        self.val.setText(self._fmt.format(v))
        self.valueChanged.emit(v)

    def _on_name_toggled(self, on):
        self._on = bool(on)
        self.toggled.emit(self._on)

    def set_on_silent(self, on):
        """Update the on/off chip from the radio without echoing a command."""
        if not self._toggleable:
            return
        self._on = bool(on)
        self.name.blockSignals(True)
        self.name.setChecked(self._on)
        self.name.blockSignals(False)

    def is_on(self):
        return bool(self._on)

    def set_value_silent(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(int(v))
        self.slider.blockSignals(False)
        self.val.setText(self._fmt.format(int(v)))

    def set_text(self, text: str):
        self.val.setText(text)


# ── frequency ────────────────────────────────────────────────────────────

def format_freq_text(hz) -> str:
    """10.131.000 grouping, MHz unpadded so 40m stays `7.074.000`."""
    try:
        hz = int(hz)
    except (TypeError, ValueError):
        hz = 0
    mhz, rest = divmod(max(0, hz), 1_000_000)
    khz, hz = divmod(rest, 1000)
    return f"{mhz}.{khz:03d}.{hz:03d}"


def digit_steps_for_text(text: str):
    """Place-value in Hz of each digit, left to right, skipping the dots."""
    n = sum(ch.isdigit() for ch in text)
    steps = []
    power = n - 1
    for ch in text:
        if ch.isdigit():
            steps.append(10 ** power)
            power -= 1
    return steps


def nudge_freq(hz, step, direction) -> int:
    return max(FREQ_MIN_HZ, min(FREQ_MAX_HZ, int(hz) + int(direction) * int(step)))


class FreqDisplay(QFrame):
    """Phosphor VFO readout with per-digit click and wheel.

    Top half of a digit adds that place; bottom half subtracts. Wheel over
    a digit (or after clicking one) steps the same place. Radio-driven
    `set_freq` never emits `tuned`.
    """

    tuned = Signal(int)

    def __init__(self, size=34, primary=True, parent=None):
        super().__init__(parent)
        self._hz = 0
        self._size = size
        self._primary = primary
        self._hits = []          # [(QRect, step_hz), ...]
        self._hover = -1         # index into _hits
        self._armed_step = DEFAULT_VFO_STEP
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip(
            "Top half of a digit: + · Bottom half: − · Wheel: step that digit")
        self.setStyleSheet(
            f"background:{T.METER_BG};"
            f"border:1px solid {T.RULE_HOT if primary else T.RULE};"
        )

    def set_freq(self, hz):
        try:
            self._hz = int(hz)
        except (TypeError, ValueError):
            self._hz = 0
        self.update()

    def freq(self):
        return self._hz

    def text(self):
        return format_freq_text(self._hz)

    def _digit_font(self):
        f = QFont(T.MONO.split(",")[0].strip('"'))
        f.setPixelSize(self._size)
        f.setBold(True)
        return f

    def _layout_hits(self):
        text = self.text()
        fm = QFontMetrics(self._digit_font())
        total = fm.horizontalAdvance(text)
        x0 = (self.width() - total) // 2
        steps = digit_steps_for_text(text)
        hits = []
        di = 0
        h = max(1, self.height())
        for i, ch in enumerate(text):
            x = x0 + fm.horizontalAdvance(text[:i])
            w = fm.horizontalAdvance(text[:i + 1]) - fm.horizontalAdvance(text[:i])
            if ch.isdigit():
                hits.append((QRect(x, 0, max(1, w), h), steps[di]))
                di += 1
        self._hits = hits
        if self._hover >= len(hits):
            self._hover = -1

    def _hit_index_at(self, x) -> int:
        x = int(x)
        for i, (rect, _step) in enumerate(self._hits):
            if rect.left() <= x < rect.right():
                return i
        return -1

    def _nudge(self, step, direction):
        new = nudge_freq(self._hz, step, direction)
        if new == self._hz:
            return
        self._hz = new
        self.update()
        self.tuned.emit(new)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        self._layout_hits()
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        font = self._digit_font()
        p.setFont(font)
        fm = QFontMetrics(font)
        text = self.text()
        total = fm.horizontalAdvance(text)
        x0 = (self.width() - total) // 2
        y0 = (self.height() + fm.ascent() - fm.descent()) // 2
        base = QColor(T.PHOS if self._primary else "#93a89f")
        p.setPen(base)
        p.drawText(x0, y0, text)
        if 0 <= self._hover < len(self._hits):
            rect, _step = self._hits[self._hover]
            p.fillRect(rect.adjusted(0, 2, 0, -2), QColor(T.PHOS_DEEP))
            digits = [c for c in text if c.isdigit()]
            p.setPen(QColor("#7bffbd") if self._primary else QColor("#c5ddd4"))
            p.drawText(rect.x(), y0, digits[self._hover])
        p.end()

    def mouseMoveEvent(self, ev):
        idx = self._hit_index_at(ev.position().x())
        if idx != self._hover:
            self._hover = idx
            self.update()
        if idx >= 0:
            self._armed_step = self._hits[idx][1]
        ev.accept()

    def leaveEvent(self, ev):
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        if not self._hits:
            self._layout_hits()
        idx = self._hit_index_at(ev.position().x())
        if idx < 0:
            return
        rect, step = self._hits[idx]
        self._armed_step = step
        self._hover = idx
        direction = 1 if ev.position().y() < rect.height() / 2 else -1
        self._nudge(step, direction)
        ev.accept()

    def wheelEvent(self, ev):
        if not self._hits:
            self._layout_hits()
        idx = self._hit_index_at(ev.position().x())
        if idx >= 0:
            step = self._hits[idx][1]
            self._armed_step = step
            self._hover = idx
        else:
            step = self._armed_step or DEFAULT_VFO_STEP
        delta = ev.angleDelta().y()
        if delta == 0:
            delta = ev.pixelDelta().y()
        if delta == 0:
            ev.accept()
            return
        self._nudge(step, 1 if delta > 0 else -1)
        ev.accept()


class TuneStrip(QWidget):
    """Tick rail under the main VFO with a centre index mark.

    Purely an orientation cue — the dial on the radio is the tuning control.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(16)

    def paintEvent(self, ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor("#233033"), 1))
        step = 17
        for x in range(step // 2, w, step):
            p.drawLine(x, 3, x, 10)
        p.setPen(QPen(QColor("#1a2427"), 1))
        p.drawLine(0, h - 1, w, h - 1)
        p.setPen(QPen(QColor(T.PHOS), 3))
        p.drawLine(w // 2, 1, w // 2, 14)
        p.end()


# ── meters ───────────────────────────────────────────────────────────────

class BarMeter(QFrame):
    """Horizontal bar with a name, a live reading and a calibrated scale.

    The scale ticks are placed through the calibration curve, so a nonlinear
    meter gets a nonlinear face — the way the printed scale on the radio
    itself is marked. Even spacing would be a lie the operator then has to
    correct for mentally.
    """

    def __init__(self, spec: meter_cal.MeterSpec, label_fn=None,
                 reading_fn=None, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._raw = 0
        self._label_fn = label_fn or (lambda v: meter_cal.format_number(v, 0))
        self._reading_fn = reading_fn
        self.setStyleSheet(f"background:{T.METER_BG};border:1px solid {T.RULE};")

    def set_spec(self, spec, label_fn=None, reading_fn=None):
        self.spec = spec
        self._label_fn = label_fn or (lambda v: meter_cal.format_number(v, 0))
        self._reading_fn = reading_fn
        self._raw = 0
        self.update()

    def set_raw(self, raw):
        self._raw = 0 if raw is None else int(raw)
        self.update()

    def reading(self):
        if self._reading_fn:
            return self._reading_fn(self._raw)
        return self.spec.format(self._raw)

    # -- painting ---------------------------------------------------------

    def _bar_rect(self):
        return QRect(6, self.height() - 15, self.width() - 12, 7)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        small = QFont(T.MONO.split(",")[0].strip('"'))
        small.setPixelSize(8)

        # reading, top right
        big = QFont(small)
        big.setPixelSize(12)
        big.setBold(True)
        p.setFont(big)
        p.setPen(QColor(T.PHOS))
        p.drawText(QRect(0, 2, self.width() - 6, 14),
                   Qt.AlignRight | Qt.AlignVCenter, self.reading())

        # bar
        bar = self._bar_rect()
        p.fillRect(bar, QColor("#0e1416"))
        frac = self.spec.fraction(self._raw)
        zone = self.spec.zone(self._raw)
        colour = {"ok": T.PHOS, "warn": T.AMBER, "crit": T.RED}[zone]
        if frac > 0:
            p.fillRect(QRect(bar.x(), bar.y(), int(bar.width() * frac), bar.height()),
                       QColor(colour))

        # scale, positioned through the curve
        p.setFont(small)
        p.setPen(QColor("#3c4a47"))
        fm = QFontMetrics(small)
        y = self.height() - 7
        for tick in self.spec.ticks:
            label = self._label_fn(tick)
            tf = self.spec.tick_fraction(tick)
            x = bar.x() + int(bar.width() * tf)
            tw = fm.horizontalAdvance(label)
            if tf < 0.04:
                tx = bar.x()
            elif tf > 0.96:
                tx = bar.right() - tw
            else:
                tx = x - tw // 2
            p.drawText(QRect(tx, y, tw + 2, 8), Qt.AlignLeft | Qt.AlignVCenter, label)
        p.end()


class SwappableMeter(QWidget):
    """A BarMeter whose source the operator picks at run time.

    One slot cannot usefully be SWR forever: tuning wants SWR, a new mic
    setting wants ALC, the compressor wants COMP, and a PA or supply problem
    wants IDD and VDD.
    """

    sourceChanged = Signal(str)

    def __init__(self, initial="SWR", parent=None):
        super().__init__(parent)
        self._spec = meter_cal.BY_KEY.get(initial, meter_cal.SWR)

        self.meter = BarMeter(self._spec, label_fn=self._tick_label)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.meter)

        self.sel = QPushButton(self._spec.key, self)
        self.sel.setFixedHeight(13)
        self.sel.setCursor(Qt.PointingHandCursor)
        self.sel.setFocusPolicy(Qt.NoFocus)
        self.sel.setStyleSheet(
            f"background:#131a1c;border:1px solid #2b3a3d;color:#9db3ac;"
            f"font-family:{T.MONO};font-size:8px;padding:0 4px;text-align:left;"
        )
        self.sel.move(5, 3)
        self.sel.clicked.connect(self._open_menu)
        self._resize_sel()

    # -- source selection -------------------------------------------------

    def _resize_sel(self):
        fm = QFontMetrics(self.sel.font())
        self.sel.setFixedWidth(fm.horizontalAdvance(self._spec.key) + 16)

    def _open_menu(self):
        menu = QMenu(self)
        for spec in meter_cal.TX_METERS:
            act = menu.addAction(f"{spec.key}\tRM{spec.rm}")
            act.setCheckable(True)
            act.setChecked(spec is self._spec)
            act.triggered.connect(lambda _=False, s=spec: self.set_source(s.key))
        menu.exec(self.sel.mapToGlobal(self.sel.rect().bottomLeft()))

    def set_source(self, key: str):
        spec = meter_cal.BY_KEY.get(key)
        if spec is None or spec is self._spec:
            return
        self._spec = spec
        self.sel.setText(spec.key)
        self._resize_sel()
        self.meter.set_spec(spec, label_fn=self._tick_label)
        self.sourceChanged.emit(spec.key)

    @staticmethod
    def _tick_label(v):
        return meter_cal.format_number(v, 0 if float(v).is_integer() else 1)

    @property
    def spec(self):
        return self._spec

    def set_raw(self, raw):
        self.meter.set_raw(raw)

    def feed(self, meters: dict):
        """Take the {rm: raw} dict from the poll loop and pick out ours."""
        if self._spec.rm in meters:
            self.meter.set_raw(meters[self._spec.rm])
