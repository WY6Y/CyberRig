"""Persistent settings stored in %APPDATA%/CyberRig/settings.json (Windows)
or ~/CyberRig/settings.json (Linux/Mac)."""

import json
import os
import sys
from pathlib import Path

# Overrides where CyberRig keeps everything. This exists for the tests, and
# the tests need it badly: `cyberrig.web.app` builds its Settings at import,
# so without an override the API suite reads the OPERATOR'S live config and
# writes into the operator's real SSTV gallery. Both actually happened — the
# suite asserted sstv_watts == 30 and got 25 because that is what the station
# was last set to, and three synthetic "receptions" (SNR 32.9, three modes in
# the same second) sat in ~/CyberRig/sstv/rx/ among the real ones. A test must
# not be able to see, or change, the running station's state.
CONFIG_DIR_ENV = "CYBERRIG_HOME"


def _config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        d = Path(override)
    else:
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
    "freedv_grid":     "",     # Maidenhead locator — required to appear on FreeDV Reporter
    "freedv_status":   "",     # free-text Reporter status line
    # FreeDV TX — `freedv_watts` is the ALC THRESHOLD, not the output. It sits
    # well above the target so the ALC never engages, and `freedv_tx_level`
    # sets what actually goes out. FreeDV's waveform has a high peak-to-average
    # ratio, so any ALC action is the radio clipping peaks into IMD — aim for
    # ZERO on the ALC meter, not "barely moving". Clamped on enter and
    # restored on exit, exactly like SSTV.
    "freedv_watts":    100,
    # FreeDV TX audio drive, amplitude 0-1. This is the control that sets
    # output power. 0.030 gave ~30-40 W average with the ALC at zero on the
    # reference FTDX10; calibrate yours against a wattmeter.
    "freedv_tx_level": 0.030,
    # Guards, all on measured quantities. See cyberrig/freedv/guard.py.
    # 70 W, not 50: RADE speech peaks ~1.5-1.9x its average, so a lower
    # ceiling cuts correct transmissions. The guard also requires the excess
    # to persist (PO_TRIP_DWELL_S) — heat, not peaks, is what hurts the finals.
    "freedv_max_watts_out": 70,     # drop PTT if the radio RADIATES above this
    "freedv_max_swr":       3.0,    # drop PTT above this
    "freedv_max_over_s":    300,    # drop a PTT held this long (stuck-key catch)
    # SSTV — 100% duty for up to 269 s (Scottie DX), so entering the mode
    # clamps TX power and exit restores it. Hard ceiling is SSTV_MAX_WATTS.
    "sstv_watts":      30,
    "sstv_mode":       "Scottie 1",   # default TX mode; PD120 for best quality
    "sstv_max_swr":    3.0,           # abort a transmission above this
    "sstv_max_watts_out": 60,         # abort if the radio RADIATES above this
    # SSTV TX audio drive, amplitude 0-1. 0.01 is -40 dBFS and gave ~28 W
    # with the ALC at zero on the reference FTDX10. It is this low because
    # `sstv_watts` is the ALC threshold, not the output.
    "sstv_tx_level":   0.01,
    "sstv_fsk_id":     True,          # send the callsign in FSK after the picture
    "sstv_dir":        "",            # blank = <config dir>/sstv (see Settings.dir)
    # Is the FTDX10's own MODE-screen [PRESET] enabled? It bundles 14 FT8
    # settings, four of which this code also writes (LCUT, HCUT, TX BPF, REAR
    # SELECT — see modes/session.py PRESET_OWNED). When on, those four are
    # PRESET's and CyberRig leaves them alone.
    #
    # DECLARED, NOT DETECTED: the CAT reference has no opcode for PRESET's
    # state. While PRESET is on, the DATA menu holds the DORMANT copy, so
    # register reads describe settings that are not in force. Set True if
    # you operate with PRESET lit.
    "ftdx10_preset_active": False,
    # WSPR mode — auto-tune a remote ATU on every band hop, with a watchdog
    # supervising the unattended beacon. See cyberrig/wspr.py; nothing here
    # transmits until /api/wspr arms it.
    #
    # The SWR numbers are loose on purpose: some bands on a multiband wire
    # only reach ~2.5:1 and still work, so a tight accept would refuse them.
    "wspr_autotune":        False,   # arm state is not persisted; this is the default
    # NEGATIVE = below the dial. All this has to clear is a WSPR receiver's
    # audio passband. See cyberrig/wspr.py DEFAULT_OFFSET_HZ.
    "wspr_tune_offset_hz":  -5000,
    # Per-band tune frequencies that override the offset — the place to pin a
    # spot your tuner is known to like, e.g. {"20m": 14090000}.
    "wspr_tune_freq":       {},
    "wspr_tune_max_s":      12,      # give up on the tuner after this
    "wspr_tune_min_s":      3,       # never call it matched sooner than this
    "wspr_swr_accept":      2.8,     # a tune counts as converged at or under this
    "wspr_swr_trip":        3.2,     # sustained SWR above this drops PTT
    "wspr_max_tx_s":        125,     # a WSPR over is 110.6 s
    "wspr_session_max_hours": 12,    # hard stop; 0 disables
    "wspr_max_trips":       3,       # trips before the hard stop
    # Remote tuner (e.g. MFJ-994BRT) — not the FTDX10 internal ATU
    "tune_watts":      20,
    "tune_mode":       "AM",   # AM carrier via CAT TX; continuous RF without key
    "tune_timeout_sec": 90,    # auto-stop safety
    # PO meter (RM5 0–255) → watts. Calibrate against an external wattmeter;
    # naive raw/255*100 often over-reads on SSB/USB-audio TX.
    "po_max_watts":    100,
    "po_cal":          0.67,
    # RTL-SDR panadapter — EXPERIMENTAL, use at your own risk/hassle (see README).
    # Points at any rtl_tcp server (rtl_tcp -a <host>); set your own host/port
    # in settings.json, not here. Optionally shares the dongle with an external
    # decoder via a REST pause/resume API at cybersdr_api_url — leave that blank
    # if you don't have one.
    "rtl_host":        "",               # e.g. "sdr-host.local" — set in settings.json
    "rtl_port":        1234,
    "rtl_sample_rate": 2048000,          # ~2 MHz visible at modest LAN bandwidth;
                                          # FFT is throttled so CPU cost doesn't scale with this.
    "rtl_center_hz":   7150000,          # fallback center if used before the rig reports a VFO
    "cybersdr_api_url": "",              # optional; blank = no external decoder to pause/resume
    # Audio device selection (RX capture + TX playback). Substring matches,
    # case-insensitive. Deny wins over everything, including an explicit index.
    # Use it for any other USB codec on the machine that TX audio must never
    # reach (e.g. a DigiRig owned by another radio). Prefer is the escape hatch
    # for a radio codec with an unusual name.
    "audio_device_deny":   [],
    "audio_device_prefer": "",
    # Single-VFO mode. On some FTDX10s CyberRig has found the radio receiving
    # on VFO B with no CAT way to move it back (`VS;` is inert, like `SV;`).
    # While this is on, every frequency/mode write is mirrored onto B with
    # `AB;` and the SWAP / A>B / B>A controls are disabled. Incompatible with
    # rig-split operation (WSJT-X/JTDX split, Fox/Hound) — leave it off unless
    # your display and your receiver disagree.
    "single_vfo": False,
    # Native panel size. True = 1.33x, readable over VNC on a 1080p screen.
    # Qt can only apply QT_SCALE_FACTOR at process start, so toggling LARGE
    # saves this and restarts the panel (CAT blips).
    "panel_large_ui": False,
    # Remote SSB TX profiles. "usb" path = browser mic into the radio's rear
    # USB codec (software gain + radio RPORT level). "front" path = the radio's
    # own front mic; its MIC gain is the control and Remote TX stays disarmed.
    # Optional per-profile keys: `mic` pins the browser capture device by label
    # substring, `hpf`/`lpf` (Hz) band-limit it, `proc` sets the radio speech
    # processor level (False turns it off — right for a distant room mic).
    "tx_profile_id": "phone",
    "tx_profiles": [
        {"id": "phone",   "name": "Phone/laptop mic", "path": "usb", "gain": 1.00, "rport": 55},
        {"id": "earbuds", "name": "Earbuds", "path": "usb", "gain": 0.65, "rport": 40},
        {"id": "front",   "name": "Front-panel mic", "path": "front"},
        {"id": "headset", "name": "USB headset", "path": "usb", "gain": 1.00,
         "rport": 55, "mic": "Headset", "hpf": 120, "lpf": 3200, "proc": 20},
        {"id": "webcam",  "name": "Webcam mic", "path": "usb", "gain": 1.00,
         "rport": 55, "mic": "Webcam", "hpf": 150, "lpf": 3200, "proc": False},
    ],
}


class Settings:
    def __init__(self):
        self._path = _config_dir() / "settings.json"
        self._data: dict = {**DEFAULTS}
        self.load()

    @property
    def dir(self) -> Path:
        """Where CyberRig keeps its state. Everything on disk hangs off here —
        do not invent a second location for a new feature."""
        return self._path.parent

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
