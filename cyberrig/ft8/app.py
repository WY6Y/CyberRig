"""FT8 listen-only demo server.

Runs as its own process on its own port, deliberately separate from
`cyberrig-panel`. The panel owns the physical CAT port and serves Hamlib NET
rigctl on 4532, which is how JTDX talks to the radio -- restarting it while the
operator is working would take his rig control down mid-QSO. Nothing here needs
the panel, so nothing here should be able to disturb it.

TRANSMIT IS BUILT BUT DISARMED. The guards now exist -- `TxGuard` (SWR trip,
stuck-TX, cycle cap, dead-man) and `SlotController` (XIT, verified, auto-cleared)
-- and Reply(4)/HaltTx(8) are wired. What is deliberately NOT wired is anything
that transmits without an explicit arm: `/api/tx/arm` must be called first, it
refuses if the operator's browser is gone, and it disarms on every trip.

HALT is the exception and is always available, armed or not. A stop must never
require a precondition.

Two inputs, both passive:
  * decodes  -- tailed from JTDX's ALL.TXT (see decodes.py for why not UDP)
  * spectrum -- the dsnoop PCM, shared with JTDX (see waterfall.py)

One WebSocket carries both. Text frames are JSON events; binary frames are
waterfall rows with an 8-byte header. Mixing them on one socket keeps the client
simple and means the row stream and the decode stream cannot drift apart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from cyberrig.ft8 import jtdx as jtdxproto
from cyberrig.ft8.cty import CtyDatabase, find_cty_dat
from cyberrig.ft8.decodes import DecodeTailer, default_log_dir
from cyberrig.ft8.rig import FT8_BAND_HZ, RigClient, SlotController, SlotError
from cyberrig.ft8.roster import Roster
from cyberrig.ft8.tx import TxGuard
from cyberrig.ft8.waterfall import FT8Waterfall

log = logging.getLogger("cyberrig.ft8.app")

STATIC = Path(__file__).parent / "static"
MAX_DECODES = 500

# Hold the shared codec only while someone is actually looking at the waterfall.
#
# dsnoop shares CAPTURE, but cyberrig-panel's LISTEN opens the codec as a DUPLEX
# stream on the raw device, and that fails while dsnoop holds the capture side.
# On 2026-09-01 this app sat idle holding the PCM and LISTEN went silent on
# the operator's phone with no error anywhere. Until the panel is taught to use
# `ftdx10_rx` too, the polite thing is to let go when nobody is connected.
IDLE_RELEASE_S = 45.0

app = FastAPI(title="CyberRig FT8 (listen-only)")

_state: dict[str, Any] = {
    "cty": None,
    "roster": None,
    "tailer": None,
    "waterfall": None,
    "decodes": deque(maxlen=MAX_DECODES),
    "clients": set(),
    "loop": None,
    "rig": None,
    "slot": None,
    "guard": None,
    "jtdx": None,
    "meters": {},
}


# ----------------------------------------------------------------- broadcast

def _fanout_json(payload: dict):
    """Thread-safe push of a JSON event to every connected client."""
    loop = _state.get("loop")
    if loop is None:
        return
    text = json.dumps(payload, separators=(",", ":"))
    for ws in list(_state["clients"]):
        asyncio.run_coroutine_threadsafe(_safe_send_text(ws, text), loop)


def _fanout_row(payload: bytes, meta: dict):
    loop = _state.get("loop")
    if loop is None:
        return
    # 8-byte header: seq u32, bins u16, floor_db*10 i16. Keeps the row a single
    # frame -- a separate JSON metadata message per row would double the message
    # rate for 8 bytes of information.
    header = struct.pack("<IHh", meta["seq"] & 0xFFFFFFFF, meta["bins"],
                         int(meta["floor_db"] * 10))
    frame = header + payload
    for ws in list(_state["clients"]):
        asyncio.run_coroutine_threadsafe(_safe_send_bytes(ws, frame), loop)


async def _safe_send_text(ws: WebSocket, text: str):
    try:
        await ws.send_text(text)
    except Exception:
        _state["clients"].discard(ws)


async def _safe_send_bytes(ws: WebSocket, data: bytes):
    try:
        await ws.send_bytes(data)
    except Exception:
        _state["clients"].discard(ws)


# ------------------------------------------------------------------ handlers

def _on_decode(rec: dict):
    roster: Roster = _state["roster"]
    call = rec.get("dx_call")
    rec["roster"] = roster.classify(call) if call else {"status": "unknown"}
    _state["decodes"].append(rec)
    if not rec.get("backfill"):
        _fanout_json({"type": "decode", "decode": rec})


def _on_event(evt: dict):
    _fanout_json({"type": "event", "event": evt})


# -------------------------------------------------------------------- routes

@app.on_event("startup")
async def _startup():
    _state["loop"] = asyncio.get_running_loop()

    cty_path = find_cty_dat()
    if cty_path is None:
        raise RuntimeError("no cty.dat found -- roster filtering cannot work")
    cty = CtyDatabase.load(cty_path)
    _state["cty"] = cty

    # Optional: an ATNO-style /api/bootstrap service that knows which DXCC
    # entities you still need. Unset, the roster tags entity/continent only.
    roster = Roster(cty, url=os.getenv("ATNO_URL", ""))
    roster.refresh(force=True)
    _state["roster"] = roster

    tailer = DecodeTailer(default_log_dir(), on_decode=_on_decode, on_event=_on_event)
    tailer.start()
    _state["tailer"] = tailer

    wf = FT8Waterfall(device=os.getenv("FT8_AUDIO_DEVICE", "ftdx10_rx"))
    if wf.start():
        wf.add_listener(_fanout_row)
    else:
        log.warning("waterfall unavailable: %s", wf.error)
    _state["waterfall"] = wf

    rig = RigClient(os.getenv("CYBERRIG_URL", "http://127.0.0.1:8000"))
    slot = SlotController(rig)
    _state["rig"] = rig
    _state["slot"] = slot

    # OFF BY DEFAULT, and that is not timidity: 2237 is a SHARED resource and
    # we do not need it. Decodes come from ALL.TXT; the UDP socket buys only
    # Reply(4) and HaltTx(8), which are transmit features, and transmit is
    # disarmed. Whoever binds 2237 first wins it, so when this became a
    # systemd service on 2026-09-05 it started at boot, took the port, and
    # GridTracker silently received nothing all evening. A background service
    # must not hold a port an interactive app needs in order to work at all.
    #
    # Set FT8_JTDX_UDP=1 to claim it (start this AFTER GridTracker and you
    # will simply lose the race). The real fix for wanting both is multicast:
    # point JTDX's UDP server at 239.255.0.1 and every client gets a copy.
    client = jtdxproto.JtdxClient(
        host=os.getenv("JTDX_UDP_HOST", "127.0.0.1"),
        port=int(os.getenv("JTDX_UDP_PORT", "2237")),
        on_message=_on_jtdx)
    if os.getenv("FT8_JTDX_UDP", "0") in ("1", "true", "yes"):
        try:
            client.start()
        except OSError as e:
            # Almost always GridTracker already holding 2237. Not fatal:
            # decodes come from ALL.TXT, only Reply/HaltTx are lost.
            log.warning("JTDX UDP unavailable (%s) — receive-only, "
                        "no Reply/HaltTx", e)
    else:
        log.info("JTDX UDP not claimed (FT8_JTDX_UDP unset) — 2237 left for "
                 "GridTracker; decodes come from ALL.TXT either way")
    _state["jtdx"] = client

    guard = TxGuard(read_state=_read_rig_state, drop_ptt=_drop_ptt,
                    halt_jtdx=_halt_jtdx, on_trip=_on_trip)
    guard.start()
    _state["guard"] = guard

    _state["idle_task"] = asyncio.create_task(_idle_watch())


@app.on_event("shutdown")
async def _shutdown():
    t = _state.get("idle_task")
    if t:
        t.cancel()
    if _state.get("tailer"):
        _state["tailer"].stop()
    if _state.get("waterfall"):
        _state["waterfall"].stop()
    if _state.get("guard"):
        _state["guard"].stop()
    if _state.get("jtdx"):
        _state["jtdx"].stop()
    if _state.get("slot"):
        # Never leave an offset applied across a restart.
        _state["slot"].on_shutdown()


def _read_rig_state() -> dict:
    st = _state["rig"].state()
    _state["meters"] = {
        "swr": st.get("swr_meter"), "alc": st.get("alc_meter"),
        "po": st.get("power_meter"), "rf_gain": st.get("rf_gain"),
        "power": st.get("power"), "is_tx": st.get("is_tx"),
        "freq": st.get("freq_a"), "mode": st.get("mode"),
        "xit": st.get("xit"),
    }
    return st


def _drop_ptt():
    _state["rig"].ptt(False)


def _halt_jtdx():
    c = _state.get("jtdx")
    if c:
        c.halt_tx(auto=False)


def _on_trip(trip):
    # A trip ends the transmission, so the slot offset goes with it.
    if _state.get("slot"):
        _state["slot"].on_halt()
    _fanout_json({"type": "trip", "trip": trip.as_dict()})
    log.error("TX trip surfaced to clients: %s", trip.detail)


async def _idle_watch():
    """Release the codec after IDLE_RELEASE_S with no clients; reclaim on demand."""
    idle_since: Optional[float] = time.time()
    while True:
        await asyncio.sleep(5)
        wf = _state.get("waterfall")
        if wf is None:
            continue
        if _state["clients"]:
            idle_since = None
            if not wf.running:
                if wf.start():
                    wf.add_listener(_fanout_row)
                    log.info("client connected — waterfall reclaimed the codec")
                else:
                    log.warning("could not reclaim codec: %s", wf.error)
        else:
            if idle_since is None:
                idle_since = time.time()
            elif wf.running and (time.time() - idle_since) > IDLE_RELEASE_S:
                wf.remove_listener(_fanout_row)
                wf.stop()
                log.info("no clients for %.0fs — released the codec so LISTEN/SSTV "
                         "can use it", IDLE_RELEASE_S)


def _on_jtdx(msg: dict):
    if msg.get("type") == jtdxproto.STATUS:
        _fanout_json({"type": "jtdx", "status": _state["jtdx"].status()})


@app.get("/")
async def index():
    # These pages are meant to be installed as PWAs, and FileResponse sends ETag +
    # Last-Modified but no Cache-Control -- which is how an iOS WebView caches a
    # deployed page heuristically and pull-to-refresh never reaches it. Force
    # revalidation. (Incident 2026-08-29.)
    return FileResponse(
        STATIC / "ft8.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/status")
async def status():
    tailer: DecodeTailer = _state["tailer"]
    wf: FT8Waterfall = _state["waterfall"]
    roster: Roster = _state["roster"]
    return JSONResponse({
        "ok": True,
        "tx_capable": True,
        "dial_mhz": tailer.dial_mhz,
        "mode": tailer.mode,
        "decodes_held": len(_state["decodes"]),
        "last_log_line_age_s": round(time.time() - tailer.last_line_at, 1)
                               if tailer.last_line_at else None,
        "audio_loss_events": tailer.loss_events,
        "waterfall": wf.config() if wf else None,
        "roster": roster.status(),
        "clients": len(_state["clients"]),
    })


@app.get("/api/tx/status")
async def tx_status():
    g, sl, c = _state["guard"], _state["slot"], _state["jtdx"]
    return JSONResponse({
        "guard": g.status() if g else None,
        "slot": sl.status() if sl else None,
        "jtdx": c.status() if c else None,
        "meters": _state.get("meters", {}),
    })


@app.post("/api/tx/arm")
async def tx_arm(body: dict = None):
    body = body or {}
    g, sl = _state["guard"], _state["slot"]
    ok_slot, why = sl.safe_to_transmit()
    if not ok_slot:
        return JSONResponse({"ok": False, "error": why}, status_code=409)
    if not g.arm(cycles=int(body.get("cycles", 0)) or None):
        return JSONResponse(
            {"ok": False, "error": "dead-man expired — reconnect and try again"},
            status_code=409)
    return JSONResponse({"ok": True, "guard": g.status()})


@app.post("/api/tx/halt")
async def tx_halt():
    """Always available, armed or not. A stop must never have a precondition."""
    _drop_ptt()
    _halt_jtdx()
    _state["guard"].disarm("halt")
    _state["slot"].on_halt()
    return JSONResponse({"ok": True})


@app.post("/api/tx/slot")
async def tx_slot(body: dict):
    sl, c = _state["slot"], _state["jtdx"]
    tx_df = (c.status() or {}).get("tx_df") if c else None
    if tx_df is None:
        return JSONResponse(
            {"ok": False, "error": "no JTDX status yet — tx_df unknown"},
            status_code=409)
    try:
        st = sl.set_slot(int(body["hz"]), tx_df=int(tx_df))
    except SlotError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": st["verified"], "slot": st})


@app.post("/api/tx/slot/clear")
async def tx_slot_clear():
    return JSONResponse({"ok": True, "slot": _state["slot"].clear("operator")})


@app.post("/api/band")
async def set_band(body: dict):
    band = str(body.get("band", "")).lower()
    if band not in FT8_BAND_HZ:
        return JSONResponse({"ok": False, "error": f"unknown band {band!r}"},
                            status_code=400)
    # Order matters: drop the offset BEFORE moving, so no window exists where
    # the radio is on a new band with an old band's XIT still applied.
    _state["slot"].on_band_change()
    _state["rig"].set_freq(FT8_BAND_HZ[band])
    return JSONResponse({"ok": True, "band": band, "hz": FT8_BAND_HZ[band]})


@app.post("/api/roster/refresh")
async def roster_refresh():
    ok = _state["roster"].refresh(force=True)
    return JSONResponse({"ok": ok, "roster": _state["roster"].status()})


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    _state["clients"].add(websocket)
    wf_now: FT8Waterfall = _state.get("waterfall")
    if wf_now is not None and not wf_now.running:
        # Reclaim now rather than on the next idle tick — a blank waterfall for
        # five seconds reads as a broken app.
        if wf_now.start():
            wf_now.add_listener(_fanout_row)
    tailer: DecodeTailer = _state["tailer"]
    wf: FT8Waterfall = _state["waterfall"]

    try:
        await websocket.send_text(json.dumps({
            "type": "hello",
            "waterfall": wf.config() if wf else None,
            "dial_mhz": tailer.dial_mhz,
            "mode": tailer.mode,
            "tx_capable": True,
            "tx": {
                "guard": _state["guard"].status() if _state.get("guard") else None,
                "slot": _state["slot"].status() if _state.get("slot") else None,
                "jtdx": _state["jtdx"].status() if _state.get("jtdx") else None,
            },
            "bands": sorted(FT8_BAND_HZ),
            "roster": _state["roster"].status(),
            # Seed the list so the UI is populated the instant it opens rather
            # than waiting up to 15 s for the next cycle.
            "decodes": list(_state["decodes"])[-200:],
        }, separators=(",", ":")))

        while True:
            # Every client message is a dead-man heartbeat. This is the ONLY
            # input that tells the guard the operator is still there.
            await websocket.receive_text()
            if _state.get("guard"):
                _state["guard"].heartbeat()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        _state["clients"].discard(websocket)


def main():
    import uvicorn
    logging.basicConfig(
        level=os.getenv("FT8_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = int(os.getenv("FT8_PORT", "8010"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
