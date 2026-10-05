"""FreeDV audio engine: USB RX → decode → browser; browser mic → encode → USB TX.

PortAudio callbacks stay thin (queue only). The modem runs on a worker thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

import numpy as np

from cyberrig.freedv.modem import StreamResampler, open_modem

log = logging.getLogger("cyberrig.freedv")

PCMListener = Callable[[bytes, int], None]
StatusListener = Callable[[dict], None]


class FreeDVEngine:
    def __init__(self, codec=None):
        self._codec = codec
        self._lock = threading.Lock()
        self._running = False
        self._modem = None
        self._mode = "RADE"
        self._rx_q: deque = deque(maxlen=48)
        self._mic_q: deque = deque(maxlen=48)
        self._tx_ring = np.zeros(0, dtype=np.float32)
        self._tx_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._pcm_listeners: list[PCMListener] = []
        self._status_listeners: list[StatusListener] = []
        self._ptt = False
        # Audio drive, and therefore the thing that actually sets output power
        # — the radio's power setting only moves the ALC threshold. The old
        # 0.35 was hard-coded with no way to see or change it; the live value
        # now comes from `freedv_tx_level` and the DRIVE slider on /freedv.
        self._tx_level = 0.05
        self._usb_rate = 48000
        self._held = False
        self.error: Optional[str] = None
        self.fallback: Optional[str] = None
        self._sync = 0
        self._snr = 0.0
        self._frames_rx = 0
        self._frames_tx = 0
        self._spec: list[int] = []
        self._usb_to_modem: Optional[StreamResampler] = None
        self._speech_to_play: Optional[StreamResampler] = None
        self._mic_to_speech: Optional[StreamResampler] = None
        self._modem_to_usb: Optional[StreamResampler] = None
        self._invert = False
        self._inv_n = 0

    def _rig_codec(self):
        if self._codec is not None:
            return self._codec
        from cyberrig.audio.rig_codec import codec
        return codec

    def status(self) -> dict:
        m = self._modem
        return {
            "running": self._running,
            "mode": (m.name if m is not None else self._mode),
            "requested_mode": self._mode,
            "fallback": self.fallback,
            "sync": bool(self._sync),
            "snr": round(float(self._snr), 1),
            "ptt": self._ptt,
            "tx_level": self._tx_level,
            "usb_rate": self._usb_rate,
            "error": self.error,
            "frames_rx": self._frames_rx,
            "frames_tx": self._frames_tx,
            "spectrum": list(self._spec),
            "invert": self._invert,
        }

    def on_pcm(self, fn: PCMListener):
        self._pcm_listeners.append(fn)

    def off_pcm(self, fn: PCMListener):
        self._pcm_listeners = [x for x in self._pcm_listeners if x is not fn]

    def start(self, mode: str = "RADE") -> dict:
        with self._lock:
            if self._running:
                return {"ok": True, "already": True, **self.status()}
            self.error = None
            self.fallback = None
            self._mode = (mode or "RADE").upper()
            try:
                self._modem = open_modem(self._mode)
            except Exception as e:
                self.error = str(e)
                return {"ok": False, "error": self.error, **self.status()}
            if self._modem.name != self._mode:
                self.fallback = f"{self._mode} unavailable, using {self._modem.name}"
            c = self._rig_codec()
            if hasattr(c, "add_input_handler"):
                c.add_input_handler("freedv", self._on_input)
            else:
                c.set_input_handler(self._on_input)
            c.set_output_handler(self._on_output)
            self._usb_rate = int(getattr(c, "sample_rate", None) or 48000)
            self._usb_to_modem = StreamResampler(self._usb_rate, self._modem.modem_rate)
            self._speech_to_play = StreamResampler(self._modem.speech_rate, 16000)
            self._modem_to_usb = StreamResampler(self._modem.modem_rate, self._usb_rate)
            self._mic_to_speech = None
            self._inv_n = 0
            self._running = True
            self._ptt = False
            self._tx_ring = np.zeros(0, dtype=np.float32)
            self._worker = threading.Thread(target=self._loop, daemon=True, name="freedv-modem")
            self._worker.start()
            log.info("FreeDV engine started mode=%s usb=%d", self._modem.name, self._usb_rate)
            return {"ok": True, **self.status()}

    def stop(self):
        with self._lock:
            self._running = False
            self._ptt = False
            c = self._rig_codec()
            if hasattr(c, "remove_input_handler"):
                c.remove_input_handler("freedv")
            c.set_output_handler(None)
            m = self._modem
            self._modem = None
        if m is not None:
            try:
                m.close()
            except Exception:
                log.exception("modem close")

    def set_mode(self, mode: str) -> dict:
        was = self._running
        ptt = self._ptt
        if was:
            self.stop()
        r = self.start(mode)
        self._ptt = ptt and r.get("ok")
        return r

    def set_ptt(self, on: bool):
        ending = self._ptt and not on
        self._ptt = bool(on)
        # USB loopback of our own RADE waveform must not go to the speaker.
        self._rx_q.clear()
        if ending and self._modem is not None:
            try:
                eoo = self._modem.end_over()
                if eoo.size:
                    self._push_tx(eoo)
            except Exception:
                log.exception("end_over")

    def set_tx_level(self, level: float) -> float:
        """Set the modem audio drive, 0.005-1.0.

        The floor is 0.005, not 0.05. The useful range is all down at the
        bottom of the scale — the difference between sitting on the ALC and
        being clear of it is a few dB, which is nothing in linear amplitude.
        A 0.05 floor left an operator aiming lower with nowhere to go, and it
        looked exactly like the control had stopped working.
        """
        self._tx_level = float(max(0.005, min(1.0, level)))
        return self._tx_level

    def set_invert(self, on: bool):
        self._invert = bool(on)
        self._inv_n = 0

    def push_mic(self, pcm_s16le: bytes, client_sr: int = 48000):
        if not self._running or not self._ptt or not pcm_s16le:
            return
        i16 = np.frombuffer(pcm_s16le, dtype="<i2")
        if i16.size == 0:
            return
        f = i16.astype(np.float32) / 32768.0
        self._mic_q.append((f, int(client_sr) or 48000))

    def _on_input(self, mono: np.ndarray):
        if not self._running:
            return
        try:
            self._rx_q.append(np.ascontiguousarray(mono, dtype=np.float32))
        except Exception:
            pass

    def _on_output(self, outdata, frames):
        ch = outdata.shape[1] if getattr(outdata, "ndim", 1) > 1 else 1
        with self._tx_lock:
            n = min(frames, len(self._tx_ring))
            mono = np.zeros(frames, dtype=np.float32)
            if n:
                mono[:n] = self._tx_ring[:n]
                self._tx_ring = self._tx_ring[n:]
        if ch >= 2:
            outdata[:, 0] = mono
            outdata[:, 1] = mono
        else:
            outdata[:, 0] = mono

    def _push_tx(self, modem_f32: np.ndarray):
        if modem_f32.size == 0:
            return
        scaled = modem_f32 * self._tx_level
        rs = self._modem_to_usb
        usb = rs.feed(scaled) if rs is not None else scaled
        with self._tx_lock:
            self._tx_ring = np.concatenate([self._tx_ring, usb])
            # cap 1s
            cap = self._usb_rate
            if len(self._tx_ring) > cap:
                self._tx_ring = self._tx_ring[-cap:]
        self._frames_tx += 1

    def _loop(self):
        while self._running:
            did = False
            if self._rx_q:
                chunk = self._rx_q.popleft()
                did = True
                try:
                    self._handle_rx(chunk)
                except Exception:
                    log.exception("freedv rx")
            if self._mic_q and self._ptt:
                mic, sr = self._mic_q.popleft()
                did = True
                try:
                    self._handle_tx(mic, sr)
                except Exception:
                    log.exception("freedv tx")
            if not did:
                time.sleep(0.005)

    def _invert_if_needed(self, modem: np.ndarray) -> np.ndarray:
        if not self._invert or modem.size == 0:
            return modem
        n = np.arange(self._inv_n, self._inv_n + len(modem))
        self._inv_n += len(modem)
        return modem * np.where((n & 1) == 0, 1.0, -1.0).astype(np.float32)

    def _handle_rx(self, usb_f32: np.ndarray):
        m = self._modem
        if m is None or self._ptt:
            return
        rs = self._usb_to_modem
        modem = rs.feed(usb_f32) if rs is not None else usb_f32
        if modem.size == 0:
            return
        modem = self._invert_if_needed(modem)
        self._update_spectrum(modem)
        speech = m.rx(modem)
        self._sync = getattr(m, "sync", 0)
        self._snr = getattr(m, "snr", 0.0)
        if speech.size == 0:
            return
        self._frames_rx += 1
        play_rs = self._speech_to_play
        play = play_rs.feed(speech) if play_rs is not None else speech
        if play.size == 0:
            return
        i16 = np.clip(play * 32767.0, -32767, 32767).astype(np.int16)
        raw = i16.tobytes()
        for fn in list(self._pcm_listeners):
            try:
                fn(raw, 16000)
            except Exception:
                pass

    def _handle_tx(self, mic_f32: np.ndarray, sr: int):
        m = self._modem
        if m is None:
            return
        sr = int(sr) or 48000
        if self._mic_to_speech is None or self._mic_to_speech.sr_in != sr:
            self._mic_to_speech = StreamResampler(sr, m.speech_rate)
        speech = self._mic_to_speech.feed(mic_f32)
        if speech.size == 0:
            return
        modem = m.tx(speech)
        if self._invert and modem.size:
            modem = self._invert_if_needed(modem)
        self._push_tx(modem)

    def _update_spectrum(self, modem_f32: np.ndarray):
        if modem_f32.size < 256:
            return
        n = min(512, len(modem_f32))
        x = modem_f32[-n:] * np.hanning(n)
        spec = np.abs(np.fft.rfft(x))
        spec = 20.0 * np.log10(np.maximum(spec, 1e-9))
        # 0-4 kHz at modem_rate (8 kHz → Nyquist 4 kHz)
        bins = spec[: min(len(spec), 64)]
        lo, hi = -80.0, -10.0
        scaled = np.clip((bins - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
        self._spec = scaled.tolist()


engine = FreeDVEngine()
