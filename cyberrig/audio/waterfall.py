"""Server-side FTDX10 USB-audio FFT waterfall.

Captures on the machine where CyberRig runs (shack PC with the FTDX10 USB audio),
computes FFT rows, and exposes them to browser clients over WebSocket. Remote VPN/LAN browsers
cannot see the radio USB mic — capture must stay on the shack PC.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

import numpy as np

log = logging.getLogger("cyberrig.waterfall")

try:
    import sounddevice as sd
    HAS_SD = True
except ImportError:
    sd = None  # type: ignore
    HAS_SD = False


FFT_SIZE = 2048
# Preferred analysis rate; we open at the device native rate and resample.
TARGET_RATE = 12000
OUT_BINS = 512


class WaterfallEngine:
    """Background capture + FFT + LAN PCM stream. Thread-safe.

    One USB capture feeds waterfall FFT rows and remote headphone audio
    (int16 LE mono @ TARGET_RATE). Use acquire()/release() so waterfall
    and audio clients can share the device.
    """

    def __init__(self):
        self._stream = None
        self._running = False
        self._lock = threading.Lock()
        self._audio_q: queue.Queue = queue.Queue(maxsize=40)
        self._row_q: queue.Queue = queue.Queue(maxsize=60)
        self._worker: Optional[threading.Thread] = None
        self._window = np.hanning(FFT_SIZE).astype(np.float32)
        self._device_index: Optional[int] = None
        self._device_name: str = ""
        self._sample_rate = TARGET_RATE
        self._db_min = -90.0
        self._db_max = -20.0
        self._listeners: list[Callable[[dict], None]] = []
        self._pcm_listeners: list[Callable[[bytes, int], None]] = []
        self._listener_lock = threading.Lock()
        self._last_row: Optional[list[int]] = None
        self._rows_sent = 0
        self._pcm_chunks = 0
        self._error: Optional[str] = None
        self._resample_buf = np.zeros(0, dtype=np.float32)
        self._users: dict[str, int] = {}  # tag → refcount
        self._user_lock = threading.Lock()
        self._pcm_gain = 1.0

    # ── Devices ───────────────────────────────────────────────────────────

    @staticmethod
    def available() -> bool:
        return HAS_SD

    @staticmethod
    def list_devices() -> list[dict[str, Any]]:
        if not HAS_SD:
            return []
        out = []
        try:
            hostapis = sd.query_hostapis()
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) <= 0:
                    continue
                ha = ""
                try:
                    ha = hostapis[d["hostapi"]]["name"]
                except Exception:
                    pass
                out.append({
                    "index": i,
                    "name": d["name"],
                    "hostapi": ha,
                    "channels": int(d["max_input_channels"]),
                    "default_sr": float(d.get("default_samplerate") or 0),
                })
        except Exception as e:
            log.warning("list_devices: %s", e)
        return out

    @staticmethod
    def _score_device(d: dict) -> int:
        """Higher = better pick for FTDX10 IF audio."""
        name = (d.get("name") or "").upper()
        ha = (d.get("hostapi") or "").upper()
        score = 0
        if "YAESU" in name or "FTDX" in name or "FT-DX" in name:
            score += 100
        if "USB AUDIO" in name or "CODEC" in name:
            score += 40
        # Prefer WASAPI / DirectSound over legacy MME mapper duplicates
        if "WASAPI" in ha:
            score += 30
        elif "DIRECTSOUND" in ha or "WINDOWS DIRECTSOUND" in ha:
            score += 20
        elif "WDM-KS" in ha:
            score += 15
        elif "MME" in ha:
            score += 5
        if "MAPPER" in name or "PRIMARY SOUND" in name:
            score -= 50
        if "REALTEK" in name or "MICROPHONE" in name:
            score -= 20
        return score

    @classmethod
    def ranked_devices(cls) -> list[dict[str, Any]]:
        devs = cls.list_devices()
        return sorted(devs, key=cls._score_device, reverse=True)

    @classmethod
    def auto_device_index(cls) -> Optional[int]:
        ranked = cls.ranked_devices()
        if not ranked:
            return None
        if cls._score_device(ranked[0]) >= 40:
            return ranked[0]["index"]
        return ranked[0]["index"]

    # ── Control ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._user_lock:
            users = dict(self._users)
        return {
            "available": HAS_SD,
            "running": self._running,
            "device_index": self._device_index,
            "device_name": self._device_name,
            "sample_rate": self._sample_rate,
            "stream_rate": TARGET_RATE,  # PCM + FFT analysis rate after resample
            "fft_size": FFT_SIZE,
            "out_bins": OUT_BINS,
            "db_min": self._db_min,
            "db_max": self._db_max,
            "hz_per_bin": (TARGET_RATE / 2) / OUT_BINS,
            "rows_sent": self._rows_sent,
            "pcm_chunks": self._pcm_chunks,
            "pcm_gain": self._pcm_gain,
            "users": users,
            "error": self._error,
        }

    def set_range(self, db_min: float, db_max: float):
        if db_max <= db_min:
            return
        self._db_min = float(db_min)
        self._db_max = float(db_max)

    def set_pcm_gain(self, gain: float):
        self._pcm_gain = float(max(0.05, min(4.0, gain)))

    def on_row(self, cb: Callable[[dict], None]):
        with self._listener_lock:
            self._listeners.append(cb)

    def off_row(self, cb: Callable[[dict], None]):
        with self._listener_lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def on_pcm(self, cb: Callable[[bytes, int], None]):
        """cb(pcm_s16le_bytes, sample_rate)."""
        with self._listener_lock:
            self._pcm_listeners.append(cb)

    def off_pcm(self, cb: Callable[[bytes, int], None]):
        with self._listener_lock:
            if cb in self._pcm_listeners:
                self._pcm_listeners.remove(cb)

    def acquire(self, tag: str, device_index: Optional[int] = None) -> dict:
        """Start capture if needed; bump user refcount for tag (e.g. 'waterfall', 'audio')."""
        with self._user_lock:
            self._users[tag] = self._users.get(tag, 0) + 1
        if self._running:
            return {"ok": True, "already": True, **self.status()}
        return self.start(device_index)

    def release(self, tag: str) -> dict:
        with self._user_lock:
            self._users[tag] = max(0, self._users.get(tag, 0) - 1)
            total = sum(self._users.values())
        if total == 0 and self._running:
            self.stop()
        return {"ok": True, **self.status()}

    def start(self, device_index: Optional[int] = None) -> dict:
        if not HAS_SD:
            return {"ok": False, "error": "sounddevice not installed (pip install sounddevice numpy)"}

        with self._lock:
            if self._running:
                return {"ok": True, "already": True, **self.status()}
            self.stop_unlocked()
            self._error = None
            self._resample_buf = np.zeros(0, dtype=np.float32)

            # Build candidate list
            ranked = self.ranked_devices()
            if device_index is not None:
                candidates = [device_index]
                # Also try other Yaesu indices if the chosen one fails
                for d in ranked:
                    if d["index"] != device_index and self._score_device(d) >= 40:
                        candidates.append(d["index"])
            else:
                candidates = [d["index"] for d in ranked if self._score_device(d) >= 40]
                if not candidates and ranked:
                    candidates = [ranked[0]["index"]]

            last_err = "No input device found"
            for idx in candidates:
                try:
                    info = sd.query_devices(idx)
                    name = info.get("name", f"device {idx}")
                    native_sr = int(info.get("default_samplerate") or 48000)
                    ch = max(1, min(2, int(info.get("max_input_channels") or 1)))

                    # Try native rate first, then common rates
                    rates_try = []
                    for r in (native_sr, 48000, 44100, 12000, 16000):
                        if r and r not in rates_try:
                            rates_try.append(int(r))

                    opened = False
                    for sr in rates_try:
                        block = FFT_SIZE if sr <= 16000 else max(FFT_SIZE, int(round(FFT_SIZE * sr / TARGET_RATE)))
                        # Keep blocksize multiple of 64 for PortAudio happiness
                        block = max(256, (block // 64) * 64)
                        try:
                            self._stream = sd.InputStream(
                                device=idx,
                                samplerate=sr,
                                channels=ch,
                                dtype="float32",
                                blocksize=block,
                                callback=self._audio_cb,
                            )
                            self._stream.start()
                            self._sample_rate = sr
                            opened = True
                            log.info(
                                "waterfall opened idx=%s %r @ %d Hz block=%d ch=%d",
                                idx, name, sr, block, ch,
                            )
                            break
                        except Exception as e:
                            last_err = f"{name} @{sr}Hz: {e}"
                            log.debug("open try failed: %s", last_err)
                            self._stream = None

                    if not opened:
                        continue

                    self._device_index = idx
                    self._device_name = name
                    self._running = True
                    self._worker = threading.Thread(
                        target=self._process_loop, daemon=True, name="waterfall-fft"
                    )
                    self._worker.start()
                    return {"ok": True, **self.status()}
                except Exception as e:
                    last_err = str(e)
                    log.debug("device %s failed: %s", idx, e)
                    self._stream = None

            self._error = last_err
            log.error("waterfall start failed: %s", last_err)
            return {"ok": False, "error": last_err, **self.status()}

    def stop(self):
        with self._lock:
            self.stop_unlocked()

    def stop_unlocked(self):
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        for q in (self._audio_q, self._row_q):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._worker = None
        log.info("waterfall stopped")

    # ── Audio / FFT ───────────────────────────────────────────────────────

    def _audio_cb(self, indata, frames, time_info, status):
        if status:
            log.debug("audio status: %s", status)
        if not self._running:
            return
        # Mono: first channel (FTDX10 IF is usually L)
        mono = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        try:
            self._audio_q.put_nowait(mono)
        except queue.Full:
            try:
                self._audio_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._audio_q.put_nowait(mono)
            except queue.Full:
                pass

    def _to_analysis_rate(self, chunk: np.ndarray) -> np.ndarray:
        """Resample device rate → TARGET_RATE when needed."""
        sr = self._sample_rate
        if abs(sr - TARGET_RATE) < 1:
            return chunk.astype(np.float32, copy=False)
        # Append to ring and emit TARGET_RATE-equivalent samples via linear resample
        self._resample_buf = np.concatenate([self._resample_buf, chunk.astype(np.float32)])
        # How many output samples we can produce
        n_out = int(len(self._resample_buf) * TARGET_RATE / sr)
        if n_out < FFT_SIZE:
            return np.zeros(0, dtype=np.float32)
        # Consume proportional input
        n_in = int(n_out * sr / TARGET_RATE)
        n_in = min(n_in, len(self._resample_buf))
        src = self._resample_buf[:n_in]
        self._resample_buf = self._resample_buf[n_in:]
        x_old = np.linspace(0.0, 1.0, num=len(src), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(x_new, x_old, src).astype(np.float32)

    def _fire_pcm(self, samples_f32: np.ndarray, rate: int):
        """Push int16 LE mono PCM to network audio listeners."""
        if not samples_f32.size:
            return
        with self._listener_lock:
            pcm_ls = list(self._pcm_listeners)
        if not pcm_ls:
            return
        g = self._pcm_gain
        clipped = np.clip(samples_f32 * g, -1.0, 1.0)
        pcm = (clipped * 32767.0).astype("<i2")  # little-endian int16
        raw = pcm.tobytes()
        self._pcm_chunks += 1
        for cb in pcm_ls:
            try:
                cb(raw, rate)
            except Exception:
                pass

    def _process_loop(self):
        pending = np.zeros(0, dtype=np.float32)
        analysis_sr = TARGET_RATE  # after resample
        while self._running:
            try:
                chunk = self._audio_q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                rs = self._to_analysis_rate(chunk)
                if len(rs) == 0:
                    # Device already at ~12k or not enough samples yet
                    if abs(self._sample_rate - TARGET_RATE) < 1:
                        pending = np.concatenate([pending, chunk.astype(np.float32)])
                        analysis_sr = self._sample_rate
                    else:
                        continue
                else:
                    pending = np.concatenate([pending, rs])
                    analysis_sr = TARGET_RATE

                # Stream PCM in ~85 ms chunks for low-latency LAN listen
                pcm_chunk = 1024
                while len(pending) >= pcm_chunk:
                    # Prefer full FFT blocks when available; always stream PCM
                    if len(pending) >= FFT_SIZE:
                        block = pending[:FFT_SIZE]
                        pending = pending[FFT_SIZE:]
                        self._fire_pcm(block, analysis_sr)
                        # FFT row
                        fft_in = block * self._window
                        mag = np.abs(np.fft.rfft(fft_in))
                        db = 20.0 * np.log10(np.maximum(mag, 1e-12))
                        if len(db) != OUT_BINS:
                            x_old = np.linspace(0, 1, len(db))
                            x_new = np.linspace(0, 1, OUT_BINS)
                            db = np.interp(x_new, x_old, db)
                        t = (db - self._db_min) / max(1e-6, (self._db_max - self._db_min))
                        t = np.clip(t, 0.0, 1.0)
                        row = (t * 255.0).astype(np.uint8).tolist()
                        self._last_row = row
                        self._rows_sent += 1
                        msg = {
                            "type": "row",
                            "bins": row,
                            "n": OUT_BINS,
                            "sr": analysis_sr,
                            "ts": time.time(),
                        }
                        try:
                            self._row_q.put_nowait(msg)
                        except queue.Full:
                            try:
                                self._row_q.get_nowait()
                            except queue.Empty:
                                pass
                            try:
                                self._row_q.put_nowait(msg)
                            except queue.Full:
                                pass
                        with self._listener_lock:
                            listeners = list(self._listeners)
                        for cb in listeners:
                            try:
                                cb(msg)
                            except Exception:
                                pass
                    else:
                        block = pending[:pcm_chunk]
                        pending = pending[pcm_chunk:]
                        self._fire_pcm(block, analysis_sr)
            except Exception:
                log.exception("waterfall FFT error")

    def pop_rows(self, max_n: int = 8) -> list[dict]:
        rows = []
        while len(rows) < max_n:
            try:
                rows.append(self._row_q.get_nowait())
            except queue.Empty:
                break
        return rows


# Singleton used by the web app
engine = WaterfallEngine()
