"""Hamlib-compatible rigctld TCP server.

Listens on a configurable port (default 4532).  WSJT-X, fldigi, N1MM Logger+,
DXLab, and most logging software can connect in "Hamlib TCP/rigctld" mode — no
virtual COM port or Com0Com pair needed for those apps.

Protocol reference: hamlib rigctld extended protocol (\\command form + short form).
"""

import socket
import threading
import logging
import time
from typing import Optional

log = logging.getLogger("cyberrig.rigctl")

#: How long get_freq waits for the first real reading before it
#: gives up. The startup poll normally lands in well under a second.
FIRST_READ_WAIT_S = 5.0

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
100000.000000 450000000.000000 0x1ff -1 -1 0x17e00007 0xf
0 0 0 0 0 0 0
1800000.000000 54000000.000000 0x1ff 5000 100000 0x17e00007 0xf
0 0 0 0 0 0 0
0x1ff 1
0x1ff 0
0 0
0xc 2400
0xc 1800
0xc 3000
0xc 0
0x2 500
0x2 2400
0x2 50
0x2 0
0x10 300
0x10 2400
0x10 50
0x10 0
0x1 8000
0x1 2400
0x1 10000
0x20 15000
0x20 8000
0x40 230000
0 0
9990
9990
10000
0
10 
10 20 30 
0xffffffffffffffff
0xffffffffffffffff
0xfffffffff7ffffff
0xfffeff7083ffffff
0xffffffffffffffff
0xffffffffffffffbf
"""


class RigctlServer:
    """TCP server implementing a subset of the hamlib rigctld protocol."""

    def __init__(self, rig, port: int = 4532, gate=None):
        self._rig = rig          # FTdx10 instance
        # Optional supervisor sitting between the client and the radio. It sees
        # every set_freq (a band hop) and can REFUSE a set_ptt. WSPR auto-tune
        # is the first user: because JTDX talks to the radio through here, the
        # tuner does not have to race the beacon -- it can simply say no, and
        # the client skips one slot. See cyberrig/wspr.py.
        self._gate = gate
        self._port = port
        # A gate that can veto a beacon is useless if the client that would
        # have sent it has gone away. Nothing else in the stack notices an
        # absent CAT client: an armed WSPR session with nobody attached looks
        # exactly like a quiet band. So we count them and tell the gate.
        self._clients = 0
        self._clients_lock = threading.Lock()
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

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return self._clients

    def _note_clients(self, delta: int) -> None:
        with self._clients_lock:
            self._clients = max(0, self._clients + delta)
            n = self._clients
        if self._gate is not None:
            try:
                self._gate.note_rigctl_clients(n)
            except AttributeError:
                pass
            except Exception:
                log.exception("rigctld gate note_rigctl_clients")

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
        self._note_clients(+1)
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
            self._note_clients(-1)
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
        # The gate may MASK these two. A CAT client follows the radio -- that
        # is what it is for -- so while WSPR mode has the VFO borrowed for a
        # tune, an honest answer here is read by JTDX as "the operator moved
        # the dial", and it adopts the tune frequency as its own. That is not
        # theoretical: on 2026-09-03 it beaconed on 18.0996, five kHz below the
        # WSPR window, where nothing on earth was listening. During a tune we
        # already refuse to transmit, so reporting the dial the client itself
        # set is both safe and true again the instant the tune ends.
        if cmd == "get_freq":
            # NEVER answer with a frequency the radio has not told us.
            # `RigState.freq_a` initialises to a hardcoded 14200000, and a
            # client that polls this is FOLLOWING us: on 2026-09-05 JTDX
            # connected one second after a panel restart, read that default as
            # truth and put the radio on 14.200 DATA-U, where no FT8 exists.
            # Seven minutes of an FT8 session decoded nothing and every
            # indicator looked healthy. A default is not a reading.
            if not getattr(self._rig.state, "freq_verified", False):
                # WAIT, do not refuse. The startup poll lands within about a
                # second, and an error here is the thing JTDX escalates into
                # "Rig control error" (2026-09-04) — which costs far more than
                # a short stall. Only a radio that never answers gets an error.
                deadline = time.time() + FIRST_READ_WAIT_S
                while time.time() < deadline:
                    if getattr(self._rig.state, "freq_verified", False):
                        break
                    time.sleep(0.05)
                else:
                    log.error("rigctld get_freq: the radio has not reported a "
                              "frequency in %.0fs — refusing rather than "
                              "serving the startup placeholder",
                              FIRST_READ_WAIT_S)
                    return "RPRT -1\n"
            hz = self._rig.state.freq_a
            if self._gate is not None:
                try:
                    hz = self._gate.mask_freq(hz)
                except Exception:
                    log.exception("rigctld gate mask_freq")
            return f"{hz}\nRPRT 0\n"

        if cmd == "get_mode":
            mode = self._rig.state.mode
            if self._gate is not None:
                try:
                    mode = self._gate.mask_mode(mode)
                except Exception:
                    log.exception("rigctld gate mask_mode")
            hl = _RIG_TO_HAMLIB.get(mode, "USB")
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
            except ValueError:
                return "RPRT -1\n"
            # A tune in flight owns the VFO; the gate takes the write and
            # applies it when it lets go, rather than us fighting the carrier.
            absorbed = False
            if self._gate is not None:
                try:
                    absorbed = self._gate.absorb_set_freq(hz)
                except Exception:
                    log.exception("rigctld gate absorb_set_freq")
            if not absorbed:
                self._rig.set_freq(hz)
                # After the write, not before: the gate may QSY away to tune,
                # and that must not be undone by the move it reacted to.
                if self._gate is not None:
                    try:
                        self._gate.note_set_freq(hz)
                    except Exception:
                        log.exception("rigctld gate note_set_freq")
            return "RPRT 0\n"

        if cmd == "set_mode" and args:
            rig_mode = _HAMLIB_TO_RIG.get(args[0].upper(), "USB")
            self._rig.set_mode(rig_mode)
            return "RPRT 0\n"

        if cmd == "set_ptt" and args:
            # Hamlib PTT values: 0=off, 1=on, 2=on via mic, 3=on via data port —
            # any nonzero means key the rig, not just literal "1". JTDX (data
            # modes) sends 3; treating only "1" as on silently ignored it.
            on = args[0] != "0"
            # SWALLOW a vetoed key -- report success, never touch the rig.
            #
            # This used to return RPRT -1, on the assumption (written into
            # wspr.py's docstring too) that "JTDX logs it and moves on to the
            # next slot". It does not. JTDX treats a failed set_ptt as a FATAL
            # CAT fault: it raises "Rig control error", un-checks Enable Tx and
            # drops this socket, and nothing short of a human clicking Retry
            # brings it back. On 2026-09-04 the gate refused exactly one beacon
            # at 22:04:00 and the link was still dead at 06:55 -- 8h51m of an
            # unattended overnight WSPR run, with no alert, because the refusal
            # path had never once behaved the way its own comment claimed.
            #
            # A client that believes it transmitted wastes one slot, which is
            # what the veto was always meant to cost. Every swallowed key is
            # logged, counted (`skipped_beacons`) and shown on the panel, so
            # the operator is still told -- just not through a return code the
            # client escalates. Masking a client's view to protect it from our
            # own borrowed VFO is already this module's idiom; see mask_freq.
            if self._gate is not None and not self._gate.allow_ptt(on):
                return "RPRT 0\n"
            self._rig.set_ptt(on)
            if self._gate is not None:
                try:
                    self._gate.note_ptt(on)
                except Exception:
                    log.exception("rigctld gate note_ptt")
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
