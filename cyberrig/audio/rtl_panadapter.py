"""RTL-SDR panadapter — wideband spectrum feed borrowed from a remote rtl_tcp server.

Unlike the FTDX10's own audio-FFT waterfall (waterfall.py), this pulls raw IQ
over the network from an RTL-SDR dongle attached to any machine running
`rtl_tcp` — commonly a *different* machine than the one running CyberRig,
possibly shared with another decoder (e.g. a WSPR app) that only opens the
dongle per-capture, so this can time-share it rather than take it over.
Any such coordination happens in web/app.py via the optional
`cybersdr_api_url` setting, not here. This module only knows how to speak
the rtl_tcp wire protocol and produce waterfall rows.

EXPERIMENTAL — gain/dB-range calibration is tuned for one specific dongle
and band; expect to need your own tuning. See README "Safety" section.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("cyberrig.rtl_panadapter")

# RTL-TCP command IDs (subset used here; mirrors cybersdr/decoder/capture.py)
CMD_SET_FREQ        = 0x01
CMD_SET_SAMPLE_RATE = 0x02
CMD_SET_GAIN_MODE   = 0x03  # 0=auto, 1=manual
CMD_SET_GAIN        = 0x04  # gain in tenths of dB

# CyberSDR always runs this dongle in MANUAL gain (only flips to auto briefly
# after its own capture, "so a follow-on client doesn't inherit manual gain").
# Auto/AGC mode here produced an unstable, asymmetric-looking spectrum (real
# content on one side of center, flat noise on the other, inconsistent frame to
# frame) — confirmed live (2026-07-22) switching to manual gain fixes it.
# 200 = 20.0 dB, matching CyberSDR's default (cybersdr/decoder/capture.py).
DEFAULT_GAIN_TENTHS = 200

RTL_FFT_SIZE = 8192
OUT_BINS = 2048  # 4x finer than the original 512 — was throwing away most of the
                 # computed FFT detail on interpolation down to the display width
ROW_INTERVAL_SEC = 1.0 / 6.0  # default waterfall scroll rate (~6 rows/sec); adjustable via set_speed()

# RTL2832U/R820T reliable floor for an actual hardware sample rate. Requests
# narrower than this (10/50/150 kHz "zoom" views) are NOT sent to rtl_tcp
# directly — the dongle can't natively sample that slow. Instead we always
# capture at this floor (which, with a fixed FFT size, gives the FINEST raw
# per-bin resolution of any valid rate) and display only the center slice of
# bins that covers the requested span, upsampled to OUT_BINS — a software zoom.
MIN_HW_SAMPLE_RATE = 250_000

CONNECT_ATTEMPT_TIMEOUT = 3.0
CONNECT_RETRIES = 13
CONNECT_RETRY_DELAY = 12.0
# CyberSDR's /api/decoder/stop only prevents its *next* WSPR capture — it does not
# abort one already in flight (~120s max per capture; confirmed in decoder/wspr.py's
# run() that it never chains a second capture once paused=True lands, even if that
# landed mid-capture). Confirmed live (2026-07-22): a brand-new TCP connection to
# rtl_tcp is accepted at the OS level even while CyberSDR's capture still owns the
# tuner, then just hangs instead of failing fast. A tight, rapid retry loop (every
# ~4s) was tried first and did NOT recover even ~168s into a single capture —
# rtl_tcp here is a minimal single-client server, and repeatedly opening/closing
# connections while busy likely confuses its accept/backlog handling rather than
# helping. Widely-spaced, few attempts (~every 15s, ~180s total) is deliberately
# gentler on it while still covering one full capture with margin.


def _send_cmd(sock: socket.socket, cmd: int, value: int) -> None:
    sock.sendall(struct.pack(">BI", cmd, value))


class RtlPanadapterEngine:
    """Background rtl_tcp capture + FFT. Thread-safe. Mirrors WaterfallEngine's shape."""

    def __init__(self):
        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._lock = threading.Lock()
        self._running = False
        self._worker: Optional[threading.Thread] = None
        self._window = np.hanning(RTL_FFT_SIZE).astype(np.float32)
        self._host = ""
        self._port = 0
        self._center_hz = 0
        self._sample_rate = 0        # actual hardware rate sent to rtl_tcp (>= MIN_HW_SAMPLE_RATE)
        self._display_span_hz = 0    # what the caller asked to see — may be narrower (a software zoom)
        # Calibrated against real 40m live data (2026-07-22) with MANUAL gain
        # (see DEFAULT_GAIN_TENTHS) — raw 20*log10(|FFT|) of 8-bit-centered IQ
        # samples runs ~10-48 dB (noise floor mean ~34) at 20.0 dB tuner gain.
        # This is a totally different scale than the audio engine's
        # normalized-float32 FFT (-90..-20) — don't reuse those numbers here.
        self._db_min = 5.0
        self._db_max = 55.0
        self._listeners: list[Callable[[dict], None]] = []
        self._listener_lock = threading.Lock()
        self._rows_sent = 0
        self._error: Optional[str] = None
        self._users: set[str] = set()
        self._user_lock = threading.Lock()
        self._last_emit_ts = 0.0
        self._row_interval_sec = ROW_INTERVAL_SEC  # adjustable at runtime via set_speed()

    # ── Status / control ─────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._user_lock:
            users = sorted(self._users)
        return {
            "available": True,
            "running": self._running,
            "host": self._host,
            "port": self._port,
            "center_hz": self._center_hz,
            # "sample_rate" here means the displayed span (what the client's axis
            # math should use) — may be narrower than the actual hardware rate
            # when zoomed in below MIN_HW_SAMPLE_RATE. hw_sample_rate is the real one.
            "sample_rate": self._display_span_hz,
            "hw_sample_rate": self._sample_rate,
            "fft_size": RTL_FFT_SIZE,
            "out_bins": OUT_BINS,
            "db_min": self._db_min,
            "db_max": self._db_max,
            "hz_per_bin": (self._display_span_hz / OUT_BINS) if self._display_span_hz else 0,
            "rows_per_sec": round(1.0 / self._row_interval_sec, 2),
            "rows_sent": self._rows_sent,
            "users": users,
            "error": self._error,
        }

    def set_range(self, db_min: float, db_max: float):
        if db_max <= db_min:
            return
        self._db_min = float(db_min)
        self._db_max = float(db_max)

    def set_speed(self, rows_per_sec: float):
        rows_per_sec = max(0.5, min(30.0, float(rows_per_sec)))
        self._row_interval_sec = 1.0 / rows_per_sec

    def on_row(self, cb: Callable[[dict], None]):
        with self._listener_lock:
            self._listeners.append(cb)

    def off_row(self, cb: Callable[[dict], None]):
        with self._listener_lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def acquire(self, tag: str, host: Optional[str] = None, port: Optional[int] = None,
                center_hz: Optional[int] = None, sample_rate: Optional[int] = None) -> dict:
        """Idempotent: calling this repeatedly for the same tag (e.g. a page reload
        re-sending "start", or a UI double-click) never over-counts — membership in
        a set, not a refcount. start() itself decides whether that's a cheap no-op
        (already running with matching params) or a real (re)connect."""
        result = self.start(host, port, center_hz, sample_rate)
        if result.get("ok"):
            with self._user_lock:
                self._users.add(tag)
        return result

    def release(self, tag: str) -> dict:
        with self._user_lock:
            self._users.discard(tag)
            empty = not self._users
        if empty and self._running:
            self.stop()
        return {"ok": True, **self.status()}

    # ── Connection ────────────────────────────────────────────────────────────

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("rtl_tcp closed connection")
            buf += chunk
        return buf

    def start(self, host: Optional[str] = None, port: Optional[int] = None,
              center_hz: Optional[int] = None, sample_rate: Optional[int] = None) -> dict:
        """`sample_rate` here is the requested DISPLAY SPAN, not necessarily the literal
        hardware rate — requests narrower than MIN_HW_SAMPLE_RATE are satisfied by
        capturing at the floor rate and showing only a center slice of bins (see
        _process_loop). Use `self._display_span_hz` (not `self._sample_rate`) as the
        caller-visible identity for the "already running with these params" check.
        """
        with self._lock:
            host = host or self._host
            port = int(port or self._port)
            center_hz = int(center_hz or self._center_hz)
            display_span_hz = int(sample_rate or self._display_span_hz)
            hw_rate = max(display_span_hz, MIN_HW_SAMPLE_RATE)

            if self._running and host == self._host and port == self._port and hw_rate == self._sample_rate:
                # Same connection already live (e.g. a page reload re-sending "start",
                # or a repeated click) — just follow the requested center if it moved,
                # don't tear down and reconnect (that would re-pause CyberSDR and
                # re-run the up-to-~3min busy-dongle retry for no reason).
                self._display_span_hz = display_span_hz
                if center_hz != self._center_hz:
                    self.set_center(center_hz)
                return {"ok": True, "already": True, **self.status()}

            # An actual span/host/port change (or a fresh start) — real reconnect.
            sample_rate = hw_rate
            self.stop_unlocked()
            self._error = None

            # A brand-new TCP connection to rtl_tcp can be accepted at the OS level
            # even while CyberSDR's in-flight capture still owns the tuner — the
            # handshake (or the connect itself) then just hangs until that capture
            # finishes. So each retry attempt covers connect *and* handshake
            # together, not just the connect() call.
            sock = None
            last_err = "connect failed"
            for attempt in range(CONNECT_RETRIES):
                s = None
                try:
                    s = socket.create_connection((host, port), timeout=CONNECT_ATTEMPT_TIMEOUT)
                    s.settimeout(CONNECT_ATTEMPT_TIMEOUT)
                    hdr = self._recv_exact(s, 12)
                    if hdr[:4] != b"RTL0":
                        raise ValueError(f"bad magic from rtl_tcp: {hdr[:4]!r}")
                    tuner_type = struct.unpack(">I", hdr[4:8])[0]
                    log.info("rtl_tcp connected %s:%d — tuner type %d", host, port, tuner_type)
                    sock = s
                    break
                except Exception as e:
                    last_err = str(e)
                    if s is not None:
                        try:
                            s.close()
                        except Exception:
                            pass
                    if attempt < CONNECT_RETRIES - 1:
                        time.sleep(CONNECT_RETRY_DELAY)

            if sock is None:
                self._error = f"rtl_tcp {host}:{port} unreachable/busy (dongle likely mid-capture on CyberSDR): {last_err}"
                log.error(self._error)
                return {"ok": False, "error": self._error, **self.status()}

            try:
                _send_cmd(sock, CMD_SET_SAMPLE_RATE, sample_rate)
                _send_cmd(sock, CMD_SET_FREQ, center_hz)
                _send_cmd(sock, CMD_SET_GAIN_MODE, 1)  # manual — see DEFAULT_GAIN_TENTHS note above
                _send_cmd(sock, CMD_SET_GAIN, DEFAULT_GAIN_TENTHS)
                sock.settimeout(1.0)
            except Exception as e:
                try:
                    sock.close()
                except Exception:
                    pass
                self._error = f"rtl_tcp tune commands failed: {e}"
                log.error(self._error)
                return {"ok": False, "error": self._error, **self.status()}

            self._sock = sock
            self._host = host
            self._port = port
            self._center_hz = center_hz
            self._sample_rate = sample_rate
            self._display_span_hz = display_span_hz
            self._running = True
            self._worker = threading.Thread(
                target=self._process_loop, daemon=True, name="rtl-panadapter-fft"
            )
            self._worker.start()
            return {"ok": True, **self.status()}

    def stop(self):
        with self._lock:
            self.stop_unlocked()

    def stop_unlocked(self):
        self._running = False
        with self._sock_lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
        self._worker = None
        log.info("rtl panadapter stopped")

    def set_center(self, hz: int):
        """Hard-follow retune — cheap live command on the already-open socket."""
        self._center_hz = int(hz)
        if not self._running:
            return
        with self._sock_lock:
            if self._sock is None:
                return
            try:
                _send_cmd(self._sock, CMD_SET_FREQ, int(hz))
            except Exception as e:
                log.debug("set_center failed: %s", e)

    # ── FFT loop ──────────────────────────────────────────────────────────────

    def _process_loop(self):
        nbytes = RTL_FFT_SIZE * 2  # 8-bit I + 8-bit Q per sample
        while self._running:
            sock = self._sock
            if sock is None:
                break
            try:
                raw = self._recv_exact(sock, nbytes)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    self._error = str(e)
                    log.warning("rtl panadapter socket error: %s", e)
                self._running = False
                break

            now = time.time()
            if now - self._last_emit_ts < self._row_interval_sec:
                continue
            self._last_emit_ts = now

            try:
                iq = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
                i = iq[0::2]
                q = iq[1::2]
                cplx = (i + 1j * q) * self._window
                spec = np.fft.fftshift(np.fft.fft(cplx))
                mag = np.abs(spec)
                db = 20.0 * np.log10(np.maximum(mag, 1e-12))
                # Software zoom: if the requested display span is narrower than the
                # actual hardware capture rate, only show the center slice of raw
                # bins covering that span (then upsample it to OUT_BINS below) —
                # this is how "10/50/150 kHz" views work despite the RTL-SDR
                # capturing at MIN_HW_SAMPLE_RATE the whole time.
                if self._display_span_hz and self._sample_rate and self._display_span_hz < self._sample_rate:
                    frac = self._display_span_hz / self._sample_rate
                    keep = max(8, int(round(len(db) * frac)))
                    lo = (len(db) - keep) // 2
                    db = db[lo: lo + keep]
                if len(db) != OUT_BINS:
                    x_old = np.linspace(0, 1, len(db))
                    x_new = np.linspace(0, 1, OUT_BINS)
                    db = np.interp(x_new, x_old, db)
                t = np.clip((db - self._db_min) / max(1e-6, self._db_max - self._db_min), 0.0, 1.0)
                row = (t * 255.0).astype(np.uint8).tolist()
                self._rows_sent += 1
                msg = {
                    "type": "row",
                    "source": "rtl",
                    "bins": row,
                    "n": OUT_BINS,
                    "sr": self._display_span_hz,  # what the client's axis math should use
                    "center_hz": self._center_hz,
                    "ts": now,
                }
                with self._listener_lock:
                    listeners = list(self._listeners)
                for cb in listeners:
                    try:
                        cb(msg)
                    except Exception:
                        pass
            except Exception:
                log.exception("rtl panadapter FFT error")


# Singleton used by the web app
engine = RtlPanadapterEngine()
