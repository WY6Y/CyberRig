"""Exclusive digital operating-mode switch: DATA-U/L + clean CAT preset.

Does not open a second USB stream. Audio holders live on RigCodec; this
module snapshots the radio, applies the digital-clean profile, and restores
it on exit. VarAC/JTDX holding the codec fail closed at RigCodec.open.

Only one mode may be entered at a time. The codec hold alone would catch
that, but it reports "USB codec busy" — which reads like a foreign app is
running. The registry here names the mode that is actually in the way.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

log = logging.getLogger("cyberrig.modes")

# Digital modes follow SSB sideband convention: LSB below 10 MHz, USB at/above.
LSB_BELOW_HZ = 10_000_000

# FTDX10 RADIO SETTING -> MODE PSK/DATA is P1=01 **P2=04**.
#
# It was P2=05 here until 2026-09-12, and the note explaining that read
# "LCUT/HCUT/BPF share the SSB P3 numbers on this table". They do not share
# anything -- P2=05 IS an SSB-shaped group (it is MODE RTTY), which is why its
# P3 numbers lined up with SSB's. Every DATA EX write this module has ever made
# landed on another mode's menu and did nothing. The group order per the CAT
# manual is 01 SSB, 02 AM, 03 FM, 04 PSK/DATA, 05 RTTY.
#
# Settled by a read-only sweep of both groups on the radio (the station PC,
# 2026-09-12). Only the 04 group has PSK TONE (P3 7) and DATA SHIFT (P3 8),
# only it runs to 18 entries, and RPORT GAIN sits at P3 17 where 1/5/17 reads
# None -- which is what made the address look dead rather than misfiled.
EX_DATA_LCUT = (1, 4, 9)
EX_DATA_HCUT = (1, 4, 11)
EX_DATA_BPF = (1, 4, 14)
EX_DATA_MOD = (1, 4, 15)
EX_DATA_REAR = (1, 4, 16)
EX_DATA_RPORT = (1, 4, 17)

# ITEMS THE RADIO'S OWN [PRESET] OWNS -- DO NOT WRITE THESE WHILE IT IS ON.
#
# The FTDX10's MODE-screen [PRESET] is a one-touch bundle of 14 registered
# items aimed at FT8, and a second touch restores what they were before. Four
# of them are ours. the operator keeps PRESET enabled permanently (photo of the MODE
# screen, 2026-09-12), so on this station PRESET is already doing the job
# _apply_preset() was written to do -- and doing it better, because it has a
# restore path the radio itself honours.
#
# Writing underneath a live PRESET is how settings move for no visible reason:
# we would set a value, PRESET's own copy would keep overriding it, and its
# restore would put back something neither side chose. So when PRESET is on we
# skip these four and let it win. DATA MOD SOURCE is deliberately NOT in this
# set -- it is not one of PRESET's 14, so it stays ours to set.
#
# There is no CAT opcode for PRESET's state (absent from the CAT reference
# entirely), so this cannot be detected -- only declared. See settings.py
# ftdx10_preset_active.
PRESET_OWNED = frozenset({EX_DATA_LCUT, EX_DATA_HCUT, EX_DATA_BPF, EX_DATA_REAR})

# SSB table SH code 20 = 3000 Hz - wide enough for RADE and for a 2300 Hz
# SSTV white tone, still a voice filter.
SH_WIDE = 20

def _preset_active() -> bool:
    """Whether the radio's own [PRESET] is enabled. Declared, never detected --
    the CAT protocol exposes no opcode for it. Defaults to True because that is
    this station, and because the failure it guards against (writing underneath
    PRESET) is worse than the one it causes (leaving four registers alone)."""
    try:
        from cyberrig.settings import Settings
        val = Settings().get("ftdx10_preset_active")
    except Exception:
        return True
    return True if val is None else bool(val)


# Registry of the one entered mode, keyed by tag.
_reg_lock = threading.Lock()
_active: dict[str, "DigitalSession"] = {}


def active_session() -> Optional["DigitalSession"]:
    """The currently entered digital mode, or None."""
    with _reg_lock:
        for s in _active.values():
            return s
    return None


def data_mode_for_hz(hz: Optional[int]) -> str:
    try:
        f = int(hz or 0)
    except (TypeError, ValueError):
        f = 0
    return "DATA-L" if 0 < f < LSB_BELOW_HZ else "DATA-U"


class DigitalSession:
    """Enter/exit an exclusive digital mode. Inject a fake rig + codec in tests.

    Subclasses override `_extra_preset` / `_extra_snapshot` / `_extra_restore`
    for anything beyond the shared digital-clean profile (SSTV clamps power,
    for instance). They must not re-implement enter/exit.
    """

    tag = "digital"
    label = "Digital"

    def __init__(self, rig, codec=None, tag: Optional[str] = None, label: Optional[str] = None):
        self.rig = rig
        self.codec = codec
        if tag:
            self.tag = tag
        if label:
            self.label = label
        self.active = False
        self.snapshot: Optional[dict] = None
        self.radio_mode: str = "DATA-U"
        self.error: Optional[str] = None
        self._codec_held = False

    def status(self) -> dict:
        freq = None
        mode = None
        connected = False
        try:
            st = getattr(self.rig, "state", None)
            if st is not None:
                freq = getattr(st, "freq_a", None)
                mode = getattr(st, "mode", None)
            connected = bool(getattr(self.rig, "is_connected", False))
        except Exception:
            pass
        holders = {}
        if self.codec is not None:
            try:
                holders = self.codec.status()
            except Exception:
                holders = {}
        other = self._other_mode()
        return {
            "tag": self.tag,
            "active": self.active,
            "radio_mode": self.radio_mode if self.active else mode,
            "data_mode_for_vfo": data_mode_for_hz(freq),
            "connected": connected,
            "freq_a": freq,
            "error": self.error,
            "codec_held": self._codec_held,
            "blocked_by": other.label if other is not None else None,
            "audio": holders,
        }

    def enter(self) -> dict:
        if self.active:
            return {"ok": True, "already": True, **self.status()}
        self.error = None

        busy = self._busy_reason()
        if busy:
            self.error = busy
            return {"ok": False, "error": busy, **self.status()}

        try:
            self.snapshot = self._snapshot()
            self._apply_preset()
        except Exception as e:
            self.error = f"CAT preset failed: {e}"
            log.exception("%s enter CAT", self.label)
            try:
                self._restore()
            except Exception:
                pass
            self.snapshot = None
            return {"ok": False, "error": self.error, **self.status()}

        held = self._hold_codec()
        if not held.get("ok"):
            self.error = held.get("error") or "USB codec busy"
            try:
                self._restore()
            except Exception:
                log.exception("%s enter restore after codec fail", self.label)
            self.snapshot = None
            return {"ok": False, "error": self.error, **self.status()}

        self.active = True
        with _reg_lock:
            _active[self.tag] = self
        log.info("%s entered radio_mode=%s", self.label, self.radio_mode)
        return {"ok": True, **self.status()}

    def exit(self) -> dict:
        if not self.active and self.snapshot is None:
            return {"ok": True, "already": True, **self.status()}
        try:
            self._release_codec()
        except Exception:
            log.exception("%s codec release", self.label)
        try:
            self._restore()
        except Exception as e:
            self.error = f"CAT restore failed: {e}"
            log.exception("%s exit CAT", self.label)
            self._unregister()
            self.active = False
            return {"ok": False, "error": self.error, **self.status()}
        self.snapshot = None
        self._unregister()
        self.active = False
        self.error = None
        log.info("%s exited, radio restored", self.label)
        return {"ok": True, **self.status()}

    def follow_freq(self, hz, force: bool = False) -> Optional[str]:
        """Keep DATA-U/DATA-L matched to the band while entered.

        Digital sideband is a property of the frequency, not of how the
        operator got there. Entering on 20m and then spinning down to 40m has
        to become DATA-L or the far end hears nothing, so this runs off the
        VFO, not just off `enter()`. Returns the new mode if it changed.

        `force` re-asserts the mode even when it already matches what we last
        set — used after something else (the remote tuner) has borrowed the
        mode and put back a stale one.
        """
        if not self.active:
            return None
        want = data_mode_for_hz(hz)
        if want == self.radio_mode and not force:
            return None
        changed = want != self.radio_mode
        _call(self.rig, "set_mode", want)
        self.radio_mode = want
        if changed:
            log.info("%s follows VFO %s -> %s", self.label, hz, want)
        return want if changed else None

    def _unregister(self):
        with _reg_lock:
            if _active.get(self.tag) is self:
                _active.pop(self.tag, None)

    # -- exclusivity ------------------------------------------------------

    def _other_mode(self) -> Optional["DigitalSession"]:
        with _reg_lock:
            for tag, s in _active.items():
                if tag != self.tag and s.active:
                    return s
        return None

    def _busy_reason(self) -> Optional[str]:
        other = self._other_mode()
        if other is not None:
            return f"{other.label} is entered - exit it first"
        if self.codec is None:
            return None
        try:
            st = self.codec.status() or {}
        except Exception as e:
            return f"USB codec status failed: {e}"
        tx = [t for t in (st.get("tx_holders") or []) if t != self.tag]
        if tx:
            return "USB codec busy - disarm REMOTE TX and close VarAC/JTDX first"
        return None

    def _hold_codec(self) -> dict:
        if self.codec is None:
            return {"ok": True, "skipped": True}
        rx = self.codec.hold("rx", self.tag)
        if not rx.get("ok"):
            return rx
        tx = self.codec.hold("tx", self.tag)
        if not tx.get("ok"):
            try:
                self.codec.release("rx", self.tag)
            except Exception:
                pass
            return tx
        self._codec_held = True
        return {"ok": True}

    def _release_codec(self):
        if self.codec is None or not self._codec_held:
            self._codec_held = False
            return
        try:
            self.codec.release("tx", self.tag)
        finally:
            self.codec.release("rx", self.tag)
            self._codec_held = False

    # -- CAT snapshot / preset --------------------------------------------

    def _snapshot(self) -> dict:
        rig = self.rig
        snap: dict[str, Any] = {}
        snap["mode"] = _call(rig, "get_mode") or getattr(getattr(rig, "state", None), "mode", None)
        snap["sh"] = _call(rig, "get_sh")
        snap["nr"] = _call(rig, "get_nr")
        snap["nb"] = _call(rig, "get_nb")
        snap["notch"] = _call(rig, "get_notch")
        snap["contour"] = _call(rig, "get_contour")
        snap["compressor"] = _call(rig, "get_compressor")
        snap["dnf"] = _call(rig, "get_dnf")
        snap["monitor"] = _call(rig, "get_monitor")
        snap["roofing"] = _call(rig, "get_roofing")
        snap["ex"] = {}
        for key in (
            EX_DATA_LCUT, EX_DATA_HCUT, EX_DATA_BPF,
            EX_DATA_MOD, EX_DATA_REAR, EX_DATA_RPORT,
        ):
            val = None
            if hasattr(rig, "ex_read"):
                try:
                    val = rig.ex_read(*key)
                except Exception:
                    val = None
            snap["ex"][key] = val
        self._extra_snapshot(snap)
        return snap

    def _apply_preset(self):
        rig = self.rig
        freq = getattr(getattr(rig, "state", None), "freq_a", None)
        if freq is None:
            freq = _call(rig, "get_freq")
        self.radio_mode = data_mode_for_hz(freq)
        _call(rig, "set_mode", self.radio_mode)
        _call(rig, "set_compressor", False)
        _call(rig, "set_nr", False)
        _call(rig, "set_nb", False)
        _call(rig, "set_notch", False)
        _call(rig, "set_contour", False)
        _call(rig, "set_dnf", False)
        _call(rig, "set_monitor", False)
        _call(rig, "set_sh", SH_WIDE)
        roof = _call(rig, "get_roofing")
        if roof in ("500", "500Hz", "500HZ"):
            _call(rig, "set_roofing", "3K")
        writes = (
            (EX_DATA_LCUT, "00"),   # OFF
            (EX_DATA_HCUT, "00"),   # OFF
            (EX_DATA_BPF, "0"),     # 50-3050
            (EX_DATA_MOD, "1"),     # REAR
            (EX_DATA_REAR, "1"),    # USB
        )
        if hasattr(rig, "ex_write"):
            for key, val in writes:
                self._ex_write_verified(key, val)
        self._extra_preset()

    def _ex_write_verified(self, key, val) -> bool:
        """Write one EX register and read it back. Returns whether it stuck.

        VERIFY, DO NOT ASSUME -- the same rule rig.py already states for the
        XIT offset, for the same reason. An EX write to an address the radio
        does not have does not raise and does not reply; it is simply ignored.
        That is exactly how a wrong menu group (P2=05 for four years) stayed
        invisible: every write "succeeded". A read-back is the independent
        observable that makes a misfiled address loud instead of silent.

        Skips anything the radio's own [PRESET] owns while that is enabled --
        see PRESET_OWNED.
        """
        rig = self.rig
        p1, p2, p3 = key
        if key in PRESET_OWNED and _preset_active():
            log.debug("%s EX %02d%02d%02d left to the radio's PRESET",
                      self.label, p1, p2, p3)
            return True
        try:
            rig.ex_write(p1, p2, p3, val)
        except Exception:
            log.warning("%s EX write %02d%02d%02d failed", self.label, p1, p2, p3)
            return False

        if not hasattr(rig, "ex_read"):
            return True
        try:
            got = rig.ex_read(p1, p2, p3)
        except Exception:
            log.warning("%s EX %02d%02d%02d could not be read back",
                        self.label, p1, p2, p3)
            return False

        # The radio zero-pads to the field width in its table, so compare as
        # integers where both sides look numeric and fall back to text.
        ok = str(got).strip() == str(val).strip()
        if not ok:
            try:
                ok = int(got) == int(val)
            except (TypeError, ValueError):
                ok = False
        if not ok:
            log.warning("%s EX %02d%02d%02d did not stick: wrote %r, read %r",
                        self.label, p1, p2, p3, val, got)
        return ok

    def _restore(self):
        snap = self.snapshot or {}
        rig = self.rig
        # Mode-specific first: a power clamp must come off even if a later
        # SSB-profile write fails.
        try:
            self._extra_restore(snap)
        except Exception:
            log.exception("%s extra restore", self.label)
        mode = snap.get("mode")
        if mode:
            _call(rig, "set_mode", mode)
        sh = snap.get("sh")
        if sh is not None:
            _call(rig, "set_sh", sh)
        for name, setter in (
            ("nr", "set_nr"),
            ("nb", "set_nb"),
            ("notch", "set_notch"),
            ("contour", "set_contour"),
            ("compressor", "set_compressor"),
            ("dnf", "set_dnf"),
            ("monitor", "set_monitor"),
        ):
            val = snap.get(name)
            if val is not None:
                _call(rig, setter, bool(val))
        roof = snap.get("roofing")
        if roof:
            _call(rig, "set_roofing", roof)
        ex = snap.get("ex") or {}
        if hasattr(rig, "ex_write"):
            for key, val in ex.items():
                if val is None:
                    continue
                try:
                    rig.ex_write(*key, str(val))
                except Exception:
                    log.warning("%s EX restore %s failed", self.label, key)

    # -- subclass hooks ---------------------------------------------------

    def _extra_snapshot(self, snap: dict):
        """Record anything else this mode will change. Mutate `snap` in place."""

    def _extra_preset(self):
        """Apply anything beyond the shared digital-clean profile."""

    def _extra_restore(self, snap: dict):
        """Undo `_extra_preset` from the values `_extra_snapshot` recorded."""


def _call(obj, name: str, *args):
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    return fn(*args)
