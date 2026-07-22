"""Macro store — JSON persistence + built-in default macros."""

import json
from cyberrig.macros.model import Macro, MacroStep
from cyberrig.settings import _config_dir


def _macros_file():
    return _config_dir() / "macros.json"

DEFAULT_MACROS: list[Macro] = [
    Macro(
        name="40m SSB Call",
        key="F1",
        color="#0d2a4a",
        desc="QSY to 40m, set SSB, send CQ CQ CQ DE $CALL",
        steps=[
            MacroStep("set_freq",  hz=7200000),
            MacroStep("set_mode",  mode="USB"),
            MacroStep("set_power", watts=100),
            MacroStep("delay",     ms=500),
            MacroStep("ptt_on"),
            MacroStep("cw",        text="CQ CQ CQ DE $CALL $CALL $CALL K"),
            MacroStep("ptt_off"),
        ],
    ),
    Macro(
        name="20m CW CQ",
        key="F2",
        color="#1a2d0a",
        desc="CQ on 20m CW",
        steps=[
            MacroStep("set_freq",  hz=14025000),
            MacroStep("set_mode",  mode="CW-U"),
            MacroStep("delay",     ms=300),
            MacroStep("cw",        text="CQ CQ DE $CALL $CALL K"),
        ],
    ),
    Macro(
        name="Quick Split",
        key="F3",
        color="#2a1a0a",
        desc="Enable split, TX +5 kHz above RX",
        steps=[
            MacroStep("cat", cmd="ST1;"),
            MacroStep("comment", text="TX is now on VFO-B (auto-offset from QS)"),
        ],
    ),
    Macro(
        name="RIT Clear",
        key="F4",
        color="#1a0a2a",
        desc="Clear RIT offset",
        steps=[
            MacroStep("cat", cmd="RC;"),
        ],
    ),
    Macro(
        name="Monitor ON",
        key="",
        color="#0a2a1a",
        desc="Enable TX monitor at 50%",
        steps=[
            MacroStep("cat", cmd="ML00001;"),
            MacroStep("cat", cmd="ML10050;"),
        ],
    ),
]


def load_macros() -> list[Macro]:
    MACROS_FILE = _macros_file()
    if not MACROS_FILE.exists():
        return list(DEFAULT_MACROS)
    try:
        with open(MACROS_FILE) as f:
            data = json.load(f)
        return [Macro.from_dict(d) for d in data]
    except Exception:
        return list(DEFAULT_MACROS)


def save_macros(macros: list[Macro]):
    MACROS_FILE = _macros_file()
    MACROS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MACROS_FILE, "w") as f:
        json.dump([m.to_dict() for m in macros], f, indent=2)


def reset_to_defaults() -> list[Macro]:
    macros = list(DEFAULT_MACROS)
    save_macros(macros)
    return macros
