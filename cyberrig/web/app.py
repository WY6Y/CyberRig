"""CyberRig FastAPI backend — WebSocket state push + REST control endpoints."""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cyberrig.audio.tx_audio import tx_engine
from cyberrig.audio.waterfall import engine as wf_engine
from cyberrig.cat.ftdx10 import FTdx10, sh_hz
from cyberrig.macros.engine import MacroRunner
from cyberrig.macros.store import load_macros, save_macros
from cyberrig.server.rigctl import RigctlServer
from cyberrig.settings import Settings

log = logging.getLogger("cyberrig.web")

_STATIC = Path(__file__).parent / "static"

app = FastAPI(title="CyberRig")
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# ── Globals ────────────────────────────────────────────────────────────────
settings = Settings()
rig = FTdx10()
runner: Optional[MacroRunner] = None
macros = load_macros()
_rigctld: Optional[RigctlServer] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_wf_clients: list[WebSocket] = []
_audio_clients: list[WebSocket] = []

# Remote-tuner TUNE (MFJ-994BRT etc.) — NOT the radio internal ATU (AC cmd)
_tune_active = False
_tune_saved_mode: Optional[str] = None
_tune_saved_power: Optional[int] = None
_tune_started: float = 0.0
_tune_timeout_task: Optional[asyncio.Task] = None


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
        "apf": s.apf,
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
        "macro_running": runner.is_running() if runner else False,
        "tune_active": _tune_active,
        "tune_watts": int(settings.get("tune_watts", 20)),
        "tune_mode": settings.get("tune_mode", "AM"),
        "tune_timeout_sec": int(settings.get("tune_timeout_sec", 90)),
        "po_max_watts": int(settings.get("po_max_watts", 100)),
        "po_cal": float(settings.get("po_cal", 0.67)),
    }


def _broadcast():
    """Schedule a WebSocket broadcast from any thread."""
    if _event_loop and not _event_loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(_state_dict()), _event_loop
        )


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
        "dnf_changed", "notch_changed", "contour_changed", "comp_changed",
        "vox_changed", "cw_changed", "af_changed", "rf_changed", "antenna_changed",
        "power_changed", "mic_changed", "monitor_changed", "lock_changed",
        "atu_changed",
    ]:
        rig.on(event, lambda *_: _broadcast())

    runner = MacroRunner(rig, settings.callsign)
    wf_engine.on_row(_wf_push_row)
    wf_engine.on_pcm(_wf_push_pcm)

    port = settings.cat_port
    baud = settings.cat_baud
    if rig.connect(port, baud):
        rig.start_polling(settings.poll_interval)
        log.info("Connected to %s @ %d", port, baud)
    else:
        log.warning("Could not connect to %s — UI will show Disconnected", port)

    _rigctld = RigctlServer(rig, port=settings.rigctl_port)
    _rigctld.start()
    log.info("rigctld listening on port %d", settings.rigctl_port)


