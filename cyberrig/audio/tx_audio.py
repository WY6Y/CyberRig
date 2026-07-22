"""TX path: LAN mic PCM → FTDX10 USB audio out (SSB/DATA modulation).

Uses a lock-protected float32 ring buffer so the PortAudio callback never
touches queue.Queue / numpy allocate (those caused multi-second lag before RF).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import numpy as np

log = logging.getLogger("cyberrig.tx_audio")

try:
    import sounddevice as sd
    HAS_SD = True
except ImportError:
    sd = None  # type: ignore
    HAS_SD = False


class _Ring:
    """SPSC-ish float32 ring (multi-writer ok via lock)."""

    def __init__(self, capacity: int):
        self.buf = np.zeros(capacity, dtype=np.float32)
        self.cap = capacity
        self.r = 0
        self.w = 0
        self.size = 0
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.r = self.w = self.size = 0

    def write(self, x: np.ndarray) -> int:
        if x.size == 0:
            return 0
        x = np.ascontiguousarray(x, dtype=np.float32)
        with self.lock:
            n = min(x.size, self.cap - self.size)
            if n <= 0:
                return 0
            # drop oldest if needed to keep live (prefer latest mic)
            if n < x.size:
                # free space by advancing read
                drop = x.size - n
                self.r = (self.r + drop) % self.cap
                self.size = max(0, self.size - drop)
                n = min(x.size, self.cap - self.size)
            first = min(n, self.cap - self.w)
            self.buf[self.w:self.w + first] = x[:first]
            second = n - first
            if second:
                self.buf[0:second] = x[first:first + second]
            self.w = (self.w + n) % self.cap
            self.size += n
            return n

    def read_into(self, out: np.ndarray) -> int:
        """Fill out with samples; pad with zeros. Returns filled count before pad."""
        n = out.size
        with self.lock:
            take = min(n, self.size)
            if take:
                first = min(take, self.cap - self.r)
                out[:first] = self.buf[self.r:self.r + first]
                second = take - first
                if second:
                    out[first:first + second] = self.buf[0:second]
                self.r = (self.r + take) % self.cap
                self.size -= take
            if take < n:
                out[take:] = 0.0
            return take


class TxAudioEngine:
    """Play network PCM to the radio USB audio output device."""

    def __init__(self):
        self._stream = None
        self._running = False
        self._lock = threading.Lock()
        self._ring = _Ring(48000 * 2)  # 2s max
        self._device_index: Optional[int] = None
        self._device_name: str = ""
        self._sample_rate = 48000
        self._channels = 2
        self._gain = 1.6
        self._error: Optional[str] = None
        self._underruns = 0
        self._frames_played = 0
        self._bytes_in = 0
        self._client_sr = 48000
        self._resample_buf = np.zeros(0, dtype=np.float32)
        self._beep_thread: Optional[threading.Thread] = None

    @staticmethod
    def available() -> bool:
        return HAS_SD

    @staticmethod
    def list_output_devices() -> list[dict[str, Any]]:
        if not HAS_SD:
            return []
        out = []
        try:
            hostapis = sd.query_hostapis()
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_output_channels", 0) <= 0:
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
                    "channels": int(d["max_output_channels"]),
                    "default_sr": float(d.get("default_samplerate") or 0),
                })
        except Exception as e:
            log.warning("list_output_devices: %s", e)
        return out

    @staticmethod
    def _score(d: dict) -> int:
        name = (d.get("name") or "").upper()
        ha = (d.get("hostapi") or "").upper()
        score = 0
        if "YAESU" in name or "FTDX" in name:
            score += 100
        if "USB AUDIO" in name or "CODEC" in name:
            score += 40
        # Prefer WASAPI, then DirectSound, then MME
        if "WASAPI" in ha:
            score += 30
        elif "DIRECTSOUND" in ha:
            score += 20
        elif "WDM-KS" in ha:
            score += 25
        elif "MME" in ha:
            score += 10
        if "MAPPER" in name or "PRIMARY" in name:
            score -= 50
        if "REALTEK" in name:
            score -= 30
        return score

    @classmethod
    def auto_output_index(cls) -> Optional[int]:
        ranked = sorted(cls.list_output_devices(), key=cls._score, reverse=True)
        if not ranked:
            return None
        return ranked[0]["index"]

    def status(self) -> dict:
        return {
            "available": HAS_SD,
            "running": self._running,
            "device_index": self._device_index,
            "device_name": self._device_name,
            "sample_rate": self._sample_rate,
            "channels": self._channels,
            "gain": self._gain,
            "underruns": self._underruns,
            "frames_played": self._frames_played,
            "bytes_in": self._bytes_in,
            "queued_samples": self._ring.size,
            "error": self._error,
        }

    def set_gain(self, g: float):
        self._gain = float(max(0.05, min(3.0, g)))

    def start(self, device_index: Optional[int] = None) -> dict:
        if not HAS_SD:
            return {"ok": False, "error": "sounddevice not installed"}
        with self._lock:
            if self._running:
                return {"ok": True, "already": True, **self.status()}
            self.stop_unlocked()
            self._error = None
            self._ring.clear()
            self._resample_buf = np.zeros(0, dtype=np.float32)

            ranked = sorted(self.list_output_devices(), key=self._score, reverse=True)
            if device_index is not None:
                candidates = [device_index] + [
                    d["index"] for d in ranked
                    if d["index"] != device_index and self._score(d) >= 40
                ]
            else:
                candidates = [d["index"] for d in ranked if self._score(d) >= 40]
                if not candidates and ranked:
                    candidates = [ranked[0]["index"]]

            last_err = "No output device"
            for idx in candidates:
                try:
                    info = sd.query_devices(idx)
                    name = info.get("name", f"out {idx}")
                    native = int(info.get("default_samplerate") or 48000)
                    max_ch = int(info.get("max_output_channels") or 1)
                    use_ch = 2 if max_ch >= 2 else 1
                    ha = ""
                    try:
                        ha = sd.query_hostapis()[info["hostapi"]]["name"]
                    except Exception:
                        pass

                    for sr in (48000, native, 44100):
                        if not sr:
                            continue
                        for block, lat in ((256, 0.02), (512, 0.04), (256, "low"), (512, None)):
                            try:
                                kwargs = dict(
                                    device=idx,
                                    samplerate=int(sr),
                                    channels=use_ch,
                                    dtype="float32",
                                    blocksize=block,
                                    callback=self._out_cb,
                                )
                                if lat is not None:
                                    kwargs["latency"] = lat
                                if "WASAPI" in ha.upper():
                                    try:
                                        kwargs["extra_settings"] = sd.WasapiSettings(exclusive=False)
                                    except Exception:
                                        pass
                                self._stream = sd.OutputStream(**kwargs)
                                self._stream.start()
                                self._sample_rate = int(sr)
                                self._channels = use_ch
                                self._device_index = idx
                                self._device_name = f"{name} [{ha}]"
                                self._running = True
                                self._underruns = 0
                                self._frames_played = 0
                                self._bytes_in = 0
                                # Prime with 40ms of near-silence so stream is hot
                                prime = np.zeros(int(self._sample_rate * 0.04), dtype=np.float32)
                                prime[0] = 1e-6
                                self._ring.write(prime)
                                log.info(
                                    "TX audio out idx=%s %r @ %d Hz ch=%d block=%s lat=%s",
                                    idx, self._device_name, sr, use_ch, block, lat,
                                )
                                return {"ok": True, **self.status()}
                            except Exception as e:
                                last_err = f"{name} @{sr}/{block}: {e}"
                                self._stream = None
                except Exception as e:
                    last_err = str(e)

            self._error = last_err
            log.error("TX audio start failed: %s", last_err)
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
        self._ring.clear()
        self._resample_buf = np.zeros(0, dtype=np.float32)
        log.info("TX audio stopped")

    def push_pcm_s16le(self, data: bytes, client_sr: int = 48000):
        if not self._running or not data:
            return
        self._client_sr = int(client_sr) or 48000
        self._bytes_in += len(data)
        i16 = np.frombuffer(data, dtype="<i2")
        if i16.size == 0:
            return
        f = (i16.astype(np.float32) * (self._gain / 32768.0))
        np.clip(f, -1.0, 1.0, out=f)
        if abs(self._client_sr - self._sample_rate) > 1:
            f = self._resample(f, self._client_sr, self._sample_rate)
        if f.size:
            self._ring.write(f)

    def play_beep(self, ms: int = 500, hz: float = 700.0, amp: float = 0.5):
        """Stream a tone into the ring in near-realtime (doesn't dump all at once)."""
        if not self._running:
            return

        def _run():
            ms_clamped = max(50, min(5000, int(ms)))
            sr = self._sample_rate
            total = int(sr * ms_clamped / 1000)
            chunk = max(256, sr // 50)  # ~20ms
            phase = 0.0
            sent = 0
            while sent < total and self._running:
                n = min(chunk, total - sent)
                t = (np.arange(n, dtype=np.float32) + 0) / float(sr)
                # continuous phase
                wave = (amp * self._gain) * np.sin(2 * np.pi * hz * t + phase)
                phase += 2 * np.pi * hz * n / sr
                # edges
                a = min(int(0.008 * sr), n // 3)
                if a > 0 and sent == 0:
                    wave[:a] *= np.linspace(0, 1, a, dtype=np.float32)
                if a > 0 and sent + n >= total:
                    wave[-a:] *= np.linspace(1, 0, a, dtype=np.float32)
                np.clip(wave, -1.0, 1.0, out=wave)
                self._ring.write(wave.astype(np.float32))
                sent += n
                time.sleep(n / sr * 0.85)

        # Don't stack beep threads forever
        if self._beep_thread and self._beep_thread.is_alive():
            return
        self._beep_thread = threading.Thread(target=_run, daemon=True, name="tx-beep")
        self._beep_thread.start()
        log.info("TX beep %dms @ %.0f Hz started", ms, hz)

    def _resample(self, x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
        if x.size == 0:
            return x
        self._resample_buf = np.concatenate([self._resample_buf, x])
        n_out = int(len(self._resample_buf) * sr_out / sr_in)
        if n_out < 1:
            return np.zeros(0, dtype=np.float32)
        n_in = min(len(self._resample_buf), int(n_out * sr_in / sr_out))
        src = self._resample_buf[:n_in]
        self._resample_buf = self._resample_buf[n_in:]
        if len(src) < 2:
            return np.zeros(0, dtype=np.float32)
        t_old = np.linspace(0.0, 1.0, num=len(src), endpoint=False)
        t_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(t_new, t_old, src).astype(np.float32)

    def _out_cb(self, outdata, frames, time_info, status):
        # Keep this path tiny — no queue, no alloc of big arrays
        if status:
            log.debug("tx out status: %s", status)
        mono = np.empty(frames, dtype=np.float32)
        got = self._ring.read_into(mono)
        if got < frames:
            self._underruns += 1
        if self._channels >= 2:
            outdata[:, 0] = mono
            outdata[:, 1] = mono
        else:
            outdata[:, 0] = mono
        self._frames_played += frames


tx_engine = TxAudioEngine()
