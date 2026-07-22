"""Persistent settings stored in %APPDATA%/CyberRig/settings.json (Windows)
or ~/.cyberrig/settings.json (Linux/Mac)."""

import json
import os
import sys
from pathlib import Path


def _config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", Path.home())
    else:
        base = Path.home()
    d = Path(base) / "CyberRig"
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULTS = {
    "cat_port":        "COM6",
    "cat_baud":        38400,
    "poll_interval":   0.15,
    "rigctl_port":     4532,
    "rigctl_enabled":  True,
    "ws_port":         4533,
    "ws_enabled":      True,
    "window_geometry": None,
    "theme":           "dark",
    "callsign":        "",     # set your call in Settings / settings.json
    # Remote tuner (e.g. MFJ-994BRT) — not the FTDX10 internal ATU
    "tune_watts":      20,
    "tune_mode":       "AM",   # AM carrier via CAT TX; continuous RF without key
    "tune_timeout_sec": 90,    # auto-stop safety
    # PO meter (RM5 0–255) → watts. Calibrate against an external wattmeter;
    # naive raw/255*100 often over-reads on SSB/USB-audio TX.
    "po_max_watts":    100,
    "po_cal":          0.67,
}


class Settings:
    def __init__(self):
        self._path = _config_dir() / "settings.json"
        self._data: dict = {**DEFAULTS}
        self.load()

    def load(self):
        if self._path.exists():
            try:
                on_disk = json.loads(self._path.read_text())
                self._data.update(on_disk)
            except Exception:
                pass

    def save(self):
        self._path.write_text(json.dumps(self._data, indent=2))

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        raise AttributeError(f"No setting: {name}")

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._data[name] = value

    def get(self, name: str, default=None):
        return self._data.get(name, default)
