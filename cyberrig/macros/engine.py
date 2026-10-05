"""MacroRunner — threaded macro executor with stop flag and variable expansion."""

import threading
import time
from typing import Callable, Optional
from cyberrig.macros.model import Macro, MacroStep, MACRO_VARS
from cyberrig.cat.ftdx10 import FTdx10


class MacroRunner:
    """Executes a Macro in a background thread.

    Callers register on_step / on_done / on_error callbacks.
    Call stop() to abort mid-run (checked between each step).
    """

    def __init__(self, rig: FTdx10, callsign: str = ""):
        self.rig = rig
        self.callsign = callsign
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, macro: Macro,
            on_step: Optional[Callable[[int, MacroStep], None]] = None,
            on_done: Optional[Callable[[bool], None]] = None):
        """Execute macro asynchronously.  on_done(True) = completed, False = stopped."""
        if self._thread and self._thread.is_alive():
            return  # already running
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._execute,
            args=(macro, on_step, on_done),
            daemon=True,
            name=f"macro-{macro.name}",
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Execution ─────────────────────────────────────────────────────────────

    def _expand(self, text: str) -> str:
        freq_khz = f"{self.rig.state.freq_a / 1e3:.3f}"
        mode = self.rig.state.mode
        text = text.replace("$FREQ_A", freq_khz)
        text = text.replace("$MODE",   mode)
        text = text.replace("$CALL",   self.callsign or "N0CALL")
        return text

    def _execute(self, macro: Macro,
                 on_step: Optional[Callable],
                 on_done: Optional[Callable]):
        aborted = False
        for i, step in enumerate(macro.steps):
            if self._stop_event.is_set():
                aborted = True
                break
            if on_step:
                try:
                    on_step(i, step)
                except Exception:
                    pass
            self._run_step(step)

        if on_done:
            try:
                on_done(not aborted)
            except Exception:
                pass

    def _run_step(self, step: MacroStep):
        t = step.stype

        if t == "comment":
            return  # documentation only

        if t == "delay":
            # Sleep in 50ms chunks so stop() is responsive
            remaining = step.ms / 1000.0
            chunk = 0.05
            while remaining > 0 and not self._stop_event.is_set():
                time.sleep(min(chunk, remaining))
                remaining -= chunk
            return

        if not self.rig.is_connected:
            return

        if t == "cat":
            cmd = step.cmd.strip()
            if cmd and not cmd.endswith(";"):
                cmd += ";"
            if cmd:
                self.rig._set(cmd)

        elif t == "set_freq":
            self.rig.set_freq(step.hz)

        elif t == "set_mode":
            self.rig.set_mode(step.mode)

        elif t == "set_power":
            self.rig.set_power(step.watts)

        elif t == "ptt_on":
            self.rig.set_ptt(True)

        elif t == "ptt_off":
            self.rig.set_ptt(False)

        elif t == "cw":
            text = self._expand(step.text)
            self.rig.send_cw(text)
