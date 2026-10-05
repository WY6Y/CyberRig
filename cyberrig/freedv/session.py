"""FreeDV operating mode: the shared digital session, plus a TX power clamp.

The preset (DATA-U/L, processor off, wide filter, rear USB mod source) lives
in `cyberrig.modes.session` because SSTV needs exactly the same radio state.
Names are re-exported here so existing imports keep working.

The power clamp is the SSTV one, ported on 2026-08-29 — see `FREEDV_MAX_WATTS`
for why a clamp that sits HIGH is the protective setting and not the timid one.
"""

from __future__ import annotations

import logging
from typing import Optional

from cyberrig.modes.session import (  # noqa: F401  (re-exported)
    EX_DATA_BPF,
    EX_DATA_HCUT,
    EX_DATA_LCUT,
    EX_DATA_MOD,
    EX_DATA_REAR,
    EX_DATA_RPORT,
    LSB_BELOW_HZ,
    SH_WIDE,
    DigitalSession,
    _call,
    data_mode_for_hz,
)

log = logging.getLogger("cyberrig.freedv")

FOREIGN_TX_TAGS = ("tx",)

# What `freedv_watts` defaults to, and the ceiling the setting cannot exceed.
#
# Both are 100 on purpose, and that is not "no protection". The radio's power
# setting is where the ALC THRESHOLD sits, not what comes out of the antenna.
# Put it on the target and the ALC becomes the limiter, which is how this
# station spent every SSTV transmission sitting at ~30% ALC — and FreeDV is
# far less forgiving of that than a picture is: its waveform is high
# peak-to-average, so ALC action clips peaks into IMD that spreads across the
# band and costs SNR at the far end. Correct FreeDV shows ZERO ALC.
#
# So the threshold goes high, the audio drive (`freedv_tx_level`) sets the
# output, and the thermal protection lives where it belongs — on the MEASURED
# power, in FreeDVTxGuard's PO trip.
DEFAULT_WATTS = 100
FREEDV_MAX_WATTS = 100


class FreeDVSession(DigitalSession):
    tag = "freedv"
    label = "FreeDV"

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
        st["max_watts"] = FREEDV_MAX_WATTS
        snap = self.snapshot or {}
        st["restore_watts"] = snap.get("power")
        return st

    def _extra_snapshot(self, snap: dict):
        snap["power"] = _call(self.rig, "get_power")

    def _extra_preset(self):
        _call(self.rig, "set_power", self.watts)
        log.info("FreeDV power set to %d W (ALC threshold, not output)", self.watts)

    def _extra_restore(self, snap: dict):
        prev = snap.get("power")
        if prev is not None:
            _call(self.rig, "set_power", int(prev))
            log.info("FreeDV power restored to %s W", prev)


def clamp_watts(watts) -> int:
    try:
        w = int(watts)
    except (TypeError, ValueError):
        w = DEFAULT_WATTS
    return max(5, min(FREEDV_MAX_WATTS, w))
