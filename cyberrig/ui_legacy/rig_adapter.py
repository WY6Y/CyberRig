"""Qt signal adapter for the (Qt-free) FTdx10 CAT class.

`cyberrig.cat.ftdx10.FTdx10` is deliberately headless: it publishes state
changes through a plain callback registry (`rig.on(event, fn)`), so the web
server can use it without dragging in Qt.

The desktop UI in this package predates that refactor and was written against
a version of `FTdx10` that exposed Qt Signals directly (`rig.freq_changed.connect(...)`).
Rather than rewrite every panel, `RigProxy` wraps a live `FTdx10` and gives it
back the signal surface the panels expect:

    rig = RigProxy(FTdx10())      # or an already-connected instance
    rig.smeter_update.connect(widget.set_value)   # Signal  -> handled here
    rig.set_freq(14_074_000)                      # method  -> proxied to FTdx10

Thread safety is the reason this class exists at all. `FTdx10._fire()` runs on
the `cat-poll` background thread. Emitting from a QObject whose affinity is the
GUI thread makes Qt use a queued connection automatically, so slots run on the
GUI thread and it is safe to touch widgets in them. Calling widget setters
straight from `_fire` (i.e. skipping this class) would be a cross-thread paint.

**Construct this on the GUI thread.**
"""

import logging

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class RigProxy(QObject):
    """Wraps an FTdx10, re-publishing its callback events as Qt Signals.

    Signals are class attributes, so normal attribute lookup finds them first;
    anything else (the ~118 FTdx10 methods, `.state`, `.is_connected`) falls
    through to the wrapped instance via `__getattr__`.
    """

    # -- 1 argument ---------------------------------------------------------
    connected_changed = Signal(object)
    freq_changed = Signal(object)
    freq_b_changed = Signal(object)
    mode_changed = Signal(object)
    ptt_changed = Signal(object)
    split_changed = Signal(object)
    fine_changed = Signal(object)
    xit_changed = Signal(object)
    smeter_update = Signal(object)
    meters_update = Signal(object)             # {rm_type: raw_count}
    preamp_changed = Signal(object)
    att_changed = Signal(object)
    agc_changed = Signal(object)
    dnf_changed = Signal(object)
    af_changed = Signal(object)
    rf_changed = Signal(object)
    antenna_changed = Signal(object)
    power_changed = Signal(object)
    mic_changed = Signal(object)
    lock_changed = Signal(object)
    radio_power_changed = Signal(object)
    roofing_changed = Signal(object)

    # -- 2 arguments --------------------------------------------------------
    rit_changed = Signal(object, object)          # (on, offset_hz)
    filter_changed = Signal(object, object)       # (bandwidth, if_shift)
    notch_changed = Signal(object, object)        # (on, position)
    contour_changed = Signal(object, object)      # (on, freq_hz)
    apf_changed = Signal(object, object)          # (on, freq_hz)
    comp_changed = Signal(object, object)         # (on, level)
    nb_changed = Signal(object, object)           # (on, level)
    nr_changed = Signal(object, object)           # (on, level)
    monitor_changed = Signal(object, object)      # (on, level)
    atu_changed = Signal(object, object)          # (atu, atu_tuning)

    # -- 3 arguments --------------------------------------------------------
    meter_update = Signal(object, object, object)  # (power, alc, swr)
    vox_changed = Signal(object, object, object)   # (on, gain, delay)

    # -- 4 arguments --------------------------------------------------------
    cw_changed = Signal(object, object, object, object)  # (speed, pitch, breakin, delay)

    #: Every event re-published. Must stay a subset of what FTdx10._fire() emits.
    EVENTS = (
        "connected_changed", "freq_changed", "freq_b_changed", "mode_changed",
        "ptt_changed", "split_changed", "fine_changed", "xit_changed", "smeter_update",
        "preamp_changed", "att_changed", "agc_changed", "dnf_changed",
        "af_changed", "rf_changed", "antenna_changed", "power_changed",
        "mic_changed", "lock_changed", "radio_power_changed", "roofing_changed",
        "rit_changed", "filter_changed", "notch_changed", "contour_changed",
        "apf_changed", "comp_changed", "nb_changed", "nr_changed", "monitor_changed",
        "atu_changed", "meter_update", "meters_update", "vox_changed", "cw_changed",
    )

    def __init__(self, rig, parent=None):
        super().__init__(parent)
        # Plain assignment: Shiboken/QObject rejects object.__setattr__ here.
        # __getattr__ guards against the pre-assignment recursion case.
        self._rig = rig
        for name in self.EVENTS:
            rig.on(name, self._emitter(name))

    def _emitter(self, name):
        """Build the callback FTdx10 will invoke on the cat-poll thread."""
        signal = getattr(self, name)

        def _emit(*args):
            # A raising callback would propagate into the poll loop and can
            # kill CAT updates entirely, so swallow and log instead.
            try:
                signal.emit(*args)
            except Exception:
                log.exception("RigProxy: failed emitting %s%r", name, args)

        return _emit

    # -- Port lifecycle, deliberately NOT named connect/disconnect -----------
    #
    # FTdx10 has `connect`/`disconnect` (open/close the serial port) and so does
    # QObject (signal wiring). Overriding them here is NOT an option: PySide6's
    # `SignalInstance.connect()` dispatches through QObject.connect internally,
    # so a Python-level override hijacks every `some_signal.connect(slot)` call
    # in the UI. These are exposed under distinct names instead, and the guard
    # below refuses to let a new collision be introduced unnoticed.

    def cat_connect(self, *args, **kwargs):
        """Open the CAT serial port. (FTdx10.connect; `connect` is Qt's.)"""
        return self._rig.connect(*args, **kwargs)

    def cat_disconnect(self, *args, **kwargs):
        """Close the CAT serial port. (FTdx10.disconnect; `disconnect` is Qt's.)"""
        return self._rig.disconnect(*args, **kwargs)

    def __getattr__(self, name):
        # Only reached when normal lookup fails, i.e. not a Signal/QObject attr.
        if name == "_rig":
            raise AttributeError(name)
        return getattr(self._rig, name)


#: FTdx10 members that QObject also defines. These can never be reached through
#: the proxy by their own name -- Qt's version always wins -- so each needs a
#: renamed accessor above, and callers must use that name.
_RENAMED = {"connect": "cat_connect", "disconnect": "cat_disconnect"}


def _assert_no_unhandled_collisions():
    from cyberrig.cat.ftdx10 import FTdx10

    qobject_names = set(dir(QObject))
    rig_names = {n for n in dir(FTdx10) if not n.startswith("_")}
    unhandled = (rig_names & qobject_names) - set(_RENAMED)
    if unhandled:
        raise RuntimeError(
            "RigProxy cannot reach these FTdx10 members -- QObject defines the "
            f"same names and wins: {sorted(unhandled)}. Add a renamed accessor "
            "on RigProxy (see cat_connect) and register it in _RENAMED. Do NOT "
            "override the name directly; that breaks Qt's own signal wiring."
        )


_assert_no_unhandled_collisions()
