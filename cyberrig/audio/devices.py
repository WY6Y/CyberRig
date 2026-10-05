"""Shared audio-device selection for the RX capture and TX playback engines.

Both engines used to carry their own near-identical scorer, written when
CyberRig only ever ran on a Windows shack PC where the FTDX10 enumerates with
"Yaesu" in the name. On Linux it does not: it appears as

    USB AUDIO  CODEC: Audio (hw:2,0)      ← the radio (BurrBrown/TI)
    USB Audio Device: Audio (hw:1,0)      ← a C-Media DigiRig

and the old scoring gave both the same +40 for containing "USB AUDIO", so the
auto-pick was decided by enumeration order. On the station PC the DigiRig is
Asterisk's and must never be opened — a tie there means TX audio going into
AllStar. Hence two changes kept in one place:

* "CODEC" scores well above a bare "USB AUDIO", so the radio wins on merit.
* A deny list (settings ``audio_device_deny``) hard-excludes a device by name,
  even when asked for explicitly. Wrong-device TX is not worth a polite
  warning.

``audio_device_prefer`` is the escape hatch: any substring match there beats
everything else, for a station whose device names look nothing like these.
"""

import logging
from typing import Optional, Sequence

log = logging.getLogger(__name__)

#: Names never selected automatically or explicitly. Substring, case-folded.
DEFAULT_DENY: tuple = ("USB Audio Device",)

#: A device must score at least this to be auto-selected.
MIN_AUTO_SCORE = 40


def _settings_list(key: str) -> tuple:
    try:
        from cyberrig.settings import Settings
        raw = Settings().get(key)
    except Exception:
        return ()
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [p for p in raw.split(",")]
    return tuple(str(p).strip() for p in raw if str(p).strip())


def deny_patterns() -> tuple:
    configured = _settings_list("audio_device_deny")
    return configured if configured else DEFAULT_DENY


def prefer_patterns() -> tuple:
    return _settings_list("audio_device_prefer")


def is_denied(name: str, deny: Optional[Sequence[str]] = None) -> bool:
    n = (name or "").upper()
    for pat in (deny if deny is not None else deny_patterns()):
        if pat.upper() in n:
            return True
    return False


def score(name: str, hostapi: str = "", *, side: str = "") -> int:
    """Higher = better pick for FTDX10 audio. Denied devices score below zero.

    ``side`` is ``"rx"``, ``"tx"``, or empty. The the station PC dsnoop PCM
    ``ftdx10_rx`` is capture-only and must beat the raw CODEC entry for RX
    (that entry vanishes from PortAudio while JTDX holds ``hw:2,0``). It is
    never a TX pick — checked before the bare ``FTDX`` substring, which
    would otherwise match ``FTDX10_RX``.
    """
    n = (name or "").upper()
    ha = (hostapi or "").upper()
    if is_denied(n):
        return -1000

    s = 0
    for pat in prefer_patterns():
        if pat.upper() in n:
            s += 200
            break
    if "FTDX10_RX" in n:
        if side != "tx":
            # 200 beats USB AUDIO CODEC (80) and a Windows "Yaesu" name (180).
            s += 200
    elif "YAESU" in n or "FTDX" in n or "FT-DX" in n:
        s += 100
    # The FTDX10's own codec reports as "USB AUDIO  CODEC" on Linux. Keep this
    # worth more than a bare "USB AUDIO", which any USB dongle also matches.
    if "CODEC" in n:
        s += 60
    if "USB AUDIO" in n:
        s += 20

    # Windows host APIs, in the order they behave best for low-latency audio.
    if "WASAPI" in ha:
        s += 30
    elif "WDM-KS" in ha:
        s += 20
    elif "DIRECTSOUND" in ha or "WINDOWS DIRECTSOUND" in ha:
        s += 20
    elif "MME" in ha:
        s += 5

    if "MAPPER" in n or "PRIMARY SOUND" in n or "PRIMARY" in n:
        s -= 50
    if "REALTEK" in n or "MICROPHONE" in n or "HDA INTEL" in n:
        s -= 20
    return s
