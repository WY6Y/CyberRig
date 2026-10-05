"""WSJT-X / JTDX UDP protocol: the control channel.

Qt serialises these with QDataStream, which is big-endian and has its own string
convention (a length prefix, with 0xFFFFFFFF meaning null, NOT empty). Getting
that one distinction wrong shifts every subsequent field and produces plausible
garbage rather than an error, which is why the decoder here is validated against
REAL captured packets from the running JTDX and not only against its own encoder.
A round-trip test proves self-consistency, never conformance.

What this build of JTDX actually implements is narrower than the published
protocol. Confirmed from the Qt moc signal names compiled into /usr/bin/jtdx:
`reply`, `replay`, `halt_tx`, `free_text`, `highlight_callsign` are present;
**`switch_configuration` and `configure` are absent.** So Configure(15) cannot be
used to set Rx/Tx DF and SwitchConfiguration(14) cannot arm Fox/Hound. Do not add
encoders for those two expecting them to work -- the slot and Hound workarounds
live elsewhere (XIT over CAT, and an ini edit plus a clean restart).

Status(1) is richer than it looks and carries three things worth having:
  * `tx_enabled` / `transmitting` / `tx_watchdog` -- is this radio about to talk
  * `tx_df` / `rx_df` -- JTDX's own audio slots, which the XIT offset is relative to
  * `special_op_mode` -- 6 = FOX, 7 = HOUND. We cannot SET Fox/Hound over UDP,
    but we can always READ whether it is armed, which is what the UI needs.

TRANSPORT NOTE: GridTracker2 already owns 127.0.0.1:2237 and JTDX unicasts to it.
Binding that port with SO_REUSEPORT would make the kernel load-balance datagrams
between the two sockets and silently steal about half of GridTracker's decodes.
The supported fix is a multicast group -- JTDX's UDP server field accepts one
(its own settings tooltip lists "IPv4 multicast group address") -- where every
listener receives every packet. `JtdxClient` handles both, but multicast is the
only mode that coexists with GridTracker.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
from typing import Any, Callable, Optional

log = logging.getLogger("cyberrig.ft8.jtdx")

MAGIC = 0xADBCCBDA
SCHEMA = 2

# Message types. Only the ones this build can actually use are encoded.
HEARTBEAT = 0
STATUS = 1
DECODE = 2
CLEAR = 3
REPLY = 4
QSO_LOGGED = 5
CLOSE = 6
REPLAY = 7
HALT_TX = 8
FREE_TEXT = 9
WSPR_DECODE = 10
LOCATION = 11
LOGGED_ADIF = 12
HIGHLIGHT_CALLSIGN = 13

SPECIAL_OP = {0: "NONE", 1: "NA VHF", 2: "EU VHF", 3: "FIELD DAY",
              4: "RTTY RU", 5: "WW DIGI", 6: "FOX", 7: "HOUND"}


# ---------------------------------------------------------------- QDataStream

class Reader:
    """Big-endian QDataStream reader. Raises on truncation rather than guessing."""

    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def _take(self, n: int) -> bytes:
        if self.i + n > len(self.d):
            raise ValueError(f"truncated: want {n} at {self.i}, have {len(self.d)}")
        b = self.d[self.i:self.i + n]
        self.i += n
        return b

    def u8(self) -> int:      return self._take(1)[0]
    def boolean(self) -> bool: return self._take(1)[0] != 0
    def u32(self) -> int:     return struct.unpack(">I", self._take(4))[0]
    def i32(self) -> int:     return struct.unpack(">i", self._take(4))[0]
    def u64(self) -> int:     return struct.unpack(">Q", self._take(8))[0]
    def f64(self) -> float:   return struct.unpack(">d", self._take(8))[0]

    def string(self) -> Optional[str]:
        """utf8 with a length prefix. 0xFFFFFFFF is NULL, which is not ''."""
        n = self.u32()
        if n == 0xFFFFFFFF:
            return None
        return self._take(n).decode("utf-8", errors="replace")

    def qtime(self) -> int:
        """QTime on the wire is msecs since midnight."""
        return self.u32()

    @property
    def remaining(self) -> int:
        return len(self.d) - self.i


class Writer:
    def __init__(self):
        self.b = bytearray()

    def u8(self, v):   self.b += struct.pack(">B", v & 0xFF)
    def boolean(self, v): self.b += b"\x01" if v else b"\x00"
    def u32(self, v):  self.b += struct.pack(">I", v & 0xFFFFFFFF)
    def i32(self, v):  self.b += struct.pack(">i", int(v))
    def f64(self, v):  self.b += struct.pack(">d", float(v))

    def string(self, s: Optional[str]):
        if s is None:
            self.u32(0xFFFFFFFF)
            return
        e = s.encode("utf-8")
        self.u32(len(e))
        self.b += e

    def qtime(self, ms: int): self.u32(ms)

    def bytes(self) -> bytes: return bytes(self.b)


# ------------------------------------------------------------------- decoding

def decode(data: bytes) -> Optional[dict[str, Any]]:
    """Parse one datagram. Returns None for anything that is not ours."""
    r = Reader(data)
    try:
        if r.u32() != MAGIC:
            return None
        schema = r.u32()
        mtype = r.u32()
        wsjtx_id = r.string()
    except ValueError:
        return None

    out: dict[str, Any] = {"schema": schema, "type": mtype, "id": wsjtx_id}
    try:
        if mtype == HEARTBEAT:
            # JTDX 2.2 sends a SHORT heartbeat: max_schema + version, and then
            # stops. Reading `revision` unconditionally throws on every real
            # packet (verified against a live capture 2026-09-01), so both
            # trailing fields are optional here.
            out["max_schema"] = r.u32()
            if r.remaining >= 4:
                out["version"] = r.string()
            if r.remaining >= 4:
                out["revision"] = r.string()
        elif mtype == STATUS:
            out.update(
                dial_frequency=r.u64(), mode=r.string(), dx_call=r.string(),
                report=r.string(), tx_mode=r.string(), tx_enabled=r.boolean(),
                transmitting=r.boolean(), decoding=r.boolean(),
                rx_df=r.u32(), tx_df=r.u32(),
                de_call=r.string(), de_grid=r.string(), dx_grid=r.string(),
                tx_watchdog=r.boolean(), sub_mode=r.string(), fast_mode=r.boolean(),
            )
            # Everything past here was added in later schema revisions. JTDX 2.2
            # stops short of some of it, so read defensively rather than assuming.
            if r.remaining >= 1:
                out["special_op_mode"] = r.u8()
                out["special_op"] = SPECIAL_OP.get(out["special_op_mode"], "?")
            if r.remaining >= 8:
                out["frequency_tolerance"] = r.u32()
                out["tr_period"] = r.u32()
            if r.remaining >= 4:
                out["configuration_name"] = r.string()
            if r.remaining >= 4:
                out["tx_message"] = r.string()
        elif mtype == DECODE:
            out.update(
                new=r.boolean(), time=r.qtime(), snr=r.i32(), delta_time=r.f64(),
                delta_frequency=r.u32(), mode=r.string(), message=r.string(),
                low_confidence=r.boolean(),
            )
            if r.remaining >= 1:
                out["off_air"] = r.boolean()
        elif mtype == CLEAR:
            if r.remaining >= 1:
                out["window"] = r.u8()
        elif mtype == QSO_LOGGED:
            out["raw_len"] = len(data)      # not needed yet; ADIF(12) is richer
        elif mtype == LOGGED_ADIF:
            out["adif"] = r.string()
        elif mtype == CLOSE:
            pass
        else:
            out["unparsed"] = True
    except ValueError as e:
        out["error"] = str(e)
    return out


# ------------------------------------------------------------------- encoding

def _header(mtype: int, wsjtx_id: str) -> Writer:
    w = Writer()
    w.u32(MAGIC)
    w.u32(SCHEMA)
    w.u32(mtype)
    w.string(wsjtx_id)
    return w


def encode_heartbeat(wsjtx_id: str, version: str = "cyberrig",
                     revision: str = "ft8") -> bytes:
    w = _header(HEARTBEAT, wsjtx_id)
    w.u32(SCHEMA)
    w.string(version)
    w.string(revision)
    return w.bytes()


def encode_reply(wsjtx_id: str, *, time_ms: int, snr: int, delta_time: float,
                 delta_frequency: int, mode: str, message: str,
                 low_confidence: bool = False, modifiers: int = 0) -> bytes:
    """Ask JTDX to work this station.

    JTDX matches these fields against a decode it still holds -- it logs
    "UDP Reply request ignored, decode not found" if they do not line up -- so
    they must be echoed from a Decode(2) verbatim, not reconstructed.

    `modifiers` are Qt keyboard modifiers, mirroring a modified double-click.
    Leave it 0 unless there is a specific reason.
    """
    w = _header(REPLY, wsjtx_id)
    w.qtime(time_ms)
    w.i32(snr)
    w.f64(delta_time)
    w.u32(delta_frequency)
    w.string(mode)
    w.string(message)
    w.boolean(low_confidence)
    w.u8(modifiers)
    return w.bytes()


def encode_halt_tx(wsjtx_id: str, auto: bool = False) -> bytes:
    """Stop transmitting. auto=False means NOW; True means at end of this over.

    Default is False on purpose. This is the abort path, and an abort that waits
    politely for the current transmission to finish is not an abort.
    """
    w = _header(HALT_TX, wsjtx_id)
    w.boolean(auto)
    return w.bytes()


def encode_free_text(wsjtx_id: str, text: str, send: bool = False) -> bytes:
    w = _header(FREE_TEXT, wsjtx_id)
    w.string(text)
    w.boolean(send)
    return w.bytes()


def encode_highlight_callsign(wsjtx_id: str, callsign: str, *,
                              bg: Optional[tuple[int, int, int]] = None,
                              fg: Optional[tuple[int, int, int]] = None,
                              last_only: bool = False) -> bytes:
    """Colour a callsign inside JTDX's own window.

    Lets the ATNO roster reach the desk display too, not just the phone.
    QColor on the wire is spec(u8=1 RGB) + alpha,r,g,b as u16 pairs.
    """
    def qcolor(c):
        if c is None:
            w.u8(0)                      # invalid colour = "reset"
            return
        w.u8(1)
        for v in (0xFFFF, c[0] << 8 | c[0], c[1] << 8 | c[1], c[2] << 8 | c[2], 0):
            w.b.extend(struct.pack(">H", v))

    w = _header(HIGHLIGHT_CALLSIGN, wsjtx_id)
    w.string(callsign)
    qcolor(bg)
    qcolor(fg)
    w.boolean(last_only)
    return w.bytes()


# --------------------------------------------------------------------- client

class JtdxClient:
    """Receive Decode/Status, and send Reply/HaltTx back to JTDX.

    WSJT-X replies to whatever address its "UDP Server" points at, and accepts
    inbound messages on that same socket. In unicast that means only ONE client
    can hold the channel -- which GridTracker already does here. In multicast
    every member receives, and any member may send.
    """

    def __init__(self, host: str = "239.255.0.1", port: int = 2237,
                 on_message: Optional[Callable[[dict], None]] = None):
        self.host = host
        self.port = port
        self._on_message = on_message
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._peer: Optional[tuple[str, int]] = None
        self.wsjtx_id: Optional[str] = None
        self.last_status: dict = {}
        self.packets = 0

    @property
    def is_multicast(self) -> bool:
        try:
            return 224 <= int(self.host.split(".")[0]) <= 239
        except (ValueError, IndexError):
            return False

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.is_multicast:
            s.bind(("", self.port))
            mreq = struct.pack("4sl", socket.inet_aton(self.host), socket.INADDR_ANY)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        else:
            # Unicast: whoever binds owns the channel. See the transport note.
            s.bind((self.host, self.port))
        s.settimeout(1.0)
        self._sock = s
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ft8-jtdx", daemon=True)
        self._thread.start()
        log.info("JTDX client on %s:%d (%s)", self.host, self.port,
                 "multicast" if self.is_multicast else "unicast")

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                if not self._stop.is_set():
                    log.exception("recv failed")
                return
            msg = decode(data)
            if msg is None:
                continue
            self.packets += 1
            self._peer = addr
            if msg.get("id"):
                self.wsjtx_id = msg["id"]
            if msg["type"] == STATUS:
                self.last_status = msg
            if self._on_message:
                try:
                    self._on_message(msg)
                except Exception:
                    log.exception("message callback failed")

    # ----------------------------------------------------------------- sending

    def _send(self, payload: bytes) -> bool:
        if self._sock is None:
            log.warning("send with no socket")
            return False
        dest = (self.host, self.port) if self.is_multicast else self._peer
        if dest is None:
            log.warning("send before any packet seen — no peer address yet")
            return False
        try:
            self._sock.sendto(payload, dest)
            return True
        except OSError:
            log.exception("send failed")
            return False

    def reply(self, decode_msg: dict, modifiers: int = 0) -> bool:
        """Work the station in this Decode message."""
        if not self.wsjtx_id:
            log.warning("reply before JTDX identified itself")
            return False
        return self._send(encode_reply(
            self.wsjtx_id,
            time_ms=decode_msg["time"], snr=decode_msg["snr"],
            delta_time=decode_msg["delta_time"],
            delta_frequency=decode_msg["delta_frequency"],
            mode=decode_msg["mode"] or "FT8", message=decode_msg["message"] or "",
            low_confidence=bool(decode_msg.get("low_confidence")),
            modifiers=modifiers))

    def halt_tx(self, auto: bool = False) -> bool:
        if not self.wsjtx_id:
            return False
        return self._send(encode_halt_tx(self.wsjtx_id, auto=auto))

    def status(self) -> dict:
        st = self.last_status
        return {
            "connected": self.wsjtx_id is not None,
            "wsjtx_id": self.wsjtx_id,
            "packets": self.packets,
            "transport": "multicast" if self.is_multicast else "unicast",
            "endpoint": f"{self.host}:{self.port}",
            "dial_frequency": st.get("dial_frequency"),
            "tx_enabled": st.get("tx_enabled"),
            "transmitting": st.get("transmitting"),
            "tx_watchdog": st.get("tx_watchdog"),
            "rx_df": st.get("rx_df"),
            "tx_df": st.get("tx_df"),
            "dx_call": st.get("dx_call"),
            "special_op": st.get("special_op"),
            "hound": st.get("special_op") == "HOUND",
        }
