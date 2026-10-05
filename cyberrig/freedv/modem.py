"""ctypes wrappers for libcodec2 FreeDV modes and (when present) librade RADE V1.

RADE is not in Debian's libcodec2 1.2. The native 2026 flagship lives in
freedv/rade_c (librade + the Opus FARGAN/LPCNet vocoder built beside it).
700D/1600/700E ride libcodec2 so we can talk to SM1000/EzDV without RADE.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("cyberrig.freedv")

FREEDV_MODE_1600 = 0
FREEDV_MODE_700D = 7
FREEDV_MODE_700E = 13
FREEDV_MODE_2020 = 8

CODEC2_MODES = {
    "700D": FREEDV_MODE_700D,
    "700E": FREEDV_MODE_700E,
    "1600": FREEDV_MODE_1600,
    "2020": FREEDV_MODE_2020,
}

RADE_USE_C_ENCODER = 0x1
RADE_USE_C_DECODER = 0x2
RADE_VERBOSE_0 = 0x8
RADE_INT16_SCALE = 16384.0
LPCNET_FRAME_SIZE = 160  # 10 ms @ 16 kHz — matches rade_c / lpcnet.h

# Search path for a CyberRig-owned rade_c prefix (see freedv/native/build.sh).
_RADE_PREFIXES = (
    Path.home() / "opt" / "freedv-rade",
)


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if x.size == 0 or sr_in == sr_out:
        return np.ascontiguousarray(x, dtype=np.float32)
    n_out = int(round(len(x) * sr_out / sr_in))
    if n_out < 1:
        return np.zeros(0, dtype=np.float32)
    t_old = np.linspace(0.0, 1.0, num=len(x), endpoint=False)
    t_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(t_new, t_old, x.astype(np.float32)).astype(np.float32)


class StreamResampler:
    """Keeps a fractional read head so 256-sample USB blocks don't click the OFDM."""

    def __init__(self, sr_in: int, sr_out: int):
        self.sr_in = int(sr_in) or 1
        self.sr_out = int(sr_out) or 1
        self._buf = np.zeros(0, dtype=np.float32)
        self._phi = 0.0

    def reset(self):
        self._buf = np.zeros(0, dtype=np.float32)
        self._phi = 0.0

    def feed(self, x: np.ndarray) -> np.ndarray:
        x = np.ascontiguousarray(x, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return x
        if self.sr_in == self.sr_out:
            return x
        self._buf = np.concatenate([self._buf, x]) if self._buf.size else x.copy()
        ratio = self.sr_in / float(self.sr_out)
        avail = (len(self._buf) - 1) - self._phi
        if avail < ratio:
            return np.zeros(0, dtype=np.float32)
        n_out = int(avail / ratio)
        if n_out < 1:
            return np.zeros(0, dtype=np.float32)
        idx = self._phi + np.arange(n_out, dtype=np.float64) * ratio
        i0 = np.floor(idx).astype(np.int32)
        frac = (idx - i0).astype(np.float32)
        y = self._buf[i0] * (1.0 - frac) + self._buf[i0 + 1] * frac
        nxt = float(idx[-1] + ratio)
        drop = int(nxt)
        if drop > 0:
            self._buf = self._buf[drop:]
            self._phi = nxt - drop
        else:
            self._phi = nxt
        return y.astype(np.float32)


def _load_codec2():
    names = [
        ctypes.util.find_library("codec2"),
        "libcodec2.so.1.2",
        "libcodec2.so.1.0",
        "libcodec2.so.1",
        "libcodec2.so",
    ]
    last = None
    for name in names:
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            lib.freedv_open.argtypes = [ctypes.c_int]
            lib.freedv_open.restype = ctypes.c_void_p
            lib.freedv_close.argtypes = [ctypes.c_void_p]
            lib.freedv_close.restype = None
            lib.freedv_tx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            lib.freedv_tx.restype = None
            lib.freedv_rx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            lib.freedv_rx.restype = ctypes.c_int
            lib.freedv_nin.argtypes = [ctypes.c_void_p]
            lib.freedv_nin.restype = ctypes.c_int
            lib.freedv_get_n_speech_samples.argtypes = [ctypes.c_void_p]
            lib.freedv_get_n_speech_samples.restype = ctypes.c_int
            lib.freedv_get_n_max_speech_samples.argtypes = [ctypes.c_void_p]
            lib.freedv_get_n_max_speech_samples.restype = ctypes.c_int
            lib.freedv_get_n_nom_modem_samples.argtypes = [ctypes.c_void_p]
            lib.freedv_get_n_nom_modem_samples.restype = ctypes.c_int
            lib.freedv_get_n_max_modem_samples.argtypes = [ctypes.c_void_p]
            lib.freedv_get_n_max_modem_samples.restype = ctypes.c_int
            lib.freedv_get_modem_sample_rate.argtypes = [ctypes.c_void_p]
            lib.freedv_get_modem_sample_rate.restype = ctypes.c_int
            if hasattr(lib, "freedv_get_speech_sample_rate"):
                lib.freedv_get_speech_sample_rate.argtypes = [ctypes.c_void_p]
                lib.freedv_get_speech_sample_rate.restype = ctypes.c_int
            if hasattr(lib, "freedv_get_sync"):
                lib.freedv_get_sync.argtypes = [ctypes.c_void_p]
                lib.freedv_get_sync.restype = ctypes.c_int
            lib.freedv_get_modem_stats.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_float),
            ]
            lib.freedv_get_modem_stats.restype = None
            return lib
        except OSError as e:
            last = e
    log.info("libcodec2 not loaded: %s", last)
    return None


