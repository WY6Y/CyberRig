"""PortAudio access to the FTDX10 USB codec — duplex TX, capture-only RX.

LISTEN used to open `sd.InputStream` at the device default (44100 Hz) and
REMOTE TX opened `sd.OutputStream` on the same ALSA `hw:*,0` at 48000 Hz.
USB audio-class devices have one clock and typically one open; the second
stream on the *same* `hw:` node is how you hang or restart the whole panel.
Seen live 2026-08-25 when arming REMOTE TX while LISTEN was already running.

That is still the rule for the raw codec. the station PC also has `ftdx10_rx`,
an ALSA dsnoop PCM that fans the capture side out to many readers (JTDX +
CyberRig). It is input-only by design — SoundOutName stays on `plughw:` —
so a duplex-only picker never even considers it (`max_output_channels==0`).

Hold RX: if the best capture device has no playback (that PCM), open an
`InputStream` on it. Hold TX: try a duplex `Stream` on the raw codec first;
if none exists, fall back to an output-only `OutputStream` on the best
playback-capable device, mirroring the RX capture-only fallback above.

That fallback is not a hypothetical: it was proven live 2026-09-11 that once
`ftdx10_rx` is in regular use (JTDX + waterfall + an FT8 capture, all via
dsnoop), the raw codec's own capture side stops enumerating input channels
at all -- `_list_side("tx")` then finds zero duplex-capable devices even
though the raw codec's playback side is untouched and available. TX only
ever needs playback; requiring input alongside it was true when dsnoop
didn't exist, not since.

Hold both: two streams on two devices, which is safe because dsnoop is
the sharing mechanism. Without `ftdx10_rx` the old one-duplex-open path
is unchanged, including LISTEN+REMOTE TX sharing it.

Device picking uses ``cyberrig.audio.devices`` (deny list, CODEC scoring,
`ftdx10_rx` weighted for RX). Do not add a "first device" fallback.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from cyberrig.audio import devices

log = logging.getLogger("cyberrig.codec")

try:
    import sounddevice as sd
    HAS_SD = True
except ImportError:
    sd = None  # type: ignore
    HAS_SD = False

PREFERRED_RATE = 48000
BLOCK = 256

InputHandler = Callable[[Any], None]          # mono float32 ndarray
OutputHandler = Callable[[Any, int], None]    # outdata, frames


class RigCodec:
    """Owns the single USB open. WaterfallEngine and TxAudioEngine hold sides."""

    def __init__(self, sd_module=None):
        self._sd = sd_module
        self._lock = threading.Lock()
        self._stream = None          # duplex Stream (TX, and RX without dsnoop)
        self._input_stream = None    # capture-only InputStream on ftdx10_rx
        self._output_stream = None   # playback-only OutputStream, TX fallback
        self._output_device_index: Optional[int] = None
        self._running = False
        self._rx_holders: set[str] = set()
        self._tx_holders: set[str] = set()
        self._on_input: Optional[InputHandler] = None
        self._extra_inputs: dict[str, InputHandler] = {}
        self._on_output: Optional[OutputHandler] = None
        self._rate = PREFERRED_RATE
        self._in_ch = 1
        self._out_ch = 2
        self._device_index: Optional[int] = None
        self._device_name: str = ""
        self._error: Optional[str] = None
        self._opens = 0  # test counter: how many times a stream was started

    def _lib(self):
        if self._sd is not None:
            return self._sd
        if not HAS_SD:
            return None
        return sd

    @property
    def sample_rate(self) -> int:
        return self._rate

    @property
    def device_index(self) -> Optional[int]:
        return self._device_index

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def running(self) -> bool:
        return self._running

    def set_input_handler(self, cb: Optional[InputHandler]):
        self._on_input = cb

    def add_input_handler(self, tag: str, cb: InputHandler):
        if tag and cb is not None:
            self._extra_inputs[str(tag)] = cb

    def remove_input_handler(self, tag: str):
        self._extra_inputs.pop(str(tag), None)

    def set_output_handler(self, cb: Optional[OutputHandler]):
        self._on_output = cb

    def status(self) -> dict:
        with self._lock:
            return self._status_unlocked()

    def hold(self, side: str, tag: str, device_index: Optional[int] = None) -> dict:
        """Add a holder on 'rx' or 'tx'. Opens capture and/or duplex as needed."""
        if side not in ("rx", "tx"):
            return {"ok": False, "error": f"side must be rx or tx, got {side!r}"}
        if not tag:
            return {"ok": False, "error": "hold tag required"}
        with self._lock:
            holders = self._rx_holders if side == "rx" else self._tx_holders
            holders.add(str(tag))
            if side == "rx":
                result = self._ensure_rx_unlocked(device_index)
            else:
                result = self._ensure_tx_unlocked(device_index)
            if not result.get("ok"):
                holders.discard(str(tag))
            return result

    def release(self, side: str, tag: str) -> dict:
        if side not in ("rx", "tx"):
            return {"ok": False, "error": f"side must be rx or tx, got {side!r}"}
        with self._lock:
            holders = self._rx_holders if side == "rx" else self._tx_holders
            holders.discard(str(tag))
            if not self._rx_holders:
                self._close_input_unlocked()
            if not self._tx_holders:
                # Duplex stays up only while an RX holder is piggybacking on it.
                if not self._rx_holders or self._input_stream is not None:
                    self._close_duplex_unlocked()
                # Output-only is TX-exclusive by construction -- never shared
                # with RX -- so it always closes once TX has no more holders.
                self._close_output_unlocked()
            self._sync_running()
            if self._running:
                log.info(
                    "rig codec %s release %r (rx=%s tx=%s running=%s)",
                    side, tag, sorted(self._rx_holders), sorted(self._tx_holders),
                    self._running,
                )
            return {"ok": True, **self._status_unlocked()}

    def close(self):
        with self._lock:
            self._rx_holders.clear()
            self._tx_holders.clear()
            self._close_unlocked()

    def _status_unlocked(self) -> dict:
        return {
            "available": bool(self._lib()),
            "running": self._running,
            "device_index": self._device_index,
            "device_name": self._device_name,
            "sample_rate": self._rate,
            "in_ch": self._in_ch,
            "out_ch": self._out_ch,
            "rx_holders": sorted(self._rx_holders),
            "tx_holders": sorted(self._tx_holders),
            "opens": self._opens,
            "capture_only": self._input_stream is not None,
            "tx_output_only": self._output_stream is not None,
            "error": self._error,
        }

    def _sync_running(self):
        self._running = (self._stream is not None or self._input_stream is not None
                          or self._output_stream is not None)

    # ── device pick ──────────────────────────────────────────────────────

    def _list_side(self, side: str) -> list[dict[str, Any]]:
        """RX: any input device. TX: duplex only (playback-only dsnoop is useless)."""
        lib = self._lib()
        if lib is None:
            return []
        need_output = side != "rx"
        out = []
        try:
            hostapis = lib.query_hostapis()
            for i, d in enumerate(lib.query_devices()):
                inn = int(d.get("max_input_channels") or 0)
                outn = int(d.get("max_output_channels") or 0)
                if inn < 1:
                    continue
                if need_output and outn < 1:
                    continue
                ha = ""
                try:
                    ha = hostapis[d["hostapi"]]["name"]
                except Exception:
                    pass
                name = d.get("name") or f"device {i}"
                if devices.is_denied(name):
                    continue
                out.append({
                    "index": i,
                    "name": name,
                    "hostapi": ha,
                    "in_ch": inn,
                    "out_ch": outn,
                    "default_sr": float(d.get("default_samplerate") or 0),
                    "score": devices.score(name, ha, side=side),
                })
        except Exception as e:
            log.warning("list_side %s: %s", side, e)
        out.sort(key=lambda d: d["score"], reverse=True)
        return out

    def _candidates(self, device_index: Optional[int], ranked: list[dict[str, Any]]) -> list[int]:
        auto = [d["index"] for d in ranked if d["score"] >= devices.MIN_AUTO_SCORE]
        if device_index is None:
            return auto
        lib = self._lib()
        try:
            info = lib.query_devices(device_index) if lib is not None else None
        except Exception:
            info = None
        if info is not None and devices.is_denied(info.get("name") or ""):
            return []  # caller turns this into a deny error
        ordered = [device_index]
        for idx in auto:
            if idx != device_index:
                ordered.append(idx)
        return ordered

    def _denied_explicit(self, device_index: Optional[int]) -> Optional[str]:
        if device_index is None:
            return None
        lib = self._lib()
        if lib is None:
            return None
        try:
            info = lib.query_devices(device_index)
        except Exception:
            return None
        name = info.get("name") or ""
        if devices.is_denied(name):
            return (f"Device {device_index} ({name}) is on the audio "
                    f"deny list (settings audio_device_deny)")
        return None

    def _rates_for(self, info: dict) -> list[int]:
        native = int(info.get("default_samplerate") or 0)
        rates: list[int] = []
        for r in (PREFERRED_RATE, native, 44100):
            if r and int(r) not in rates:
                rates.append(int(r))
        return rates

    # ── open / close ─────────────────────────────────────────────────────

    def _ensure_tx_unlocked(self, device_index: Optional[int]) -> dict:
        if self._stream is not None:
            log.info(
                "rig codec tx hold on existing duplex (rx=%s tx=%s)",
                sorted(self._rx_holders), sorted(self._tx_holders),
            )
            return {"ok": True, "already": True, **self._status_unlocked()}
        if self._output_stream is not None:
            log.info("rig codec tx hold on existing output-only stream (tx=%s)",
                     sorted(self._tx_holders))
            return {"ok": True, "already": True, **self._status_unlocked()}

        result = self._open_duplex_unlocked(device_index)
        if result.get("ok"):
            return result

        # No duplex-capable device -- e.g. dsnoop owns the raw codec's capture
        # side, so it enumerates with zero input channels. TX only ever needs
        # playback, so fall back to an output-only OutputStream on the best
        # playback-capable device (mirrors the RX capture-only fallback).
        duplex_err = result.get("error")
        fallback = self._open_output_unlocked(device_index)
        if fallback.get("ok"):
            log.info("rig codec tx: no duplex device (%s) -- opened output-only "
                     "instead", duplex_err)
            return fallback
        fallback["error"] = f"duplex: {duplex_err}; output-only: {fallback.get('error')}"
        return fallback

    def _ensure_rx_unlocked(self, device_index: Optional[int]) -> dict:
        if self._input_stream is not None:
            log.info(
                "rig codec rx hold on existing capture (rx=%s tx=%s)",
                sorted(self._rx_holders), sorted(self._tx_holders),
            )
            return {"ok": True, "already": True, **self._status_unlocked()}

        lib = self._lib()
        if lib is None:
            self._error = "sounddevice not installed"
            return {"ok": False, "error": self._error, **self._status_unlocked()}

        denied = self._denied_explicit(device_index)
        if denied:
            self._error = denied
            return {"ok": False, "error": denied, **self._status_unlocked()}

        candidates = self._candidates(device_index, self._list_side("rx"))
        last_err = "No USB capture device found"
        for idx in candidates:
            try:
                info = lib.query_devices(idx)
            except Exception as e:
                last_err = str(e)
                continue
            inn = int(info.get("max_input_channels") or 0)
            outn = int(info.get("max_output_channels") or 0)
            if inn < 1:
                continue
            if outn < 1:
                opened = self._start_input(idx, info)
                if opened is not None:
                    return opened
                last_err = self._error or last_err
                continue
            if self._output_stream is not None and idx == self._output_device_index:
                # A live TX output-only stream already owns this exact
                # device index; opening a second (duplex) stream on the
                # same hw: node is the dual-open hazard this module exists
                # to avoid. Try the next candidate instead of this one.
                last_err = (f"{info.get('name', f'device {idx}')}: "
                            f"busy with TX output-only stream")
                continue
            if self._stream is not None:
                log.info(
                    "rig codec rx hold on existing duplex (rx=%s tx=%s)",
                    sorted(self._rx_holders), sorted(self._tx_holders),
                )
                return {"ok": True, "already": True, **self._status_unlocked()}
            opened = self._start_duplex(idx, info)
            if opened is not None:
                return opened
            last_err = self._error or last_err

        if self._stream is not None:
            return {"ok": True, "already": True, **self._status_unlocked()}
        self._error = last_err
        log.error("rig codec capture open failed: %s", last_err)
        return {"ok": False, "error": last_err, **self._status_unlocked()}

    def _open_duplex_unlocked(self, device_index: Optional[int]) -> dict:
        lib = self._lib()
        if lib is None:
            self._error = "sounddevice not installed"
            return {"ok": False, "error": self._error, **self._status_unlocked()}

        denied = self._denied_explicit(device_index)
        if denied:
            self._error = denied
            return {"ok": False, "error": denied, **self._status_unlocked()}

        candidates = self._candidates(device_index, self._list_side("tx"))
        last_err = "No duplex USB codec found"
        for idx in candidates:
            try:
                info = lib.query_devices(idx)
            except Exception as e:
                last_err = str(e)
                continue
            opened = self._start_duplex(idx, info)
            if opened is not None:
                return opened
            last_err = self._error or last_err
        self._error = last_err
        # Not necessarily a real failure -- _ensure_tx_unlocked falls back to
        # output-only right after this, and that fallback succeeding is the
        # expected, common case once dsnoop owns the raw codec's capture
        # side. An operator watching logs during an on-air test should not
        # see an ERROR immediately before a clean TX open.
        log.info("rig codec duplex open unavailable: %s", last_err)
        return {"ok": False, "error": last_err, **self._status_unlocked()}

    def _start_duplex(self, idx: int, info: dict) -> Optional[dict]:
        lib = self._lib()
        name = info.get("name", f"device {idx}")
        in_ch = max(1, min(2, int(info.get("max_input_channels") or 1)))
        out_ch = max(1, min(2, int(info.get("max_output_channels") or 1)))
        last_err = "No duplex USB codec found"
        for sr in self._rates_for(info):
            for lat in ("low", 0.02, None):
                stream = None
                try:
                    kwargs = dict(
                        device=idx,
                        samplerate=sr,
                        channels=(in_ch, out_ch),
                        dtype="float32",
                        blocksize=BLOCK,
                        callback=self._callback,
                    )
                    if lat is not None:
                        kwargs["latency"] = lat
                    stream = lib.Stream(**kwargs)
                    stream.start()
                    self._stream = stream
                    self._rate = sr
                    self._in_ch = in_ch
                    self._out_ch = out_ch
                    self._device_index = idx
                    self._device_name = name
                    self._error = None
                    self._opens += 1
                    self._sync_running()
                    log.info(
                        "rig codec duplex open idx=%s %r @ %d Hz in=%d out=%d "
                        "block=%d lat=%s rx=%s tx=%s",
                        idx, name, sr, in_ch, out_ch, BLOCK, lat,
                        sorted(self._rx_holders), sorted(self._tx_holders),
                    )
                    return {"ok": True, **self._status_unlocked()}
                except Exception as e:
                    last_err = f"{name} @{sr}/{lat}: {e}"
                    # A constructed-but-unstarted (or half-started) Stream
                    # left open here is exactly the same-hw:-node dual-open
                    # hazard this module exists to avoid -- close it before
                    # trying the next rate/latency or falling back to
                    # output-only on this same device index.
                    self._close_stream(stream)
                    self._stream = None
        self._error = last_err
        return None

    def _open_output_unlocked(self, device_index: Optional[int]) -> dict:
        lib = self._lib()
        if lib is None:
            self._error = "sounddevice not installed"
            return {"ok": False, "error": self._error, **self._status_unlocked()}

        denied = self._denied_explicit(device_index)
        if denied:
            self._error = denied
            return {"ok": False, "error": denied, **self._status_unlocked()}

        candidates = self._candidates(device_index, self._list_tx_output_only())
        last_err = "No USB playback device found"
        for idx in candidates:
            try:
                info = lib.query_devices(idx)
            except Exception as e:
                last_err = str(e)
                continue
            opened = self._start_output(idx, info)
            if opened is not None:
                return opened
            last_err = self._error or last_err
        self._error = last_err
        return {"ok": False, "error": last_err, **self._status_unlocked()}

    def _list_tx_output_only(self) -> list[dict[str, Any]]:
        """TX fallback candidates: playback-capable, input not required.

        Separate from ``_list_side("tx")``, which still requires input too --
        that stays correct for a genuine duplex pick. This is only reached
        once duplex has already failed.
        """
        lib = self._lib()
        if lib is None:
            return []
        out = []
        try:
            hostapis = lib.query_hostapis()
            for i, d in enumerate(lib.query_devices()):
                outn = int(d.get("max_output_channels") or 0)
                if outn < 1:
                    continue
                ha = ""
                try:
                    ha = hostapis[d["hostapi"]]["name"]
                except Exception:
                    pass
                name = d.get("name") or f"device {i}"
                if devices.is_denied(name):
                    continue
                out.append({
                    "index": i,
                    "name": name,
                    "hostapi": ha,
                    "in_ch": int(d.get("max_input_channels") or 0),
                    "out_ch": outn,
                    "default_sr": float(d.get("default_samplerate") or 0),
                    "score": devices.score(name, ha, side="tx"),
                })
        except Exception as e:
            log.warning("list_side tx-output-only: %s", e)
        out.sort(key=lambda d: d["score"], reverse=True)
        return out

    def _start_output(self, idx: int, info: dict) -> Optional[dict]:
        lib = self._lib()
        OutputStream = getattr(lib, "OutputStream", None)
        if OutputStream is None:
            self._error = "sounddevice OutputStream not available"
            return None
        name = info.get("name", f"device {idx}")
        out_ch = max(1, min(2, int(info.get("max_output_channels") or 1)))
        last_err = f"{name}: output-only open failed"
        for sr in self._rates_for(info):
            for lat in ("low", 0.02, None):
                stream = None
                try:
                    kwargs = dict(
                        device=idx,
                        samplerate=sr,
                        channels=out_ch,
                        dtype="float32",
                        blocksize=BLOCK,
                        callback=self._output_callback,
                    )
                    if lat is not None:
                        kwargs["latency"] = lat
                    stream = OutputStream(**kwargs)
                    stream.start()
                    self._output_stream = stream
                    self._output_device_index = idx
                    self._rate = sr
                    self._out_ch = out_ch
                    self._device_index = idx
                    self._device_name = name
                    self._error = None
                    self._opens += 1
                    self._sync_running()
                    log.info(
                        "rig codec output-only open idx=%s %r @ %d Hz out=%d "
                        "block=%d lat=%s tx=%s",
                        idx, name, sr, out_ch, BLOCK, lat, sorted(self._tx_holders),
                    )
                    return {"ok": True, **self._status_unlocked()}
                except Exception as e:
                    last_err = f"{name} @{sr}/{lat}: {e}"
                    self._close_stream(stream)
                    self._output_stream = None
        self._error = last_err
        return None

    def _start_input(self, idx: int, info: dict) -> Optional[dict]:
        lib = self._lib()
        InputStream = getattr(lib, "InputStream", None)
        if InputStream is None:
            self._error = "sounddevice InputStream not available"
            return None
        name = info.get("name", f"device {idx}")
        in_ch = max(1, min(2, int(info.get("max_input_channels") or 1)))
        last_err = f"{name}: capture-only open failed"
        for sr in self._rates_for(info):
            for lat in ("low", 0.02, None):
                stream = None
                try:
                    kwargs = dict(
                        device=idx,
                        samplerate=sr,
                        channels=in_ch,
                        dtype="float32",
                        blocksize=BLOCK,
                        callback=self._input_callback,
                    )
                    if lat is not None:
                        kwargs["latency"] = lat
                    stream = InputStream(**kwargs)
                    stream.start()
                    self._input_stream = stream
                    self._rate = sr
                    self._in_ch = in_ch
                    if self._stream is None:
                        self._out_ch = 0
                    self._device_index = idx
                    self._device_name = name
                    self._error = None
                    self._opens += 1
                    self._sync_running()
                    log.info(
                        "rig codec capture-only open idx=%s %r @ %d Hz in=%d "
                        "block=%d lat=%s rx=%s",
                        idx, name, sr, in_ch, BLOCK, lat,
                        sorted(self._rx_holders),
                    )
                    return {"ok": True, **self._status_unlocked()}
                except Exception as e:
                    last_err = f"{name} @{sr}/{lat}: {e}"
                    self._close_stream(stream)
                    self._input_stream = None
        self._error = last_err
        return None

    def _close_stream(self, stream) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def _close_input_unlocked(self):
        self._close_stream(self._input_stream)
        self._input_stream = None

    def _close_duplex_unlocked(self):
        self._close_stream(self._stream)
        self._stream = None

    def _close_output_unlocked(self):
        self._close_stream(self._output_stream)
        self._output_stream = None
        self._output_device_index = None

    def _close_unlocked(self):
        self._close_input_unlocked()
        self._close_duplex_unlocked()
        self._close_output_unlocked()
        self._running = False
        log.info("rig codec closed")

    def _fan_input(self, indata) -> None:
        extras = list(self._extra_inputs.values())
        if self._on_input is None and not extras:
            return
        mono = indata[:, 0].copy() if getattr(indata, "ndim", 1) > 1 else indata.copy()
        if self._on_input is not None:
            self._on_input(mono)
        for cb in extras:
            cb(mono)

    def _input_callback(self, indata, frames, time_info, status):
        # PortAudio input thread. Never raise.
        if status:
            log.debug("rig codec capture status: %s", status)
        try:
            if self._rx_holders and indata is not None:
                self._fan_input(indata)
        except Exception:
            log.exception("rig codec input callback")

    def _output_callback(self, outdata, frames, time_info, status):
        # PortAudio output-only thread (TX fallback). Fill outdata always.
        if status:
            log.debug("rig codec output status: %s", status)
        try:
            if outdata is not None:
                if self._tx_holders and self._on_output is not None:
                    self._on_output(outdata, frames)
                else:
                    outdata.fill(0)
        except Exception:
            log.exception("rig codec output callback")
            if outdata is not None:
                try:
                    outdata.fill(0)
                except Exception:
                    pass

    def _callback(self, indata, outdata, frames, time_info, status):
        # PortAudio duplex thread. Fill outdata always. Never raise.
        if status:
            log.debug("rig codec status: %s", status)
        try:
            # Capture-only InputStream already owns RX; don't double-feed.
            if self._input_stream is None and self._rx_holders and indata is not None:
                self._fan_input(indata)
            if outdata is not None:
                if self._tx_holders and self._on_output is not None:
                    self._on_output(outdata, frames)
                else:
                    outdata.fill(0)
        except Exception:
            log.exception("rig codec callback")
            if outdata is not None:
                try:
                    outdata.fill(0)
                except Exception:
                    pass


codec = RigCodec()
