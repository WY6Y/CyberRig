# CyberRig

**Self-hosted control for the Yaesu FTDX10, in a browser or a native window.**

CyberRig talks to the radio over CAT (USB serial) and gives you a full control
surface from any modern browser on your LAN or VPN, plus a fixed-geometry Qt panel
for the PC next to the radio. It goes beyond CAT control, with in-process FreeDV/RADE
digital voice, SSTV send and receive, an FT8 companion page for JTDX, remote SSB mic
TX, and WSPR auto-tune for an external tuner. One process owns the serial port and
shares it with everything else through a Hamlib-compatible rigctld.

> **Status: active development.** CyberRig is the daily driver of the FTDX10 at the
> station that built it, but it is **not a finished product**. APIs, UI layout and
> TX behaviour change without notice. Every transmit path has guards (see
> [Safety](#safety)), but you are the control operator.

---

## Features

| Area | What's included |
|------|-----------------|
| **Web UI** | Single-page control panel, no frontend build step, installable as a PWA |
| **Qt panel** | `panel.py` — fixed-geometry native panel (PySide6) sharing the same rig as the web UI; optional 1.33x LARGE mode for VNC |
| **Live state** | WebSocket pushes frequency, mode, meters and front-panel settings; front-panel changes track in the UI |
| **VFO** | Per-digit click, wheel step, type-in entry; optional single-VFO mirroring for radios that wander onto VFO B |
| **Receive chain** | ATT / IPO / preamp, roofing filter, width/shift, contour, notch, DNF, NB/NR, APF, AGC |
| **Meters** | S-meter; on TX: PO (calibratable), ALC, SWR, plus pop-out meter windows |
| **Internal ATU** | ATU on/off + TUNE start/abort (CAT `AC`) |
| **REMOTE TUNE** | Bounded carrier for an *external* auto-tuner (e.g. MFJ-994BRT), with SWR verification and one owner for the stop |
| **WSPR auto-tune** | Arms a watchdog: every band hop from your WSPR client tunes the external tuner off-frequency, checks SWR came down, then returns to the dial. Stuck TX or runaway SWR drops PTT |
| **FreeDV / RADE** | In-process RADE V1 modem at `/freedv` — no `freedv-gui` needed. Power ceiling, SWR trip, stuck-key catch, drive slider, optional FreeDV Reporter spotting |
| **SSTV** | `/sstv` — send (Scottie 1/2/DX, Martin 1/2, Robot 36, PD90/120/180, with FSK ID) and receive with a gallery; overlay templates with `{call}`, `{rst}`, `{grid}` macros, including a send-back card of the picture you received |
| **FT8 companion** | Separate process on port 8010: JTDX decode list with DXCC entity tagging, live waterfall from a shared ALSA capture, and operator-armed TX with an SWR trip, stuck-TX catch, cycle cap and dead-man |
| **Remote SSB TX** | Browser mic → radio USB codec with CAT PTT. Per-mic profiles (gain, RPORT level, HPF/LPF, speech processor) |
| **LAN audio** | RX listen stream to any browser |
| **Waterfall** | Narrowband USB-audio FFT with click-to-QSY; optional **experimental** RTL-SDR panadapter via `rtl_tcp` |
| **rigctld** | Hamlib NET rigctl on port **4532** for WSJT-X, JTDX, fldigi, VarAC, N1MM. Never serves a frequency the radio has not reported |
| **Parametric EQ** | TX DSP EQ + mic P-EQ, 3 bands each |
| **Macros** | JSON step sequences with `$CALL` / frequency substitution and a full editor |
| **Memory channels** | Named snapshots of freq/mode/power/filter/ATT/preamp, with a 6-slot quick-recall bar |
| **Radio power** | Turn the FTDX10 on and off over CAT (`PS`) |

---

## Requirements

- **Radio:** Yaesu **FTDX10** over USB (Enhanced port = CAT, USB codec = audio)
- **Host:** Python **3.11+** on the machine that owns the USB cable. Linux and
  Windows both work. FreeDV and the FT8 shared-capture setup are Linux-tested.
- **Browser:** current Chrome, Edge, Firefox or Safari. The remote mic needs a
  secure context (HTTPS or localhost)
- **Optional:**
  - `PySide6` for the Qt panel (`requirements-desktop.txt`)
  - `rade_c` built locally for FreeDV (see below)
  - JTDX and a `cty.dat` for the FT8 companion

---

## Quick start

```bash
git clone https://github.com/WY6Y/CyberRig.git
cd CyberRig
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py                     # web UI only, http://localhost:8000
```

Or the Qt panel with the web server embedded in the same process:

```bash
pip install -r requirements-desktop.txt
python panel.py                    # panel + web UI on :8000
python panel.py --no-web           # panel alone
```

Then:

1. Set the CAT port. Windows shows two FTDX10 ports: **Enhanced** = CAT (use this),
   **Standard** = PTT/CW. On Linux it is usually `/dev/ttyUSB0`. A stable
   `/dev/serial/by-id/...` path is better, because device numbers move.
2. Set your **callsign** (and grid, for FreeDV Reporter) in `settings.json`:
   `%APPDATA%\CyberRig\settings.json` on Windows, `~/CyberRig/settings.json`
   elsewhere. `CYBERRIG_HOME` overrides the location.
3. Calibrate `po_cal` against a real wattmeter before trusting the PO meter.

**Only one process may own the serial port.** Two processes on the same port
will not fail. They interleave writes and steal each other's replies, so you get
wrong readings instead of an error. Run `panel.py` *or* `main.py`, not both, and
point other software at rigctld on 4532.

The server listens on `0.0.0.0`. Put a reverse proxy with TLS in front of it, or
keep it on a VPN. **Do not** port-forward rig control to the internet.

---

## Ports

| Port | Service |
|------|---------|
| **8000** | Web UI, REST, WebSockets, `/freedv`, `/sstv`, `/waterfall`, `/eq`, `/macros-editor`, `/memories` |
| **4532** | Hamlib NET rigctl (WSJT-X/JTDX: Rig = *Hamlib NET rigctl*, server `localhost:4532`) |
| **8010** | FT8 companion (`python -m cyberrig.ft8.app`) |

Behind a reverse proxy, route `/ft8/*` to port 8010 with the prefix stripped.
The FT8 page works out its own base path.

---

## FreeDV / RADE

The `/freedv` page loads `librade_speech.so`, a thin wrapper around
[rade_c](https://github.com/freedv/rade_c) included in `cyberrig/freedv/native/`:

```bash
# after building rade_c in ~/src/rade_c
RADE_SRC=~/src/rade_c cyberrig/freedv/native/build.sh   # installs to ~/opt/freedv-rade/lib
```

Set `CYBERRIG_LIBRADE_SPEECH` to use another location. FreeDV needs the radio's
USB codec to itself, so close JTDX/VarAC first. Radio mode follows the VFO:
DATA-U above 10 MHz, DATA-L below.

## FT8 companion

The FT8 page does not replace JTDX. It reads JTDX's `ALL.TXT` for decodes, which
leaves UDP port 2237 free for GridTracker, and it draws its own waterfall from the
same RX audio. On Linux, share the codec's capture with ALSA dsnoop. Copy
[`docs/asound.conf.example`](docs/asound.conf.example) to `/etc/asound.conf` and
point JTDX's input at `ftdx10_rx`.

```bash
CYBERRIG_URL=http://127.0.0.1:8000 python -m cyberrig.ft8.app
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `CYBERRIG_URL` | `http://127.0.0.1:8000` | Where the main CyberRig server is |
| `FT8_PORT` | `8010` | Listen port |
| `FT8_AUDIO_DEVICE` | `ftdx10_rx` | Capture device for the waterfall |
| `JTDX_LOG_DIR` | JTDX's default | Where `ALL.TXT` lives |
| `CYBERRIG_CTY_DAT` | searches JTDX/WSJT-X installs | Path to `cty.dat` (AD1C) for entity tagging |
| `FT8_JTDX_UDP` | `0` | `1` claims JTDX's UDP port for Reply/HaltTx. Off by default so GridTracker keeps it |
| `JTDX_UDP_HOST` / `JTDX_UDP_PORT` | `127.0.0.1` / `2237` | JTDX UDP server |
| `ATNO_URL` | *(empty)* | Optional service returning confirmed/needed DXCC entities to tag decodes as "needed" |

---

## Settings

Everything lives in `settings.json`, and defaults are in `cyberrig/settings.py`
along with the reasoning behind each one. The ones you will most likely change:

| Key | Default | Notes |
|-----|---------|-------|
| `cat_port` | `COM6` | Serial device for CAT |
| `callsign` / `freedv_grid` | *(empty)* | Your call and Maidenhead grid |
| `po_cal` | `0.67` | PO meter scale. Calibrate with a wattmeter |
| `freedv_tx_level` / `sstv_tx_level` | `0.030` / `0.01` | Audio drive that sets output power in those modes. Aim for ALC at zero |
| `tx_profiles` / `tx_profile_id` | example profiles | Remote-mic profiles: `gain`, `rport`, optional `mic`, `hpf`, `lpf`, `proc` |
| `audio_device_deny` | `[]` | Substrings of audio devices TX must never use, e.g. a second USB codec that belongs to another radio |
| `single_vfo` | `false` | Mirror every write to VFO B. Turn on only if the radio receives on B and you can't move it back. Incompatible with rig split |
| `ftdx10_preset_active` | `false` | Set to `true` if you keep the FTDX10's MODE-screen PRESET on. CyberRig then leaves the four menu items it owns alone |
| `wspr_*`, `tune_*` | see file | External tuner and WSPR watchdog thresholds |
| `rtl_host` | *(empty)* | **Experimental** RTL-SDR panadapter via `rtl_tcp`. Blank turns it off |

---

## FTDX10 CAT notes

Hand-checked against Yaesu CAT Operation Manual **2308-F**. Don't assume a generic
Hamlib command list is right for this radio.

- Serial: **38400 8N2** (two stop bits)
- Mode names: `CW-U` / `CW-L`
- PTT: `TX1;` / `TX0;`. Split: `ST` (not `SP`)
- EX menus: `EX{p1:02d}{p2:02d}{p3:02d}{value};`
- `VS;` and `SV;` are accepted and do nothing. `AB;` (A→B) works
- The S-meter raw scale is 0–50 with S9 = 18, not 0–30
- A write to a menu address the radio lacks neither errors nor replies. Read it
  back and compare
- With MODE-screen PRESET on, the DATA menu holds the *dormant* copy of those settings

The module docstrings and comments record what was learned on the air, including
dates and the failure that taught it. Read them before changing a guard.

---

## Safety

- **You are the control operator.** Every TX path here can put RF on the air.
- Test transmit, remote mic, TUNE and FreeDV into a **dummy load** first.
- Guards are on measured quantities. FreeDV and SSTV clamp power on entry and
  restore it on exit, and trip on SWR and radiated watts. FT8 TX needs an
  explicit arm and disarms on SWR, stuck TX, a cycle cap, or the dead-man (the
  browser page went away). REMOTE TUNE and WSPR mode have hard timeouts.
- WSPR mode is an unattended beacon. Know your local rules for unattended
  operation before arming it.
- Close other apps holding the CAT port or the USB codec before starting.
- The RTL-SDR panadapter is **experimental** and depends on your dongle and
  `rtl_tcp` setup. It is off unless you set `rtl_host`.

---

## Development

```bash
pip install pytest
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

Tests run against a fake radio and a temporary `CYBERRIG_HOME`, so they never
touch a real station's settings. PySide6 is needed for the panel tests.

If something breaks on your radio, open an issue with your OS, Python version,
CAT device, what you clicked, and the server log lines.

---

## License and credits

MIT. See [LICENSE](LICENSE). Written by WY6Y.

Bundled fonts (Share Tech Mono, VT323, Rajdhani) are under the SIL Open Font
License 1.1. RADE is by the FreeDV project (David Rowe VK5DGR and contributors), and
`rade_c` is BSD-2-Clause. Build it from their repository. `cty.dat` is by AD1C and is not
bundled.

Yaesu and FTDX10 are trademarks of their respective owners. This project is not
affiliated with or endorsed by Yaesu Musen Co., Ltd.