def _load_rade_speech():
    """CyberRig's speech wrapper (LPCNet + FARGAN + librade)."""
    candidates = [
        Path.home() / "opt" / "freedv-rade" / "lib" / "librade_speech.so",
    ]
    env = os.environ.get("CYBERRIG_LIBRADE_SPEECH")
    if env:
        candidates.insert(0, Path(env))
    last = None
    for path in candidates:
        if not path.exists():
            continue
        try:
            lib = ctypes.CDLL(str(path))
            lib.rs_open.restype = ctypes.c_void_p
            lib.rs_open.argtypes = []
            lib.rs_close.argtypes = [ctypes.c_void_p]
            lib.rs_close.restype = None
            lib.rs_n_tx.argtypes = [ctypes.c_void_p]
            lib.rs_n_tx.restype = ctypes.c_int
            lib.rs_n_eoo.argtypes = [ctypes.c_void_p]
            lib.rs_n_eoo.restype = ctypes.c_int
            lib.rs_sync.argtypes = [ctypes.c_void_p]
            lib.rs_sync.restype = ctypes.c_int
            lib.rs_snr.argtypes = [ctypes.c_void_p]
            lib.rs_snr.restype = ctypes.c_float
            lib.rs_nin.argtypes = [ctypes.c_void_p]
            lib.rs_nin.restype = ctypes.c_int
            lib.rs_nin_max.argtypes = [ctypes.c_void_p]
            lib.rs_nin_max.restype = ctypes.c_int
            lib.rs_tx_frame.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int16),
                ctypes.POINTER(ctypes.c_float),
            ]
            lib.rs_tx_frame.restype = ctypes.c_int
            lib.rs_tx_eoo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)]
            lib.rs_tx_eoo.restype = ctypes.c_int
            lib.rs_rx_block.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_float),
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_float),
                ctypes.c_int,
            ]
            lib.rs_rx_block.restype = ctypes.c_int
            lib._path = str(path)
            return lib
        except OSError as e:
            last = e
    if last:
        log.info("librade_speech not available: %s", last)
    return None


def _load_rade():
    """Load librade + the Opus-with-LPCNet it was linked against."""
    candidates = []
    for prefix in _RADE_PREFIXES:
        candidates.append(prefix / "lib" / "librade.so")
        candidates.append(prefix / "lib" / "librade.so.0.1")
        # in-tree build
        candidates.append(prefix / "src" / "librade.so")
    home_build = Path.home() / "src" / "rade_c" / "build" / "src" / "librade.so"
    candidates.append(home_build)
    env = os.environ.get("CYBERRIG_LIBRADE")
    if env:
        candidates.insert(0, Path(env))

    last = None
    for path in candidates:
        if not path.exists():
            continue
        try:
            # Preload the in-tree libopus so LPCNet symbols resolve.
            opus_dir = path.parent.parent / "opus"
            for extra in (
                path.parent / "libopus.so",
                opus_dir / "libopus.so",
            ):
                if extra.exists():
                    try:
                        ctypes.CDLL(str(extra), mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass
            lib = ctypes.CDLL(str(path))
            lib.rade_initialize.argtypes = []
            lib.rade_initialize.restype = None
            lib.rade_finalize.argtypes = []
            lib.rade_finalize.restype = None
            lib.rade_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
            lib.rade_open.restype = ctypes.c_void_p
            lib.rade_close.argtypes = [ctypes.c_void_p]
            lib.rade_close.restype = None
            lib.rade_n_tx_out.argtypes = [ctypes.c_void_p]
            lib.rade_n_tx_out.restype = ctypes.c_int
            lib.rade_n_tx_eoo_out.argtypes = [ctypes.c_void_p]
            lib.rade_n_tx_eoo_out.restype = ctypes.c_int
            lib.rade_nin.argtypes = [ctypes.c_void_p]
            lib.rade_nin.restype = ctypes.c_int
            lib.rade_nin_max.argtypes = [ctypes.c_void_p]
            lib.rade_nin_max.restype = ctypes.c_int
            lib.rade_n_features_in_out.argtypes = [ctypes.c_void_p]
            lib.rade_n_features_in_out.restype = ctypes.c_int
            lib.rade_sync.argtypes = [ctypes.c_void_p]
            lib.rade_sync.restype = ctypes.c_int
            lib.rade_snrdB_3k_est.argtypes = [ctypes.c_void_p]
            lib.rade_snrdB_3k_est.restype = ctypes.c_float
            lib.rade_freq_offset.argtypes = [ctypes.c_void_p]
            lib.rade_freq_offset.restype = ctypes.c_float
            class RADE_COMP(ctypes.Structure):
                _fields_ = [("real", ctypes.c_float), ("imag", ctypes.c_float)]
            lib.RADE_COMP = RADE_COMP
            lib.rade_tx.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(RADE_COMP),
                ctypes.POINTER(ctypes.c_float),
            ]
            lib.rade_tx.restype = ctypes.c_int
            lib.rade_tx_eoo.argtypes = [ctypes.c_void_p, ctypes.POINTER(RADE_COMP)]
            lib.rade_tx_eoo.restype = ctypes.c_int
            lib.rade_rx.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(RADE_COMP),
            ]
            lib.rade_rx.restype = ctypes.c_int
            lib._path = str(path)
            return lib
        except OSError as e:
            last = e
            log.info("librade load failed %s: %s", path, e)
    if last:
        log.info("librade not available: %s", last)
    return None


