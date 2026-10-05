"""CyberRig FastAPI backend — WebSocket state push + REST control endpoints."""

import asyncio
import base64
import io
import json
import logging
import math
import time
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cyberrig.audio.rig_codec import codec as rig_codec
from cyberrig.audio.tx_audio import tx_engine
from cyberrig.audio.waterfall import engine as wf_engine
from cyberrig.audio.rtl_panadapter import engine as rtl_engine
from cyberrig.cat.ftdx10 import FTdx10, REOPEN_INTERVAL, sh_hz
from cyberrig.cat import meter_cal
from cyberrig.macros.engine import MacroRunner
from cyberrig.macros.model import Macro
from cyberrig.macros.store import load_macros, save_macros, reset_to_defaults as reset_macros_to_defaults
from cyberrig.memories.model import MemoryChannel
from cyberrig.memories.store import load_memories, save_memories
from cyberrig.memories.ops import recall as recall_channel, snapshot
from cyberrig.server.rigctl import RigctlServer
from cyberrig.settings import DEFAULTS, Settings
from cyberrig.tune import RemoteTuner
from cyberrig.wspr import WsprAutoTune
from cyberrig.freedv.session import FreeDVSession
from cyberrig.freedv.engine import engine as fdv_engine
from cyberrig.freedv.modem import available_modes as fdv_available_modes
from cyberrig.freedv.reporter import reporter as fdv_reporter
from cyberrig.freedv.guard import FreeDVTxGuard
from cyberrig.freedv.session import data_mode_for_hz
from cyberrig.modes.session import active_session
from cyberrig.sstv.modes import mode_list as sstv_mode_list
from cyberrig.sstv.session import SSTVSession
from cyberrig.sstv.templates import TemplateStore, macro_context
from cyberrig.sstv.rx import RxStore, SSTVReceiver
from cyberrig.sstv.tx import SSTVTransmitter

log = logging.getLogger("cyberrig.web")

_STATIC = Path(__file__).parent / "static"

app = FastAPI(title="CyberRig")
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# ── Globals ────────────────────────────────────────────────────────────────
settings = Settings()
rig = FTdx10()
runner: Optional[MacroRunner] = None
macros = load_macros()
memories = load_memories()
_rigctld: Optional[RigctlServer] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_wf_clients: list[WebSocket] = []
_audio_clients: list[WebSocket] = []
_fdv_clients: list[WebSocket] = []

# Remote-tuner TUNE (MFJ-994BRT etc.) — NOT the radio internal ATU (AC cmd).
# Owned by cyberrig/tune.py so the Qt panel's OPS TUNE button and this server
# share one state, and therefore one set of PTT/ATU/power interlocks. Created
# in _startup(), once `rig` and `settings` exist.
remote_tuner: Optional[RemoteTuner] = None  # built below, once rig exists


# ── WebSocket manager ──────────────────────────────────────────────────────
class _WSManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        await self._send_one(ws, _state_dict())

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        msg = json.dumps(data)
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)

    async def _send_one(self, ws: WebSocket, data: dict):
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            if ws in self.active:
                self.active.remove(ws)


manager = _WSManager()


def _state_dict() -> dict:
    s = rig.state
    return {
        "connected": rig.is_connected,
        "freq_a": s.freq_a,
        "freq_b": s.freq_b,
        "mode": s.mode,
        "smeter": s.smeter,
        "is_tx": s.is_tx,
        "power": s.power,
        "power_meter": s.power_meter,
        "alc_meter": s.alc_meter,
        "swr_meter": s.swr_meter,
        "split": s.split,
        "fine": s.fine,
        "tx_vfo": s.tx_vfo,
        "rit": s.rit,
        "xit": s.xit,
        "rit_offset": s.rit_offset,
        "sh": s.sh,
        "sh_hz": sh_hz(s.sh, s.mode),
        "if_shift": s.if_shift,
        "af_gain": s.af_gain,
        "rf_gain": s.rf_gain,
        "agc": s.agc,
        "preamp": s.preamp,
        "att": s.att,
        "nb": s.nb,
        "nb_level": s.nb_level,
        "nr": s.nr,
        "nr_level": s.nr_level,
        "dnf": s.dnf,
        "notch": s.notch,
        "notch_pos": s.notch_pos,
        "contour": s.contour,
        "contour_freq": s.contour_freq,
        "contour_level": s.contour_level,
        "contour_width": s.contour_width,
        "roofing": s.roofing,
        "notch_width": s.notch_width,
        "apf": s.apf,
        "apf_freq": s.apf_freq,
        "compressor": s.compressor,
        "comp_level": s.comp_level,
        "vox": s.vox,
        "vox_gain": s.vox_gain,
        "vox_delay": s.vox_delay,
        "monitor": s.monitor,
        "mon_level": s.mon_level,
        "cw_speed": s.cw_speed,
        "cw_pitch": s.cw_pitch,
        "cw_breakin": s.cw_breakin,
        "cw_delay": s.cw_delay,
        "antenna": s.antenna,
        "locked": s.locked,
        "mic_gain": s.mic_gain,
        "atu": s.atu,
        "atu_tuning": s.atu_tuning,
        "radio_on": s.radio_on,
        "macro_running": runner.is_running() if runner else False,
        "tune_active": _tune_is_active(),
        "tune_watts": int(settings.get("tune_watts", 20)),
        "tune_mode": settings.get("tune_mode", "AM"),
        "tune_timeout_sec": int(settings.get("tune_timeout_sec", 90)),
        "wspr": wspr.status(),
        "po_max_watts": int(settings.get("po_max_watts", 100)),
        "po_cal": float(settings.get("po_cal", 0.67)),
    }


def _broadcast():
    """Schedule a WebSocket broadcast from any thread."""
    if _event_loop and not _event_loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(_state_dict()), _event_loop
        )


# Built here rather than in _startup() so `python panel.py --no-web`, which
# imports this module without ever running the server's startup handler, still
# gets a working TUNE button. _broadcast() is a no-op until the loop exists.
remote_tuner = RemoteTuner(rig, settings, on_change=_broadcast)

# WSPR mode: auto-tune on band hop + the watchdog for the unattended beacon.
# Built here so it exists before _startup() hands it to the rigctld server as
# its gate. Disarmed until POST /api/wspr — importing this module must never
# put anything in the path of a transmission.
wspr = WsprAutoTune(rig, settings, remote_tuner, on_change=_broadcast)

freedv_session = FreeDVSession(rig, rig_codec, watts=settings.get("freedv_watts"))
fdv_engine.set_tx_level(settings.get("freedv_tx_level") or 0.05)


def _fdv_trip(reason: str):
    """A FreeDV guard tripped: get off the air, then tell every page why."""
    try:
        fdv_engine.set_ptt(False)
    except Exception:
        log.exception("freedv trip: engine unkey")
    try:
        rig.set_ptt(False)
    except Exception:
        log.exception("freedv trip: rig unkey")
    try:
        fdv_reporter.transmit(False, "RADEV1")
    except Exception:
        pass


fdv_guard = FreeDVTxGuard(rig, settings, on_trip=_fdv_trip, on_change=_broadcast)


def _fdv_key(on: bool):
    """The one place FreeDV keys the radio.

    PTT was set from three call sites (REST, websocket, cleanup) and the
    guards would be decorative if any of them could bypass them — the same
    reason SSTV keys through a single transmitter object.
    """
    on = bool(on)
    fdv_engine.set_ptt(on)
    rig.set_ptt(on)
    fdv_reporter.transmit(on, "RADEV1")
    if on:
        fdv_guard.start()
    else:
        fdv_guard.stop()


sstv_session = SSTVSession(rig, rig_codec, watts=settings.get("sstv_watts"))
sstv_tx = SSTVTransmitter(rig, rig_codec, sstv_session, settings,
                          on_change=_broadcast)
_sstv_dir = Path(settings.get("sstv_dir") or (settings.dir / "sstv"))
sstv_templates = TemplateStore(_sstv_dir / "templates")
sstv_store = RxStore(_sstv_dir / "rx")
sstv_rx = SSTVReceiver(rig_codec, sstv_store, rig, on_change=_broadcast)


# ── Startup / shutdown ────────────────────────────────────────────────────
def _wf_push_row(msg: dict):
    """Called from waterfall FFT thread — schedule broadcast to /ws/waterfall clients."""
    if not _event_loop or _event_loop.is_closed():
        return
    # Attach live VFO so clients can QSY without an extra poll
    payload = {
        **msg,
        "vfo": rig.state.freq_a,
        "mode": rig.state.mode,
        "connected": rig.is_connected,
    }
    asyncio.run_coroutine_threadsafe(_wf_broadcast(payload), _event_loop)


def _wf_push_pcm(pcm: bytes, rate: int):
    """Called from capture thread — binary PCM to /ws/audio clients."""
    if not _event_loop or _event_loop.is_closed() or not _audio_clients:
        return
    asyncio.run_coroutine_threadsafe(_audio_broadcast(pcm), _event_loop)


def _fdv_push_pcm(pcm: bytes, rate: int):
    if not _event_loop or _event_loop.is_closed() or not _fdv_clients:
        return
    asyncio.run_coroutine_threadsafe(_fdv_broadcast_pcm(pcm), _event_loop)


async def _fdv_broadcast_pcm(pcm: bytes):
    dead = []
    for ws in list(_fdv_clients):
        try:
            await ws.send_bytes(pcm)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _fdv_clients:
            _fdv_clients.remove(ws)


