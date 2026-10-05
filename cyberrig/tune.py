"""Remote-tuner TUNE — a continuous carrier for an external autotuner.

This is NOT the FTDX10's internal ATU (CAT ``AC``); that lives in
``FTdx10.atu_tune()``. This keys a low-power AM carrier so a remote tuner such
as an MFJ-994BRT has something to match against, then puts the operator's mode
and power back exactly as they were.

It lived inside ``cyberrig/web/app.py`` as module globals and an asyncio task
until the Qt panel needed the same button. Two independent copies of a routine
that keys the transmitter is exactly the wrong thing to have — the interlocks
(no normal PTT while tuning, no internal-ATU cycle while tuning, no macro
while tuning) only work if both UIs are looking at the same state. So it is one
object, owned by whoever owns the rig, and the safety timeout runs on a
``threading.Timer`` rather than the event loop so it is callable from a Qt slot
as safely as from a request handler.
"""

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("cyberrig.tune")


class RemoteTuner:
    def __init__(self, rig, settings, on_change: Optional[Callable[[], None]] = None):
        self._rig = rig
        self._settings = settings
        self._on_change = on_change
        self._lock = threading.RLock()
        self._active = False
        self._saved_mode: Optional[str] = None
        self._saved_power: Optional[int] = None
        self._started: float = 0.0
        self._timer: Optional[threading.Timer] = None

    # ── state ────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    def status(self) -> dict:
        return {
            "tune_active": self._active,
            "tune_watts": int(self._settings.get("tune_watts", 20)),
            "tune_mode": self._settings.get("tune_mode", "AM"),
            "tune_timeout_sec": int(self._settings.get("tune_timeout_sec", 90)),
            "elapsed": (time.time() - self._started) if self._active else 0,
            "saved_mode": self._saved_mode,
            "saved_power": self._saved_power,
        }

    def _changed(self):
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                log.exception("tune on_change")

    # ── control ──────────────────────────────────────────────────────────

    def start(self, watts: Optional[int] = None) -> dict:
        """Key a continuous carrier at ~20 W for an external autotuner."""
        with self._lock:
            rig = self._rig
            if not rig.is_connected:
                return {"ok": False, "error": "Not connected to radio"}
            if self._active:
                return {"ok": True, "already": True}

            mode = str(self._settings.get("tune_mode", "AM") or "AM")
            w = int(watts if watts is not None else self._settings.get("tune_watts", 20))
            w = max(5, min(100, w))
            timeout = int(self._settings.get("tune_timeout_sec", 90))

            # Snapshot operator state. state.power can lag, so ask the radio.
            self._saved_mode = rig.state.mode or "USB"
            self._saved_power = rig.state.power if rig.state.power else 50
            try:
                pc = rig.get_power()
                if pc is not None:
                    self._saved_power = pc
            except Exception:
                pass

            if rig.state.is_tx:
                rig.set_ptt(False)
                time.sleep(0.15)

            try:
                rig.set_power(w)
                time.sleep(0.05)
                rig.set_mode(mode)
                time.sleep(0.1)
                # AM/FM: CAT TX1 gives a continuous carrier (CW would need a key)
                rig.set_ptt(True)
            except Exception as e:
                log.exception("tune start failed")
                self._stop_locked(restore=True)
                return {"ok": False, "error": str(e)}

            self._active = True
            self._started = time.time()
            log.info("Remote TUNE ON — %s @ %dW (saved %s @ %sW)",
                     mode, w, self._saved_mode, self._saved_power)

            self._arm_timeout(timeout)
            result = {
                "ok": True,
                "tune_active": True,
                "mode": mode,
                "watts": w,
                "timeout_sec": timeout,
                "saved_mode": self._saved_mode,
                "saved_power": self._saved_power,
            }
        self._changed()
        return result

    def stop(self, restore: bool = True) -> dict:
        with self._lock:
            self._stop_locked(restore=restore)
        self._changed()
        return {"ok": True, "tune_active": False}

    # ── internals ────────────────────────────────────────────────────────

    def _stop_locked(self, restore: bool = True):
        self._cancel_timeout()
        was = self._active
        saved_mode = self._saved_mode
        saved_power = self._saved_power
        self._active = False
        self._started = 0.0
        self._saved_mode = None
        self._saved_power = None
        try:
            self._rig.set_ptt(False)
        except Exception as e:
            log.warning("tune stop PTT: %s", e)
        if restore and was:
            # The FTDX10 ignores MD/PC while still settling out of TX.
            time.sleep(0.25)
            try:
                if saved_mode:
                    self._rig.set_mode(saved_mode)
                if saved_power is not None:
                    self._rig.set_power(int(saved_power))
                log.info("Remote TUNE stopped — restored %s @ %sW",
                         saved_mode, saved_power)
            except Exception as e:
                log.warning("tune restore: %s", e)
        elif was:
            log.info("Remote TUNE stopped")

    def _arm_timeout(self, seconds: int):
        self._cancel_timeout()
        if seconds <= 0:
            return
        t = threading.Timer(seconds, self._on_timeout, args=(seconds,))
        t.daemon = True
        self._timer = t
        t.start()

    def _cancel_timeout(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timeout(self, seconds: int):
        with self._lock:
            if not self._active:
                return
            log.warning("Remote TUNE auto-stopped after %ds safety timeout", seconds)
            self._stop_locked(restore=True)
        self._changed()