@app.on_event("shutdown")
async def _shutdown():
    _stop_tune(restore=True)
    try:
        # Safety: never leave the radio keyed
        rig.set_ptt(False)
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
    acquired = False
    # Hello + status
    await ws.send_text(json.dumps({
        "type": "hello",
        "status": wf_engine.status(),
        "devices": wf_engine.list_devices(),
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
                idx = msg.get("device_index", None)
                result = wf_engine.acquire("waterfall", idx)
                acquired = result.get("ok", False)
                await ws.send_text(json.dumps({"type": "status", **result}))
            elif mtype == "stop":
                if acquired:
                    wf_engine.release("waterfall")
                    acquired = False
                await ws.send_text(json.dumps({"type": "status", **wf_engine.status(), "ok": True}))
            elif mtype == "range":
                wf_engine.set_range(float(msg.get("db_min", -90)), float(msg.get("db_max", -20)))
                await ws.send_text(json.dumps({"type": "status", **wf_engine.status(), "ok": True}))
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
        if acquired:
            try:
                wf_engine.release("waterfall")
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


def _usb_tx_mod_apply_fast():
    """Write-only USB TX routing (no EX reads — avoids serial timeouts on key-down)."""
    try:
        rig.ex_write(1, 1, 13, "1")   # SSB MOD = REAR
        rig.ex_write(1, 1, 14, "1")   # SSB REAR = USB
        rig.ex_write(1, 1, 15, "080")  # RPORT gain
        rig.ex_write(1, 5, 15, "1")   # DATA MOD = REAR
    except Exception as e:
        log.warning("usb_tx_mod apply: %s", e)


def _usb_tx_mod_setup() -> dict:
    """Point FTDX10 SSB (and DATA) TX audio at USB rear path.

    Required for remote mic → Yaesu USB AUDIO CODEC → RF:
      EX010113 = SSB MOD SOURCE → REAR (1)   [was often MIC=0]
      EX010114 = SSB REAR SELECT → USB (1)   [was often DATA=0]
      EX010115 = SSB RPORT GAIN → raise for drive
      EX010515 = DATA MOD SOURCE → REAR (1)
    Returns previous values for restore.
    """
    saved = {}
    try:
        saved["ssb_mod"] = rig.ex_read(1, 1, 13)
        saved["ssb_rear"] = rig.ex_read(1, 1, 14)
        saved["ssb_gain"] = rig.ex_read(1, 1, 15)
        saved["data_mod"] = rig.ex_read(1, 5, 15)
    except Exception as e:
        log.warning("usb_tx_mod read: %s", e)

    try:
        _usb_tx_mod_apply_fast()
        log.info(
            "USB TX mod setup: SSB MOD=REAR REAR=USB RPORT=80 (was mod=%s rear=%s gain=%s)",
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
            rig.ex_write(1, 5, 15, str(saved["data_mod"]))
        log.info("USB TX mod restored: %s", saved)
    except Exception as e:
        log.warning("usb_tx_mod restore: %s", e)


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
                    if _tune_active:
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
                    _broadcast()
                    await ws.send_text(json.dumps({"type": "ptt", "ok": True, "on": True}))
                elif not on and ptt_on:
                    try:
                        rig.set_ptt(False)
                    except Exception as e:
                        log.warning("ptt off: %s", e)
                    ptt_on = False
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


# ── Static UI / PWA ───────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/waterfall")
async def waterfall_page():
    """Separate-window audio FFT waterfall (FTDX10 USB audio on the shack PC)."""
    return FileResponse(str(_STATIC / "waterfall.html"))


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
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(str(_STATIC / "icons" / "favicon.ico"))


# ── State ────────────────────────────────────────────────────────────────
@app.get("/api/state")
async def get_state():
    return _state_dict()


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
    return wf_engine.acquire("api", req.device_index)


@app.post("/api/waterfall/stop")
async def wf_stop():
    return wf_engine.release("api")


@app.get("/api/waterfall/status")
async def wf_status():
    return wf_engine.status()


# ── Connection ────────────────────────────────────────────────────────────
class ConnectReq(BaseModel):
    port: str
    baud: int = 38400

@app.post("/api/connect")
async def do_connect(req: ConnectReq):
    if rig.is_connected:
        rig.disconnect()
    ok = rig.connect(req.port, req.baud)
    if ok:
        settings.cat_port = req.port
        settings.cat_baud = req.baud
        settings.save()
        rig.start_polling(settings.poll_interval)
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

@app.post("/api/vfo_swap")
async def vfo_swap():
    rig.swap_vfo(); return {"ok": True}

@app.post("/api/vfo_a_to_b")
async def vfo_a_to_b():
    rig.vfo_a_to_b(); return {"ok": True}

@app.post("/api/vfo_b_to_a")
async def vfo_b_to_a():
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
    if _tune_active and not req.tx:
        _stop_tune(restore=True)
        _broadcast()
        return {"ok": True, "tune_stopped": True}
    if _tune_active and req.tx:
        return {"ok": False, "error": "Remote TUNE is active — press TUNE off first"}
    rig.set_ptt(req.tx)
    _broadcast()
    return {"ok": True}


# ── Remote tuner TUNE (MFJ etc.) — not internal ATU ──────────────────────
class TuneReq(BaseModel):
    on: bool
    watts: Optional[int] = None  # default from settings (20W for MFJ-994BRT)


def _cancel_tune_timeout():
    global _tune_timeout_task
    if _tune_timeout_task and not _tune_timeout_task.done():
        _tune_timeout_task.cancel()
    _tune_timeout_task = None


def _stop_tune(restore: bool = True):
    """Drop carrier and optionally restore pre-tune mode/power."""
    global _tune_active, _tune_saved_mode, _tune_saved_power, _tune_started
    _cancel_tune_timeout()
    was = _tune_active
    saved_mode = _tune_saved_mode
    saved_power = _tune_saved_power
    _tune_active = False
    _tune_started = 0.0
    _tune_saved_mode = None
    _tune_saved_power = None
    try:
        rig.set_ptt(False)
    except Exception as e:
        log.warning("tune stop PTT: %s", e)
    # FTDX10 often ignores MD/PC while still in TX settle — brief pause
    if restore and was:
        time.sleep(0.25)
        try:
            if saved_mode:
                rig.set_mode(saved_mode)
            if saved_power is not None:
                rig.set_power(int(saved_power))
            log.info("Remote TUNE stopped — restored %s @ %sW",
                     saved_mode, saved_power)
        except Exception as e:
            log.warning("tune restore: %s", e)
    elif was:
        log.info("Remote TUNE stopped")


async def _tune_timeout_watch(seconds: int):
    try:
        await asyncio.sleep(seconds)
        if _tune_active:
            log.warning("Remote TUNE auto-stopped after %ds safety timeout", seconds)
            _stop_tune(restore=True)
            _broadcast()
    except asyncio.CancelledError:
        return


def _start_tune(watts: Optional[int] = None) -> dict:
    """Key a continuous AM carrier at ~20W for remote autotuners (MFJ-994BRT).

    Does NOT run the FTDX10 internal ATU (AC command). Separate control only.
    """
    global _tune_active, _tune_saved_mode, _tune_saved_power, _tune_started
    global _tune_timeout_task

    if not rig.is_connected:
        return {"ok": False, "error": "Not connected to radio"}
    if _tune_active:
        return {"ok": True, "already": True}

    mode = str(settings.get("tune_mode", "AM") or "AM")
    w = int(watts if watts is not None else settings.get("tune_watts", 20))
    w = max(5, min(100, w))
    timeout = int(settings.get("tune_timeout_sec", 90))

    # Snapshot operator state
    _tune_saved_mode = rig.state.mode or "USB"
    _tune_saved_power = rig.state.power if rig.state.power else 50
    try:
        pc = rig.get_power()
        if pc is not None:
            _tune_saved_power = pc
    except Exception:
        pass

    # Unkey first if already TX
    if rig.state.is_tx:
        rig.set_ptt(False)
        time.sleep(0.15)

    try:
        rig.set_power(w)
        time.sleep(0.05)
        rig.set_mode(mode)
        time.sleep(0.1)
        # AM/FM: CAT TX1 produces continuous carrier (unlike CW without key)
        rig.set_ptt(True)
    except Exception as e:
        log.exception("tune start failed")
        _stop_tune(restore=True)
        return {"ok": False, "error": str(e)}

    _tune_active = True
    _tune_started = time.time()
    log.info("Remote TUNE ON — %s @ %dW (saved %s @ %sW)",
             mode, w, _tune_saved_mode, _tune_saved_power)

    _cancel_tune_timeout()
    if _event_loop and timeout > 0:
        _tune_timeout_task = _event_loop.create_task(_tune_timeout_watch(timeout))

    return {
        "ok": True,
        "tune_active": True,
        "mode": mode,
        "watts": w,
        "timeout_sec": timeout,
        "saved_mode": _tune_saved_mode,
        "saved_power": _tune_saved_power,
    }


@app.post("/api/tune")
async def remote_tune(req: TuneReq):
    """Toggle remote-tuner carrier. Not the internal ATU."""
    if req.on:
        result = _start_tune(req.watts)
    else:
        _stop_tune(restore=True)
        result = {"ok": True, "tune_active": False}
    _broadcast()
    return result


@app.get("/api/tune")
async def tune_status():
    return {
        "tune_active": _tune_active,
        "tune_watts": int(settings.get("tune_watts", 20)),
        "tune_mode": settings.get("tune_mode", "AM"),
        "tune_timeout_sec": int(settings.get("tune_timeout_sec", 90)),
        "elapsed": (time.time() - _tune_started) if _tune_active else 0,
        "saved_mode": _tune_saved_mode,
        "saved_power": _tune_saved_power,
    }


# ── Power ────────────────────────────────────────────────────────────────
class PowerReq(BaseModel):
    watts: int

@app.post("/api/power")
async def set_power(req: PowerReq):
    if _tune_active:
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


# ── Contour ───────────────────────────────────────────────────────────────
@app.post("/api/contour")
async def set_contour(req: BoolReq):
    rig.set_contour(req.on); return {"ok": True}

@app.post("/api/contour_freq")
async def set_contour_freq(req: FreqHzReq):
    rig.set_contour_freq(req.hz); return {"ok": True}

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
    if _tune_active:
        return {"ok": False, "error": "Remote TUNE is active — stop it first"}
    rig.atu_tune()
    return {"ok": True, "atu": rig.state.atu, "atu_tuning": rig.state.atu_tuning}


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