_MODES_CACHE: Optional[dict] = None


def available_modes() -> dict:
    """Which codecs this process can actually open."""
    global _MODES_CACHE
    if _MODES_CACHE is not None:
        return dict(_MODES_CACHE)
    out = {"RADE": False, "700D": False, "700E": False, "1600": False, "2020": False}
    c2 = _load_codec2()
    if c2 is not None:
        for name, code in CODEC2_MODES.items():
            try:
                h = c2.freedv_open(code)
            except Exception:
                h = None
            if h:
                out[name] = True
                try:
                    c2.freedv_close(h)
                except Exception:
                    pass
    try:
        if _load_rade_speech() is not None:
            out["RADE"] = True
    except Exception:
        pass
    _MODES_CACHE = dict(out)
    return out


class Codec2Modem:
    """One FreeDV API instance (700D/1600/…). Speech and modem are int16 @ API rates."""

    def __init__(self, mode: str = "700D"):
        self.name = mode.upper()
        code = CODEC2_MODES.get(self.name)
        if code is None:
            raise ValueError(f"unknown codec2 mode {mode}")
        self._lib = _load_codec2()
        if self._lib is None:
            raise RuntimeError("libcodec2 not found")
        self._h = self._lib.freedv_open(code)
        if not self._h:
            raise RuntimeError(f"freedv_open({self.name}) failed")
        get_sr = getattr(self._lib, "freedv_get_speech_sample_rate", None)
        self.speech_rate = int(get_sr(self._h) if get_sr else 8000) or 8000
        self.modem_rate = int(self._lib.freedv_get_modem_sample_rate(self._h) or 8000)
        self.n_speech = int(self._lib.freedv_get_n_speech_samples(self._h))
        self.n_modem = int(self._lib.freedv_get_n_nom_modem_samples(self._h))
        self.n_max_speech = int(self._lib.freedv_get_n_max_speech_samples(self._h))
        self.n_max_modem = int(self._lib.freedv_get_n_max_modem_samples(self._h))
        self._rx_buf = np.zeros(0, dtype=np.int16)
        self._tx_speech = np.zeros(0, dtype=np.int16)
        self.sync = 0
        self.snr = 0.0

    def close(self):
        if self._h and self._lib:
            self._lib.freedv_close(self._h)
        self._h = None

    def tx(self, speech_f32: np.ndarray) -> np.ndarray:
        """speech float32 @ speech_rate → modem float32 @ modem_rate."""
        if speech_f32.size:
            i16 = np.clip(speech_f32 * 32767.0, -32767, 32767).astype(np.int16)
            self._tx_speech = np.concatenate([self._tx_speech, i16])
        out = []
        ns = self.n_speech
        nm = self.n_modem
        while len(self._tx_speech) >= ns:
            frame = self._tx_speech[:ns]
            self._tx_speech = self._tx_speech[ns:]
            mod = np.zeros(nm, dtype=np.int16)
            self._lib.freedv_tx(
                self._h,
                mod.ctypes.data_as(ctypes.c_void_p),
                frame.ctypes.data_as(ctypes.c_void_p),
            )
            out.append(mod)
        if not out:
            return np.zeros(0, dtype=np.float32)
        cat = np.concatenate(out)
        return cat.astype(np.float32) / 32768.0

    def rx(self, modem_f32: np.ndarray) -> np.ndarray:
        """modem float32 @ modem_rate → speech float32 @ speech_rate (may be empty)."""
        if modem_f32.size:
            i16 = np.clip(modem_f32 * 32767.0, -32767, 32767).astype(np.int16)
            self._rx_buf = np.concatenate([self._rx_buf, i16])
        speech_chunks = []
        while True:
            nin = int(self._lib.freedv_nin(self._h))
            if nin <= 0 or len(self._rx_buf) < nin:
                break
            demod = self._rx_buf[:nin]
            self._rx_buf = self._rx_buf[nin:]
            speech = np.zeros(self.n_max_speech, dtype=np.int16)
            nout = int(self._lib.freedv_rx(
                self._h,
                speech.ctypes.data_as(ctypes.c_void_p),
                demod.ctypes.data_as(ctypes.c_void_p),
            ))
            if nout > 0:
                speech_chunks.append(speech[:nout].copy())
        sync = ctypes.c_int(0)
        snr = ctypes.c_float(0.0)
        self._lib.freedv_get_modem_stats(self._h, ctypes.byref(sync), ctypes.byref(snr))
        self.sync = int(sync.value)
        self.snr = float(snr.value)
        if not speech_chunks:
            return np.zeros(0, dtype=np.float32)
        cat = np.concatenate(speech_chunks)
        return cat.astype(np.float32) / 32768.0

    def end_over(self) -> np.ndarray:
        return np.zeros(0, dtype=np.float32)


