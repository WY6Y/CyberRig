"""FreeDV Reporter client (qso.freedv.org) — socket.io poller.

View-only if call/grid are empty. With both set, role=report so your
call appears on qso.freedv.org. Protocol matches freedv-gui / radae_decoder.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

log = logging.getLogger("cyberrig.freedv")

HOST = "https://qso.freedv.org"
PATH = "/socket.io/"
UA = "CyberRig-FreeDV/1.0"


def _mhz(freq) -> float:
    try:
        f = float(freq or 0)
    except (TypeError, ValueError):
        return 0.0
    if f > 1_000_000:
        return f / 1_000_000.0
    if f > 3_000:
        return f / 1_000.0
    return f


def _hz(freq) -> int:
    mhz = _mhz(freq)
    return int(round(mhz * 1_000_000)) if mhz else 0


def apply_event(stations: dict, name: str, data: Any) -> None:
    # bulk_update is a *batch* of events, not always a full snapshot.
    # Clearing here wiped the initial dump as soon as a later rx_report batch arrived.
    if name == "bulk_update" and isinstance(data, list):
        for item in data:
            if isinstance(item, list) and len(item) >= 2:
                apply_event(stations, item[0], item[1])
        return
    if not isinstance(data, dict):
        return
    if name == "new_connection":
        sid = data.get("sid")
        if sid:
            st = stations.setdefault(sid, {})
            st.update(data)
    elif name == "remove_connection":
        stations.pop(data.get("sid"), None)
    elif name in ("freq_change", "tx_report", "rx_report", "message_update"):
        sid = data.get("sid")
        if not sid:
            return
        st = stations.setdefault(sid, {})
        if name == "tx_report":
            if data.get("mode"):
                st["mode"] = data["mode"]
            st["transmitting"] = bool(data.get("transmitting"))
            if data.get("last_tx"):
                st["last_tx"] = data["last_tx"]
            if data.get("callsign"):
                st["callsign"] = data["callsign"]
        elif name == "freq_change":
            if "freq" in data:
                st["freq"] = data["freq"]
            st["rx_callsign"] = ""
            st["rx_snr"] = None
        elif name == "rx_report":
            st["rx_callsign"] = data.get("callsign") or ""
            st["rx_mode"] = data.get("mode") or ""
            st["rx_snr"] = data.get("snr")
            if not st.get("callsign") and data.get("receiver_callsign"):
                st["callsign"] = data["receiver_callsign"]
            if not st.get("grid_square") and data.get("receiver_grid_square"):
                st["grid_square"] = data["receiver_grid_square"]
        elif name == "message_update":
            st["message"] = data.get("message") or ""
        if data.get("grid_square") and name != "rx_report":
            st["grid_square"] = data["grid_square"]
        if data.get("last_update"):
            st["last_update"] = data["last_update"]


def stations_to_rows(stations: dict) -> list[dict]:
    rows = []
    for sid, s in stations.items():
        hz = _hz(s.get("freq"))
        if not hz and not s.get("callsign"):
            continue
        rows.append({
            "sid": sid,
            "callsign": s.get("callsign") or "?",
            "grid": s.get("grid_square") or "",
            "hz": hz,
            "mhz": round(hz / 1_000_000.0, 3) if hz else 0.0,
            "mode": s.get("mode") or "",
            "tx": bool(s.get("transmitting")),
            "message": s.get("message") or "",
            "last_tx": s.get("last_tx") or "",
            "last_update": s.get("last_update") or "",
            "rx_callsign": s.get("rx_callsign") or "",
            "rx_snr": s.get("rx_snr"),
        })
    rows.sort(key=lambda r: (not r["tx"], r["hz"] or 99e9, r["callsign"]))
    return rows


def _split_eio(body: str) -> list[str]:
    if "\x1e" in body:
        return [p for p in body.split("\x1e") if p]
    return [body] if body else []


def _parse_sio_packets(body: str) -> list[list]:
    """Walk Engine.IO packets from the left. Do not search for 42[ inside JSON."""
    events = []
    dec = json.JSONDecoder()
    i = 0
    n = len(body)
    while i < n:
        if body[i] == "\x1e":
            i += 1
            continue
        if i + 1 < n and body[i] == "4" and body[i + 1] in "0123456":
            kind = body[i:i + 2]
            i += 2
            if i < n and body[i] in "{[":
                try:
                    obj, end = dec.raw_decode(body, i)
                except json.JSONDecodeError:
                    break
                i = end
                if kind == "42" and isinstance(obj, list):
                    events.append(obj)
            continue
        if body[i] == "0" and i + 1 < n and body[i + 1] == "{":
            try:
                _, end = dec.raw_decode(body, i + 1)
            except json.JSONDecodeError:
                break
            i = end
            continue
        i += 1
    return events


class Reporter:
    def __init__(self, host: str = HOST):
        self.host = host.rstrip("/")
        self._lock = threading.Lock()
        self._stations: dict[str, dict] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.connected = False
        self.error: Optional[str] = None
        self.updated = 0.0
        self.callsign = ""
        self.grid = ""
        self.version = "CyberRig"
        self.message = ""
        self._url_live: Optional[str] = None
        self._ready = False
        self.visible = False
        self._freq = 0
        self._tx = False
        self._mode = "RADEV1"

    def configure(self, callsign: str = "", grid: str = "", message: str = "",
                  version: str = "CyberRig"):
        self.callsign = (callsign or "").strip().upper()
        self.grid = (grid or "").strip()[:6]
        self.message = message or ""
        self.version = version or "CyberRig"

    def reporting(self) -> bool:
        return bool(self.callsign and self.grid)

    def status(self) -> dict:
        with self._lock:
            rows = stations_to_rows(self._stations)
        tx = sum(1 for r in rows if r["tx"])
        return {
            "connected": self.connected,
            "error": self.error,
            "count": len(rows),
            "tx": tx,
            "updated": self.updated,
            "stations": rows,
            "reporting": self.reporting(),
            "visible": self.visible,
            "callsign": self.callsign,
            "grid": self.grid,
            "role": "report" if self.reporting() else "view",
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="freedv-reporter")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def auth_payload(self) -> dict:
        if not self.reporting():
            return {"role": "view", "protocol_version": 2}
        return {
            "role": "report",
            "callsign": self.callsign,
            "grid_square": self.grid,
            "version": self.version,
            "rx_only": False,
            "os": "Linux",
            "protocol_version": 2,
        }

    def emit(self, event: str, data: dict):
        url = self._url_live
        if not url or not self._ready:
            return False
        pkt = "42" + json.dumps([event, data], separators=(",", ":"))
        try:
            self._post(url, pkt)
            return True
        except Exception as e:
            log.warning("reporter emit %s: %s", event, e)
            return False

    def show(self, freq_hz: int = 0, mode: str = "RADEV1"):
        self.visible = True
        if freq_hz:
            self._freq = int(freq_hz)
        if mode:
            self._mode = mode
        self.emit("show_self", {})
        self._send_state()

    def hide(self):
        self.visible = False
        self._tx = False
        self.emit("hide_self", {})

    def freq_change(self, freq_hz: int):
        self._freq = int(freq_hz or 0)
        if self.visible:
            self.emit("freq_change", {"freq": self._freq})

    def transmit(self, on: bool, mode: str = "RADEV1"):
        self._tx = bool(on)
        if mode:
            self._mode = mode
        if self.visible:
            self.emit("tx_report", {
                "mode": self._mode,
                "transmitting": self._tx,
            })

    def _send_state(self):
        if self._freq:
            self.emit("freq_change", {"freq": self._freq})
        self.emit("tx_report", {"mode": self._mode, "transmitting": self._tx})
        if self.message:
            self.emit("message_update", {"message": self.message})

    def _url(self, sid: Optional[str] = None) -> str:
        q = {"EIO": "4", "transport": "polling"}
        if sid:
            q["sid"] = sid
        return self.host + PATH + "?" + urllib.parse.urlencode(q)

    def _get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")

    def _post(self, url: str, data: str) -> str:
        req = urllib.request.Request(
            url, data=data.encode(), method="POST",
            headers={"User-Agent": UA, "Content-Type": "text/plain;charset=UTF-8"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", "replace")

    def _ingest(self, events: list):
        with self._lock:
            for ev in events:
                if not ev:
                    continue
                name = ev[0]
                data = ev[1] if len(ev) > 1 else {}
                apply_event(self._stations, name, data)
            self.updated = time.time()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as e:
                self.connected = False
                self.error = str(e)
                log.warning("reporter: %s", e)
                self._stop.wait(5)

    def _session(self):
        self._ready = False
        self._url_live = None
        hs = self._get(self._url())
        if not hs.startswith("0"):
            raise RuntimeError(f"bad handshake {hs[:80]!r}")
        info = json.loads(hs[1:])
        sid = info["sid"]
        url = self._url(sid)
        self._url_live = url
        auth = json.dumps(self.auth_payload(), separators=(",", ":"))
        self._post(url, "40" + auth)
        self.connected = True
        self.error = None
        log.info(
            "FreeDV Reporter connected sid=%s role=%s call=%s grid=%s",
            sid, self.auth_payload().get("role"), self.callsign, self.grid,
        )
        while not self._stop.is_set():
            try:
                body = self._get(url)
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):
                    raise
                raise
            for pkt in _split_eio(body):
                if pkt == "2":
                    try:
                        self._post(url, "3")
                    except Exception:
                        pass
                elif pkt.startswith("41"):
                    raise RuntimeError("socket.io disconnect")
                elif "42[" in pkt:
                    events = _parse_sio_packets(pkt)
                    self._ingest(events)
                    if any(e and e[0] == "connection_successful" for e in events):
                        self._ready = True
                        if self.visible:
                            self._send_state()


reporter = Reporter()
