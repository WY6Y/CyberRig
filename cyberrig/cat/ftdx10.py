"""FTDX10 CAT protocol handler — corrected against Yaesu CAT OM 2308-F.

Qt-free, headless version.  Register callbacks with rig.on(event, fn).
All CAT commands are identical to the original — only the event system changed.
"""

import logging
import threading
import time
import serial
from typing import Optional, Callable

# ── Mode codes (from MD command, page 15) ────────────────────────────────────
MODES = {
    "1": "LSB",  "2": "USB",     "3": "CW-U",    "4": "FM",
    "5": "AM",   "6": "RTTY-L",  "7": "CW-L",    "8": "DATA-L",
    "9": "RTTY-U","A": "DATA-FM","B": "FM-N",     "C": "DATA-U",
    "D": "AM-N", "E": "PSK",     "F": "DATA-FM-N",
}
MODE_CODES = {v: k for k, v in MODES.items()}

HAMLIB_MODES = {
    "LSB": "LSB", "USB": "USB", "CW-U": "CW", "CW-L": "CWR",
    "FM": "FM", "FM-N": "FMN", "AM": "AM", "AM-N": "AMN",
    "RTTY-L": "RTTY", "RTTY-U": "RTTYR",
    "DATA-U": "PKTUSB", "DATA-L": "PKTLSB", "DATA-FM": "PKTFM",
    "PSK": "PKTUSB", "DATA-FM-N": "PKTFM",
}
HAMLIB_MODE_REV = {v: k for k, v in HAMLIB_MODES.items()}

# ── SH (WIDTH) bandwidth table — Table 3 from CAT manual page 21 ─────────────
SH_BW_SSB  = [0, 300, 400, 600, 850, 1100, 1200, 1500, 1650, 1800,
               1950, 2100, 2250, 2400, 2450, 2500, 2600, 2700, 2800, 2900,
               3000, 3200, 3500, 4000]

SH_BW_CW   = [0, 50, 100, 150, 200, 250, 300, 350, 400, 450,
               500, 600, 800, 1200, 1400, 1700, 2000, 2400, 3000, 3200,
               3500, 4000]

SH_BW_RTTY = [0, 50, 100, 150, 200, 250, 305, 350, 400, 450,
               500, 600, 800, 1200, 1400, 1700, 2000, 2400, 3000, 3200,
               3500, 4000]

SH_BW_PSK  = [0, 50, 100, 150, 200, 250, 300, 350, 400, 450,
               500, 600, 800, 1200, 1400, 1700, 2000, 2400, 3000, 3200,
               3500, 4000]


def sh_bw_table(mode: str) -> list:
    if mode in ("CW-U", "CW-L"):
        return SH_BW_CW
    if mode in ("RTTY-L", "RTTY-U"):
        return SH_BW_RTTY
    if mode in ("PSK",):
        return SH_BW_PSK
    return SH_BW_SSB


def sh_hz(code: int, mode: str) -> int:
    t = sh_bw_table(mode)
    return t[min(code, len(t) - 1)]


def sh_max_code(mode: str) -> int:
    return len(sh_bw_table(mode)) - 1


_DELAY_TABLE = [30, 50, 100, 150, 200, 250] + list(range(300, 3001, 100))


def ms_to_delay_code(ms: int) -> int:
    return min(range(len(_DELAY_TABLE)), key=lambda i: abs(_DELAY_TABLE[i] - ms))


AGC_SET   = {0: "OFF", 1: "FAST", 2: "MID", 3: "SLOW", 4: "AUTO"}
AGC_ANS   = {0: "OFF", 1: "FAST", 2: "MID", 3: "SLOW",
             4: "AUTO-FAST", 5: "AUTO-MID", 6: "AUTO-SLOW"}
AGC_CODES = {"OFF": 0, "FAST": 1, "MID": 2, "SLOW": 3, "AUTO": 4,
             "AUTO-FAST": 4, "AUTO-MID": 4, "AUTO-SLOW": 4}

PREAMP = {"0": "IPO", "1": "AMP1", "2": "AMP2"}
PREAMP_CODES = {v: k for k, v in PREAMP.items()}
# Front-panel IPO button cycles these in order, then wraps.
PREAMP_CYCLE = ["IPO", "AMP1", "AMP2"]

ATT = {"0": "OFF", "1": "6dB", "2": "12dB", "3": "18dB"}
ATT_CODES = {v: k for k, v in ATT.items()}
# Front-panel ATT button cycles these in order, then wraps back to OFF.
ATT_CYCLE = ["OFF", "6dB", "12dB", "18dB"]


def next_preamp(current: Optional[str]) -> str:
    """Next IPO/AMP1/AMP2 step for a single-button cycle."""
    cur = current if current in PREAMP_CYCLE else "IPO"
    return PREAMP_CYCLE[(PREAMP_CYCLE.index(cur) + 1) % len(PREAMP_CYCLE)]


def next_att(current: Optional[str]) -> str:
    """Next ATT step for a single-button cycle (OFF → 6 → 12 → 18 → OFF)."""
    cur = current if current in ATT_CYCLE else "OFF"
    return ATT_CYCLE[(ATT_CYCLE.index(cur) + 1) % len(ATT_CYCLE)]

# Roofing filter — CAT RF (OM 2308-F). Set uses P2; read answers P3.
# 300 Hz is the optional XF-130CN. AUTO is not in the FTDX10 RF table.
ROOF_SET = {"12K": "1", "3K": "2", "500": "4", "300": "5"}
ROOF_GET = {
    "1": "12K", "6": "12K",
    "2": "3K",  "7": "3K",
    "4": "500", "9": "500",
    "5": "300", "A": "300", "a": "300",
}

BAND_DEFAULT_FREQ = {
    "160m": 1850000, "80m": 3800000, "60m": 5371500, "40m": 7200000,
    "30m": 10125000, "20m": 14200000, "17m": 18130000, "15m": 21300000,
    "12m": 24940000, "10m": 28500000, "6m": 50125000,
}

BAND_EDGES = {
    "160m": (1_800_000, 2_000_000),
    "80m":  (3_500_000, 4_000_000),
    "60m":  (5_330_500, 5_403_500),
    "40m":  (7_000_000, 7_300_000),
    "30m":  (10_100_000, 10_150_000),
    "20m":  (14_000_000, 14_350_000),
    "17m":  (18_068_000, 18_168_000),
    "15m":  (21_000_000, 21_450_000),
    "12m":  (24_890_000, 24_990_000),
    "10m":  (28_000_000, 29_700_000),
    "6m":   (50_000_000, 54_000_000),
}

# 3-band parametric EQ (EX 03 03), per CAT manual Table 2:
#   TX DSP EQ (PRMTRC EQ):   bands 1-3 -> P3 bases 02, 05, 08
#   MIC P-EQ  (P PRMTRC EQ): bands 1-3 -> P3 bases 11, 14, 17
# Each band is 3 consecutive P3 offsets: freq code, level, bandwidth.
EQ_BASES = {"tx": [2, 5, 8], "mic": [11, 14, 17]}
EQ_BAND_RANGE = ["low", "mid", "high"]  # per band index 0/1/2

EQ_FREQ_OPTIONS = {
    "low":  {0: "OFF", 1: "100", 2: "200", 3: "300", 4: "400", 5: "500", 6: "600", 7: "700"},
    "mid":  {0: "OFF", 1: "700", 2: "800", 3: "900", 4: "1000", 5: "1100", 6: "1200",
             7: "1300", 8: "1400", 9: "1500"},
    "high": {0: "OFF", 1: "1500", 2: "1600", 3: "1700", 4: "1800", 5: "1900", 6: "2000",
             7: "2100", 8: "2200", 9: "2300", 10: "2400", 11: "2500", 12: "2600",
             13: "2700", 14: "2800", 15: "2900", 16: "3000", 17: "3100", 18: "3200"},
}


#: RM types that also have a named RigState attribute, kept so existing
#: consumers (web UI status payload, ATU tune peak-hold) go on working while
#: the panel selects arbitrary meters.
_METER_ALIAS = {5: "power_meter", 4: "alc_meter", 6: "swr_meter"}

#: Polled during TX unless a consumer narrows or widens it. These three are
#: what the web UI and the ATU tune routine need, so they are the floor.
DEFAULT_METER_SOURCES = frozenset({5, 4, 6})


log = logging.getLogger("cyberrig.cat")

#: Consecutive unanswered commands before the CAT link is declared dead.
#: Each _cmd() gives up after ~0.45 s, so this is a few seconds of silence.
SILENT_LIMIT = 20

#: Don't retry a wedged port more often than this (seconds).
REOPEN_INTERVAL = 5.0

#: How long to keep listening after the last byte, waiting for a data frame
#: that trails its own echo. Only used until a data frame actually lands —
#: see _cmd_raw(). Every millisecond here is serial lock the PTT write waits on.
FRAME_GRACE = 0.03


def _has_data_frame(answers: list, prefix: str, echoed: str) -> bool:
    """True once a frame that is real payload (not the command echo) has landed."""
    for a in answers:
        up = a.upper()
        if up.startswith(prefix) and up != echoed and len(a) > len(echoed):
            return True
    return False


