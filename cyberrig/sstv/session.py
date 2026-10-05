"""SSTV operating mode: the shared digital preset, plus a TX power clamp.

SSTV is the only mode CyberRig transmits that is 100% duty cycle for minutes
at a time — Scottie DX is 269 seconds of unbroken carrier. The FTDX10 will
happily do that at 100 W and cook itself, so entering SSTV snapshots the
current power and clamps it. Exit puts the old value back.

DO NOT REMOVE the clamp to "let the user decide". The whole point is that the
decision is made once, in settings, instead of in the two seconds before a
four-minute transmission starts.
"""

from __future__ import annotations

import logging
from typing import Optional

from cyberrig.modes.session import DigitalSession, _call

log = logging.getLogger("cyberrig.sstv")

# Conservative default for a 100 W radio at 100% duty. Overridable via the
# `sstv_watts` setting; still hard-limited by SSTV_MAX_WATTS below. Note this
# is the ALC threshold, not the output — see SSTV_MAX_WATTS.
DEFAULT_WATTS = 30

# A ceiling the SETTING cannot exceed. Raised from 50 to 100 on 2026-08-29,
# deliberately, because clamping PO low was protecting the wrong thing.
#
# PO is not what the radio radiates — it is where the ALC threshold sits. Set
# it at the target and the ALC ends up doing the limiting, which is how this
# station spent every SSTV transmission sitting on the ALC at ~30%: 43 W out
# of a 50 W clamp is no headroom at all. The operator's FT8 practice is the
# correct one and transfers unchanged — put the ceiling well above the target
# (100 W), then set the actual output with the audio drive, and the ALC never
# engages. What matters for a 270 s 100%-duty transmission is the WATTS THAT
# COME OUT, and that is now guarded directly by SSTVTransmitter's measured-PO
# trip rather than implied by a low ceiling.
SSTV_MAX_WATTS = 100


class SSTVSession(DigitalSession):
    tag = "sstv"
    label = "SSTV"

    def __init__(self, rig, codec=None, watts: Optional[int] = None):
        super().__init__(rig, codec)
        self.watts = clamp_watts(watts if watts is not None else DEFAULT_WATTS)

    def set_watts(self, watts: int) -> int:
        """Change the clamp. Applies immediately when already entered."""
        self.watts = clamp_watts(watts)
        if self.active:
            _call(self.rig, "set_power", self.watts)
        return self.watts

    def status(self) -> dict:
        st = super().status()
        st["watts"] = self.watts
        st["max_watts"] = SSTV_MAX_WATTS
        snap = self.snapshot or {}
        st["restore_watts"] = snap.get("power")
        return st

    def _extra_snapshot(self, snap: dict):
        snap["power"] = _call(self.rig, "get_power")

    def _extra_preset(self):
        _call(self.rig, "set_power", self.watts)
        log.info("SSTV power clamped to %d W", self.watts)

    def _extra_restore(self, snap: dict):
        prev = snap.get("power")
        if prev is not None:
            _call(self.rig, "set_power", int(prev))
            log.info("SSTV power restored to %s W", prev)


def clamp_watts(watts) -> int:
    try:
        w = int(watts)
    except (TypeError, ValueError):
        w = DEFAULT_WATTS
    return max(5, min(SSTV_MAX_WATTS, w))
