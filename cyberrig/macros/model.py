"""Macro data model: MacroStep and Macro dataclasses."""

from dataclasses import dataclass, field
from typing import Any


# Step type registry:
#   cat       — send raw CAT string, e.g. "NB01;"
#   delay     — wait ms milliseconds
#   set_freq  — QSY to hz Hz
#   set_mode  — change to mode (LSB/USB/CW-U/...)
#   set_power — set TX power in watts
#   cw        — send CW text (supports vars: $CALL $FREQ_A $MODE)
#   ptt_on    — assert PTT (TX on)
#   ptt_off   — release PTT (TX off)
#   comment   — documentation only, never executed

STEP_TYPES = [
    "cat", "delay", "set_freq", "set_mode", "set_power",
    "cw", "ptt_on", "ptt_off", "comment",
]

# Variable tokens expanded at runtime from rig state
MACRO_VARS = {
    "$FREQ_A": "VFO-A frequency in kHz (e.g. 14.200)",
    "$MODE":   "Current mode (e.g. USB)",
    "$CALL":   "Station callsign from settings (e.g. W1AW)",
}


@dataclass
class MacroStep:
    stype: str            # one of STEP_TYPES
    # Populated depending on stype:
    cmd:    str = ""      # cat: raw CAT string (with semicolon)
    ms:     int = 100     # delay: milliseconds
    hz:     int = 0       # set_freq: Hz
    mode:   str = ""      # set_mode: mode string
    watts:  int = 50      # set_power: watts
    text:   str = ""      # cw / comment: text (cw supports $VARS)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "MacroStep":
        s = cls(stype=d.get("stype", "comment"))
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s

    def summary(self) -> str:
        if self.stype == "cat":       return f"CAT: {self.cmd}"
        if self.stype == "delay":     return f"⏱ {self.ms} ms"
        if self.stype == "set_freq":  return f"QSY {self.hz/1e6:.4f} MHz"
        if self.stype == "set_mode":  return f"Mode → {self.mode}"
        if self.stype == "set_power": return f"Power → {self.watts} W"
        if self.stype == "cw":        return f"CW: {self.text}"
        if self.stype == "ptt_on":    return "PTT ON"
        if self.stype == "ptt_off":   return "PTT OFF"
        if self.stype == "comment":   return f"# {self.text}"
        return self.stype


@dataclass
class Macro:
    name:    str
    key:     str = ""          # optional keyboard shortcut, e.g. "F5"
    color:   str = "#1a4a2a"   # button background color
    steps:   list[MacroStep] = field(default_factory=list)
    desc:    str = ""

    def to_dict(self) -> dict:
        return {
            "name":  self.name,
            "key":   self.key,
            "color": self.color,
            "desc":  self.desc,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Macro":
        m = cls(name=d.get("name", "Unnamed"))
        m.key   = d.get("key", "")
        m.color = d.get("color", "#1a4a2a")
        m.desc  = d.get("desc", "")
        m.steps = [MacroStep.from_dict(s) for s in d.get("steps", [])]
        return m
