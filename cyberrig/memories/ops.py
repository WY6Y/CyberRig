"""Snapshot and recall operations for memory channels.

Shared by the web API (`cyberrig.web.app`) and the desktop UI
(`cyberrig.ui_legacy.panels.memory_panel`) so the recall *ordering* lives in one
place. That ordering is not arbitrary -- see `recall()`.
"""

from cyberrig.memories.model import MemoryChannel


def snapshot(state, name: str = "") -> MemoryChannel:
    """Capture the rig's current state as a new channel."""
    return MemoryChannel(
        name=(name or "").strip() or "Memory",
        freq=state.freq_a,
        mode=state.mode,
        power=state.power,
        sh=state.sh,
        att=state.att,
        preamp=state.preamp,
    )


def recall(rig, ch: MemoryChannel) -> None:
    """Write a channel back to the rig.

    Order is load-bearing in two directions, and they pull against each other:

    * `sh` (filter width) codes are clamped against whatever mode is
      *currently* selected, so `sh` must come after `mode`.
    * A frequency write that crosses into another band makes the radio restore
      that band's last-used mode from its band-stacking register, silently
      overwriting a mode set beforehand -- the same behaviour the A/B swap path
      already works around (see `FA/FB alone recall band-stack` in
      tests/test_freedv_session.py). So `mode` must come after `freq`.

    Hence freq -> mode -> sh. Setting mode first (as this did until
    2026-08-28) worked for same-band recalls and silently failed for every
    cross-band one: recalling an 80m LSB channel from 40m landed on the right
    frequency in DATA-U.
    """
    rig.set_freq(ch.freq)
    rig.set_mode(ch.mode)
    rig.set_sh(ch.sh)
    rig.set_power(ch.power)
    rig.set_att(ch.att)
    rig.set_preamp(ch.preamp)