class RigState:
    def __init__(self):
        # These are placeholders, not readings. `freq_verified` stays False
        # until the radio itself has answered, because anything that SERVES
        # this value (rigctld, and therefore JTDX) would otherwise present a
        # hardcoded default as the dial. See cyberrig/server/rigctl.py.
        self.freq_a: int = 14200000
        self.freq_b: int = 14200000
        self.freq_verified: bool = False
        self.mode: str = "USB"
        self.smeter: int = 0
        self.power_meter: int = 0
        self.alc_meter: int = 0
        self.swr_meter: int = 0
        #: Raw RM counts keyed by RM type, for every meter currently polled.
        #: power/alc/swr_meter above stay as aliases for RM 5/4/6 so the web
        #: UI and the ATU tune routine keep working unchanged.
        self.meters: dict = {}
        self.is_tx: bool = False
        self.power: int = 50
        self.split: bool = False
        self.tx_vfo: str = "A"
        self.fine: bool = False
        self.rit: bool = False
        self.xit: bool = False
        self.rit_offset: int = 0
        self.sh: int = 13
        self.if_shift: int = 0
        self.af_gain: int = 150
        self.rf_gain: int = 200
        self.agc: str = "AUTO"
        self.preamp: str = "IPO"
        self.att: str = "OFF"
        self.nb: bool = False
        self.nb_level: int = 5
        self.nr: bool = False
        self.nr_level: int = 5
        self.dnf: bool = False
        self.notch: bool = False
        self.notch_pos: int = 1000
        self.contour: bool = False
        self.contour_freq: int = 1000
        self.contour_level: int = 0
        self.contour_width: int = 6
        self.roofing: str = "3K"
        self.notch_width: str = "WIDE"
        self.apf: bool = False
        self.apf_freq: int = 0
        self.compressor: bool = False
        self.comp_level: int = 50
        self.vox: bool = False
        self.vox_gain: int = 50
        self.vox_delay: int = 100
        self.monitor: bool = False
        self.mon_level: int = 50
        self.cw_speed: int = 20
        self.cw_pitch: int = 600
        self.cw_breakin: bool = False
        self.cw_delay: int = 200
        self.antenna: int = 1
        self.locked: bool = False
        self.mic_gain: int = 50
        # Internal ATU (CAT AC): on/off + tuning cycle (not external REMOTE TUNE)
        self.atu: bool = False
        self.atu_tuning: bool = False
        # Main rig power (CAT PS) — separate from CyberRig's own CAT connection
        self.radio_on: bool = True


