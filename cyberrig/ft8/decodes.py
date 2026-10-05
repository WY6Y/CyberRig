"""Decode feed: tail JTDX's ALL.TXT.

Why a log file and not the UDP protocol, which is richer and is what the final
build will use: GridTracker2 is already bound to 0.0.0.0:2237 on this machine and
JTDX unicasts there. A second listener on that port does not duplicate the stream
-- with SO_REUSEPORT the kernel load-balances datagrams between sockets, so we
would silently steal roughly half of GridTracker's decodes. Breaking a working
tool to demo a new one is not a trade worth making.

Tailing ALL.TXT is completely passive: no port, no config change, no JTDX restart,
and it cannot perturb the decoder. It gives every decode with timestamp, SNR, DT,
audio frequency and message. What it cannot give is the Reply(4) path for actually
calling a station -- which is fine, because this build is receive-only.

Moving to UDP later means either switching JTDX to multicast (224.0.0.1:2237, where
every listener gets every packet) or using GridTracker's own relay to forward to a
second port. Both need JTDX or GridTracker touched, so both wait for a quiet moment.

Line formats in the wild (JTDX v2.2.159):

    20260901_030045 -14  0.4 1920 ~ CQ XQ3SK FF46             ^
    20260901_025558  14.074 MHz  FT8
    20260901_030230.219(0)  Transmitting 14.074 MHz + 2052Hz  FT8:  RI1ANC K1ABC FN42
    20260901_025545  partial loss of data                     d

Only the first is a decode. The second and third are how we learn the dial
frequency without asking the radio. The fourth is JTDX's audio-underrun warning
and is worth surfacing -- it is the one symptom that says the capture path is
failing, and in August it was the ONLY symptom of a broken sound card.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("cyberrig.ft8.decodes")

DECODE_RE = re.compile(
    r"^(?P<ts>\d{8}_\d{6})\s+"
    r"(?P<snr>-?\d+)\s+"
    r"(?P<dt>-?\d+\.\d+)\s+"
    r"(?P<freq>\d+)\s+"
    r"(?P<modechar>[~+^&$@])\s"
    r"(?P<msg>.*?)\s*$"
)
DIAL_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})\s+(?P<mhz>\d+\.\d+)\s+MHz\s+(?P<mode>\S+)")
TX_RE = re.compile(
    r"^(?P<ts>\d{8}_\d{6})[\d.()]*\s+Transmitting\s+(?P<mhz>\d+\.\d+)\s+MHz\s*\+\s*"
    r"(?P<freq>\d+)Hz\s+(?P<mode>\S+):\s+(?P<msg>.*?)\s*$"
)
LOSS_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})\s+partial loss of data")

# Trailing JTDX decoration: ^ = new call, * = worked before, etc. Stripped from
# the message before parsing so the last token is really the grid.
FLAG_CHARS = "^*?!&$@#+"

CQ_RE = re.compile(r"^CQ(?:\s+(?:DX|NA|EU|AS|AF|SA|OC|AN|POTA|SOTA|TEST|[A-Z]{2}))?\s+(?P<call>[A-Z0-9/]+)(?:\s+(?P<grid>[A-R]{2}\d{2}))?\s*$")
DIRECTED_RE = re.compile(r"^(?P<to>[A-Z0-9/<>.]+)\s+(?P<de>[A-Z0-9/]+)\s+(?P<rest>.*)$")
GRID_RE = re.compile(r"^[A-R]{2}\d{2}$")


def _parse_ts(ts: str) -> float:
    dt = datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_message(msg: str) -> dict:
    """Pull the interesting station out of an FT8 message.

    Returns dx_call (the station this decode is ABOUT, i.e. the sender), to_call
    (who they are working, if anyone), grid, report, and whether it is a CQ.
    """
    text = msg.strip().rstrip(FLAG_CHARS).strip()
    out = {"cq": False, "dx_call": None, "to_call": None, "grid": None, "report": None}

    m = CQ_RE.match(text)
    if m:
        out["cq"] = True
        out["dx_call"] = m.group("call")
        out["grid"] = m.group("grid")
        return out

    m = DIRECTED_RE.match(text)
    if m:
        out["to_call"] = m.group("to")
        out["dx_call"] = m.group("de")
        rest = (m.group("rest") or "").strip()
        if GRID_RE.match(rest):
            out["grid"] = rest
        elif rest:
            out["report"] = rest
        return out

    return out


class DecodeTailer:
    """Follows ALL.TXT and calls back with structured decodes.

    Handles the month rollover (JTDX writes YYYYMM_ALL.TXT) and truncation by
    re-resolving the path and re-stat'ing on every poll. A tailer that caches an
    fd silently goes deaf at midnight on the 1st, which is exactly when you are
    least likely to notice.
    """

    def __init__(self, log_dir: Path, on_decode: Callable[[dict], None],
                 on_event: Optional[Callable[[dict], None]] = None,
                 poll_s: float = 1.0):
        self._dir = Path(log_dir)
        self._on_decode = on_decode
        self._on_event = on_event or (lambda e: None)
        self._poll = poll_s
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pos = 0
        self._path: Optional[Path] = None
        self.dial_mhz: Optional[float] = None
        self.mode: str = "FT8"
        self.last_line_at: float = 0.0
        self.loss_events: int = 0

    def current_path(self) -> Optional[Path]:
        """Newest YYYYMM_ALL.TXT in the directory."""
        candidates = sorted(self._dir.glob("*_ALL.TXT"))
        return candidates[-1] if candidates else None

    def start(self, backfill_lines: int = 400):
        path = self.current_path()
        if path is None:
            log.error("no ALL.TXT under %s -- decode feed will stay empty", self._dir)
        else:
            self._path = path
            self._pos = path.stat().st_size
            # Dial frequency must be seeded from further back than the decode
            # backfill reaches. JTDX writes a "14.074 MHz FT8" line only when the
            # band CHANGES, so on a station that has sat on one band all evening
            # the most recent one can be thousands of decodes ago -- and without
            # it every decode carries a null frequency and the UI reads "--.---"
            # forever rather than for a moment.
            self._seed_dial(path)
            self._backfill(path, backfill_lines)
        self._thread = threading.Thread(target=self._run, name="ft8-tail", daemon=True)
        self._thread.start()
        log.info("tailing %s", self._path)

    def stop(self):
        self._stop.set()

    def _seed_dial(self, path: Path, window_bytes: int = 4 << 20):
        """Scan backwards for the most recent dial-frequency line."""
        try:
            size = path.stat().st_size
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(max(0, size - window_bytes))
                lines = f.read().splitlines()
        except OSError as e:
            log.warning("dial seed failed: %s", e)
            return
        for line in reversed(lines):
            m = DIAL_RE.match(line) or TX_RE.match(line)
            if m:
                try:
                    self.dial_mhz = float(m.group("mhz"))
                    self.mode = m.group("mode").rstrip(":")
                    log.info("dial seeded from log: %.3f MHz %s", self.dial_mhz, self.mode)
                except (ValueError, IndexError):
                    continue
                return
        log.warning("no dial-frequency line in the last %d bytes of %s",
                    window_bytes, path.name)

    def _backfill(self, path: Path, n: int):
        """Seed the list so the UI is not empty on first load."""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-n:]
        except OSError as e:
            log.warning("backfill failed: %s", e)
            return
        for line in tail:
            self._handle(line, backfill=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                path = self.current_path()
                if path is None:
                    self._stop.wait(self._poll)
                    continue
                if path != self._path:
                    log.info("ALL.TXT rolled over: %s -> %s", self._path, path)
                    self._path = path
                    self._pos = 0
                size = path.stat().st_size
                if size < self._pos:
                    log.info("ALL.TXT truncated -- restarting from 0")
                    self._pos = 0
                if size > self._pos:
                    with path.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(self._pos)
                        chunk = f.read()
                        self._pos = f.tell()
                    for line in chunk.splitlines():
                        self._handle(line)
            except Exception:
                log.exception("tail loop error")
            self._stop.wait(self._poll)

    def _handle(self, line: str, backfill: bool = False):
        line = line.rstrip("\n")
        if not line:
            return
        self.last_line_at = time.time()

        m = DECODE_RE.match(line)
        if m:
            msg = m.group("msg")
            rec = {
                "ts": _parse_ts(m.group("ts")),
                "ts_utc": m.group("ts"),
                "snr": int(m.group("snr")),
                "dt": float(m.group("dt")),
                "freq": int(m.group("freq")),
                "msg": msg.rstrip(FLAG_CHARS).strip(),
                "dial_mhz": self.dial_mhz,
                "backfill": backfill,
            }
            rec.update(parse_message(msg))
            self._on_decode(rec)
            return

        m = DIAL_RE.match(line) or TX_RE.match(line)
        if m:
            try:
                self.dial_mhz = float(m.group("mhz"))
                self.mode = m.group("mode").rstrip(":")
            except (ValueError, IndexError):
                pass
            return

        m = LOSS_RE.match(line)
        if m and not backfill:
            self.loss_events += 1
            log.warning("JTDX reported partial loss of data (%d this session)",
                        self.loss_events)
            self._on_event({"type": "audio_loss", "ts_utc": m.group("ts")})


def default_log_dir() -> Path:
    return Path(os.getenv("JTDX_LOG_DIR", str(Path.home() / ".local" / "share" / "JTDX")))
