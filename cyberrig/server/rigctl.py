"""Hamlib-compatible rigctld TCP server.

Listens on a configurable port (default 4532).  WSJT-X, fldigi, N1MM Logger+,
DXLab, and most logging software can connect in "Hamlib TCP/rigctld" mode — no
virtual COM port or Com0Com pair needed for those apps.

Protocol reference: hamlib rigctld extended protocol (\\command form + short form).
"""

import socket
import threading
import logging
from typing import Optional

log = logging.getLogger("cyberrig.rigctl")

# Hamlib mode names ↔ FTDX10 mode names
_HAMLIB_TO_RIG = {
    "USB":    "USB",
    "LSB":    "LSB",
    "CW":     "CW-U",
    "CWR":    "CW-L",
    "FM":     "FM",
    "FMN":    "FM-N",
    "AM":     "AM",
    "AMN":    "AM-N",
    "RTTY":   "RTTY-L",
    "RTTYR":  "RTTY-U",
    "PKTUSB": "DATA-U",
    "PKTLSB": "DATA-L",
    "PKTFM":  "DATA-FM",
}
_RIG_TO_HAMLIB = {v: k for k, v in _HAMLIB_TO_RIG.items()}

# Short-form (single letter) command → canonical name. Hamlib's rigctld
# protocol uses CASE here to mean get vs set — lowercase=get, uppercase=set
# — so this map must be built with exact case, never .lower()'d as a whole.
_SHORT_CMD_MAP = {
    "f": "get_freq",  "F": "set_freq",
    "m": "get_mode",  "M": "set_mode",
    "t": "get_ptt",   "T": "set_ptt",
    "v": "get_vfo",   "V": "set_vfo",
    "l": "get_level", "L": "set_level",
    "s": "get_split_vfo", "S": "set_split_vfo",
    "q": "quit",      "Q": "quit",
}

# Minimal dump_state block recognised by WSJT-X / flrig
_DUMP_STATE = """\
0
2
2 FTDX10 via CyberRig
100000.000000 450000000.000000 0x100003ff -1 -1 0x16000003 0x3
0 0 0 0 0 0 0
1800000.000000 54000000.000000 0x100003ff 5000 200000 0x16000003 0x3
0 0 0 0 0 0 0
0x100003ff
0x100003ff
0x00000000
0x00000000
0x00000000
0x00000000
0
0
0
0
0
0
150
1
0
0
0
USB:2700 LSB:2700 CW:500 CWR:500 FM:15000 FMN:9000 AM:6000 AMN:3000 RTTY:2000 RTTYR:2000 PKTUSB:3000 PKTLSB:3000
0
VFOA VFOB
MEM
none
none
none
RPRT 0
"""


class RigctlServer:
    """TCP server implementing a subset of the hamlib rigctld protocol."""

    def __init__(self, rig, port: int = 4532):
        self._rig = rig          # FTdx10 instance
        self._port = port
        self._server_sock: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._serve, daemon=True, name="rigctl-server"
        )
        self._thread.start()

    def stop(self):
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass

    @property
    def port(self) -> int:
        return self._port

    # ------------------------------------------------------------------ #

    def _serve(self):
        try:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind(("0.0.0.0", self._port))
            self._server_sock.listen(8)
            log.info("rigctld listening on port %d", self._port)
            while self._running:
                try:
                    self._server_sock.settimeout(1.0)
                    conn, addr = self._server_sock.accept()
                    log.info("rigctld client: %s", addr)
                    t = threading.Thread(
                        target=self._handle_client,
                        args=(conn, addr),
                        daemon=True,
                        name=f"rigctl-{addr[1]}",
                    )
                    t.start()
                except socket.timeout:
                    pass
                except OSError:
                    break
        except Exception as e:
            log.error("rigctld server error: %s", e)

    def _handle_client(self, conn: socket.socket, addr):
        buf = ""
        try:
            conn.settimeout(30.0)
            while self._running:
                try:
                    data = conn.recv(256)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data.decode(errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        resp = self._dispatch(line)
                        conn.sendall(resp.encode())
        except Exception as e:
            log.debug("rigctld client %s error: %s", addr, e)
        finally:
            conn.close()
            log.info("rigctld client disconnected: %s", addr)

    # ------------------------------------------------------------------ #
    # Command dispatcher
    # ------------------------------------------------------------------ #

    def _dispatch(self, line: str) -> str:
        # Extended (backslash) form: \command [args...] — command is already an
        # unambiguous word (e.g. "set_freq"), case doesn't carry meaning.
        if line.startswith("\\"):
            parts = line[1:].split()
            cmd = parts[0].lower()
            args = parts[1:]
        else:
            # Short form: a SINGLE LETTER, and hamlib's rigctld protocol uses
            # CASE to distinguish get vs set (f=get_freq, F=set_freq; m/M;
            # t/T; v/V — lowercase always "get", uppercase always "set").
            # Lowercasing this (as the code used to) silently turned every
            # set command into its get equivalent — CAT looked like it worked
            # (RPRT 0 came back) but nothing ever actually changed on the rig.
            letter = line[0]
            args = line[1:].split()
            cmd = _SHORT_CMD_MAP.get(letter, letter.lower())

        # ---- read commands ----
        if cmd == "get_freq":
            return f"{self._rig.state.freq_a}\nRPRT 0\n"

        if cmd == "get_mode":
            hl = _RIG_TO_HAMLIB.get(self._rig.state.mode, "USB")
            return f"{hl}\n3000\nRPRT 0\n"

        if cmd == "get_ptt":
            return f"{'1' if self._rig.state.is_tx else '0'}\nRPRT 0\n"

        if cmd == "get_vfo":
            return "VFOA\nRPRT 0\n"

        if cmd == "dump_state":
            return _DUMP_STATE

        if cmd == "get_info":
            return "FTDX10 via CyberRig\nRPRT 0\n"

        # ---- write commands ----
        if cmd == "set_freq" and args:
            try:
                hz = int(float(args[0]))
                self._rig.set_freq(hz)
                return "RPRT 0\n"
            except ValueError:
                return "RPRT -1\n"

        if cmd == "set_mode" and args:
            rig_mode = _HAMLIB_TO_RIG.get(args[0].upper(), "USB")
            self._rig.set_mode(rig_mode)
            return "RPRT 0\n"

        if cmd == "set_ptt" and args:
            self._rig.set_ptt(args[0] == "1")
            return "RPRT 0\n"

        if cmd == "set_vfo":
            return "RPRT 0\n"   # single VFO for now

        if cmd == "get_level" and args:
            lvl = args[0].upper()
            if lvl == "STRENGTH":
                # Convert raw 0–30 to dBm-ish: S9 = -73 dBm
                raw = self._rig.state.smeter
                dbm = -127 + (raw / 30.0) * 54
                return f"{dbm:.1f}\nRPRT 0\n"
            return "0\nRPRT 0\n"

        if cmd == "get_split_vfo":
            return "0\nVFOA\nRPRT 0\n"

        if cmd in ("set_split_vfo", "set_split_freq", "set_split_mode"):
            return "RPRT 0\n"

        if cmd == "chk_vfo":
            return "CHKVFO 0\nRPRT 0\n"

        if cmd == "quit":
            return "RPRT 0\n"

        log.debug("rigctld: unknown command %r", line)
        return "RPRT -1\n"
