# CyberRig

**Browser-based control for the Yaesu FTDX10.**

CyberRig is a self-hosted web app that talks to the radio over CAT (USB serial) and
gives you a full control surface in any modern browser: VFO, mode, meters, filters,
RX/TX options, macros, and more. It is meant as an open, hackable alternative to
closed desktop controllers.

> **Status: active development (WIP)**  
> CyberRig is usable on a real FTDX10 in the shack, but it is **not a finished
> product**. APIs, UI layout, and remote-TX behaviour can change without notice.
> Expect rough edges. Contributions and bug reports are welcome; please do not
> treat this as turnkey commercial software.

---

## Features

| Area | What’s included |
|------|-----------------|
| **Web UI** | Single-page control panel (no frontend build step) |
| **Live state** | WebSocket pushes frequency, mode, meters, and front-panel settings |
| **Bi-directional CAT** | App → radio *and* radio → app (front-panel changes track in the UI) |
| **VFO** | Per-digit click (top half +, bottom half −), wheel step, TYPE / long-press for MHz entry |
| **Meters** | S-meter; on TX: PO (calibratable), ALC, SWR |
| **Internal ATU** | Built-in FTDX10 tuner: **ATU** on/off + **TUNE** start/abort (CAT `AC`) |
| **REMOTE TUNE** | Carrier for an *external* auto-tuner (e.g. MFJ) — separate from internal ATU |
| **Waterfall** | Separate window: USB-audio FFT + click-to-QSY |
| **LAN / VPN audio** | Optional RX listen stream; experimental remote mic TX |
| **rigctld** | Hamlib-compatible TCP on port **4532** for WSJT-X, fldigi, N1MM, etc. |
| **Macros** | JSON step sequences with `$CALL` / frequency substitution |
| **PWA** | Installable shell (icons + service worker; live API never cached) |

Legacy PySide6 desktop code lives under `cyberrig/ui_legacy/` and is **not** the
primary path anymore.

---

## Requirements

- **Radio:** Yaesu **FTDX10** over USB (Enhanced port = CAT)
- **Host:** Python **3.11+** on the machine that owns the USB cable  
  (typically a Windows shack PC; Linux works if the serial device is exposed)
- **Browser:** current Chrome, Edge, Firefox, or Safari on the same LAN or VPN
- **Optional:** sound device for waterfall / listen / remote TX (USB audio CODEC)

---

## Quick start

On the PC attached to the radio:

```bash
git clone git@github.com:WY6Y/CyberRig.git
cd CyberRig
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
# source .venv/bin/activate

pip install -r requirements.txt
python main.py
```

Open **http://localhost:8000**

1. Confirm the CAT **COM port** (Windows Device Manager → Ports).  
   FTDX10 creates two ports: **Enhanced** = CAT (use this), **Standard** = PTT/CW (leave alone).
2. Set the port in the UI if it is not already correct (saved under
   `%APPDATA%\CyberRig\settings.json` on Windows, or `~/CyberRig/settings.json` elsewhere).
3. Set your **callsign** in settings before using CW macros that expand `$CALL`.

Default listen address: `0.0.0.0:8000`. Put a reverse proxy and TLS in front if you
expose it beyond localhost (Tailscale, WireGuard, etc.). **Do not** port-forward
raw rig control to the open internet.

---

## Ports

| Port | Service |
|------|---------|
| **8000** | Web UI, REST, WebSocket (`/ws`), waterfall (`/waterfall`), audio WS |
| **4532** | `rigctld`-style Hamlib TCP (when enabled) |

WSJT-X example: Rig = *Hamlib NET rigctl*, Network server = `localhost:4532`.

---

## Project layout

```
CyberRig/
├── main.py                 # uvicorn entry (port 8000)
├── requirements.txt
├── cyberrig/
│   ├── cat/ftdx10.py       # FTDX10 CAT driver (Yaesu CAT OM 2308-F)
│   ├── cat/menus.py        # EX menu tables
│   ├── web/app.py          # FastAPI + WebSocket + REST
│   ├── web/static/         # UI, PWA, waterfall, audio/TX helpers
│   ├── audio/              # RX FFT waterfall + optional TX USB audio
│   ├── macros/             # Macro model / store / runner
│   ├── server/rigctl.py    # Hamlib NET rigctl bridge
│   ├── settings.py         # Persistent settings
│   └── ui_legacy/          # Archived desktop UI (not required for web)
└── README.md
```

---

## Settings

| Key | Default | Notes |
|-----|---------|--------|
| `cat_port` | `COM3` | Serial device for CAT (e.g. `COM6`, `/dev/ttyUSB0`) |
| `cat_baud` | `38400` | FTDX10 fixed rate |
| `poll_interval` | `0.3` | Seconds between CAT poll cycles |
| `rigctl_port` | `4532` | Hamlib TCP port |
| `callsign` | *(empty)* | Used in macro `$CALL` expansion |
| `tune_watts` | `20` | REMOTE TUNE power (external tuner) |
| `tune_mode` | `AM` | Carrier mode for external tune |
| `tune_timeout_sec` | `90` | Auto-stop safety for REMOTE TUNE |
| `po_max_watts` | `100` | Full-scale assumption for PO bar |
| `po_cal` | `0.67` | Scale factor — calibrate with a real wattmeter |

---

## FTDX10 CAT notes

Hand-checked against Yaesu CAT Operation Manual **2308-F**. Do not assume generic
Hamlib command lists are correct for this radio:

- Serial: **38400 8N2** (two stop bits)
- Mode names: `CW-U` / `CW-L` (not plain `CW` / `CWR` in CAT)
- PTT: `TX1;` on, `TX0;` off
- Split: `ST` (not `SP`)
- Filter width: set `SH00{nn};` (both P1 and P2 zeros)
- EX menus: `EX{p1:02d}{p2:02d}{p3:02d}{value};` (six-digit prefix)
- No `SL` lo-cut command on FTDX10

---

## Safety (please read)

- **You are the control operator.** Remote keying can put RF on the air.
- Prefer a **dummy load** while testing transmit, remote mic, or REMOTE TUNE.
- Do not leave unattended automatic TX running.
- **ATU / TUNE** drives the radio’s **built-in** tuner and **keys the radio** during
  a match — use a safe antenna or dummy load.
- **REMOTE TUNE** is a separate control for an **external** tuner carrier path
  (e.g. MFJ); it is not the internal ATU.
- Remote SSB mic TX is **experimental** — latency, ALC, and USB MOD path settings
  still need care. Close Win4Yaesu (or anything else holding the CAT COM port)
  before starting CyberRig.

---

## Development status

**Working well enough for shack use (as of mid‑2026):** CAT control, live meters,
front-panel setting sync, REMOTE TUNE, waterfall, LAN RX listen, PWA shell, first
live QSOs from the web path.

**Still cooking:** remote mic TX polish, bulletproof Windows autostart, packaging,
broader documentation, and general hardening for third-party stations.

If something breaks on your radio or OS, open an issue with: OS, Python version,
COM port / device name, and what you clicked. Screenshots of the UI and any
console / server log lines help a lot.

---

## License

MIT — see [LICENSE](LICENSE).

Yaesu and FTDX10 are trademarks of their respective owners. This project is not
affiliated with or endorsed by Yaesu Musen Co., Ltd.
