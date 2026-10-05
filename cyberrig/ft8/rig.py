"""CAT side: talk to CyberRig's panel, and own the XIT transmit offset.

Two jobs, kept in one place because they share a client.

`RigClient` is a thin HTTP wrapper over `cyberrig-panel` (which owns the physical
serial port). Nothing here opens a serial port itself -- the panel is the single
CAT owner and JTDX only borrows it through Hamlib rigctl on 4532.

`SlotController` owns the transmit slot. JTDX v2.2 has no `Configure(15)` message,
so its audio TX frequency cannot be set over UDP at all. The workaround is XIT:
the radio offsets its TRANSMIT frequency while leaving receive alone, so

    emitted audio-equivalent = JTDX's tx_df + XIT offset

and CyberRig can move the emission anywhere in the sub-band without JTDX's
knowledge. Verified on the radio 2026-09-01: `XT1;` plus a clarifier of -313
produced `IF002014074000-031301C00000` with the dial unchanged.

WHY VERIFICATION IS NOT OPTIONAL HERE. CyberRig's poll loop refreshes the XIT
FLAG from the IF register, but it never refreshes the clarifier OFFSET -- that
value is its own local belief and nothing checks it. The offset is the whole
point of this module, and a wrong one means transmitting on a frequency nobody
asked for, silently. So every set is read back from the radio's own IF register
before the slot is considered good. That is the `VS0;` lesson: confirm by an
independent observable, not by the write returning ok.

AND IT MUST BE DONE BEFORE KEYING: the panel deliberately skips IF polling while
transmitting ("serial time goes to meters/PTT only"), so there is no read-back
available mid-over. Set the slot, verify it, then key.

A LEFT-ON XIT IS A SILENT QRM MACHINE. It survives band changes, it is invisible
unless something draws it, and the operator will not hear it because receive is
unaffected. Every path that ends a transmission clears it.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

from cyberrig.bands import FT8_DIAL_HZ

log = logging.getLogger("cyberrig.ft8.rig")

PANEL_URL = "http://127.0.0.1:8000"

# The FT8 sub-band in audio terms. Refuse to place a transmission outside it --
# a fat-fingered drag must not put a signal where the filter rolls off or, worse,
# outside the passband entirely.
SLOT_MIN_HZ = 200
SLOT_MAX_HZ = 2900

# FTDX10 clarifier range.
XIT_MAX_HZ = 9990


class RigClient:
    """HTTP to cyberrig-panel. Every call is short and fails loudly."""

    def __init__(self, base: str = PANEL_URL, timeout: float = 4.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _req(self, path: str, payload: Optional[dict] = None) -> Any:
        url = f"{self.base}{path}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = r.read()
        return json.loads(body) if body else {}

    # ---- reads ----
    def state(self) -> dict:
        return self._req("/api/state")

    def if_register(self) -> dict:
        """The radio's own IF; parse — the independent observable.

        Carries the clarifier offset and the RIT/XIT flags as the RADIO reports
        them, which `/api/state` does not for the offset.
        """
        d = self._req("/api/debug/cat")
        return d.get("parsed_if") or {}

    # ---- writes ----
    def ptt(self, tx: bool) -> dict:
        return self._req("/api/ptt", {"tx": bool(tx)})

    def set_xit(self, on: bool) -> dict:
        return self._req("/api/xit", {"on": bool(on)})

    def set_clarifier(self, hz: int) -> dict:
        return self._req("/api/rit_offset", {"hz": int(hz)})

    def clear_clarifier(self) -> dict:
        return self._req("/api/rit_clear", {})

    def set_freq(self, hz: int) -> dict:
        return self._req("/api/freq", {"hz": int(hz)})


# FT8 watering holes. One copy, in cyberrig/bands.py, because HUNT MODE hops
# to the same dials and a second band plan is exactly what drifts.
FT8_BAND_HZ = FT8_DIAL_HZ


class SlotError(Exception):
    pass


class SlotController:
    """The transmit slot, implemented as an XIT offset and verified every time."""

    def __init__(self, rig: RigClient):
        self._rig = rig
        self._lock = threading.Lock()
        self._target: Optional[int] = None      # desired audio slot, Hz
        self._offset: int = 0                   # applied XIT, Hz
        self._verified: bool = False
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------ apply

    def set_slot(self, target_hz: int, tx_df: int) -> dict:
        """Place the emission at `target_hz` given JTDX's current audio slot.

        Raises SlotError rather than silently clamping to something the operator
        did not ask for -- a slot quietly moved 400 Hz is worse than a refusal.
        """
        target = int(target_hz)
        if not (SLOT_MIN_HZ <= target <= SLOT_MAX_HZ):
            raise SlotError(
                f"slot {target} Hz is outside the FT8 sub-band "
                f"({SLOT_MIN_HZ}-{SLOT_MAX_HZ} Hz)")
        offset = target - int(tx_df)
        if abs(offset) > XIT_MAX_HZ:
            raise SlotError(f"offset {offset} Hz exceeds the clarifier range")

        with self._lock:
            self._target = target
            self._verified = False
            self._last_error = None

        if offset == 0:
            # Nothing to offset: make sure any previous XIT is gone rather than
            # leaving it set and merely believing it is zero.
            self.clear(reason="slot equals JTDX tx_df")
            with self._lock:
                self._target = target
            return self.status()

        self._rig.set_xit(True)
        self._rig.set_clarifier(offset)
        with self._lock:
            self._offset = offset
        self.verify()
        return self.status()

    def verify(self) -> bool:
        """Read the offset back from the radio's IF register.

        `/api/state` cannot answer this: the panel refreshes the XIT flag from IF
        but never the clarifier value, so asking it would only echo our own write.
        """
        try:
            reg = self._rig.if_register()
        except (urllib.error.URLError, OSError, ValueError) as e:
            with self._lock:
                self._verified = False
                self._last_error = f"read-back failed: {e}"
            log.error("XIT read-back failed: %s", e)
            return False

        want = self._offset
        got = reg.get("clarifier")
        xit_on = bool(reg.get("xit"))
        ok = (got == want) and (xit_on or want == 0)
        with self._lock:
            self._verified = ok
            self._last_error = None if ok else (
                f"radio reports clarifier {got} xit={xit_on}, wanted {want}")
        if not ok:
            log.error("XIT NOT VERIFIED: %s", self._last_error)
        return ok

    # ------------------------------------------------------------------ clear

    def clear(self, reason: str = "") -> dict:
        """Remove the offset. Every path that ends a transmission calls this.

        Best-effort and never raises: this runs on abort paths, and a clear that
        throws would mask the trip that caused it.
        """
        try:
            self._rig.clear_clarifier()
            self._rig.set_xit(False)
        except Exception:
            log.exception("XIT clear FAILED (%s) — offset may still be applied", reason)
        with self._lock:
            self._offset = 0
            self._target = None
            self._verified = True
        if reason:
            log.info("XIT cleared (%s)", reason)
        return self.status()

    # Named hooks so callers read as intent rather than as bookkeeping.
    def on_band_change(self): self.clear(reason="band change")
    def on_halt(self):        self.clear(reason="halt")
    def on_deadman(self):     self.clear(reason="dead-man")
    def on_shutdown(self):    self.clear(reason="shutdown")

    # ----------------------------------------------------------------- status

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def active(self) -> bool:
        return self._offset != 0

    def status(self) -> dict:
        with self._lock:
            return {
                "target_slot": self._target,
                "offset_hz": self._offset,
                "active": self._offset != 0,
                "verified": self._verified,
                "error": self._last_error,
                "range": [SLOT_MIN_HZ, SLOT_MAX_HZ],
            }

    def safe_to_transmit(self) -> tuple[bool, str]:
        """An unverified offset must not be keyed on."""
        with self._lock:
            if self._offset == 0:
                return True, ""
            if not self._verified:
                return False, self._last_error or "XIT offset not verified"
            return True, ""