async def _fdv_broadcast_json(payload: dict):
    text = json.dumps(payload)
    dead = []
    for ws in list(_fdv_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _fdv_clients:
            _fdv_clients.remove(ws)


async def _wf_broadcast(payload: dict):
    if not _wf_clients:
        return
    text = json.dumps(payload)
    dead = []
    for ws in list(_wf_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _wf_clients:
            _wf_clients.remove(ws)


def _cybersdr_post(path: str):
    """Best-effort pause/resume call into CyberSDR's WSPR decoder API.

    CyberSDR itself fails fast with no retry on a dropped rtl_tcp connection,
    so a missed call here just means WSPR resumes on its own next cycle, or
    the panadapter proceeds without having freed the dongle first — swallow
    everything rather than blocking the panadapter start/stop on it.
    """
    base = settings.get("cybersdr_api_url", "")
    if not base:
        return
    try:
        req = urllib.request.Request(base.rstrip("/") + path, data=b"", method="POST")
        urllib.request.urlopen(req, timeout=2.0)
    except Exception as e:
        log.warning("cybersdr %s failed (continuing): %s", path, e)


def _maybe_follow_vfo(hz: int):
    """Soft-follow: only recenter the RTL panadapter when the VFO nears/leaves the
    edge of the currently displayed span, not on every single freq_changed tick.

    Hard-follow (recenter on every change) was tried first — with the panadapter's
    span this small relative to how fast CAT polling reports frequency changes
    while tuning, it made the axis/labels visibly shift on every tick ("constantly
    changing"). Soft-follow keeps the view stable while the VFO moves within it,
    matching how a normal SDR panadapter behaves.
    """
    st = rtl_engine.status()
    if not st.get("running"):
        return
    center = st.get("center_hz") or 0
    sr = st.get("sample_rate") or 0
    if not sr:
        return
    half = sr / 2
    margin = sr * 0.1  # recenter once within 10% of either edge, or fully outside
    if hz < (center - half + margin) or hz > (center + half - margin):
        rtl_engine.set_center(hz)


async def _release_ws_source(src: str, tag: str):
    """Release whichever engine `src` refers to; hand the RTL dongle back to CyberSDR
    only once EVERY holder has released (not on each individual disconnect — another
    connection/tab may still be actively viewing the panadapter)."""
    if src == "rtl":
        result = rtl_engine.release(tag)
        if not result.get("running"):
            await asyncio.to_thread(_cybersdr_post, "/api/decoder/start")
    else:
        wf_engine.release(tag)


async def _audio_broadcast(pcm: bytes):
    if not _audio_clients:
        return
    dead = []
    for ws in list(_audio_clients):
        try:
            await ws.send_bytes(pcm)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _audio_clients:
            _audio_clients.remove(ws)


@app.on_event("startup")
async def _startup():
    global _event_loop, runner, _rigctld
    _event_loop = asyncio.get_running_loop()

    for event in [
        "connected_changed", "freq_changed", "freq_b_changed", "mode_changed",
        "smeter_update", "meter_update", "ptt_changed", "split_changed",
        "rit_changed", "xit_changed", "filter_changed", "agc_changed",
        "preamp_changed", "att_changed", "nb_changed", "nr_changed",
        "dnf_changed", "notch_changed", "contour_changed", "apf_changed", "comp_changed",
        "vox_changed", "cw_changed", "af_changed", "rf_changed", "antenna_changed",
        "power_changed", "mic_changed", "monitor_changed", "lock_changed",
        "atu_changed", "radio_power_changed", "roofing_changed",
    ]:
        rig.on(event, lambda *_: _broadcast())

    runner = MacroRunner(rig, settings.callsign)
    wf_engine.on_row(_wf_push_row)
    wf_engine.on_pcm(_wf_push_pcm)
    rtl_engine.on_row(_wf_push_row)
    fdv_engine.on_pcm(_fdv_push_pcm)
    fdv_reporter.configure(
        settings.callsign or "",
        settings.get("freedv_grid") or "",
        settings.get("freedv_status") or "",
    )
    fdv_reporter.start()
    rig.on("freq_changed", lambda hz: _maybe_follow_vfo(int(hz)))
    rig.on("freq_changed", lambda hz: _fdv_on_freq(int(hz)))
    rig.on("freq_changed", lambda hz: _digital_follow_sideband(int(hz)))

    rig.single_vfo = bool(settings.get("single_vfo", True))
    if rig.single_vfo:
        log.info("single-VFO mode ON — freq/mode writes mirror to VFO B (AB;), "
                 "SWAP/A>B/B>A disabled")

    port = settings.cat_port
    baud = settings.cat_baud
    if rig.connect(port, baud):
        rig.start_polling(settings.poll_interval)
        log.info("Connected to %s @ %d", port, baud)
    else:
        log.warning("Could not connect to %s — retrying every %.0fs until the rig answers",
                    port, REOPEN_INTERVAL)
        rig.retry_until_connected(port, baud, settings.poll_interval)

    _rigctld = RigctlServer(rig, port=settings.rigctl_port, gate=wspr)
    _rigctld.start()
    log.info("rigctld listening on port %d", settings.rigctl_port)


@app.on_event("shutdown")
async def _shutdown():
    try:
        wspr.disarm("shutting down")
    except Exception:
        pass
    _stop_tune(restore=True)
    try:
        # Safety: never leave the radio keyed
        rig.set_ptt(False)
    except Exception:
        pass
    try:
        fdv_engine.stop()
        freedv_session.exit()
    except Exception:
        pass
    try:
        sstv_tx.abort("shutting down")
        sstv_rx.stop()
        sstv_session.exit()
    except Exception:
        pass
    try:
        fdv_reporter.stop()
    except Exception:
        pass
    try:
        tx_engine.stop()
    except Exception:
        pass
    try:
        wf_engine.stop()
    except Exception:
        pass
    try:
        rtl_engine.stop()
    except Exception:
        pass
    rig.disconnect()
    if _rigctld:
        _rigctld.stop()


# ── WebSocket endpoint ────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()   # keep connection alive; ignore inbound
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.websocket("/ws/waterfall")
async def waterfall_ws(ws: WebSocket):
    """Stream FFT rows + accept control messages (start/stop/range/qsy)."""
    await ws.accept()
    _wf_clients.append(ws)
    acquired_source: Optional[str] = None
    # Unique per-connection tag — NOT a shared literal like "waterfall". The
    # engines' acquire/release use a set (membership, not a refcount) so a
    # second connection reusing the same tag string would be a no-op on
    # acquire, and then wipe out the *first* connection's hold when it
    # disconnects and releases that same tag — killing a still-active viewer's
    # session. (Confirmed live 2026-07-22: a diagnostic script sharing the
    # "waterfall" tag disconnected and silently stopped the real browser session,
    # leaving CyberSDR paused with nothing using the dongle.)
    ws_tag = f"ws-{id(ws)}"
    # Hello + status
    await ws.send_text(json.dumps({
        "type": "hello",
        "status": wf_engine.status(),
        "devices": wf_engine.list_devices(),
        "rtl_status": rtl_engine.status(),
        "vfo": rig.state.freq_a,
        "mode": rig.state.mode,
    }))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "start":
                src = msg.get("source", "audio")
                if acquired_source and acquired_source != src:
                    await _release_ws_source(acquired_source, ws_tag)
                    acquired_source = None
                if src == "rtl":
                    await asyncio.to_thread(_cybersdr_post, "/api/decoder/stop")
                    center = msg.get("center_hz") or rig.state.freq_a or settings.get("rtl_center_hz")
                    # start() retries the rtl_tcp connect for up to ~3min (absorbing
                    # CyberSDR's in-flight-capture race) — run off the event loop.
                    result = await asyncio.to_thread(
                        rtl_engine.acquire,
                        ws_tag,
                        settings.get("rtl_host"),
                        settings.get("rtl_port"),
                        center,
                        msg.get("sample_rate") or settings.get("rtl_sample_rate"),
                    )
                    if not result.get("ok"):
                        # We paused CyberSDR expecting to take the dongle; the connect
                        # ultimately failed (retries exhausted) — don't strand it paused.
                        await asyncio.to_thread(_cybersdr_post, "/api/decoder/start")
                else:
                    result = wf_engine.acquire(ws_tag, msg.get("device_index"))
                acquired_source = src if result.get("ok") else None
                await ws.send_text(json.dumps({"type": "status", "source": src, **result}))
            elif mtype == "stop":
                stopped_src = acquired_source or "audio"
                if acquired_source:
                    await _release_ws_source(acquired_source, ws_tag)
                    acquired_source = None
                status = rtl_engine.status() if stopped_src == "rtl" else wf_engine.status()
                await ws.send_text(json.dumps({"type": "status", "source": stopped_src, **status, "ok": True}))
            elif mtype == "range":
                # Audio (float32-normalized FFT) and RTL (raw 8-bit IQ FFT) run on
                # totally different magnitude scales — applying one Floor/Ceil to
                # both saturates whichever engine isn't the intended target.
                db_min, db_max = float(msg.get("db_min", -90)), float(msg.get("db_max", -20))
                if acquired_source == "rtl":
                    rtl_engine.set_range(db_min, db_max)
                    status = rtl_engine.status()
                else:
                    wf_engine.set_range(db_min, db_max)
                    status = wf_engine.status()
                await ws.send_text(json.dumps({"type": "status", **status, "ok": True}))
            elif mtype == "speed":
                rtl_engine.set_speed(float(msg.get("rows_per_sec", 6)))
                await ws.send_text(json.dumps({"type": "status", **rtl_engine.status(), "ok": True}))
            elif mtype == "qsy":
                # offset_hz from left edge of audio baseband (0 … SR/2)
                try:
                    offset = int(msg.get("offset_hz", 0))
                    base = rig.state.freq_a
                    mode = (rig.state.mode or "").upper()
                    # USB/CW-U/DATA-U: signal is above VFO; LSB family: below
                    if mode in ("LSB", "CW-L", "RTTY-L", "DATA-L"):
                        new_hz = base - offset
                    else:
                        new_hz = base + offset
                    rig.set_freq(int(new_hz))
                    _broadcast()
                    await ws.send_text(json.dumps({
                        "type": "qsy", "ok": True, "freq": int(new_hz), "offset_hz": offset
                    }))
                except Exception as e:
                    await ws.send_text(json.dumps({"type": "qsy", "ok": False, "error": str(e)}))
            elif mtype == "ping":
                await ws.send_text(json.dumps({
                    "type": "pong",
                    "vfo": rig.state.freq_a,
                    "mode": rig.state.mode,
                    "status": wf_engine.status(),
                }))
    except WebSocketDisconnect:
        pass
    finally:
        if acquired_source:
            try:
                await _release_ws_source(acquired_source, ws_tag)
            except Exception:
                pass
        if ws in _wf_clients:
            _wf_clients.remove(ws)


@app.websocket("/ws/audio")
async def audio_ws(ws: WebSocket):
    """Stream FTDX10 USB RX audio as raw s16le mono PCM for browser playback.

    Protocol:
      1) server → text JSON hello {type, sample_rate, format, channels}
      2) server → binary frames of int16 little-endian mono samples
      3) client → optional text JSON {type:gain, value:0.05..4.0}
    """
    await ws.accept()
    _audio_clients.append(ws)
    acquired = False
    try:
        # Start capture (shared with waterfall if already running)
        result = wf_engine.acquire("audio", None)
        acquired = bool(result.get("ok"))
        await ws.send_text(json.dumps({
            "type": "hello",
            "ok": acquired,
            "error": result.get("error"),
            "sample_rate": int(result.get("stream_rate") or 12000),
            "format": "s16le",
            "channels": 1,
            "device_name": result.get("device_name") or "",
            "status": wf_engine.status(),
        }))
        if not acquired:
            return
        while True:
            # Keepalive / gain control from client; binary not expected from client
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=30.0)
            except asyncio.TimeoutError:
                # idle ping
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
                continue
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    continue
                if data.get("type") == "gain":
                    wf_engine.set_pcm_gain(float(data.get("value", 1.0)))
                    await ws.send_text(json.dumps({
                        "type": "gain", "value": wf_engine.status().get("pcm_gain")
                    }))
                elif data.get("type") == "stop":
                    break
            # ignore client binary
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("audio_ws: %s", e)
    finally:
        if acquired:
            try:
                wf_engine.release("audio")
            except Exception:
                pass
        if ws in _audio_clients:
            _audio_clients.remove(ws)


def _tx_profiles() -> list:
    raw = settings.get("tx_profiles")
    if not (isinstance(raw, list) and raw):
        return [dict(p) for p in DEFAULTS["tx_profiles"]]
    # settings.json holds the whole list, so a profile or field added to
    # DEFAULTS never reaches a station that has saved one. Fill in missing
    # ids and missing keys; saved values (tuned gain/RPORT) always win.
    defaults = {p.get("id"): p for p in DEFAULTS["tx_profiles"]}
    out = [{**defaults.get(p.get("id"), {}), **p} for p in raw]
    have = {p.get("id") for p in raw}
    return out + [dict(p) for p in DEFAULTS["tx_profiles"] if p.get("id") not in have]


def _tx_profile(pid: Optional[str] = None) -> dict:
    profiles = _tx_profiles()
    want = pid or settings.get("tx_profile_id") or "phone"
    for p in profiles:
        if p.get("id") == want:
            return p
    return profiles[0]


def _rport_str(profile: Optional[dict] = None) -> str:
    p = profile or _tx_profile()
    try:
        v = int(p.get("rport", 50))
    except (TypeError, ValueError):
        v = 50
    return f"{max(0, min(100, v)):03d}"


def _usb_tx_mod_apply_fast():
    """Write-only USB TX routing (no EX reads — avoids serial timeouts on key-down)."""
    try:
        rig.ex_write(1, 1, 13, "1")   # SSB MOD = REAR
        rig.ex_write(1, 1, 14, "1")   # SSB REAR = USB
        rig.ex_write(1, 1, 15, _rport_str())  # RPORT from active TX profile
        rig.ex_write(1, 4, 15, "1")   # DATA MOD = REAR (P2=04 is PSK/DATA;
                                      # 05 is RTTY -- see modes/session.py)
    except Exception as e:
        log.warning("usb_tx_mod apply: %s", e)


def _usb_tx_mod_setup() -> dict:
    """Point FTDX10 SSB (and DATA) TX audio at USB rear path.

    Required for remote mic → Yaesu USB AUDIO CODEC → RF:
      EX010113 = SSB MOD SOURCE → REAR (1)   [was often MIC=0]
      EX010114 = SSB REAR SELECT → USB (1)   [was often DATA=0]
      EX010115 = SSB RPORT GAIN → raise for drive
      EX010415 = DATA MOD SOURCE → REAR (1)   [was EX010515, the RTTY menu]
    Returns previous values for restore.
    """
    saved = {}
    try:
        saved["ssb_mod"] = rig.ex_read(1, 1, 13)
        saved["ssb_rear"] = rig.ex_read(1, 1, 14)
        saved["ssb_gain"] = rig.ex_read(1, 1, 15)
        saved["data_mod"] = rig.ex_read(1, 4, 15)
    except Exception as e:
        log.warning("usb_tx_mod read: %s", e)

    # A profile with "proc" owns the radio speech processor while armed.
    # A webcam mic 2-3 ft from the mouth: PROC lifts the room's reverb
    # tails up to speech level, which read on air as "in a bathroom"
    # (2026-09-25, PROC on at 30). Restored on disarm with the MOD menus.
    want_proc = _tx_profile().get("proc")
    if want_proc is not None:
        try:
            saved["proc"] = (bool(rig.state.compressor), rig.state.comp_level)
            if want_proc is False:
                rig.set_compressor(False)
            elif want_proc is True:   # on, at the operator's current level
                rig.set_compressor(True)
            else:
                rig.set_compressor(True, int(want_proc))
            log.info("TX profile PROC=%s (was %s)", want_proc, saved["proc"])
        except Exception as e:
            log.warning("tx profile proc: %s", e)
            saved.pop("proc", None)

    try:
        _usb_tx_mod_apply_fast()
        log.info(
            "USB TX mod setup: SSB MOD=REAR REAR=USB RPORT=%s profile=%s (was mod=%s rear=%s gain=%s)",
            _rport_str(), _tx_profile().get("id"),
            saved.get("ssb_mod"), saved.get("ssb_rear"), saved.get("ssb_gain"),
        )
    except Exception as e:
        log.warning("usb_tx_mod write: %s", e)
    return saved


def _usb_tx_mod_restore(saved: dict):
    if not saved:
        return
    try:
        if saved.get("ssb_mod") is not None:
            rig.ex_write(1, 1, 13, str(saved["ssb_mod"]))
        if saved.get("ssb_rear") is not None:
            rig.ex_write(1, 1, 14, str(saved["ssb_rear"]))
        if saved.get("ssb_gain") is not None:
            rig.ex_write(1, 1, 15, str(saved["ssb_gain"]))
        if saved.get("data_mod") is not None:
            rig.ex_write(1, 4, 15, str(saved["data_mod"]))
        if saved.get("proc") is not None:
            on, lv = saved["proc"]
            rig.set_compressor(on, lv)
        log.info("USB TX mod restored: %s", saved)
    except Exception as e:
        log.warning("usb_tx_mod restore: %s", e)


def _log_over_fill(key_t, key_bytes: int, client_sr: int):
    """Report how much of a keyed over actually carried mic audio.

    Mic PCM arrives as s16le mono at ``client_sr``, so realtime is
    ``client_sr * 2`` bytes/sec. Fill well under 100% means the operator was
    transmitting silence for part of the over -- the browser mic starting
    late is the way that happens (fixed in remote-tx.js by keying on the
    first frame, but a slow network can still starve it).
    """
    if not key_t:
        return
    held = time.time() - key_t
    if held <= 0.05:
        return
    delivered = int(tx_engine.status().get("bytes_in") or 0) - int(key_bytes or 0)
    rate = max(1, int(client_sr or 48000) * 2)
    audio_s = max(0.0, delivered / rate)
    fill = audio_s / held * 100.0
    msg = "over: %.1fs keyed, %.1fs audio (%.0f%% fill)" % (held, audio_s, fill)
    if fill < 50.0:
        log.warning("%s - transmitted mostly silence", msg)
    else:
        log.info(msg)


@app.websocket("/ws/tx")
async def tx_audio_ws(ws: WebSocket):
    """Remote SSB/DATA TX: browser mic PCM + PTT over LAN.

    Protocol:
      server → text hello {ok, sample_rate preferred, devices}
      client → text {type:"ptt", on:true|false}
      client → text {type:"gain", value:0.05..2.5}
      client → binary s16le mono PCM @ client sample_rate
      client → text {type:"hello", sample_rate:48000}  (optional, before PCM)

    On disconnect: PTT off + stop TX audio out + restore MOD SOURCE.
    """
    await ws.accept()
    ptt_on = False
    client_sr = 48000
    key_t = None
    key_bytes = 0
    started = False
    mod_saved: dict = {}
    try:
        # Route radio TX audio to USB before opening Windows playback device
        mod_saved = _usb_tx_mod_setup()
        result = tx_engine.start(None)
        started = bool(result.get("ok"))
        await ws.send_text(json.dumps({
            "type": "hello",
            "ok": started,
            "error": result.get("error"),
            "device_name": result.get("device_name") or "",
            "device_rate": result.get("sample_rate") or 48000,
            "prefer_client_rate": 48000,
            "format": "s16le",
            "channels": 1,
            "outputs": tx_engine.list_output_devices(),
            "status": tx_engine.status(),
            "mod_setup": {
                "ssb_mod_source": "REAR",
                "ssb_rear_select": "USB",
                "previous": mod_saved,
            },
            "profile": _tx_profile(),
            "hint": (
                "USB TX path armed: SSB MOD=REAR, REAR SELECT=USB. "
                "Use mode USB/LSB. Hold PTT to key CAT + stream mic. "
                "Settings restore when you disarm Remote TX."
            ),
        }))
        if not started:
            _usb_tx_mod_restore(mod_saved)
            return

        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("type") != "websocket.receive":
                continue

            if msg.get("bytes") is not None:
                # Mic PCM only while PTT is held (ignore otherwise)
                if ptt_on:
                    tx_engine.push_pcm_s16le(msg["bytes"], client_sr)
                continue

            text = msg.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue

            mtype = data.get("type")
            if mtype == "hello":
                client_sr = int(data.get("sample_rate") or client_sr)
            elif mtype == "gain":
                tx_engine.set_gain(float(data.get("value", 0.85)))
                await ws.send_text(json.dumps({
                    "type": "gain", "value": tx_engine.status().get("gain")
                }))
            elif mtype == "ptt":
                on = bool(data.get("on"))
                if on and not ptt_on:
                    if _tune_is_active():
                        await ws.send_text(json.dumps({
                            "type": "ptt", "ok": False,
                            "error": "Remote TUNE is active — stop TUNE first",
                        }))
                        continue
                    if not rig.is_connected:
                        await ws.send_text(json.dumps({
                            "type": "ptt", "ok": False, "error": "Radio not connected",
                        }))
                        continue
                    # Key immediately — no pilot tone (was beeping W9IMS on key-up)
                    try:
                        _usb_tx_mod_apply_fast()
                    except Exception as e:
                        log.warning("ptt mod setup: %s", e)
                    try:
                        rig.set_ptt(True)
                    except Exception as e:
                        log.warning("ptt on: %s", e)
                    ptt_on = True
                    key_t = time.time()
                    key_bytes = int(tx_engine.status().get("bytes_in") or 0)
                    _broadcast()
                    await ws.send_text(json.dumps({"type": "ptt", "ok": True, "on": True}))
                elif not on and ptt_on:
                    try:
                        rig.set_ptt(False)
                    except Exception as e:
                        log.warning("ptt off: %s", e)
                    ptt_on = False
                    # How much of that over actually carried audio? A keyed
                    # radio with no mic bytes is silence on the air and is
                    # otherwise completely invisible -- it cost a whole net
                    # before anyone measured it (2026-09-04). Never again.
                    _log_over_fill(key_t, key_bytes, client_sr)
                    key_t = None
                    _broadcast()
                    await ws.send_text(json.dumps({"type": "ptt", "ok": True, "on": False}))
            elif mtype == "beep":
                # Server tone path test (dummy load). Keep this non-blocking:
                # CAT calls are short; poll thread owns meter updates.
                if not rig.is_connected:
                    await ws.send_text(json.dumps({"type": "beep", "ok": False, "error": "not connected"}))
                    continue
                ms = int(data.get("ms", 2000))
                hz = float(data.get("hz", 700))
                amp = float(data.get("amp", 0.55))
                unkey = bool(data.get("unkey", True))
                watts = data.get("watts")
                # Ack immediately so the browser never times out waiting
                await ws.send_text(json.dumps({
                    "type": "beep",
                    "ok": True,
                    "ms": ms,
                    "hz": hz,
                    "phase": "starting",
                }))
                try:
                    _usb_tx_mod_apply_fast()
                    if watts is not None:
                        rig.set_power(int(watts))
                except Exception as e:
                    log.warning("beep prep: %s", e)
                # Start tone first, brief pre-roll, then key
                tx_engine.play_beep(ms=min(ms + 200, 5000), hz=hz, amp=amp)
                await asyncio.sleep(0.12)
                if not ptt_on:
                    try:
                        rig.set_ptt(True)
                    except Exception as e:
                        log.warning("beep ptt: %s", e)
                    ptt_on = True
                    _broadcast()
                await ws.send_text(json.dumps({
                    "type": "beep",
                    "ok": True,
                    "ms": ms,
                    "hz": hz,
                    "phase": "keyed",
                    "status": tx_engine.status(),
                }))
                hold = max(0.3, ms / 1000.0)
                peak = {"power_meter": 0, "alc_meter": 0, "swr_meter": 0}
                t_end = time.time() + hold
                while time.time() < t_end:
                    await asyncio.sleep(0.2)
                    peak["power_meter"] = max(peak["power_meter"], int(rig.state.power_meter or 0))
                    peak["alc_meter"] = max(peak["alc_meter"], int(rig.state.alc_meter or 0))
                    peak["swr_meter"] = max(peak["swr_meter"], int(rig.state.swr_meter or 0))
                if unkey and ptt_on:
                    await asyncio.sleep(0.05)
                    try:
                        rig.set_ptt(False)
                    except Exception:
                        pass
                    ptt_on = False
                    _broadcast()
                await ws.send_text(json.dumps({
                    "type": "beep_done",
                    "ok": True,
                    "ms": ms,
                    "hz": hz,
                    "peak": peak,
                    "status": tx_engine.status(),
                }))
            elif mtype == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("tx_audio_ws: %s", e)
    finally:
        if ptt_on:
            try:
                rig.set_ptt(False)
                _broadcast()
            except Exception:
                pass
            _log_over_fill(key_t, key_bytes, client_sr)
        try:
            tx_engine.stop()
        except Exception:
            pass
        _usb_tx_mod_restore(mod_saved)


@app.get("/api/tx/devices")
async def tx_devices():
    return {
        "available": tx_engine.available(),
        "outputs": tx_engine.list_output_devices(),
        "auto_index": tx_engine.auto_output_index(),
        "status": tx_engine.status(),
    }


@app.get("/api/audio")
async def audio_status():
    """Shared USB codec: one duplex stream for LISTEN + REMOTE TX."""
    return {
        "codec": rig_codec.status(),
        "rx": wf_engine.status(),
        "tx": tx_engine.status(),
        "freedv": fdv_engine.status(),
    }


def _freedv_status() -> dict:
    s = rig.state
    return {
        **freedv_session.status(),
        "modem": fdv_engine.status(),
        "modes": fdv_available_modes(),
        "callsign": settings.callsign or "",
        "grid": settings.get("freedv_grid") or "",
        "reporter": fdv_reporter.status(),
        "tx_level": fdv_engine.status().get("tx_level"),
        **fdv_guard.status(),
        "power": s.power,
        "power_meter": s.power_meter,
        "alc_meter": s.alc_meter,
        "swr_meter": s.swr_meter,
        "is_tx": s.is_tx,
        "tune_active": _tune_is_active(),
        "tune_watts": int(settings.get("tune_watts", 20)),
        "tune_mode": settings.get("tune_mode", "AM"),
        "atu": rig.state.atu,
        "atu_tuning": rig.state.atu_tuning,
    }


@app.get("/api/freedv")
async def get_freedv():
    return {"ok": True, **_freedv_status()}


class FreedvEnterReq(BaseModel):
    mode: Optional[str] = None


@app.post("/api/freedv/enter")
async def freedv_enter(req: FreedvEnterReq = None):
    if _tune_is_active():
        return {"ok": False, "error": "Remote TUNE is active"}
    mode = (req.mode if req else None) or settings.get("freedv_codec") or "RADE"
    fdv_guard.stop()
    r = freedv_session.enter()
    if not r.get("ok"):
        return r
    er = fdv_engine.start(mode)
    if not er.get("ok"):
        freedv_session.exit()
        return {"ok": False, "error": er.get("error"), **_freedv_status()}
    fdv_reporter.show(int(rig.state.freq_a or 0), "RADEV1")
    _broadcast()
    return {"ok": True, **_freedv_status()}


def _fdv_on_freq(hz: int):
    if freedv_session.active:
        fdv_reporter.freq_change(int(hz))


def _digital_follow_sideband(hz: int):
    """DATA-U at/above 10 MHz, DATA-L below — track the VFO while entered.

    Runs for whichever digital mode is entered (FreeDV or SSTV), off every
    freq change: the knob on the radio, a spot click, the page's own VFO.
    Skipped while the remote tuner owns the mode (it is on AM and restores
    what it borrowed) and while transmitting — never change sideband on air.
    """
    if _tune_is_active():
        return
    s = active_session()
    if s is None:
        return
    try:
        if getattr(rig.state, "is_tx", False):
            return
        if s.follow_freq(int(hz)):
            _broadcast()
    except Exception:
        log.exception("digital sideband follow")


@app.post("/api/freedv/exit")
async def freedv_exit():
    try:
        _fdv_key(False)
    except Exception:
        pass
    fdv_reporter.hide()
    fdv_engine.stop()
    r = freedv_session.exit()
    _broadcast()
    return {**r, **_freedv_status()}


class FreedvModeReq(BaseModel):
    mode: str


@app.post("/api/freedv/mode")
async def freedv_mode(req: FreedvModeReq):
    settings._data["freedv_codec"] = req.mode
    if not fdv_engine.status().get("running"):
        return {"ok": True, **_freedv_status()}
    r = fdv_engine.set_mode(req.mode)
    return {**r, **_freedv_status()}


class FreedvPttReq(BaseModel):
    on: bool


class FreedvInvertReq(BaseModel):
    on: bool


@app.post("/api/freedv/invert")
async def freedv_invert(req: FreedvInvertReq):
    fdv_engine.set_invert(req.on)
    return {"ok": True, **_freedv_status()}


class FreedvQsyReq(BaseModel):
    hz: int


@app.post("/api/freedv/qsy")
async def freedv_qsy(req: FreedvQsyReq):
    rig.set_freq(req.hz)
    if freedv_session.active:
        freedv_session.follow_freq(int(req.hz), force=True)
        fdv_reporter.freq_change(int(req.hz))
    _broadcast()
    return {"ok": True, "hz": req.hz, "mode": data_mode_for_hz(req.hz), **_freedv_status()}


# ── SSTV ──────────────────────────────────────────────────────────────────
# Phase 0: the radio session only. Encoding, decoding and the /sstv page are
# not wired yet — entering here gets you the digital preset and the power
# clamp, and nothing transmits.
def _sstv_status() -> dict:
    return {
        **sstv_session.status(),
        **sstv_tx.status(),
        "callsign": settings.callsign or "",
        "grid": settings.get("freedv_grid") or "",
        "tx_level": settings.get("sstv_tx_level"),
        "fsk_id": (settings.callsign or "") if settings.get("sstv_fsk_id") else "",
        "max_swr": settings.get("sstv_max_swr"),
        "tune_active": _tune_is_active(),
        "tune_watts": int(settings.get("tune_watts", 20)),
        "tune_mode": settings.get("tune_mode", "AM"),
        "atu": rig.state.atu,
        "atu_tuning": rig.state.atu_tuning,
        "rf_gain": rig.state.rf_gain,
        **_sstv_meters(),
        **sstv_rx.status(),
        "macros": macro_context(
            rig.state.freq_a, rig.state.mode,
            settings.callsign or "", settings.get("freedv_grid") or ""),
    }


def _sstv_meters() -> dict:
    """Meters through the real calibration curves, computed here so the page
    does not carry its own copy of the tables."""
    from cyberrig.cat.meter_cal import ALC, PO, SWR
    st = rig.state
    raw_po = st.power_meter or 0
    has_rf = bool(st.is_tx) and raw_po >= 3
    return {
        "is_tx": bool(st.is_tx),
        "watts_out": int(round(PO.to_value(raw_po))) if has_rf else 0,
        "alc_out": int(round(ALC.to_value(st.alc_meter or 0))) if has_rf else 0,
        "swr_out": round(SWR.to_value(st.swr_meter or 0), 1) if has_rf else None,
    }


@app.get("/api/sstv")
async def get_sstv():
    return {"ok": True, **_sstv_status()}


@app.get("/api/sstv/modes")
async def get_sstv_modes():
    """Mode table for the selector. Duration is included so the UI can label it."""
    return {"ok": True, "modes": sstv_mode_list(), "default": settings.get("sstv_mode")}


@app.post("/api/sstv/enter")
async def sstv_enter():
    if _tune_is_active():
        return {"ok": False, "error": "Remote TUNE is active"}
    r = sstv_session.enter()
    if r.get("ok"):
        sstv_rx.start()
    _broadcast()
    return {**_sstv_status(), **r}


@app.post("/api/sstv/exit")
async def sstv_exit():
    sstv_tx.abort("SSTV exited")
    sstv_rx.stop()
    try:
        rig.set_ptt(False)
    except Exception:
        pass
    r = sstv_session.exit()
    _broadcast()
    return {**_sstv_status(), **r}


def _png(img) -> Response:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/sstv/rx/image")
async def sstv_rx_image():
    """The picture currently arriving, as far as it has got."""
    img = sstv_rx.current_image()
    if img is None:
        return Response(status_code=204)
    return _png(img)


@app.get("/api/sstv/rx/last.png")
async def sstv_rx_last():
    """The last received picture as currently rendered — slant and all."""
    img = sstv_rx.last_image()
    if img is None:
        return Response(status_code=204)
    return _png(img)


class SstvAdjustReq(BaseModel):
    slant_ppm: Optional[float] = None
    shift_ms: Optional[float] = None
    auto: bool = False


@app.post("/api/sstv/rx/adjust")
async def sstv_rx_adjust(req: SstvAdjustReq):
    """Re-render the last picture. No new audio is needed or used."""
    return {**sstv_rx.adjust_last(req.slant_ppm, req.shift_ms, req.auto)}


@app.post("/api/sstv/rx/resave")
async def sstv_rx_resave():
    r = sstv_rx.resave_last()
    return {**r, "items": sstv_store.list()}


@app.get("/api/sstv/gallery")
async def sstv_gallery():
    return {"ok": True, "items": sstv_store.list()}


@app.get("/api/sstv/gallery/{name}")
async def sstv_gallery_image(name: str):
    # .wav serves the raw receive audio: the only artefact that can be fed
    # back through a CHANGED decoder, or into PySSTV/QSSTV for a second
    # opinion on whether a shredded picture was our fault or the band's.
    if name.endswith(".wav"):
        p = sstv_store.path(name[:-4], ext="wav")
        if p is None:
            return Response(status_code=404)
        return FileResponse(str(p), media_type="audio/wav")
    p = sstv_store.path(name[:-4] if name.endswith(".png") else name)
    if p is None:
        return Response(status_code=404)
    return FileResponse(str(p), media_type="image/png")


@app.delete("/api/sstv/gallery/{rid}")
async def sstv_gallery_delete(rid: str):
    r = sstv_store.delete(rid)
    return {**r, "items": sstv_store.list()}


@app.get("/api/sstv/templates")
async def sstv_list_templates():
    return {"ok": True, "templates": sstv_templates.list()}


class SstvTemplateReq(BaseModel):
    template: dict


@app.post("/api/sstv/templates")
async def sstv_save_template(req: SstvTemplateReq):
    try:
        t = sstv_templates.save(req.template)
    except OSError as e:
        return {"ok": False, "error": f"could not save: {e}"}
    return {"ok": True, "template": t, "templates": sstv_templates.list()}


@app.delete("/api/sstv/templates/{tid}")
async def sstv_delete_template(tid: str):
    r = sstv_templates.delete(tid)
    return {**r, "templates": sstv_templates.list()}


class SstvTxReq(BaseModel):
    image: str                       # data: URL or bare base64 PNG/JPEG
    mode: Optional[str] = None
    fit: str = "cover"
    level: Optional[float] = None


@app.post("/api/sstv/tx")
async def sstv_start_tx(req: SstvTxReq):
    """Render and transmit. Never called by anything but a human pressing SEND."""
    if _tune_is_active():
        return {"ok": False, "error": "Remote TUNE is active"}
    try:
        raw = _decode_data_url(req.image)
    except Exception as e:
        return {**_sstv_status(), "ok": False, "error": f"bad image: {e}"}
    mode = req.mode or settings.get("sstv_mode") or "Scottie 1"
    level = req.level if req.level is not None else settings.get("sstv_tx_level")
    if req.level is not None:
        settings._data["sstv_tx_level"] = float(req.level)
    settings._data["sstv_mode"] = mode
    fsk = (settings.callsign or "") if settings.get("sstv_fsk_id") else ""
    # Fall back to the SSTV default, not to 0.5 — with the power ceiling at
    # 100 W the drive IS the output, so a stale half-scale fallback is a very
    # loud accident. SSTV path only.
    r = sstv_tx.start(io.BytesIO(raw), mode,
                      amplitude=float(level or DEFAULTS["sstv_tx_level"]),
                      fit=req.fit, fsk_id=fsk)
    _broadcast()
    return {**_sstv_status(), **r}


@app.post("/api/sstv/abort")
async def sstv_abort_tx():
    r = sstv_tx.abort("aborted by operator")
    _broadcast()
    return {**_sstv_status(), **r}


def _decode_data_url(s: str) -> bytes:
    """Accept either a full `data:image/png;base64,...` URL or bare base64."""
    txt = (s or "").strip()
    if txt.startswith("data:"):
        head, _, txt = txt.partition(",")
        if "base64" not in head:
            raise ValueError("only base64 data URLs are supported")
    return base64.b64decode(txt, validate=False)


class SstvWattsReq(BaseModel):
    watts: int


@app.post("/api/sstv/watts")
async def sstv_watts(req: SstvWattsReq):
    w = sstv_session.set_watts(req.watts)
    settings._data["sstv_watts"] = w
    settings.save()
    _broadcast()
    return {"ok": True, **_sstv_status()}


class FreedvWattsReq(BaseModel):
    watts: int


@app.post("/api/freedv/watts")
async def freedv_watts(req: FreedvWattsReq):
    """The rig's power setting while entered — the ALC THRESHOLD, not output.

    Raising this does not raise what the antenna sees; it moves the ceiling
    the ALC starts limiting at. The DRIVE control below is what sets power.
    """
    w = freedv_session.set_watts(req.watts)
    settings._data["freedv_watts"] = w
    settings.save()
    _broadcast()
    return {"ok": True, **_freedv_status()}


class FreedvLevelReq(BaseModel):
    level: float


@app.post("/api/freedv/level")
async def freedv_level(req: FreedvLevelReq):
    """TX audio drive — the control that actually sets output power."""
    lvl = fdv_engine.set_tx_level(req.level)
    settings._data["freedv_tx_level"] = lvl
    settings.save()
    log.info("FreeDV TX drive %.3f (%.1f dBFS)", lvl, 20 * math.log10(lvl))
    _broadcast()
    return {"ok": True, **_freedv_status()}


@app.post("/api/freedv/ptt")
async def freedv_ptt(req: FreedvPttReq):
    if not freedv_session.active:
        return {"ok": False, "error": "FreeDV is not entered"}
    if _tune_is_active():
        return {"ok": False, "error": "Remote TUNE is active"}
    _fdv_key(req.on)
    _broadcast()
    return {"ok": True, **_freedv_status()}


@app.websocket("/ws/freedv")
async def freedv_ws(ws: WebSocket):
    """Decoded RX speech out, mic PCM in, PTT/mode as JSON.

    server → hello JSON, then binary s16le @ 16 kHz (decoded speech)
    client → binary s16le mic, JSON {type:ptt,on}, {type:hello,sample_rate}
    """
    await ws.accept()
    _fdv_clients.append(ws)
    client_sr = 48000
    try:
        await ws.send_text(json.dumps({
            "type": "hello",
            "ok": True,
            "sample_rate": 16000,
            "format": "s16le",
            "channels": 1,
            **_freedv_status(),
        }))
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if fdv_engine.status().get("ptt"):
                    fdv_engine.push_mic(msg["bytes"], client_sr)
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue
            mtype = data.get("type")
            if mtype == "hello":
                try:
                    client_sr = int(data.get("sample_rate") or 48000)
                except (TypeError, ValueError):
                    client_sr = 48000
            elif mtype == "ptt":
                on = bool(data.get("on"))
                if not freedv_session.active:
                    await ws.send_text(json.dumps({
                        "type": "error", "error": "FreeDV is not entered",
                    }))
                    continue
                if _tune_is_active():
                    await ws.send_text(json.dumps({
                        "type": "error", "error": "Remote TUNE is active",
                    }))
                    continue
                _fdv_key(on)
                _broadcast()
                await ws.send_text(json.dumps({
                    "type": "ptt", "on": on, **_freedv_status(),
                }))
            elif mtype == "ping":
                await ws.send_text(json.dumps({
                    "type": "status", **_freedv_status(),
                }))
            elif mtype == "mode":
                mode = data.get("mode") or "RADE"
                fdv_engine.set_mode(mode)
                await ws.send_text(json.dumps({
                    "type": "status", **_freedv_status(),
                }))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("freedv_ws: %s", e)
    finally:
        if ws in _fdv_clients:
            _fdv_clients.remove(ws)
        if not _fdv_clients and fdv_engine.status().get("ptt"):
            try:
                _fdv_key(False)
            except Exception:
                pass


class TxProfileReq(BaseModel):
    id: Optional[str] = None
    gain: Optional[float] = None
    rport: Optional[int] = None


@app.get("/api/tx/profiles")
async def get_tx_profiles():
    return {
        "ok": True,
        "profiles": _tx_profiles(),
        "active_id": _tx_profile().get("id"),
        "active": _tx_profile(),
    }


@app.post("/api/tx/profile")
async def set_tx_profile(req: TxProfileReq):
    profiles = _tx_profiles()
    pid = req.id or settings.get("tx_profile_id") or "phone"
    found = None
    for i, p in enumerate(profiles):
        if p.get("id") == pid:
            found = p
            if req.gain is not None:
                p["gain"] = max(0.1, min(3.0, float(req.gain)))
            if req.rport is not None:
                p["rport"] = max(0, min(100, int(req.rport)))
            profiles[i] = p
            break
    if found is None:
        return {"ok": False, "error": f"unknown TX profile {pid!r}"}
    settings.tx_profile_id = found["id"]
    settings.tx_profiles = profiles
    try:
        settings.save()
    except Exception:
        log.exception("could not persist TX profile")
    # Live RPORT if USB TX is already armed
    if found.get("path") != "front" and tx_engine.status().get("running"):
        try:
            rig.ex_write(1, 1, 15, _rport_str(found))
        except Exception as e:
            log.warning("live RPORT write: %s", e)
    return {"ok": True, "profiles": profiles, "active_id": found["id"], "active": found}


# ── Static UI / PWA ───────────────────────────────────────────────────────
def _page(name: str) -> FileResponse:
    """An operating page, with its ETag revalidated on every load.

    FileResponse sends an ETag and a Last-Modified but no Cache-Control, which
    leaves a browser free to heuristically cache the page — and an installed
    iOS PWA holds it in its own WebView cache, where pull-to-refresh does not
    reach. That is how a deployed FreeDV DRIVE slider stayed invisible on the
    phone while `curl` showed it live. `no-cache` still allows the 304: it
    means revalidate, not "never store".
    """
    return FileResponse(str(_STATIC / name), headers={"Cache-Control": "no-cache"})


@app.get("/")
async def index():
    return _page("index.html")


@app.get("/waterfall")
async def waterfall_page():
    """Retired. LISTEN still uses the capture engine; there is no FFT UI."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/", status_code=302)


@app.get("/sstv")
async def sstv_page():
    """SSTV operating page — upload, template, send. Decode lands in phase 4."""
    return _page("sstv.html")


@app.get("/freedv")
async def freedv_page():
    """FreeDV operating page — RADE/700D in-process, not freedv-gui."""
    return _page("freedv.html")


@app.get("/eq")
async def eq_page():
    """Separate-window parametric EQ (TX DSP EQ / Mic P-EQ)."""
    return _page("eq.html")


@app.get("/macros-editor")
async def macros_editor_page():
    """Separate-window macro configuration (add/edit/delete/reorder + run)."""
    return _page("macros-editor.html")


@app.get("/memories")
async def memories_page():
    """Separate-window frequency memory channels (store/recall/edit)."""
    return _page("memories.html")


@app.get("/manifest.json")
async def manifest_root():
    """Some install prompts look at /manifest.json; primary is /static/manifest.json."""
    return FileResponse(str(_STATIC / "manifest.json"), media_type="application/manifest+json")


@app.get("/service-worker.js")
async def sw_root():
    """Scope-friendly SW at site root so it can control /."""
    return FileResponse(
        str(_STATIC / "service-worker.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(str(_STATIC / "icons" / "favicon.ico"))


# ── State ────────────────────────────────────────────────────────────────
@app.get("/api/state")
async def get_state():
    return _state_dict()


def _cal_payload(spec) -> dict:
    return {
        "rm": spec.rm,
        "key": spec.key,
        "unit": spec.unit,
        "decimals": spec.decimals,
        "full_raw": spec.full_raw,
        "ticks": list(spec.ticks),
        "warn_raw": spec.warn_raw,
        "crit_raw": spec.crit_raw,
        "cal": [list(p) for p in spec.cal],
        "tx_only": spec.tx_only,
    }


@app.get("/api/meter_cal")
async def get_meter_cal():
    """Raw RM/SM count → real-world curves, straight from cyberrig/cat/meter_cal.py.

    The browser used to carry its own scaling (raw/255 for the bars, a hand
    tuned ``po_cal`` fudge for watts, an invented SWR curve) which read ALC 4x
    low and PO ~24% low. Serving the tables keeps the panel and the web UI on
    one set of numbers — fix a curve in meter_cal.py and both follow.
    """
    return {
        "meters": {m.key: _cal_payload(m) for m in meter_cal.TX_METERS},
        "smeter": _cal_payload(meter_cal.SMETER),
    }


@app.get("/api/debug/cat")
async def debug_cat():
    """Raw IF/MD frames for mode-parse diagnosis. Read-only."""
    out = {}
    # VS; is the receive-VFO selector (VS0=A, VS1=B). Added 2026-08-28: FA
    # writes were landing on a VFO the radio was not listening to, which reads
    # as "the frequency display is wrong" while mode changes stay audible.
    for cmd in ("IF;", "MD0;", "MD1;", "FA;", "FB;", "ST;", "FT;", "VS;"):
        try:
            out[cmd] = rig._cmd(cmd)
        except Exception as e:
            out[cmd] = f"err:{e}"
    try:
        out["parsed_if"] = rig.get_if_status()
        out["parsed_md0"] = rig.get_mode()
        out["state_mode"] = rig.state.mode
        out["state_freq_a"] = rig.state.freq_a
        r = out.get("IF;") or ""
        out["if_len"] = len(r)
        if len(r) > 22:
            out["if_freq_slice"] = r[5:14]
            out["if_idx19_28"] = {str(i): r[i] for i in range(19, min(28, len(r)))}
    except Exception as e:
        out["parse_err"] = str(e)
    return out


@app.get("/api/debug/rm")
async def debug_rm():
    """Raw RM responses for TX meter diagnosis."""
    out = {}
    for m in (4, 5, 6):
        try:
            raw = rig._cmd(f"RM{m};")
            out[f"RM{m}"] = raw
            out[f"parsed_{m}"] = rig.get_meter(m)
        except Exception as e:
            out[f"RM{m}"] = f"err:{e}"
    try:
        out["TX"] = rig._cmd("TX;")
        out["PC"] = rig._cmd("PC;")
        out["is_tx_state"] = rig.state.is_tx
    except Exception as e:
        out["tx_err"] = str(e)
    return out



# ── Waterfall REST (also controllable via /ws/waterfall) ─────────────────
@app.get("/api/waterfall/devices")
async def wf_devices():
    return {
        "available": wf_engine.available(),
        "devices": wf_engine.list_devices(),
        "auto_index": wf_engine.auto_device_index(),
        "status": wf_engine.status(),
    }


class WfStartReq(BaseModel):
    device_index: Optional[int] = None


@app.post("/api/waterfall/start")
async def wf_start(req: WfStartReq = WfStartReq()):
    rtl_engine.release("api")  # mutual exclusion — only one waterfall source active at a time
    return wf_engine.acquire("api", req.device_index)


@app.post("/api/waterfall/stop")
async def wf_stop():
    return wf_engine.release("api")


@app.get("/api/waterfall/status")
async def wf_status():
    return wf_engine.status()


# ── RTL-SDR Panadapter REST — EXPERIMENTAL, see README "Safety" ──────────────
class PanStartReq(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    center_hz: Optional[int] = None
    sample_rate: Optional[int] = None


@app.post("/api/panadapter/start")
async def pan_start(req: PanStartReq = PanStartReq()):
    wf_engine.release("api")  # mutual exclusion — only one waterfall source active at a time
    await asyncio.to_thread(_cybersdr_post, "/api/decoder/stop")
    center = req.center_hz or rig.state.freq_a or settings.get("rtl_center_hz")
    # start() retries the rtl_tcp connect for up to ~3min (absorbing CyberSDR's
    # in-flight-capture race) — run off the event loop so other requests aren't blocked.
    result = await asyncio.to_thread(
        rtl_engine.acquire,
        "api",
        req.host or settings.get("rtl_host"),
        req.port or settings.get("rtl_port"),
        center,
        req.sample_rate or settings.get("rtl_sample_rate"),
    )
    if not result.get("ok"):
        # We paused CyberSDR expecting to take the dongle; the connect ultimately
        # failed (retries exhausted) — don't strand it paused.
        await asyncio.to_thread(_cybersdr_post, "/api/decoder/start")
    return result


@app.post("/api/panadapter/stop")
async def pan_stop():
    result = rtl_engine.release("api")
    if not result.get("running"):
        # Only resume CyberSDR once every holder (e.g. a browser tab via /ws/waterfall)
        # has released too — don't yank the dongle out from under another active viewer.
        await asyncio.to_thread(_cybersdr_post, "/api/decoder/start")
    return result


@app.get("/api/panadapter/status")
async def pan_status():
    return rtl_engine.status()


# ── Connection ────────────────────────────────────────────────────────────
class ConnectReq(BaseModel):
    port: str
    baud: int = 38400

@app.post("/api/connect")
async def do_connect(req: ConnectReq):
    # Unconditional: a retry loop may be running while disconnected, and a
    # second poll thread on the same port would double every CAT query.
    rig.disconnect()
    ok = rig.connect(req.port, req.baud)
    if ok:
        settings.cat_port = req.port
        settings.cat_baud = req.baud
        settings.save()
        rig.start_polling(settings.poll_interval)
    else:
        # Keep trying the saved port, not a possibly mistyped new one.
        rig.retry_until_connected(settings.cat_port, settings.cat_baud,
                                  settings.poll_interval)
    _broadcast()
    return {"ok": ok}

@app.post("/api/disconnect")
async def do_disconnect():
    rig.disconnect()
    _broadcast()
    return {"ok": True}


# ── VFO ──────────────────────────────────────────────────────────────────
class FreqReq(BaseModel):
    hz: int

class BandReq(BaseModel):
    band: str

@app.post("/api/freq")
async def set_freq(req: FreqReq):
    rig.set_freq(req.hz); return {"ok": True}

@app.post("/api/freq_b")
async def set_freq_b(req: FreqReq):
    rig.set_freq_b(req.hz); return {"ok": True}

class VfoSelectReq(BaseModel):
    vfo: str  # "a" or "b"


@app.post("/api/vfo_select")
async def vfo_select(req: VfoSelectReq):
    """Choose which VFO the radio receives on (VS0=A, VS1=B).

    CyberRig displays and tunes VFO A exclusively (FA/IF). If the radio is
    left receiving on B, every tune lands on a VFO nobody is listening to and
    the UI looks stale while being perfectly truthful about VFO A -- and IF;
    reports VFO A regardless of VS, so no amount of polling could see it.
    Found on the air 2026-08-28: recalls "worked" while FT8 kept playing.
    """
    v = (req.vfo or "").strip().lower()
    if v not in ("a", "b"):
        return {"ok": False, "error": "vfo must be 'a' or 'b'"}
    rig._cmd_raw("VS0;" if v == "a" else "VS1;")
    sel = rig._cmd("VS;")
    _broadcast()
    return {"ok": True, "vs": sel}


def _single_vfo_block():
    """Refuse the A/B controls while single-VFO mode is on.

    The UI hides these buttons, but the endpoints are reachable directly and
    from the desktop panel -- and an A/B operation is exactly what stranded
    the radio on VFO B in the first place.
    """
    if rig.single_vfo:
        return {"ok": False, "error": "single-VFO mode is on (settings.single_vfo)"}
    return None


@app.post("/api/vfo_swap")
async def vfo_swap():
    blocked = _single_vfo_block()
    if blocked:
        return blocked
    rig.swap_vfo(); return {"ok": True}

@app.post("/api/vfo_a_to_b")
async def vfo_a_to_b():
    blocked = _single_vfo_block()
    if blocked:
        return blocked
    rig.vfo_a_to_b(); return {"ok": True}

@app.post("/api/vfo_b_to_a")
async def vfo_b_to_a():
    blocked = _single_vfo_block()
    if blocked:
        return blocked
    rig.vfo_b_to_a(); return {"ok": True}

@app.post("/api/band")
async def set_band(req: BandReq):
    rig.go_band(req.band); return {"ok": True}


# ── Mode ─────────────────────────────────────────────────────────────────
class ModeReq(BaseModel):
    mode: str

@app.post("/api/mode")
async def set_mode(req: ModeReq):
    rig.set_mode(req.mode); return {"ok": True}


# ── PTT ──────────────────────────────────────────────────────────────────
class PTTReq(BaseModel):
    tx: bool

@app.post("/api/ptt")
async def set_ptt(req: PTTReq):
    # Don't fight remote-tune: stop tune first if operator uses normal PTT
    if _tune_is_active() and not req.tx:
        _stop_tune(restore=True)
        _broadcast()
        return {"ok": True, "tune_stopped": True}
    if _tune_is_active() and req.tx:
        return {"ok": False, "error": "Remote TUNE is active — press TUNE off first"}
    if sstv_tx.active:
        # Manual PTT during an SSTV transmission can only mean "stop".
        sstv_tx.abort("PTT pressed")
        _broadcast()
        return {"ok": True, "sstv_stopped": True}
    rig.set_ptt(req.tx)
    _broadcast()
    return {"ok": True}


# ── Remote tuner TUNE (MFJ etc.) — not internal ATU ──────────────────────
class TuneReq(BaseModel):
    on: bool
    watts: Optional[int] = None  # default from settings (20W for MFJ-994BRT)


def _tune_is_active() -> bool:
    return bool(remote_tuner and remote_tuner.active)


def _start_tune(watts: Optional[int] = None) -> dict:
    if not remote_tuner:
        return {"ok": False, "error": "Tuner not ready"}
    return remote_tuner.start(watts)


def _stop_tune(restore: bool = True):
    if remote_tuner:
        remote_tuner.stop(restore=restore)
    if not restore:
        return
    # The tuner puts back the mode it borrowed, which is stale if the operator
    # crossed 10 MHz while tuning. Re-assert from the VFO we are actually on.
    s = active_session()
    if s is None:
        return
    try:
        s.follow_freq(int(getattr(rig.state, "freq_a", 0) or 0), force=True)
    except Exception:
        log.exception("sideband re-assert after tune")


@app.post("/api/tune")
async def remote_tune(req: TuneReq):
    """Toggle remote-tuner carrier. Not the internal ATU."""
    if req.on:
        # A held FreeDV PTT or a picture in flight would fight the tune
        # carrier for the transmitter. TUNE is the operator saying "stop
        # what you are doing and key a carrier", so it wins.
        try:
            _fdv_key(False)
        except Exception:
            pass
        if sstv_tx.active:
            sstv_tx.abort("Remote TUNE pressed")
        result = _start_tune(req.watts)
    else:
        _stop_tune(restore=True)
        result = {"ok": True, "tune_active": False}
    _broadcast()
    return result


# ── WSPR mode (auto-tune on band hop + unattended beacon watchdog) ───────
class WsprReq(BaseModel):
    on: bool


@app.post("/api/wspr")
async def wspr_arm(req: WsprReq):
    """Arm/disarm WSPR mode. Arming tunes the band we are already on."""
    if req.on and not rig.is_connected:
        return {"ok": False, "error": "No CAT link — cannot supervise a beacon"}
    st = wspr.arm(True) if req.on else wspr.disarm("operator")
    _broadcast()
    return {"ok": True, **st}


@app.get("/api/wspr")
async def wspr_status():
    return wspr.status()


@app.post("/api/wspr/clear")
async def wspr_clear():
    """Operator has looked at the band and says carry on."""
    st = wspr.clear_hold()
    _broadcast()
    return {"ok": True, **st}


class WsprConfigReq(BaseModel):
    # Free-form so a single key can be poked without resending the rest.
    values: dict


@app.post("/api/wspr/config")
async def wspr_config(req: WsprConfigReq):
    """Change a `wspr_*` setting without restarting.

    Restarting this service drops JTDX's rigctld socket and JTDX does not
    reconnect on its own — it just shows "Rig control error" until the operator
    re-runs Test CAT (hit live 2026-09-03). Tuning a threshold is exactly the
    thing you want to do repeatedly while beacons are running, so it must not
    cost a restart. `WsprAutoTune` reads every threshold through
    `settings.get()` on each use, so a write here takes effect on the next tune.
    """
    allowed = {k for k in DEFAULTS if k.startswith("wspr_")}
    bad = sorted(set(req.values) - allowed)
    if bad:
        return {"ok": False, "error": f"not wspr settings: {', '.join(bad)}",
                "allowed": sorted(allowed)}
    for k, v in req.values.items():
        setattr(settings, k, v)
    settings.save()
    log.info("WSPR settings changed: %s", req.values)
    _broadcast()
    return {"ok": True, "changed": req.values, **wspr.status()}


@app.post("/api/wspr/resume")
async def wspr_resume():
    """Clear a HARD STOP. Separate from /clear on purpose — a hard stop means
    the watchdog fired repeatedly and the antenna deserves a look first."""
    st = wspr.resume()
    _broadcast()
    return {"ok": True, **st}


class TuneCycleReq(BaseModel):
    seconds: Optional[float] = None   # default 4s; clamped below
    watts: Optional[int] = None       # default from settings (20W for MFJ-994BRT)


#: On-demand tune cycle bounds. A remote tune is the operator saying "key a
#: carrier so the MFJ can find a match" -- it is not a mode, and it has no
#: business lasting longer than it takes to converge. The MFJ-994BRT lands in
#: well under 4s on a band it has seen before.
TUNE_CYCLE_DEFAULT_S = 4.0
TUNE_CYCLE_MIN_S = 1.0
TUNE_CYCLE_MAX_S = 15.0

#: Copied from wspr.py's tune watcher, which has 54/54 converged tunes behind
#: it (P139). The MFJ swings wildly while it searches, so a single good sample
#: proves nothing -- SWR has to STAY at or under the accept threshold before
#: this calls it converged.
TUNE_CYCLE_SETTLE_S = 0.8
TUNE_CYCLE_HOLD_S = 0.8
TUNE_CYCLE_POLL_S = 0.10


@app.post("/api/tune/cycle")
async def tune_cycle(req: TuneCycleReq = None):
    """Fire ONE bounded remote-tune carrier and report whether it converged.

    Added 2026-09-13 so an automation client can tune on demand after a band
    change, with no human at the panel. The difference from ``POST /api/tune`` that matters
    is **who owns the stop**: that endpoint toggles, so whoever turned the
    carrier on has to come back and turn it off, and a caller that dies in
    between leaves it keyed until the 90s safety timeout. Here the duration is
    the request, the stop runs in a ``finally`` on this side, and the caller
    cannot leave a carrier on the air by going away. That matters a great deal
    more when the caller is reached over a radio link than when it is a button
    six inches from the operator.

    Deliberately does NOT QSY. The WSPR auto-tuner moves +/-5 kHz before it
    keys because it must never transmit inside a WSPR window; an operator who
    just said "go to twenty and tune it" means tune here. Courtesy about
    picking a clear spot stays with the operator, where it already was.

    SWR is converted through ``meter_cal`` before it is compared or reported.
    Never compare the raw RM6 count -- on 2026-09-01 that reported the operator's
    healthy antenna as SWR 52 and aborted four of his remote tunes.
    """
    req = req or TuneCycleReq()

    if not rig.is_connected:
        return {"ok": False, "error": "Not connected to radio"}
    if _tune_is_active():
        # Not an error worth failing on: a re-sent request during a cycle
        # already in flight should be a no-op, not a second carrier. A voice
        # link makes that retry likely -- silence reads as "it didn't happen".
        return {"ok": True, "already": True, "tune_active": True}
    if rig.state.atu_tuning:
        return {"ok": False, "error": "Internal ATU is already tuning"}
    if sstv_tx.active:
        return {"ok": False, "error": "SSTV is transmitting"}
    if fdv_engine.status().get("ptt"):
        return {"ok": False, "error": "FreeDV is transmitting"}

    seconds = float(req.seconds if req.seconds is not None else TUNE_CYCLE_DEFAULT_S)
    seconds = max(TUNE_CYCLE_MIN_S, min(TUNE_CYCLE_MAX_S, seconds))

    res = _start_tune(req.watts)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "tuner would not start"}

    accept = float(settings.get("wspr_swr_accept", 2.0))
    swr = None
    converged = False
    t0 = time.time()
    try:
        good_since = None
        while (time.time() - t0) < seconds:
            await asyncio.sleep(TUNE_CYCLE_POLL_S)
            elapsed = time.time() - t0
            if elapsed < TUNE_CYCLE_SETTLE_S:
                continue   # PA and MFJ are still ramping; meters mean nothing yet
            raw = getattr(rig.state, "swr_meter", None)
            if raw is None:
                continue
            swr = round(meter_cal.SWR.to_value(float(raw)), 2)
            if swr <= accept:
                good_since = good_since or time.time()
                if (time.time() - good_since) >= TUNE_CYCLE_HOLD_S:
                    converged = True
                    break
            else:
                good_since = None
    finally:
        # The one line that must never be conditional. Whoever keys, unkeys.
        _stop_tune(restore=True)
        _broadcast()

    return {
        "ok": True,
        "converged": converged,
        "swr": swr,
        "accept": accept,
        "seconds": round(time.time() - t0, 1),
        "requested_seconds": seconds,
    }


@app.get("/api/tune")
async def tune_status():
    if not remote_tuner:
        return {"tune_active": False}
    return {
        **remote_tuner.status(),
    }


# ── Power ────────────────────────────────────────────────────────────────
class PowerReq(BaseModel):
    watts: int

@app.post("/api/power")
async def set_power(req: PowerReq):
    if _tune_is_active():
        return {"ok": False, "error": "Remote TUNE is active"}
    rig.set_power(req.watts); return {"ok": True}


# ── Filter ───────────────────────────────────────────────────────────────
class SHReq(BaseModel):
    code: int

class IFShiftReq(BaseModel):
    hz: int

@app.post("/api/sh")
async def set_sh(req: SHReq):
    rig.set_sh(req.code)
    _broadcast()
    return {"ok": True, "sh": rig.state.sh, "sh_hz": sh_hz(rig.state.sh, rig.state.mode)}

@app.post("/api/if_shift")
async def set_if_shift(req: IFShiftReq):
    rig.set_if_shift(req.hz)
    _broadcast()
    return {"ok": True, "if_shift": rig.state.if_shift}


# ── AGC ──────────────────────────────────────────────────────────────────
class AGCReq(BaseModel):
    mode: str

@app.post("/api/agc")
async def set_agc(req: AGCReq):
    rig.set_agc(req.mode); return {"ok": True}


# ── Preamp / ATT ─────────────────────────────────────────────────────────
class StrValReq(BaseModel):
    mode: str

@app.post("/api/preamp")
async def set_preamp(req: StrValReq):
    rig.set_preamp(req.mode); return {"ok": True}

@app.post("/api/att")
async def set_att(req: StrValReq):
    rig.set_att(req.mode); return {"ok": True}


# ── Gain ─────────────────────────────────────────────────────────────────
class IntValReq(BaseModel):
    val: int

@app.post("/api/af_gain")
async def set_af(req: IntValReq):
    rig.set_af_gain(req.val); return {"ok": True}

@app.post("/api/rf_gain")
async def set_rf(req: IntValReq):
    rig.set_rf_gain(req.val); return {"ok": True}

@app.post("/api/mic_gain")
async def set_mic(req: IntValReq):
    rig.set_mic_gain(req.val); return {"ok": True}


# ── NB / NR / DNF ────────────────────────────────────────────────────────
class NBReq(BaseModel):
    on: bool
    level: Optional[int] = None

class BoolReq(BaseModel):
    on: bool

@app.post("/api/nb")
async def set_nb(req: NBReq):
    rig.set_nb(req.on, req.level); return {"ok": True}

@app.post("/api/nr")
async def set_nr(req: NBReq):
    rig.set_nr(req.on, req.level); return {"ok": True}

@app.post("/api/dnf")
async def set_dnf(req: BoolReq):
    rig.set_dnf(req.on); return {"ok": True}


# ── Notch ────────────────────────────────────────────────────────────────
class NotchReq(BaseModel):
    on: bool
    hz: Optional[int] = None

class FreqHzReq(BaseModel):
    hz: int

@app.post("/api/notch")
async def set_notch(req: NotchReq):
    rig.set_notch(req.on, req.hz); return {"ok": True}

@app.post("/api/notch_freq")
async def set_notch_freq(req: FreqHzReq):
    rig.set_notch_freq(req.hz); return {"ok": True}

class NotchWidthReq(BaseModel):
    wide: bool

@app.post("/api/notch_width")
async def set_notch_width(req: NotchWidthReq):
    rig.set_notch_width(req.wide); return {"ok": True}


# ── Contour ───────────────────────────────────────────────────────────────
@app.post("/api/contour")
async def set_contour(req: BoolReq):
    rig.set_contour(req.on); return {"ok": True}

@app.post("/api/contour_freq")
async def set_contour_freq(req: FreqHzReq):
    rig.set_contour_freq(req.hz); return {"ok": True}

@app.post("/api/contour_level")
async def set_contour_level(req: IntValReq):
    rig.set_contour_level(req.val); return {"ok": True}

@app.post("/api/contour_width")
async def set_contour_width(req: IntValReq):
    rig.set_contour_width(req.val); return {"ok": True}

class RoofReq(BaseModel):
    filter: str

@app.post("/api/roofing")
async def set_roofing(req: RoofReq):
    rig.set_roofing(req.filter); return {"ok": True}

@app.post("/api/apf")
async def set_apf(req: BoolReq):
    rig.set_apf(req.on); return {"ok": True}


# ── Split / RIT / XIT ─────────────────────────────────────────────────────
class SplitReq(BaseModel):
    on: bool
    tx_vfo: str = "B"

class RITOffsetReq(BaseModel):
    hz: int

@app.post("/api/split")
async def set_split(req: SplitReq):
    rig.set_split(req.on, req.tx_vfo); return {"ok": True}

@app.post("/api/rit")
async def set_rit(req: BoolReq):
    rig.set_rit(req.on); return {"ok": True}

@app.post("/api/xit")
async def set_xit(req: BoolReq):
    rig.set_xit(req.on); return {"ok": True}

@app.post("/api/rit_clear")
async def rit_clear():
    rig.clear_rit(); return {"ok": True}

@app.post("/api/rit_offset")
async def set_rit_offset(req: RITOffsetReq):
    rig.set_rit_offset(req.hz); return {"ok": True}

@app.post("/api/rit_up")
async def rit_up():
    rig.rit_up(); return {"ok": True}

@app.post("/api/rit_down")
async def rit_down():
    rig.rit_down(); return {"ok": True}


# ── Compressor ────────────────────────────────────────────────────────────
class CompReq(BaseModel):
    on: bool
    level: Optional[int] = None

@app.post("/api/compressor")
async def set_compressor(req: CompReq):
    rig.set_compressor(req.on, req.level); return {"ok": True}


# ── VOX ──────────────────────────────────────────────────────────────────
class VOXReq(BaseModel):
    on: bool
    gain: Optional[int] = None
    delay_ms: Optional[int] = None

@app.post("/api/vox")
async def set_vox(req: VOXReq):
    rig.set_vox(req.on, req.gain, req.delay_ms); return {"ok": True}


# ── Monitor ──────────────────────────────────────────────────────────────
class MonReq(BaseModel):
    on: bool
    level: Optional[int] = None

@app.post("/api/monitor")
async def set_monitor(req: MonReq):
    rig.set_monitor(req.on, req.level); return {"ok": True}


# ── CW ───────────────────────────────────────────────────────────────────
class CWSendReq(BaseModel):
    text: str

class WPMReq(BaseModel):
    wpm: int

class PitchReq(BaseModel):
    hz: int

@app.post("/api/cw_speed")
async def set_cw_speed(req: WPMReq):
    rig.set_cw_speed(req.wpm); return {"ok": True}

@app.post("/api/cw_pitch")
async def set_cw_pitch(req: PitchReq):
    rig.set_cw_pitch(req.hz); return {"ok": True}

@app.post("/api/cw_breakin")
async def set_cw_breakin(req: BoolReq):
    rig.set_cw_breakin(req.on); return {"ok": True}

@app.post("/api/cw_send")
async def cw_send(req: CWSendReq):
    rig.send_cw(req.text); return {"ok": True}


# ── Antenna ───────────────────────────────────────────────────────────────
class AntReq(BaseModel):
    ant: int

@app.post("/api/antenna")
async def set_antenna(req: AntReq):
    rig.set_antenna(req.ant); return {"ok": True}


@app.post("/api/fine")
async def set_fine(req: BoolReq):
    """FINE TUNING (CAT FN) — the radio MAIN dial's step, not the app's.

    Defined here rather than beside the other VFO routes because BoolReq is
    declared further down the file.
    """
    rig.set_fine(req.on)
    return {"ok": True, "fine": rig.state.fine}


# ── Internal ATU (FTDX10 AC command) ─────────────────────────────────────
# Separate from REMOTE TUNE (external MFJ carrier).
@app.post("/api/atu")
async def set_atu(req: BoolReq):
    """Enable/bypass the radio's built-in antenna tuner."""
    rig.set_atu(req.on)
    return {"ok": True, "atu": rig.state.atu, "atu_tuning": rig.state.atu_tuning}

@app.post("/api/atu/tune")
async def atu_tune():
    """Start or abort the internal ATU tuning cycle (keys the radio briefly)."""
    if _tune_is_active():
        return {"ok": False, "error": "Remote TUNE is active — stop it first"}
    # The cycle keys the transmitter itself, so it cannot share the air with
    # a picture already going out or a held FreeDV PTT.
    if sstv_tx.active:
        return {"ok": False, "error": "SSTV is transmitting — abort it first"}
    if fdv_engine.status().get("ptt"):
        return {"ok": False, "error": "FreeDV is transmitting — release PTT first"}
    rig.atu_tune()
    return {"ok": True, "atu": rig.state.atu, "atu_tuning": rig.state.atu_tuning}


# ── Main Rig Power (CAT PS) — radio's own power switch, not app connect ────
@app.post("/api/radio_power")
async def set_radio_power(req: BoolReq):
    rig.set_radio_power(req.on)
    return {"ok": True, "radio_on": rig.state.radio_on}


# ── Parametric EQ (EX 03 03: TX DSP EQ / Mic P-EQ) ───────────────────────
# Read on demand only (not part of the continuous poll) — matches the old
# desktop panel's "Read from Radio" UX; these are radio-menu items, not
# things that drift on their own.
class EQReadReq(BaseModel):
    section: str  # "tx" | "mic"
    band: int     # 0/1/2 = low/mid/high

class EQWriteReq(BaseModel):
    section: str
    band: int
    freq: int   # raw EX code, see EQ_FREQ_OPTIONS per band range
    level: int  # signed dB, -20..+10
    bw: int     # 1..10

@app.post("/api/eq/read")
async def eq_read(req: EQReadReq):
    val = rig.get_eq_band(req.section, req.band)
    if val is None:
        return {"ok": False, "error": "no response from radio"}
    return {"ok": True, **val}

@app.post("/api/eq/write")
async def eq_write(req: EQWriteReq):
    rig.set_eq_band(req.section, req.band, req.freq, req.level, req.bw)
    return {"ok": True}


# ── EX menu ───────────────────────────────────────────────────────────────
class EXReq(BaseModel):
    p1: int; p2: int; p3: int

class EXWriteReq(BaseModel):
    p1: int; p2: int; p3: int; value: str

@app.post("/api/ex_read")
async def ex_read(req: EXReq):
    val = rig.ex_read(req.p1, req.p2, req.p3)
    return {"ok": True, "value": val}

@app.post("/api/ex_write")
async def ex_write(req: EXWriteReq):
    rig.ex_write(req.p1, req.p2, req.p3, req.value)
    return {"ok": True}


# ── Macros ────────────────────────────────────────────────────────────────
@app.get("/api/macros")
async def get_macros():
    return [m.to_dict() for m in macros]

@app.post("/api/macros/run/{idx}")
async def run_macro(idx: int):
    if idx < 0 or idx >= len(macros):
        return {"ok": False, "error": "index out of range"}
    if runner and runner.is_running():
        runner.stop()
    def on_done(completed):
        _broadcast()
    runner.run(macros[idx], on_done=on_done)
    return {"ok": True}

@app.post("/api/macros/stop")
async def stop_macro():
    if runner:
        runner.stop()
    return {"ok": True}


class MacrosSaveReq(BaseModel):
    macros: list[dict]

@app.post("/api/macros/save")
async def save_macros_endpoint(req: MacrosSaveReq):
    """Replace the whole macro list (editor does full add/edit/delete/reorder client-side)."""
    new_macros = [Macro.from_dict(d) for d in req.macros]
    macros[:] = new_macros
    save_macros(macros)
    return {"ok": True, "macros": [m.to_dict() for m in macros]}

@app.post("/api/macros/reset_defaults")
async def reset_macros_endpoint():
    macros[:] = reset_macros_to_defaults()
    return {"ok": True, "macros": [m.to_dict() for m in macros]}


# ── Memory channels ───────────────────────────────────────────────────────
# Snapshots of freq/mode/power/filter width/ATT/preamp. Store = capture the
# rig's current live state; Recall = write those settings back over CAT.
@app.get("/api/memories")
async def get_memories():
    return [m.to_dict() for m in memories]


class MemStoreReq(BaseModel):
    name: str

@app.post("/api/memories/store")
async def store_memory(req: MemStoreReq):
    ch = snapshot(rig.state, req.name.strip() or f"Ch {len(memories) + 1}")
    memories.append(ch)
    save_memories(memories)
    return {"ok": True, "memories": [m.to_dict() for m in memories]}


class MemStoreSlotReq(BaseModel):
    name: str

@app.post("/api/memories/store_slot/{idx}")
async def store_memory_slot(idx: int, req: MemStoreSlotReq):
    """Store into a fixed slot index (used by the main-window hypermemory bar),
    padding with blank channels so slot numbering stays stable regardless of
    how many memories exist yet."""
    if idx < 0:
        return {"ok": False, "error": "index out of range"}
    ch = snapshot(rig.state, req.name.strip() or f"CH{idx + 1:02d}")
    while len(memories) <= idx:
        memories.append(MemoryChannel(name=f"CH{len(memories) + 1:02d}"))
    memories[idx] = ch
    save_memories(memories)
    return {"ok": True, "memories": [m.to_dict() for m in memories]}


@app.post("/api/memories/recall/{idx}")
async def recall_memory(idx: int):
    if idx < 0 or idx >= len(memories):
        return {"ok": False, "error": "index out of range"}
    # Ordering (mode before filter width) lives in cyberrig.memories.ops.
    recall_channel(rig, memories[idx])
    _broadcast()
    return {"ok": True}


@app.post("/api/memories/delete/{idx}")
async def delete_memory(idx: int):
    if idx < 0 or idx >= len(memories):
        return {"ok": False, "error": "index out of range"}
    memories.pop(idx)
    save_memories(memories)
    return {"ok": True, "memories": [m.to_dict() for m in memories]}


class MemoriesSaveReq(BaseModel):
    memories: list[dict]

@app.post("/api/memories/save")
async def save_memories_endpoint(req: MemoriesSaveReq):
    """Bulk save after editing fields / reordering in the memory panel."""
    new_memories = [MemoryChannel.from_dict(d) for d in req.memories]
    memories[:] = new_memories
    save_memories(memories)
    return {"ok": True, "memories": [m.to_dict() for m in memories]}