class RadeModem:
    """RADE V1 via librade_speech.so (LPCNet + FARGAN + rade_c)."""

    def __init__(self):
        self.name = "RADE"
        self._lib = _load_rade_speech()
        if self._lib is None:
            raise RuntimeError("librade_speech.so not found — run cyberrig/freedv/native/build.sh")
        self._h = self._lib.rs_open()
        if not self._h:
            raise RuntimeError("rs_open failed")
        self.speech_rate = 16000
        self.modem_rate = 8000
        self.n_tx = int(self._lib.rs_n_tx(self._h) or 960)
        self.n_eoo = int(self._lib.rs_n_eoo(self._h) or 1152)
        self.sync = 0
        self.snr = 0.0
        self._rx_buf = np.zeros(0, dtype=np.float32)
        self._tx_pcm = np.zeros(0, dtype=np.int16)

    def close(self):
        if self._h and self._lib:
            self._lib.rs_close(self._h)
        self._h = None

    def tx(self, speech_f32: np.ndarray) -> np.ndarray:
        if speech_f32.size:
            i16 = np.clip(speech_f32 * 32767.0, -32767, 32767).astype(np.int16)
            self._tx_pcm = np.concatenate([self._tx_pcm, i16])
        out = []
        while len(self._tx_pcm) >= LPCNET_FRAME_SIZE:
            frame = np.ascontiguousarray(self._tx_pcm[:LPCNET_FRAME_SIZE])
            self._tx_pcm = self._tx_pcm[LPCNET_FRAME_SIZE:]
            buf = (ctypes.c_float * self.n_tx)()
            n = int(self._lib.rs_tx_frame(
                self._h,
                frame.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
                buf,
            ))
            if n > 0:
                out.append(np.ctypeslib.as_array(buf)[:n].copy())
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out).astype(np.float32)

    def rx(self, modem_f32: np.ndarray) -> np.ndarray:
        if modem_f32.size:
            self._rx_buf = np.concatenate([self._rx_buf, modem_f32.astype(np.float32)])
        speech = []
        while True:
            nin = int(self._lib.rs_nin(self._h))
            if nin <= 0 or len(self._rx_buf) < nin:
                break
            chunk = np.ascontiguousarray(self._rx_buf[:nin], dtype=np.float32)
            self._rx_buf = self._rx_buf[nin:]
            max_out = 12 * LPCNET_FRAME_SIZE
            out = (ctypes.c_float * max_out)()
            n = int(self._lib.rs_rx_block(
                self._h,
                chunk.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                nin,
                out,
                max_out,
            ))
            self.sync = int(self._lib.rs_sync(self._h))
            self.snr = float(self._lib.rs_snr(self._h))
            if n > 0:
                speech.append(np.ctypeslib.as_array(out)[:n].copy())
        if not speech:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(speech).astype(np.float32)

    def end_over(self) -> np.ndarray:
        buf = (ctypes.c_float * max(self.n_eoo, 1))()
        n = int(self._lib.rs_tx_eoo(self._h, buf))
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        return np.ctypeslib.as_array(buf)[:n].copy().astype(np.float32)


def open_modem(mode: str):
    name = (mode or "RADE").upper()
    if name == "RADE":
        try:
            return RadeModem()
        except Exception as e:
            log.warning("RADE unavailable (%s); falling back to 700D", e)
            name = "700D"
    return Codec2Modem(name)