class FTdx10:
    """FTDX10 CAT driver — headless, no Qt.

    Register event callbacks:
        rig.on("freq_changed", fn)   # fn(hz: int)
        rig.on("mode_changed", fn)   # fn(mode: str)
        rig.on("smeter_update", fn)  # fn(val: int)
        ... see _fire calls below for all event names and signatures.

    Callbacks run from the poll thread; keep them non-blocking.
    """

    def __init__(self):
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._cb_lock = threading.Lock()
        self._callbacks: dict[str, list] = {}
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False
        self.state = RigState()
        self.is_connected = False
        #: Single-VFO mode: mirror every freq/mode write onto VFO B with `AB;`.
        #: Set from settings["single_vfo"] at startup. See _mirror_a_to_b().
        self.single_vfo = False
        #: Consecutive CAT commands that got no answer at all. A wedged USB
        #: serial handle throws no exception — reads simply return nothing —
        #: so this counter is the only thing that can notice.
        self._silent = 0
        self._port: Optional[str] = None
        self._baud: int = 38400
        self._last_reopen: float = 0.0
        #: RM types polled while transmitting. Mutate through
        #: request_meters()/release_meters() rather than assigning, so two
        #: consumers (the panel and the web UI) cannot clobber each other.
        self.meter_sources: set = set(DEFAULT_METER_SOURCES)
        self._meter_requests: dict = {}
        # Internal ATU tune-cycle bookkeeping (radio often won't report P3=2 on read)
        self._atu_tune_t0: float = 0.0
        self._atu_saw_tx: bool = False

    # ── Meter source selection ───────────────────────────────────────────────

    def request_meters(self, owner: str, rm_types) -> None:
        """Ask for `rm_types` to be polled during TX on behalf of `owner`.

        Each caller owns one named slot in the request table; the polled set is
        always the union of every request plus DEFAULT_METER_SOURCES. Calling
        again with the same owner replaces that owner's previous request, so a
        UI can just re-request on every change without bookkeeping.
        """
        self._meter_requests[owner] = {int(r) for r in rm_types}
        self._recompute_meter_sources()

    def release_meters(self, owner: str) -> None:
        """Drop `owner`'s request. Safe to call for an unknown owner."""
        if self._meter_requests.pop(owner, None) is not None:
            self._recompute_meter_sources()

    def _recompute_meter_sources(self) -> None:
        wanted = set(DEFAULT_METER_SOURCES)
        for req in self._meter_requests.values():
            wanted |= req
        self.meter_sources = wanted

    # ── Callback API ─────────────────────────────────────────────────────────

    def on(self, event: str, callback: Callable):
        with self._cb_lock:
            self._callbacks.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Callable):
        with self._cb_lock:
            cbs = self._callbacks.get(event, [])
            if callback in cbs:
                cbs.remove(callback)

    def _fire(self, event: str, *args):
        with self._cb_lock:
            cbs = list(self._callbacks.get(event, []))
        for cb in cbs:
            try:
                cb(*args)
            except Exception:
                pass

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, port: str, baud: int = 38400) -> bool:
        # Remember the target before opening, so a failed first attempt (port
        # missing, rig still booting or in standby) leaves _reopen() something
        # to retry — see retry_until_connected().
        self._port, self._baud = port, baud
        try:
            ser = serial.Serial(
                port, baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_TWO,
                timeout=0.5,
                write_timeout=1.0,
            )
            self._ser = ser
            self._silent = 0
            resp = self._cmd_raw("ID;")
            if resp is None:
                ser.close()
                self._ser = None
                return False
            self.is_connected = True
            self._fire("connected_changed", True)
            return True
        except Exception:
            self._ser = None
            return False

    def disconnect(self):
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None
        with self._lock:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
        self.is_connected = False
        self._fire("connected_changed", False)

    def retry_until_connected(self, port: str, baud: int, interval: float = 0.25):
        """Run the poll loop with no live link, so its reopen path keeps trying.

        A failed connect() used to be final: nothing started the poll loop, so
        the panel sat Disconnected until someone pressed Connect. Found live
        2026-09-25 when the station PC booted off the new UPS before the FTDX10
        answered, and again after a Shelly power cycle left the rig in standby.
        The loop already recovers a link that dies mid-session (every
        REOPEN_INTERVAL); this hands it a link that never came up. It does not
        wake the rig — powering on stays the operator's call (P163).
        """
        self._port, self._baud = port, baud
        self.start_polling(interval)

    def start_polling(self, interval: float = 0.25):
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, args=(interval,), daemon=True, name="cat-poll"
        )
        self._poll_thread.start()

    # ── Low-level I/O ────────────────────────────────────────────────────────

    def _cmd(self, cmd: str) -> Optional[str]:
        """Send a CAT query, tracking whether the radio is answering at all.

        `_cmd_raw` swallows every failure and returns None, which is why a dead
        link used to be invisible: the poll loop kept its last values, the
        setters kept updating state optimistically, and the UI cheerfully
        reported a frequency the radio was not on. Found live 2026-08-25 after
        the port sat wedged for seven hours. Anything that talks to the radio
        goes through here, so this is where silence gets counted.
        """
        answer = self._cmd_raw(cmd)
        if answer is None:
            self._note_silence()
        else:
            self._silent = 0
        return answer

    def _note_silence(self):
        """Count an unanswered command; declare the link dead past the limit."""
        self._silent += 1
        if self._silent >= SILENT_LIMIT and self.is_connected:
            log.warning(
                "CAT link silent for %d consecutive commands — marking disconnected",
                self._silent,
            )
            self.is_connected = False
            self._fire("connected_changed", False)

    def _reopen(self) -> bool:
        """Close and reopen the serial port after the link went silent.

        A wedged CP2105 handle recovers on reopen — that is exactly what
        restarting the service did by hand on 2026-08-25. If the radio itself
        is off or unplugged, `connect()` fails and we simply try again later.
        """
        if not self._port:
            return False
        now = time.monotonic()
        if now - self._last_reopen < REOPEN_INTERVAL:
            return False
        self._last_reopen = now
        with self._lock:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
        self._silent = 0
        if self.connect(self._port, self._baud):
            log.info("CAT link recovered by reopening %s", self._port)
            try:
                self.sync_from_rig(full=True)
            except Exception:
                pass
            return True
        return False

    def _cmd_raw(self, cmd: str) -> Optional[str]:
        """Send a CAT query and return the best answer frame (no trailing ';').

        Yaesu USB CAT often *echoes* the command (e.g. ``RM5;``) before the
        real payload (``RM5123000;``).  Stopping at the first semicolon kept
        only the echo, so PO/ALC/SWR always parsed as empty/zero while TX.
        """
        if not self._ser or not self._ser.is_open:
            return None
        prefix = cmd.rstrip(";")[:2].upper()  # RM, FA, SH, TX, …
        echoed = cmd.rstrip(";").upper()
        with self._lock:
            try:
                self._ser.reset_input_buffer()
                raw_cmd = cmd.encode()
                self._ser.write(raw_cmd)
                buf = b""
                answers: list[str] = []
                deadline = time.monotonic() + 0.45
                quiet_until = None
                while time.monotonic() < deadline:
                    # Drain whatever has arrived. `read(n)` with a fixed timeout
                    # blocks for the WHOLE timeout when fewer than n bytes are
                    # waiting, which is how a 10 ms answer used to cost ~100 ms
                    # of held serial lock — the PTT write queues behind it.
                    n = self._ser.in_waiting
                    if n:
                        buf += self._ser.read(n)
                        while b";" in buf:
                            part, buf = buf.split(b";", 1)
                            text = part.decode(errors="ignore").strip()
                            if text:
                                answers.append(text)
                        if _has_data_frame(answers, prefix, echoed):
                            break  # answer in hand — release the lock now
                        quiet_until = time.monotonic() + FRAME_GRACE
                    elif quiet_until and time.monotonic() >= quiet_until:
                        # Echo only, and the line went quiet: nothing more coming
                        break
                    else:
                        time.sleep(0.002)
                if not answers:
                    return None
                # Prefer longest frame matching the command family (skips echo)
                candidates = [a for a in answers if a.upper().startswith(prefix)]
                if not candidates:
                    return answers[-1]
                # Drop pure echoes (same as command without trailing junk)
                data = [a for a in candidates if a.upper() != echoed and len(a) > len(echoed)]
                if data:
                    return max(data, key=len)
                # Fallback: longest candidate
                return max(candidates, key=len)
            except Exception:
                return None

    def _set(self, cmd: str):
        if not self._ser or not self._ser.is_open:
            return
        with self._lock:
            try:
                self._ser.write(cmd.encode())
            except Exception:
                pass

    # ── VFO / Frequency ──────────────────────────────────────────────────────

    def get_freq(self) -> Optional[int]:
        r = self._cmd("FA;")
        if r and r.startswith("FA") and len(r) >= 11:
            try:
                return int(r[2:11])
            except ValueError:
                pass
        return None

    def _mirror_a_to_b(self):
        """Copy VFO A onto B when single-VFO mode is on.

        `AB;` is the one A/B command this radio actually honours (`SV;` and
        `VS;` are both inert on it), so this is how "one VFO" is enforced:
        whichever VFO the radio is really receiving on, both hold the same
        frequency and mode. Driven by settings["single_vfo"], which
        app.py copies onto the rig at startup.
        """
        if not self.single_vfo:
            return
        try:
            self._set("AB;")
        except Exception as e:
            log.warning("single_vfo mirror A>B failed: %s", e)

    def set_freq(self, hz: int):
        hz = max(30_000, min(75_000_000, int(hz)))
        self._set(f"FA{hz:09d};")
        self.state.freq_a = hz
        # We commanded it, so we know it.
        self.state.freq_verified = True
        self._mirror_a_to_b()
        self._fire("freq_changed", hz)

    def get_freq_b(self) -> Optional[int]:
        r = self._cmd("FB;")
        if r and r.startswith("FB") and len(r) >= 11:
            try:
                return int(r[2:11])
            except ValueError:
                pass
        return None

    def set_freq_b(self, hz: int):
        hz = max(30_000, min(75_000_000, int(hz)))
        self._set(f"FB{hz:09d};")
        self.state.freq_b = hz
        self._fire("freq_b_changed", hz)

    def get_fine(self) -> Optional[bool]:
        """FINE TUNING (FN) — the MAIN dial's step, not ours.

        Yaesu FTDX10 CAT reference: FN0 off, FN1 on. It divides the physical
        tuning knob's step by ten (SSB/CW 10 Hz -> 1 Hz, RTTY/DATA 100 Hz ->
        10 Hz). CyberRig tunes by writing an absolute frequency with FA, so
        this changes nothing about the app's own tuning — only the knob's.
        """
        r = self._cmd("FN;")
        if r and r.startswith("FN") and len(r) >= 3:
            return r[2] == "1"
        return None

    def set_fine(self, on: bool):
        self._set("FN1;" if on else "FN0;")
        self.state.fine = bool(on)
        self._fire("fine_changed", bool(on))

    def _refresh_vfos(self):
        """Re-read A and B after a command that moved them both.

        SV/AB/BA answer nothing (set-only in the CAT reference), so without
        this the UI shows the old pair until the poll catches up — VFO A
        within a cycle, VFO B only every fourth. Cheap: two round trips.
        """
        fa = self.get_freq()
        if fa:
            # The radio answered: the cached dial is a reading now, not the
            # placeholder rigctld must never serve.
            self.state.freq_verified = True
            if fa != self.state.freq_a:
                self.state.freq_a = fa
                self._fire("freq_changed", fa)
        fb = self.get_freq_b()
        if fb and fb != self.state.freq_b:
            self.state.freq_b = fb
            self._fire("freq_b_changed", fb)

    def _refresh_mode(self):
        """Re-read MAIN mode from MD0 — not from IF.

        IF's P6 mode byte goes stale across FA/FB/SV/AB and even after a
        successful MD0 set (measured live 2026-08-27: MD0=DATA-U while IF
        still reported USB for seconds). Polling IF for mode makes Data-U
        and A/B look like they "snap back".
        """
        mode = self.get_mode()
        if mode and mode != self.state.mode:
            self.state.mode = mode
            self._fire("mode_changed", mode)

    def swap_vfo(self):
        """Exchange VFO A and B (frequency and mode).

        `SV;` is what the CAT reference documents ("SWAP VFO — changes the
        VFO-A and VFO-B", set-only) and it is INERT on this FTDX10: measured
        2026-08-26 on live hardware, VFO A and B unchanged 6 seconds after
        the command, with no lock, no split and not transmitting. `AB;`
        through the same code path worked in the same session, so it is not
        the write path.

        So: send the documented command, read back, and if the radio ignored
        it, swap by explicit FA/FB + MD0/MD1 writes. Frequency-only writes
        are not enough — crossing a band boundary makes the radio recall
        each band's stacked mode, which is not the same as swapping the
        modes the operator actually had on A and B.
        """
        before_a = self.get_freq()
        before_b = self.get_freq_b()
        before_ma = self.get_mode()
        before_mb = self.get_mode_b()
        self._set("SV;")
        self._refresh_vfos()
        self._refresh_mode()
        if before_a is None or before_b is None or before_a == before_b:
            return
        if self.state.freq_a != before_a or self.state.freq_b != before_b:
            return                      # the radio honoured SV after all
        log.info("SV ignored by the radio — swapping A/B by explicit write")
        # Freq first, then mode: a bare FA/FB into another band recalls the
        # band-stack mode, so the MD writes must land after.
        self.set_freq_b(before_a)
        if before_ma:
            self.set_mode_b(before_ma)
        self.set_freq(before_b)
        if before_mb:
            self.set_mode(before_mb)
        self._refresh_vfos()
        self._refresh_mode()

    def vfo_a_to_b(self):
        self._set("AB;")
        self._refresh_vfos()
        self._refresh_mode()

    def vfo_b_to_a(self):
        self._set("BA;")
        self._refresh_vfos()
        self._refresh_mode()

    def go_band(self, band: str):
        if band in BAND_DEFAULT_FREQ:
            self.set_freq(BAND_DEFAULT_FREQ[band])

    # ── Mode ─────────────────────────────────────────────────────────────────

    def get_mode(self) -> Optional[str]:
        r = self._cmd("MD0;")
        if r and r.startswith("MD") and len(r) >= 4:
            return MODES.get(r[3], "USB")
        return None

    def get_mode_b(self) -> Optional[str]:
        """SUB / VFO-B operating mode (MD1)."""
        r = self._cmd("MD1;")
        if r and r.startswith("MD") and len(r) >= 4:
            return MODES.get(r[3], "USB")
        return None

    def set_mode(self, mode: str):
        code = MODE_CODES.get(mode, "2")
        self._set(f"MD0{code};")
        self._mirror_a_to_b()
        # Confirm from MD0. IF P6 is not trustworthy after VFO/mode writes, so
        # an optimistic state.mode alone gets overwritten by a stale IF poll.
        confirmed = self.get_mode()
        self.state.mode = confirmed or mode
        self._fire("mode_changed", self.state.mode)

    def set_mode_b(self, mode: str):
        code = MODE_CODES.get(mode, "2")
        self._set(f"MD1{code};")

    # ── TX / PTT ─────────────────────────────────────────────────────────────
    # TX1;=CAT TX ON  TX0;=CAT TX OFF  TX2; is read-only indicator only

    def set_ptt(self, tx: bool):
        self._set("TX1;" if tx else "TX0;")
        self.state.is_tx = tx
        self._fire("ptt_changed", tx)

    def get_ptt(self) -> Optional[bool]:
        r = self._cmd("TX;")
        if r and r.startswith("TX") and len(r) >= 3:
            return r[2] in ("1", "2")
        return None

    # ── Meters ───────────────────────────────────────────────────────────────

    def get_smeter(self) -> Optional[int]:
        r = self._cmd("SM0;")
        if r and r.startswith("SM") and len(r) >= 6:
            try:
                return int(r[3:6])
            except ValueError:
                pass
        return None

    def get_meter(self, mtype: int) -> Optional[int]:
        """RM: 1=S, 3=COMP, 4=ALC, 5=PO, 6=SWR, 7=IDD, 8=VDD.

        Answer form (2308-F): RM + P1 + P2(000-255) + P3(000 fixed)
        e.g. RM5123000 → value 123. Some firmwares omit P3 → RM5123.
        """
        r = self._cmd(f"RM{mtype};")
        if not r or not r.startswith("RM") or len(r) < 5:
            return None
        try:
            # Prefer 3 digits after type nibble at [2]
            # RM5xxx… → [3:6]
            if len(r) >= 6 and r[3:6].isdigit():
                return int(r[3:6])
            # Fallback: last 3 digits in the string
            digits = "".join(ch for ch in r[2:] if ch.isdigit())
            if len(digits) >= 3:
                return int(digits[-3:])
            if digits:
                return int(digits)
        except ValueError:
            pass
        return None

    # ── Power ────────────────────────────────────────────────────────────────

    def get_power(self) -> Optional[int]:
        r = self._cmd("PC;")
        if r and r.startswith("PC") and len(r) >= 5:
            try:
                return int(r[2:5])
            except ValueError:
                pass
        return None

    def set_power(self, watts: int):
        w = max(5, min(100, int(watts)))
        self._set(f"PC{w:03d};")
        self.state.power = w
        self._fire("power_changed", w)

    # ── Main Rig Power (PS) ──────────────────────────────────────────────────
    # This is the radio's own AC power switch over CAT — separate from
    # CyberRig's serial connect/disconnect, which just opens/closes COM6.

    def get_radio_power(self) -> Optional[bool]:
        r = self._cmd("PS;")
        if r and r.startswith("PS") and len(r) >= 3:
            return r[2] == "1"
        return None

    def set_radio_power(self, on: bool):
        """Turn the rig's main power on/off (CAT PS0/PS1).

        Manual (PS command): powering on requires dummy data be sent first,
        then the real "PS1;" 1-2s later, or the rig won't wake from standby.
        The USB CAT interface itself stays alive while the rig is off, so no
        reconnect/COM-port cycling is needed either direction.

        A rig in standby does not answer ID;, so connect() fails and there is
        no open port to send through. Power-on then opens the port just for the
        wake pair and lets the reopen loop pick the link up once the rig
        answers; otherwise the UI button silently did nothing (2026-09-25).
        """
        if on and not (self._ser and self._ser.is_open):
            self._wake_from_standby()
            self.state.radio_on = on
            self._fire("radio_power_changed", on)
            return
        self._set(f"PS{'1' if on else '0'};")
        if on:
            def _wake():
                time.sleep(1.2)
                self._set("PS1;")
            threading.Thread(target=_wake, daemon=True, name="ps-wake").start()
        self.state.radio_on = on
        self._fire("radio_power_changed", on)

    def _wake_from_standby(self):
        """Send the PS1 wake pair through a short-lived port of our own."""
        if not self._port:
            return
        def _wake():
            # Held across the pair so a reopen attempt's ID; queues behind it.
            with self._lock:
                try:
                    ser = serial.Serial(
                        self._port, self._baud,
                        bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_TWO,
                        timeout=0.5,
                        write_timeout=1.0,
                    )
                except Exception:
                    log.warning("Radio power-on: could not open %s", self._port)
                    return
                try:
                    ser.write(b"PS1;")
                    time.sleep(1.2)
                    ser.write(b"PS1;")
                except Exception:
                    log.warning("Radio power-on: wake write failed on %s", self._port)
                finally:
                    try:
                        ser.close()
                    except Exception:
                        pass
            log.info("Radio power-on sent while disconnected; waiting for the rig to answer")
        threading.Thread(target=_wake, daemon=True, name="ps-wake").start()

    # ── Filter Width (SH, Table 3) ────────────────────────────────────────────

    def get_sh(self) -> Optional[int]:
        # Manual: Read SH0;  Answer SH P1 P2 P3 P3  → e.g. SH0013 (P1=0,P2=0,P3=13)
        r = self._cmd("SH0;")
        if r and r.startswith("SH") and len(r) >= 6:
            try:
                return int(r[4:6])
            except ValueError:
                pass
        # Older/short answers
        if r and r.startswith("SH") and len(r) >= 5:
            try:
                return int(r[3:5])
            except ValueError:
                pass
        return None

    def set_sh(self, code: int):
        # Manual: Set SH P1 P2 P3 P3;  with P1=0, P2=0, P3=00–23
        # Must be SH00{code:02d};  — NOT SH0{code:02d}; (that drops P2 and is ignored)
        max_c = sh_max_code(self.state.mode)
        code = max(0, min(max_c, int(code)))
        self._set(f"SH00{code:02d};")
        self.state.sh = code
        self._emit_filter()

    def _emit_filter(self):
        bw = sh_hz(self.state.sh, self.state.mode)
        self._fire("filter_changed", bw, self.state.if_shift)

    # ── IF Shift (IS) ─────────────────────────────────────────────────────────
    # IS00+XXXX; or IS00-XXXX; where XXXX = 0000-1200 Hz (20Hz steps)

    def get_if_shift(self) -> Optional[int]:
        r = self._cmd("IS0;")
        if r and r.startswith("IS") and len(r) >= 9:
            try:
                sign = 1 if r[4] == "+" else -1
                return sign * int(r[5:9])
            except (ValueError, IndexError):
                pass
        return None

    def set_if_shift(self, hz: int):
        hz = max(-1200, min(1200, int(hz)))
        hz = round(hz / 20) * 20
        sign = "+" if hz >= 0 else "-"
        self._set(f"IS00{sign}{abs(hz):04d};")
        self.state.if_shift = hz
        self._emit_filter()

    # ── Roofing filter (RF) ──────────────────────────────────────────────────
    # Set RF0{P2};  P2 1=12k 2=3k 4=500 5=300(opt)
    # Read RF0; → RF0{P3};  P3 6=12k 7=3k 9=500 A=300

    def get_roofing(self) -> Optional[str]:
        r = self._cmd("RF0;")
        if r and r.startswith("RF") and len(r) >= 4:
            return ROOF_GET.get(r[3])
        return None

    def set_roofing(self, name: str):
        code = ROOF_SET.get(str(name).upper())
        if code is None:
            return
        self._set(f"RF0{code};")
        self.state.roofing = str(name).upper()
        self._fire("roofing_changed", self.state.roofing)

    # ── AGC ──────────────────────────────────────────────────────────────────

    def get_agc(self) -> Optional[str]:
        r = self._cmd("GT0;")
        if r and r.startswith("GT") and len(r) >= 4:
            try:
                code = int(r[3])
                return AGC_ANS.get(code, "AUTO")
            except (ValueError, IndexError):
                pass
        return None

    def set_agc(self, mode: str):
        code = AGC_CODES.get(mode, 4)
        self._set(f"GT0{code};")
        self.state.agc = mode
        self._fire("agc_changed", mode)

    # ── Preamp / ATT ─────────────────────────────────────────────────────────

    def get_preamp(self) -> Optional[str]:
        r = self._cmd("PA0;")
        if r and r.startswith("PA") and len(r) >= 4:
            return PREAMP.get(r[3], "IPO")
        return None

    def set_preamp(self, mode: str):
        code = PREAMP_CODES.get(mode, "0")
        self._set(f"PA0{code};")
        self.state.preamp = mode
        self._fire("preamp_changed", mode)

    def get_att(self) -> Optional[str]:
        r = self._cmd("RA0;")
        if r and r.startswith("RA") and len(r) >= 4:
            return ATT.get(r[3], "OFF")
        return None

    def set_att(self, mode: str):
        code = ATT_CODES.get(mode, "0")
        self._set(f"RA0{code};")
        self.state.att = mode
        self._fire("att_changed", mode)

    # ── AF / RF Gain ─────────────────────────────────────────────────────────

    def get_af_gain(self) -> Optional[int]:
        r = self._cmd("AG0;")
        if r and r.startswith("AG") and len(r) >= 6:
            try:
                return int(r[3:6])
            except ValueError:
                pass
        return None

    def set_af_gain(self, val: int):
        val = max(0, min(255, int(val)))
        self._set(f"AG0{val:03d};")
        self.state.af_gain = val
        self._fire("af_changed", val)

    def get_rf_gain(self) -> Optional[int]:
        r = self._cmd("RG0;")
        if r and r.startswith("RG") and len(r) >= 6:
            try:
                return int(r[3:6])
            except ValueError:
                pass
        return None

    def set_rf_gain(self, val: int):
        val = max(0, min(255, int(val)))
        self._set(f"RG0{val:03d};")
        self.state.rf_gain = val
        self._fire("rf_changed", val)

    # ── Mic Gain ─────────────────────────────────────────────────────────────

    def set_mic_gain(self, val: int):
        val = max(0, min(100, int(val)))
        self._set(f"MG{val:03d};")
        self.state.mic_gain = val
        self._fire("mic_changed", val)

    # ── Noise Blanker ─────────────────────────────────────────────────────────
    # NB0{0|1};  NL0{000-010};

    def get_nb(self) -> Optional[bool]:
        r = self._cmd("NB0;")
        if r and r.startswith("NB") and len(r) >= 4:
            return r[3] == "1"
        return None

    def get_nb_level(self) -> Optional[int]:
        r = self._cmd("NL0;")
        if r and r.startswith("NL") and len(r) >= 6:
            try:
                return int(r[3:6])
            except ValueError:
                pass
        return None

    def set_nb(self, on: bool, level: Optional[int] = None):
        self._set(f"NB0{'1' if on else '0'};")
        self.state.nb = on
        if level is not None:
            lv = max(0, min(10, int(level)))
            self._set(f"NL0{lv:03d};")
            self.state.nb_level = lv
        self._fire("nb_changed", self.state.nb, self.state.nb_level)

    def set_nb_level(self, level: int):
        lv = max(0, min(10, int(level)))
        self._set(f"NL0{lv:03d};")
        self.state.nb_level = lv
        self._fire("nb_changed", self.state.nb, lv)

    # ── Noise Reduction ──────────────────────────────────────────────────────
    # NR0{0|1};  RL0{01-15};

    def get_nr(self) -> Optional[bool]:
        r = self._cmd("NR0;")
        if r and r.startswith("NR") and len(r) >= 4:
            return r[3] == "1"
        return None

    def get_nr_level(self) -> Optional[int]:
        r = self._cmd("RL0;")
        if r and r.startswith("RL") and len(r) >= 5:
            try:
                return int(r[3:5])
            except ValueError:
                pass
        return None

    def set_nr(self, on: bool, level: Optional[int] = None):
        self._set(f"NR0{'1' if on else '0'};")
        self.state.nr = on
        if level is not None:
            lv = max(1, min(15, int(level)))
            self._set(f"RL0{lv:02d};")
            self.state.nr_level = lv
        self._fire("nr_changed", self.state.nr, self.state.nr_level)

    def set_nr_level(self, level: int):
        lv = max(1, min(15, int(level)))
        self._set(f"RL0{lv:02d};")
        self.state.nr_level = lv
        self._fire("nr_changed", self.state.nr, lv)

    # ── Auto Notch (DNF) ─────────────────────────────────────────────────────

    def get_dnf(self) -> Optional[bool]:
        r = self._cmd("BC0;")
        if r and r.startswith("BC") and len(r) >= 4:
            return r[3] == "1"
        return None

    def set_dnf(self, on: bool):
        self._set(f"BC0{'1' if on else '0'};")
        self.state.dnf = on
        self._fire("dnf_changed", on)

    # ── Manual Notch ─────────────────────────────────────────────────────────
    # BP00{000|001};   BP01{001-320};  (P3 × 10 = Hz)

    def get_notch(self) -> Optional[bool]:
        r = self._cmd("BP00;")
        if r and r.startswith("BP00") and len(r) >= 7:
            return r[4:7] != "000"
        return None

    def get_notch_pos(self) -> Optional[int]:
        r = self._cmd("BP01;")
        if r and r.startswith("BP01") and len(r) >= 7:
            try:
                return int(r[4:7]) * 10
            except ValueError:
                pass
        return None

    def set_notch(self, on: bool, position_hz: Optional[int] = None):
        self._set(f"BP00{'001' if on else '000'};")
        self.state.notch = on
        if position_hz is not None:
            pos = max(10, min(3200, int(position_hz)))
            pos = (pos // 10) * 10
            code = pos // 10
            self._set(f"BP01{code:03d};")
            self.state.notch_pos = pos
        self._fire("notch_changed", on, self.state.notch_pos)

    def set_notch_freq(self, hz: int):
        pos = max(10, min(3200, int(hz)))
        pos = (pos // 10) * 10
        code = pos // 10
        self._set(f"BP01{code:03d};")
        self.state.notch_pos = pos
        self._fire("notch_changed", self.state.notch, pos)

    # ── Contour / APF ────────────────────────────────────────────────────────
    # CO0{P2}{P3:04d};  P2=0 on/off, 1 freq, 2 apf on/off, 3 apf freq

    def set_contour(self, on: bool):
        self._set(f"CO00{'0001' if on else '0000'};")
        self.state.contour = on
        self._fire("contour_changed", on, self.state.contour_freq)

    def set_contour_freq(self, hz: int):
        hz = max(10, min(3200, int(hz)))
        self._set(f"CO01{hz:04d};")
        self.state.contour_freq = hz
        self._fire("contour_changed", self.state.contour, hz)

    def get_contour_level(self) -> Optional[int]:
        raw = self.ex_read(3, 2, 2)
        if raw is None:
            return None
        try:
            return max(-40, min(20, int(str(raw).strip())))
        except ValueError:
            return None

    def set_contour_level(self, db: int):
        db = max(-40, min(20, int(db)))
        self.ex_write(3, 2, 2, f"{db:+03d}")
        self.state.contour_level = db
        self._fire("contour_changed", self.state.contour, self.state.contour_freq)

    def get_contour_width(self) -> Optional[int]:
        raw = self.ex_read(3, 2, 3)
        if raw is None:
            return None
        try:
            return max(1, min(11, int(str(raw).strip())))
        except ValueError:
            return None

    def set_contour_width(self, w: int):
        w = max(1, min(11, int(w)))
        self.ex_write(3, 2, 3, f"{w:02d}")
        self.state.contour_width = w
        self._fire("contour_changed", self.state.contour, self.state.contour_freq)

    def get_notch_width(self) -> Optional[str]:
        raw = self.ex_read(3, 2, 4)
        if raw is None:
            return None
        s = str(raw).strip()
        if s in ("0", "NARROW"):
            return "NARROW"
        if s in ("1", "WIDE"):
            return "WIDE"
        return None

    def set_notch_width(self, wide: bool):
        self.ex_write(3, 2, 4, "1" if wide else "0")
        self.state.notch_width = "WIDE" if wide else "NARROW"
        self._fire("notch_changed", self.state.notch, self.state.notch_pos)

    def set_apf(self, on: bool):
        self._set(f"CO02{'0001' if on else '0000'};")
        self.state.apf = on
        self._fire("apf_changed", self.state.apf, self.state.apf_freq)

    def set_apf_freq(self, hz: int):
        hz = max(-250, min(250, int(hz)))
        code = (hz + 250) // 10
        self._set(f"CO03{code:04d};")
        self.state.apf_freq = hz
        self._fire("apf_changed", self.state.apf, hz)

    def get_contour(self) -> Optional[bool]:
        # Answer is CO00{P3:04d}: the on/off flag is the LAST digit (r[7]).
        # r[6] is always "0", which read contour as OFF on every poll and
        # undid each button press.
        r = self._cmd("CO00;")
        if r and r.startswith("CO00") and len(r) >= 8:
            return r[4:8] == "0001"
        return None

    def get_contour_freq(self) -> Optional[int]:
        r = self._cmd("CO01;")
        if r and r.startswith("CO01") and len(r) >= 8:
            try:
                return int(r[4:8])
            except ValueError:
                pass
        return None

    def get_apf(self) -> Optional[bool]:
        r = self._cmd("CO02;")
        if r and r.startswith("CO02") and len(r) >= 8:
            return r[4:8] == "0001" or r[7] == "1"
        return None

    def get_apf_freq(self) -> Optional[int]:
        r = self._cmd("CO03;")
        if r and r.startswith("CO03") and len(r) >= 8:
            try:
                return int(r[4:8]) * 10 - 250
            except ValueError:
                pass
        return None

    # ── Split ─────────────────────────────────────────────────────────────────
    # ST{0|1|2};   FT2;=MAIN TX  FT3;=SUB TX

    def set_split(self, on: bool, tx_vfo: str = "B"):
        self._set(f"ST{'1' if on else '0'};")
        if on:
            # Set codes, not answer codes: 2 = MAIN (A), 3 = SUB (B).
            self._set("FT3;" if tx_vfo == "B" else "FT2;")
            self.state.tx_vfo = tx_vfo
        # Turning split off writes no FT, so tx_vfo is not ours to claim —
        # leave it for the poll to read back off the radio.
        self.state.split = on
        self._fire("split_changed", on)

    def get_split(self) -> Optional[bool]:
        r = self._cmd("ST;")
        if r and r.startswith("ST") and len(r) >= 3:
            # 0 off, 1 on, 2 on + 5 kHz up — anything but 0 is split.
            return r[2] != "0"
        return None

    def get_tx_vfo(self) -> Optional[str]:
        """Which VFO transmits, read from the radio (FT).

        FT is asymmetric, like GT: the SET parameter takes 2 for MAIN and 3
        for SUB, but the ANSWER reports 0 for MAIN and 1 for SUB (Yaesu
        FTDX10 CAT reference). Parsing the answer with the set codes would
        never match, and the app would keep believing whatever it last wrote
        — which is exactly how the panel's AGC button sat dark on AUTO.

        MAIN is VFO A, SUB is VFO B on this radio.
        """
        r = self._cmd("FT;")
        if r and r.startswith("FT") and len(r) >= 3:
            if r[2] == "0":
                return "A"
            if r[2] == "1":
                return "B"
        return None

    # ── RIT / XIT ────────────────────────────────────────────────────────────

    def set_rit(self, on: bool):
        self._set(f"RT{'1' if on else '0'};")
        self.state.rit = on
        self._fire("rit_changed", on, self.state.rit_offset)

    def set_xit(self, on: bool):
        self._set(f"XT{'1' if on else '0'};")
        self.state.xit = on
        self._fire("xit_changed", on)

    def clear_rit(self):
        self._set("RC;")
        self.state.rit_offset = 0
        self._fire("rit_changed", self.state.rit, 0)

    def rit_up(self, hz: int = 100):
        self._set(f"RU{abs(hz):04d};")

    def rit_down(self, hz: int = 100):
        self._set(f"RD{abs(hz):04d};")

    def set_rit_offset(self, hz: int):
        self.clear_rit()
        if hz > 0:
            self._set(f"RU{min(hz, 9990):04d};")
        elif hz < 0:
            self._set(f"RD{min(-hz, 9990):04d};")
        self.state.rit_offset = hz
        self._fire("rit_changed", self.state.rit, hz)

    # ── Compressor ───────────────────────────────────────────────────────────
    # PR0{0|1};   PL{000-100};

    def get_compressor(self) -> Optional[bool]:
        r = self._cmd("PR0;")
        if r and r.startswith("PR") and len(r) >= 4:
            return r[3] == "1"
        return None

    def get_comp_level(self) -> Optional[int]:
        r = self._cmd("PL;")
        if r and r.startswith("PL") and len(r) >= 5:
            try:
                return int(r[2:5])
            except ValueError:
                pass
        return None

    def set_compressor(self, on: bool, level: Optional[int] = None):
        self._set(f"PR0{'1' if on else '0'};")
        self.state.compressor = on
        if level is not None:
            lv = max(0, min(100, int(level)))
            self._set(f"PL{lv:03d};")
            self.state.comp_level = lv
        self._fire("comp_changed", self.state.compressor, self.state.comp_level)

    def set_comp_level(self, level: int):
        lv = max(0, min(100, int(level)))
        self._set(f"PL{lv:03d};")
        self.state.comp_level = lv
        self._fire("comp_changed", self.state.compressor, lv)

    # ── VOX ──────────────────────────────────────────────────────────────────
    # VX{0|1};   VG{000-100};   VD{00-33};

    def get_vox(self) -> Optional[bool]:
        r = self._cmd("VX;")
        if r and r.startswith("VX") and len(r) >= 3:
            return r[2] == "1"
        return None

    def get_vox_gain(self) -> Optional[int]:
        r = self._cmd("VG;")
        if r and r.startswith("VG") and len(r) >= 5:
            try:
                return int(r[2:5])
            except ValueError:
                pass
        return None

    def set_vox(self, on: bool, gain: Optional[int] = None, delay_ms: Optional[int] = None):
        self._set(f"VX{'1' if on else '0'};")
        self.state.vox = on
        if gain is not None:
            g = max(0, min(100, int(gain)))
            self._set(f"VG{g:03d};")
            self.state.vox_gain = g
        if delay_ms is not None:
            code = ms_to_delay_code(delay_ms)
            self._set(f"VD{code:02d};")
            self.state.vox_delay = _DELAY_TABLE[code]
        self._fire("vox_changed", self.state.vox, self.state.vox_gain, self.state.vox_delay)

    def set_vox_gain(self, gain: int):
        g = max(0, min(100, int(gain)))
        self._set(f"VG{g:03d};")
        self.state.vox_gain = g
        self._fire("vox_changed", self.state.vox, g, self.state.vox_delay)

    def set_vox_delay(self, delay_ms: int):
        code = ms_to_delay_code(delay_ms)
        self._set(f"VD{code:02d};")
        self.state.vox_delay = _DELAY_TABLE[code]
        self._fire("vox_changed", self.state.vox, self.state.vox_gain, self.state.vox_delay)

    # ── Monitor ──────────────────────────────────────────────────────────────
    # ML0{0000|0001};   ML1{0000-0100};

    def get_monitor(self) -> Optional[bool]:
        r = self._cmd("ML0;")
        if r and r.startswith("ML0") and len(r) >= 7:
            return r[3:7] != "0000"
        return None

    def get_mon_level(self) -> Optional[int]:
        r = self._cmd("ML1;")
        if r and r.startswith("ML1") and len(r) >= 7:
            try:
                return int(r[3:7])
            except ValueError:
                pass
        return None

    def set_monitor(self, on: bool, level: Optional[int] = None):
        self._set(f"ML0{'0001' if on else '0000'};")
        self.state.monitor = on
        if level is not None:
            lv = max(0, min(100, int(level)))
            self._set(f"ML1{lv:04d};")
            self.state.mon_level = lv
        self._fire("monitor_changed", self.state.monitor, self.state.mon_level)

    def set_mon_level(self, level: int):
        lv = max(0, min(100, int(level)))
        self._set(f"ML1{lv:04d};")
        self.state.mon_level = lv
        self._fire("monitor_changed", self.state.monitor, lv)

    # ── CW ───────────────────────────────────────────────────────────────────
    # KS{004-060};   KP{00-75};   BI{0|1};   SD{00-33};
    # KM5{text};  KY5;

    def get_cw_speed(self) -> Optional[int]:
        r = self._cmd("KS;")
        if r and r.startswith("KS") and len(r) >= 5:
            try:
                return int(r[2:5])
            except ValueError:
                pass
        return None

    def get_cw_breakin(self) -> Optional[bool]:
        r = self._cmd("BI;")
        if r and r.startswith("BI") and len(r) >= 3:
            return r[2] == "1"
        return None

    def set_cw_speed(self, wpm: int):
        w = max(4, min(60, int(wpm)))
        self._set(f"KS{w:03d};")
        self.state.cw_speed = w
        self._fire("cw_changed", w, self.state.cw_pitch, self.state.cw_breakin, self.state.cw_delay)

    def set_cw_pitch(self, hz: int):
        hz = max(300, min(1050, int(hz)))
        code = (hz - 300) // 10
        self._set(f"KP{code:02d};")
        self.state.cw_pitch = hz
        self._fire("cw_changed", self.state.cw_speed, hz, self.state.cw_breakin, self.state.cw_delay)

    def set_cw_breakin(self, on: bool):
        self._set(f"BI{'1' if on else '0'};")
        self.state.cw_breakin = on
        self._fire("cw_changed", self.state.cw_speed, self.state.cw_pitch, on, self.state.cw_delay)

    def set_cw_delay(self, ms: int):
        code = ms_to_delay_code(ms)
        self._set(f"SD{code:02d};")
        self.state.cw_delay = _DELAY_TABLE[code]

    def send_cw(self, text: str):
        text = text.upper()[:50]
        self._set(f"KM5{text};")
        self._set("KY5;")

    # ── Antenna ──────────────────────────────────────────────────────────────

    def get_antenna(self) -> Optional[int]:
        r = self._cmd("AN0;")
        if r and r.startswith("AN") and len(r) >= 4:
            try:
                return int(r[3])
            except ValueError:
                pass
        return None

    def set_antenna(self, ant: int):
        a = max(1, min(2, int(ant)))
        self._set(f"AN0{a};")
        self.state.antenna = a
        self._fire("antenna_changed", a)

    # ── Internal antenna tuner (AC) ──────────────────────────────────────────
    # Manual 2308-F: AC P1 P2 P3;
    #   P1=0 fixed, P2=0 fixed
    #   P3=0 Tuner OFF · 1 Tuner ON · 2 Tuning Start / Tuning Stop
    # Set e.g. AC001;  Read AC;  Answer AC00n;

    def get_atu(self) -> Optional[dict]:
        """Return {'on': bool, 'tuning': bool} or None if unread.

        Note: many FTDX10 firmware builds only answer P3=0/1 on read;
        P3=2 is primarily a write (start/stop). UI 'tuning' may be driven
        by software timers + TX activity when the radio never reports 2.
        """
        r = self._cmd("AC;")
        if not r or not r.startswith("AC"):
            return None
        code = None
        if len(r) >= 5 and r[2:4] == "00":
            try:
                code = int(r[4])
            except ValueError:
                pass
        if code is None:
            try:
                code = int(r[-1])
            except ValueError:
                return None
        return {
            "on": code in (1, 2),
            "tuning": code == 2,
        }

    def set_atu(self, on: bool):
        """Enable or bypass the FTDX10 internal ATU (AC001 / AC000)."""
        self._set(f"AC00{'1' if on else '0'};")
        self.state.atu = bool(on)
        if not on:
            self.state.atu_tuning = False
            self._atu_tune_t0 = 0.0
            self._atu_saw_tx = False
        self._fire("atu_changed", self.state.atu, self.state.atu_tuning)

    def atu_tune(self):
        """Start or stop the internal ATU tuning cycle (AC002).

        P3=2 toggles: first send starts tune (keys radio), second aborts.
        When finished the radio usually leaves the tuner ON.
        """
        if not self.state.atu and not self.state.atu_tuning:
            self._set("AC001;")
            self.state.atu = True
        self._set("AC002;")
        if self.state.atu_tuning:
            # Abort
            self.state.atu_tuning = False
            self._atu_tune_t0 = 0.0
            self._atu_saw_tx = False
        else:
            self.state.atu_tuning = True
            self.state.atu = True
            self._atu_tune_t0 = time.time()
            self._atu_saw_tx = False
        self._fire("atu_changed", self.state.atu, self.state.atu_tuning)

    # ── Lock ─────────────────────────────────────────────────────────────────

    def get_lock(self) -> Optional[bool]:
        r = self._cmd("LK;")
        if r and r.startswith("LK") and len(r) >= 3:
            return r[2] == "1"
        return None

    def set_lock(self, on: bool):
        self._set(f"LK{'1' if on else '0'};")
        self.state.locked = on
        self._fire("lock_changed", on)

    # ── Mic gain (read) ──────────────────────────────────────────────────────

    def get_mic_gain(self) -> Optional[int]:
        r = self._cmd("MG;")
        if r and r.startswith("MG") and len(r) >= 5:
            try:
                return int(r[2:5])
            except ValueError:
                pass
        return None

    # ── Quick Split ──────────────────────────────────────────────────────────

    def quick_split(self):
        self._set("QS;")

    def set_mox(self, on: bool):
        self._set(f"MX{'1' if on else '0'};")

    # ── EX Menu ──────────────────────────────────────────────────────────────
    # EX{p1:02d}{p2:02d}{p3:02d}[value];

    def ex_read(self, p1: int, p2: int, p3: int) -> Optional[str]:
        prefix = f"EX{p1:02d}{p2:02d}{p3:02d}"
        r = self._cmd(f"{prefix};")
        if r and r.startswith("EX") and len(r) > 8:
            return r[8:]
        return None

    def ex_write(self, p1: int, p2: int, p3: int, value: str):
        self._set(f"EX{p1:02d}{p2:02d}{p3:02d}{value};")

    # ── Parametric EQ (EX 03 03) ────────────────────────────────────────────
    # section: "tx" (TX DSP EQ) or "mic" (Mic P-EQ); band: 0/1/2 (low/mid/high)

    def get_eq_band(self, section: str, band: int) -> Optional[dict]:
        base = EQ_BASES[section][band]
        fc = self.ex_read(3, 3, base)
        lv = self.ex_read(3, 3, base + 1)
        bw = self.ex_read(3, 3, base + 2)
        if fc is None or lv is None or bw is None:
            return None
        try:
            return {"freq": int(fc), "level": int(lv) - 20, "bw": int(bw)}
        except ValueError:
            return None

    def set_eq_band(self, section: str, band: int, freq: int, level: int, bw: int):
        """freq/bw are raw EX codes (see EQ_FREQ_OPTIONS); level is signed dB (-20..+10)."""
        base = EQ_BASES[section][band]
        lv_val = level + 20
        self.ex_write(3, 3, base, f"{freq:02d}")
        self.ex_write(3, 3, base + 1, f"{lv_val:03d}")
        self.ex_write(3, 3, base + 2, f"{bw:02d}")

    # ── IF composite read ─────────────────────────────────────────────────────
    # [0:2]=IF [2:5]=mem [5:14]=freq(9) [14]=sign [15:19]=clari(4)
    # [19]=RXclar [20]=TXclar [21]=mode [22]=vfo …
    #
    # P6 (mode) is parsed for diagnostics but MUST NOT drive state.mode —
    # after FA/FB/SV/AB or MD0 it can lag the real MAIN mode for seconds
    # while MD0 is already correct (live 2026-08-27).

    def get_if_status(self) -> Optional[dict]:
        r = self._cmd("IF;")
        if not r or not r.startswith("IF") or len(r) < 27:
            return None
        try:
            freq  = int(r[5:14])
            sign  = 1 if r[14] == "+" else -1
            clari = sign * int(r[15:19])
            rx_c  = r[19] == "1"
            tx_c  = r[20] == "1"
            mode  = MODES.get(r[21], "USB")
            return {"freq": freq, "clarifier": clari,
                    "rit": rx_c, "xit": tx_c, "mode": mode}
        except (IndexError, ValueError):
            return None

    # ── Poll helpers (radio → app; only fire when value actually changed) ────

    def _assign(self, attr: str, value, event: Optional[str] = None, *event_args) -> bool:
        """Set state.attr if value is not None and differs; optionally fire event."""
        if value is None:
            return False
        if getattr(self.state, attr) == value:
            return False
        setattr(self.state, attr, value)
        if event:
            self._fire(event, *(event_args if event_args else (value,)))
        return True

    def _poll_preamp_att(self):
        """Front-panel IPO/AMP/ATT — high priority (user-visible mismatch case)."""
        self._assign("preamp", self.get_preamp(), "preamp_changed")
        self._assign("att", self.get_att(), "att_changed")

    def _poll_filter_agc(self):
        sh = self.get_sh()
        ifs = self.get_if_shift()
        filt_changed = False
        if sh is not None and sh != self.state.sh:
            self.state.sh = sh
            filt_changed = True
        if ifs is not None and ifs != self.state.if_shift:
            self.state.if_shift = ifs
            filt_changed = True
        if filt_changed:
            self._emit_filter()
        self._assign("agc", self.get_agc(), "agc_changed")
        self._assign("roofing", self.get_roofing(), "roofing_changed")

    def _poll_gains_power(self):
        self._assign("af_gain", self.get_af_gain(), "af_changed")
        self._assign("rf_gain", self.get_rf_gain(), "rf_changed")
        self._assign("power", self.get_power(), "power_changed")
        self._assign("mic_gain", self.get_mic_gain(), "mic_changed")

    def _poll_noise(self):
        nb = self.get_nb()
        nb_lv = self.get_nb_level()
        if nb is not None or nb_lv is not None:
            changed = False
            if nb is not None and nb != self.state.nb:
                self.state.nb = nb
                changed = True
            if nb_lv is not None and nb_lv != self.state.nb_level:
                self.state.nb_level = nb_lv
                changed = True
            if changed:
                self._fire("nb_changed", self.state.nb, self.state.nb_level)

        nr = self.get_nr()
        nr_lv = self.get_nr_level()
        if nr is not None or nr_lv is not None:
            changed = False
            if nr is not None and nr != self.state.nr:
                self.state.nr = nr
                changed = True
            if nr_lv is not None and nr_lv != self.state.nr_level:
                self.state.nr_level = nr_lv
                changed = True
            if changed:
                self._fire("nr_changed", self.state.nr, self.state.nr_level)

    def _poll_notch_contour(self):
        self._assign("dnf", self.get_dnf(), "dnf_changed")
        notch = self.get_notch()
        npos = self.get_notch_pos()
        if notch is not None or npos is not None:
            changed = False
            if notch is not None and notch != self.state.notch:
                self.state.notch = notch
                changed = True
            if npos is not None and npos != self.state.notch_pos:
                self.state.notch_pos = npos
                changed = True
            if changed:
                self._fire("notch_changed", self.state.notch, self.state.notch_pos)

        ctr = self.get_contour()
        cfreq = self.get_contour_freq()
        if ctr is not None or cfreq is not None:
            changed = False
            if ctr is not None and ctr != self.state.contour:
                self.state.contour = ctr
                changed = True
            if cfreq is not None and cfreq != self.state.contour_freq:
                self.state.contour_freq = cfreq
                changed = True
            if changed:
                self._fire("contour_changed", self.state.contour, self.state.contour_freq)
        lvl = self.get_contour_level()
        if lvl is not None and lvl != self.state.contour_level:
            self.state.contour_level = lvl
            self._fire("contour_changed", self.state.contour, self.state.contour_freq)
        wid = self.get_contour_width()
        if wid is not None and wid != self.state.contour_width:
            self.state.contour_width = wid
            self._fire("contour_changed", self.state.contour, self.state.contour_freq)
        nw = self.get_notch_width()
        if nw is not None and nw != self.state.notch_width:
            self.state.notch_width = nw
            self._fire("notch_changed", self.state.notch, self.state.notch_pos)

        apf = self.get_apf()
        afreq = self.get_apf_freq()
        if apf is not None or afreq is not None:
            changed = False
            if apf is not None and apf != self.state.apf:
                self.state.apf = apf
                changed = True
            if afreq is not None and afreq != self.state.apf_freq:
                self.state.apf_freq = afreq
                changed = True
            if changed:
                self._fire("apf_changed", self.state.apf, self.state.apf_freq)

    def _poll_tx_toggles(self):
        self._assign("split", self.get_split(), "split_changed")
        self._assign("tx_vfo", self.get_tx_vfo(), "split_changed",
                     self.state.split)
        self._assign("fine", self.get_fine(), "fine_changed")
        comp = self.get_compressor()
        clv = self.get_comp_level()
        if comp is not None or clv is not None:
            changed = False
            if comp is not None and comp != self.state.compressor:
                self.state.compressor = comp
                changed = True
            if clv is not None and clv != self.state.comp_level:
                self.state.comp_level = clv
                changed = True
            if changed:
                self._fire("comp_changed", self.state.compressor, self.state.comp_level)

        vox = self.get_vox()
        vg = self.get_vox_gain()
        if vox is not None or vg is not None:
            changed = False
            if vox is not None and vox != self.state.vox:
                self.state.vox = vox
                changed = True
            if vg is not None and vg != self.state.vox_gain:
                self.state.vox_gain = vg
                changed = True
            if changed:
                self._fire("vox_changed", self.state.vox, self.state.vox_gain, self.state.vox_delay)

        mon = self.get_monitor()
        ml = self.get_mon_level()
        if mon is not None or ml is not None:
            changed = False
            if mon is not None and mon != self.state.monitor:
                self.state.monitor = mon
                changed = True
            if ml is not None and ml != self.state.mon_level:
                self.state.mon_level = ml
                changed = True
            if changed:
                self._fire("monitor_changed", self.state.monitor, self.state.mon_level)

    def _poll_atu(self):
        """Sync internal ATU on/off (+ clear tuning when cycle ends)."""
        atu = self.get_atu()
        changed = False
        if atu is not None:
            on = bool(atu.get("on"))
            # Only force tuning=True from CAT if radio reports P3=2
            if atu.get("tuning"):
                if not self.state.atu_tuning:
                    self.state.atu_tuning = True
                    self._atu_tune_t0 = time.time()
                    changed = True
            if on != self.state.atu:
                self.state.atu = on
                changed = True
                if not on and self.state.atu_tuning:
                    self.state.atu_tuning = False
                    self._atu_tune_t0 = 0.0
                    self._atu_saw_tx = False

        if self.state.atu_tuning:
            if self.state.is_tx:
                self._atu_saw_tx = True
            # Cycle done: keyed then returned to RX, or 30s safety timeout
            elapsed = time.time() - self._atu_tune_t0 if self._atu_tune_t0 else 0
            if (self._atu_saw_tx and not self.state.is_tx and elapsed > 1.0) or elapsed > 30:
                self.state.atu_tuning = False
                self._atu_tune_t0 = 0.0
                self._atu_saw_tx = False
                # After a successful cycle the tuner is almost always left ON
                if not self.state.atu:
                    self.state.atu = True
                changed = True

        if changed:
            self._fire("atu_changed", self.state.atu, self.state.atu_tuning)

    def _poll_antenna_cw(self):
        self._assign("antenna", self.get_antenna(), "antenna_changed")
        self._assign("locked", self.get_lock(), "lock_changed")
        self._assign("radio_on", self.get_radio_power(), "radio_power_changed")
        self._poll_atu()
        cw = self.get_cw_speed()
        bi = self.get_cw_breakin()
        if cw is not None or bi is not None:
            changed = False
            if cw is not None and cw != self.state.cw_speed:
                self.state.cw_speed = cw
                changed = True
            if bi is not None and bi != self.state.cw_breakin:
                self.state.cw_breakin = bi
                changed = True
            if changed:
                self._fire(
                    "cw_changed",
                    self.state.cw_speed,
                    self.state.cw_pitch,
                    self.state.cw_breakin,
                    self.state.cw_delay,
                )

    def sync_from_rig(self, full: bool = False):
        """One-shot radio → state sync (used at connect and for full refresh).

        full=True reads every UI-visible control; False does the fast subset.
        Safe to call from poll thread only (uses CAT serial lock via _cmd).
        """
        self._poll_preamp_att()
        self._poll_filter_agc()
        if full:
            self._poll_gains_power()
            self._poll_noise()
            self._poll_notch_contour()
            self._poll_tx_toggles()
            self._poll_antenna_cw()

    # ── Poll loop ─────────────────────────────────────────────────────────────

    def _poll_loop(self, interval: float):
        err = 0
        cycle = 0
        # Initial full pull so UI matches front panel right after connect
        try:
            self.sync_from_rig(full=True)
        except Exception:
            pass

        while self._running:
            try:
                if not self.is_connected:
                    # Link declared dead by _note_silence(). Keep the loop alive
                    # and keep trying to get it back, rather than exiting and
                    # leaving the app to show stale values forever.
                    self._reopen()
                    time.sleep(interval)
                    continue

                # While TX: skip IF (long reply) — serial time goes to meters/PTT only
                if not self.state.is_tx:
                    if_data = self.get_if_status()
                    if if_data:
                        self.state.freq_verified = True
                        if if_data["freq"] != self.state.freq_a:
                            self.state.freq_a = if_data["freq"]
                            self._fire("freq_changed", if_data["freq"])
                        # Mode comes from MD0 below — IF P6 is stale after VFO writes.
                        if if_data["rit"] != self.state.rit:
                            self.state.rit = if_data["rit"]
                            self._fire("rit_changed", if_data["rit"], self.state.rit_offset)
                        if if_data.get("xit") is not None and if_data["xit"] != self.state.xit:
                            self.state.xit = if_data["xit"]
                            self._fire("xit_changed", if_data["xit"])
                    self._refresh_mode()

                if cycle % 3 == 0 or self.state.is_tx:
                    tx = self.get_ptt()
                    if tx is not None and tx != self.state.is_tx:
                        self.state.is_tx = tx
                        self._fire("ptt_changed", tx)

                if self.state.is_tx:
                    # Read only what a consumer has asked for. Each RM is a
                    # serial round trip out of a budget already tight enough
                    # that get_if_status() is skipped entirely during TX, so
                    # polling all eight types unconditionally would cost real
                    # meter latency. _cmd collects multi-frame answers, so
                    # these are no longer stuck on the RM echo.
                    changed = False
                    for rm in sorted(self.meter_sources):
                        val = self.get_meter(rm)
                        if val is None:
                            continue
                        self.state.meters[rm] = val
                        alias = _METER_ALIAS.get(rm)
                        if alias:
                            setattr(self.state, alias, val)
                        changed = True
                    if changed:
                        # Legacy 3-tuple: the web UI and the ATU tune routine
                        # read these. Any of the three not currently polled
                        # keeps its last value rather than reporting zero.
                        self._fire(
                            "meter_update",
                            self.state.power_meter,
                            self.state.alc_meter,
                            self.state.swr_meter,
                        )
                        self._fire("meters_update", dict(self.state.meters))
                else:
                    sm = self.get_smeter()
                    if sm is not None:
                        self.state.smeter = sm
                        self._fire("smeter_update", sm)

                    # Clear stale TX meters when not transmitting. This has to
                    # zero state.meters (the dict the panel renders through
                    # meters_update) as well as the three legacy aliases —
                    # clearing only the aliases left the panel's PO/SWR/ALC
                    # bars frozen at the last TX reading until the next TX.
                    # Costs no serial time, so it runs every cycle rather than
                    # waiting for the cycle%4 slot below.
                    if (any(self.state.meters.values())
                            or self.state.power_meter
                            or self.state.alc_meter
                            or self.state.swr_meter):
                        self.state.power_meter = 0
                        self.state.alc_meter = 0
                        self.state.swr_meter = 0
                        for rm in list(self.state.meters):
                            self.state.meters[rm] = 0
                        self._fire("meter_update", 0, 0, 0)
                        self._fire("meters_update", dict(self.state.meters))

                    if cycle % 4 == 0:
                        fb = self.get_freq_b()
                        if fb and fb != self.state.freq_b:
                            self.state.freq_b = fb
                            self._fire("freq_b_changed", fb)

                    # Rotate secondary CAT reads so front-panel changes track in the
                    # UI without starving IF/smeter. ~0.3s interval → each group ~1.5–2s.
                    # Preamp/ATT every 5 cycles (~1.5s) — highest user-visible mismatch.
                    slot = cycle % 10
                    if slot == 0 or slot == 5:
                        self._poll_preamp_att()
                    elif slot == 1:
                        self._poll_filter_agc()
                    elif slot == 2:
                        self._poll_gains_power()
                    elif slot == 3:
                        self._poll_noise()
                    elif slot == 4:
                        self._poll_notch_contour()
                    elif slot == 6:
                        self._poll_tx_toggles()
                    elif slot == 7:
                        self._poll_antenna_cw()

                # While internal ATU is cycling, re-check often so TUNE clears
                # when the radio returns to RX (works during TX meter path too).
                if self.state.atu_tuning and cycle % 2 == 0:
                    self._poll_atu()

                err = 0
                cycle = (cycle + 1) % 64
            except Exception:
                err += 1
                if err >= 10 and self.is_connected:
                    log.warning("CAT poll failed %d times — marking disconnected", err)
                    self.is_connected = False
                    self._fire("connected_changed", False)
                    err = 0
            # PO/ALC/SWR only move while transmitting — poll much faster then
            # (RX-side cadence is unchanged, so front-panel tracking traffic
            # doesn't increase when meters don't matter).
            time.sleep(0.08 if self.state.is_tx else interval)
