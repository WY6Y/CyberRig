"""Live SSTV receive: codec audio in, pictures on disk.

Two threads and one rule. The PortAudio callback only ever queues; every
decision happens on the worker. A callback that blocks is a gap in somebody
else's picture — and, on a duplex codec, in our own transmission too.

While nothing is arriving the trace is a rolling few seconds, just enough to
recognise a VIS header. The moment one is found the buffer stops rolling and
keeps everything, because the whole point of `Reception` is being able to
re-render a picture after it has arrived.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from cyberrig.sstv.decode import FMDemodulator, Reception, find_vis, lead_skip
from cyberrig.sstv.modes import SSTVMode, get_mode

log = logging.getLogger("cyberrig.sstv")

# How much audio to keep while waiting for a header. The header is 910 ms; a
# few seconds of slack covers a chopped opening and a slow worker.
LISTEN_SECONDS = 4.0

# Look for a header once per this much NEW AUDIO, not once per this much wall
# time. Gating on the clock while the buffer rolls on sample count means a
# producer running ahead of real time — a backlog draining after a stall, or a
# test — can scroll a header out of the window before it is ever looked at.
SEARCH_EVERY_SAMPLES = 12000

# Ceiling on the trace kept for re-rendering the last picture. Scottie DX is
# about 50 MB; anything past this and the slant slider is not worth the RAM.
KEEP_TRACE_BYTES = 96 * 1024 * 1024

# Keep the raw audio of every reception, not just the demodulated trace.
# The trace is what the decoder DECIDED it heard; the audio is what actually
# arrived. When a picture comes out shredded those are different questions,
# and only the second one can be re-run against changed decoder code. Two
# on-air failures were diagnosed against synthetic audio because the real
# audio no longer existed anywhere.
#
# Scottie DX is 270 s: 26 MB as 16-bit mono at 48 kHz. Worth the disk.
KEEP_AUDIO_SECONDS = 300.0

# Tuning display: a histogram of demodulated tones over this window. An FFT
# of the audio would show a signal is present; a tone histogram shows where
# the signal's tones are LANDING, which is what tells you if you are tuned.
TUNE_WINDOW_S = 1.5
TUNE_LO, TUNE_HI, TUNE_BINS = 1000.0, 2400.0, 56

# Give up on a picture that stops arriving. A real transmission that gets
# stepped on should still save what it got.
STALL_SECONDS = 6.0


@dataclass
class RxItem:
    """One saved picture."""
    id: str
    mode: str
    when: float
    freq_hz: Optional[int]
    snr: float
    slant_ppm: float
    complete: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id, "mode": self.mode, "when": self.when,
            "freq_hz": self.freq_hz, "snr": round(self.snr, 1),
            "slant_ppm": round(self.slant_ppm), "complete": self.complete,
            "url": f"/api/sstv/gallery/{self.id}.png",
        }


def to_display(img: np.ndarray, mode_name: str = ""):
    """The decoded grid resampled to the aspect the mode is VIEWED at.

    None of these modes has square pixels: every one of them is displayed 4:3
    whatever its pixel count, which is why Martin 2's 160x256 is the same
    picture as Martin 1's 320x256. The TX side already crops to
    `display_aspect` (encode.py) — the RX side was writing the raw grid, so a
    saved picture opened on its own came out squashed towards portrait.

    On screen this hid perfectly: the RX pane's canvas is stretched to a 4:3
    wrapper by CSS and the gallery thumbnails are `object-fit: cover` in a
    128x96 box, so both fixed the aspect on the way past. Only the file itself
    was wrong, and the file is what gets opened in a new tab, mailed, or
    pasted into a QSL.
    """
    from PIL import Image

    im = Image.fromarray(img)
    try:
        aspect = get_mode(mode_name).display_aspect
    except KeyError:
        aspect = 4.0 / 3.0
    w, h = im.size
    if not w or not h or abs(w / h - aspect) < 0.01:
        return im
    # Only ever ADD pixels. Squeezing the long axis down would throw away
    # decoded detail to fix what is only a framing problem.
    if w / h < aspect:
        w = int(round(h * aspect))
    else:
        h = int(round(w / aspect))
    return im.resize((w, h), Image.LANCZOS)


class RxStore:
    """Received pictures on disk: a PNG plus a sidecar of what it was."""

    def __init__(self, directory):
        self.dir = Path(directory)

    def save(self, img: np.ndarray, meta: dict) -> RxItem:
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta.get("when") or time.time()))
        base = f"{stamp}-{_slug(meta.get('mode', 'sstv'))}"
        name, n = base, 1
        while (self.dir / f"{name}.png").exists():
            n += 1
            name = f"{base}-{n}"
        to_display(img, str(meta.get("mode") or "")).save(self.dir / f"{name}.png")
        item = RxItem(
            id=name, mode=str(meta.get("mode") or ""), when=float(meta.get("when") or time.time()),
            freq_hz=meta.get("freq_hz"), snr=float(meta.get("snr") or 0.0),
            slant_ppm=float(meta.get("slant_ppm") or 0.0),
            complete=bool(meta.get("complete", True)),
        )
        payload = item.to_dict()
        for k in ("afc_hz", "autoslant_ok", "autoslant_pulses"):
            if k in meta:
                payload[k] = meta[k]
        (self.dir / f"{name}.json").write_text(json.dumps(payload, indent=2))
        log.info("SSTV RX saved %s", name)
        return item

    def save_audio(self, rid: str, audio, rate: int) -> Optional[Path]:
        """Write the raw receive audio beside the PNG, as 16-bit mono WAV.

        A WAV rather than the trace or a .npy because the point is to hand it
        to ANY decoder — ours after a change, PySSTV, QSSTV, MMSSTV, a human
        with headphones. A trace can only be re-rendered by the code that
        produced it, which is no help when that code is the suspect.
        """
        import wave

        if audio is None or len(audio) == 0:
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / f"{_slug(rid)}.wav"
        a = np.asarray(audio, dtype=np.float32)
        peak = float(np.max(np.abs(a))) if a.size else 0.0
        # Normalise only if it would clip. Scaling quiet audio up would throw
        # away the one thing this file is for: what the level actually was.
        if peak > 1.0:
            a = a / peak
        pcm = np.clip(a * 32767.0, -32768, 32767).astype("<i2")
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(rate))
            w.writeframes(pcm.tobytes())
        return p

    def save_trace(self, rid: str, rx) -> Optional[Path]:
        """Write the demod trace beside the PNG for offline re-render."""
        self.dir.mkdir(parents=True, exist_ok=True)
        # savez_compressed appends .npz; give it a bare stem path.
        p = self.dir / f"{_slug(rid)}.npz"
        np.savez_compressed(
            str(p),
            trace=np.ascontiguousarray(rx.trace, dtype=np.float32),
            start=np.int64(rx.start),
            rate=np.int64(rx.rate),
            slant=np.float64(rx.slant),
            shift_ms=np.float64(rx.shift_ms),
            mode=np.asarray(str(rx.mode.name)),
        )
        return Path(str(p) + ".npz")

    def list(self, limit: int = 200) -> list[dict]:
        try:
            files = sorted(self.dir.glob("*.json"), reverse=True)[:limit]
        except OSError:
            return []
        out = []
        for f in files:
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            d["id"] = f.stem
            d["url"] = f"/api/sstv/gallery/{f.stem}.png"
            if (self.dir / f"{f.stem}.wav").exists():
                d["audio"] = f"/api/sstv/gallery/{f.stem}.wav"
            out.append(d)
        return out

    def path(self, rid: str, ext: str = "png") -> Optional[Path]:
        p = self.dir / f"{_slug(rid)}.{ext}"
        return p if p.exists() else None

    def delete(self, rid: str) -> dict:
        p = self.dir / f"{_slug(rid)}.png"
        if not p.exists():
            return {"ok": False, "error": "no such picture"}
        p.unlink()
        # The sidecar, the trace and the audio go with it. The trace was
        # already being orphaned here, which is how a deleted gallery still
        # filled a disk.
        for ext in ("json", "npz", "wav"):
            (self.dir / f"{_slug(rid)}.{ext}").unlink(missing_ok=True)
        return {"ok": True}


def _slug(s: str) -> str:
    keep = "-_"
    return "".join(c for c in str(s) if c.isalnum() or c in keep)[:80] or "rx"


class SSTVReceiver:
    def __init__(self, codec, store: RxStore, rig=None,
                 on_change: Optional[Callable[[], None]] = None):
        self._codec = codec
        self._store = store
        self._rig = rig
        self._on_change = on_change

        self._lock = threading.RLock()
        # Unbounded on purpose. A maxlen queue silently drops audio when the
        # worker hiccups; open-loop SSTV then paints garbage from that point
        # on — the "top quarter fine, rest shredded" failure we saw live.
        self._q: deque = deque()
        self._drops = 0
        self._worker: Optional[threading.Thread] = None
        self._running = False

        self._rate = 48000
        self._stream_seen = 0       # absolute samples handed to the demodulator
        self._demod: Optional[FMDemodulator] = None
        # Growable buffer: repeated np.concatenate on a multi-minute Scottie
        # trace is O(n²) copies and can stall the worker long enough to
        # desync the picture even with an unbounded queue.
        self._trace_buf = np.zeros(0, dtype=np.float32)
        self._trace_len = 0
        # Raw audio. Its indices are NOT interchangeable with the trace's:
        # the demodulator holds a tail back, a TX blanking resets it, and the
        # two buffers are trimmed by different rules. Both therefore carry the
        # absolute stream-sample index of their own element 0, and anything
        # crossing between them goes through those. Assuming a shared origin
        # is what silently saved a WAV starting in the middle of a picture.
        self._audio_buf = np.zeros(0, dtype=np.float32)
        self._audio_len = 0
        self._audio_origin = 0      # stream index of _audio_buf[0]
        self._trace_origin = 0      # stream index of _trace_buf[0]
        self._rx: Optional[Reception] = None
        self._searched_at = 0
        self._last_audio = 0.0
        self._last_len = 0
        self._afc_hz = 0.0

        self.saved: Optional[dict] = None    # most recent, for the UI to notice
        self.last: Optional[Reception] = None  # its trace, kept for re-rendering
        self.error: Optional[str] = None

    @property
    def _trace(self) -> np.ndarray:
        return self._trace_buf[:self._trace_len]

    def _reset_trace(self):
        self._trace_buf = np.zeros(0, dtype=np.float32)
        self._trace_len = 0
        self._trace_origin = self._stream_seen

    def _append_trace(self, tr: np.ndarray) -> np.ndarray:
        """Append demod samples without quadratic reallocation."""
        if tr.size == 0:
            return self._trace
        need = self._trace_len + int(tr.size)
        if need > len(self._trace_buf):
            cap = max(need, max(len(self._trace_buf) * 2, int(self._rate * 8)))
            buf = np.zeros(cap, dtype=np.float32)
            if self._trace_len:
                buf[:self._trace_len] = self._trace_buf[:self._trace_len]
            self._trace_buf = buf
        self._trace_buf[self._trace_len:need] = tr
        self._trace_len = need
        return self._trace

    @property
    def _audio(self) -> np.ndarray:
        return self._audio_buf[:self._audio_len]

    def _reset_audio(self):
        self._audio_buf = np.zeros(0, dtype=np.float32)
        self._audio_len = 0
        self._audio_origin = self._stream_seen

    def _append_audio(self, block: np.ndarray):
        """Same growable-buffer trick as the trace, for the same reason."""
        if block.size == 0:
            return
        need = self._audio_len + int(block.size)
        if need > len(self._audio_buf):
            cap = max(need, max(len(self._audio_buf) * 2, int(self._rate * 8)))
            buf = np.zeros(cap, dtype=np.float32)
            if self._audio_len:
                buf[:self._audio_len] = self._audio_buf[:self._audio_len]
            self._audio_buf = buf
        self._audio_buf[self._audio_len:need] = block
        self._audio_len = need
        # Hard ceiling so a receiver left listening overnight cannot grow
        # without bound. The trace has LISTEN_SECONDS to bound it; this is
        # the equivalent for a reception that never finishes.
        cap = int(KEEP_AUDIO_SECONDS * self._rate * 1.2)
        if self._audio_len > cap:
            drop = self._audio_len - cap
            self._set_audio(self._audio[drop:], origin=self._audio_origin + drop)

    def _set_audio(self, a: np.ndarray, origin: Optional[int] = None):
        a = np.ascontiguousarray(a, dtype=np.float32)
        self._audio_buf = a
        self._audio_len = int(a.size)
        if origin is not None:
            self._audio_origin = int(origin)

    def _set_trace(self, tr: np.ndarray, origin: Optional[int] = None):
        """Replace the whole trace (cut-to-header, roll while listening)."""
        tr = np.ascontiguousarray(tr, dtype=np.float32)
        self._trace_buf = tr
        self._trace_len = int(tr.size)
        if origin is not None:
            self._trace_origin = int(origin)

    def _audio_for_trace(self, i: int) -> Optional[int]:
        """Trace index -> index into the audio buffer, or None if not held."""
        j = (self._trace_origin + int(i)) - self._audio_origin
        return j if 0 <= j <= self._audio_len else None

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> dict:
        with self._lock:
            if self._running:
                return {"ok": True, "already": True}
            self._rate = int(getattr(self._codec, "sample_rate", None) or 48000)
            self._demod = FMDemodulator(self._rate)
            self._stream_seen = 0
            self._reset_trace()
            self._reset_audio()
            self._rx = None
            self._afc_hz = 0.0
            self._q.clear()
            self._drops = 0
            self._running = True
            if hasattr(self._codec, "add_input_handler"):
                self._codec.add_input_handler("sstv", self._on_input)
            else:
                self._codec.set_input_handler(self._on_input)
            self._worker = threading.Thread(target=self._loop, daemon=True, name="sstv-rx")
            self._worker.start()
        log.info("SSTV receiver listening at %d Hz", self._rate)
        return {"ok": True}

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._running = False
            worker = self._worker
            try:
                if hasattr(self._codec, "remove_input_handler"):
                    self._codec.remove_input_handler("sstv")
                else:
                    self._codec.set_input_handler(None)
            except Exception:
                log.exception("SSTV receiver detach")
        # Outside the lock, and WITHOUT clearing anything first: the worker
        # saves a half-received picture on its way out, and dropping the
        # reception here would throw away the very thing it is about to keep.
        if worker is not None and worker.is_alive():
            worker.join(timeout=5.0)
        with self._lock:
            self._rx = None
            self._reset_trace()
            self._reset_audio()
            self._q.clear()
            self._worker = None

    # ── audio ────────────────────────────────────────────────────────────

    def _on_input(self, mono: np.ndarray):
        """PortAudio thread. Queue and return — never decode here."""
        if not self._running:
            return
        try:
            # Soft peak limit: a slammed RFG makes FM demod spit noise that
            # looks exactly like a shredded picture even when timing is fine.
            x = np.ascontiguousarray(mono, dtype=np.float32)
            peak = float(np.max(np.abs(x))) if x.size else 0.0
            if peak > 0.55:
                x = x * np.float32(0.55 / peak)
            self._q.append(x)
            # Warn if the worker is falling behind (should stay near 0).
            qlen = len(self._q)
            if qlen > 400 and qlen % 100 == 0:
                log.warning("SSTV RX audio queue depth %d — worker behind", qlen)
        except Exception:
            pass

    def _is_tx(self) -> bool:
        st = getattr(self._rig, "state", None)
        return bool(getattr(st, "is_tx", False))

    def _loop(self):
        while self._running:
            if not self._q:
                # A picture that stops arriving mid-transmission still gets
                # saved. This is the only place that can notice, because
                # noticing requires audio to have STOPPED.
                if (self._rx is not None and self._last_audio
                        and time.time() - self._last_audio > STALL_SECONDS):
                    try:
                        self._finish(stalled=True)
                    except Exception:
                        log.exception("SSTV stall finish")
                time.sleep(0.02)
                continue
            block = self._q.popleft()
            try:
                self._consume(block)
            except Exception:
                self.error = "decode failed"
                log.exception("SSTV receive")
        # Anything half-received when we stop is still worth keeping.
        try:
            self._finish(stalled=True)
        except Exception:
            log.exception("SSTV receive finish")

    def _consume(self, block: np.ndarray):
        if self._is_tx():
            # Our own transmission loops back through the codec. Decoding it
            # would fill the gallery with copies of what we just sent.
            # `reset()` discards the demodulator's held-back tail, so the
            # trace permanently loses samples the audio never saw. Start both
            # buffers over rather than let them drift a block apart.
            self._demod.reset()
            self._stream_seen += int(block.size)
            self._reset_trace()
            self._reset_audio()
            return
        # Before anything else: keep what actually arrived. Indices track the
        # trace because the demodulator is sample-for-sample from the start of
        # the stream, and both buffers are cut and rolled together below.
        self._append_audio(block)
        tr = self._demod.feed(block)
        self._stream_seen += int(block.size)
        self._last_audio = time.time()
        if tr.size:
            # Once VIS AFC has locked, keep applying it to new samples so
            # black/white and sync stay on the nominal map for the whole picture.
            if self._rx is not None and abs(self._afc_hz) >= 1.0:
                tr = tr - np.float32(self._afc_hz)
            self._append_trace(tr)
        # Deliberately NOT an early return when the demodulator has nothing
        # to give yet. It only emits on whole blocks, so the last few buffers
        # of every transmission produce none — and those are exactly the ones
        # during which the picture finishes and has to be flushed and saved.

        if self._rx is not None:
            self._rx.trace = self._trace
            # The demodulator holds up to a block of audio back. Without
            # this, the last third of a second of every transmission never
            # arrives, no picture ever reaches its last line, and nothing is
            # ever saved automatically.
            # "Within a line of the end", not "at the end". VIS detection
            # lands a few tens of samples late, so the exact final sample is
            # never reached and a test for it deadlocks: the picture waits
            # forever for audio that has already been consumed.
            need = self._rx.start + self._rx.total_samples
            per = self._rx.mode.line_ms / 1000.0 * self._rate
            remaining = need - self._trace_len
            if 0 < remaining <= self._demod.block + per:
                tail = self._demod.flush()
                if tail.size:
                    if abs(self._afc_hz) >= 1.0:
                        tail = tail - np.float32(self._afc_hz)
                    self._append_trace(tail)
                    self._rx.trace = self._trace
            if self._rx.complete:
                self._finish()
            elif self._trace_len - self._last_len > self._rate // 4:
                self._last_len = self._trace_len
                self._changed()
            return

        # Listening: roll the buffer so an idle receiver does not grow forever.
        if self._trace_len - self._searched_at < SEARCH_EVERY_SAMPLES:
            return
        # Hand the raw audio over too: the header is found spectrally, which
        # sees roughly 18 dB deeper into the noise than reading the frequency
        # trace can. Only the slice that lines up with the trace is safe to
        # pass — the two buffers do not share an index.
        a0 = self._audio_for_trace(0)
        aud = None
        if a0 is not None:
            aud = self._audio[a0:a0 + self._trace_len]
            if aud.size < self._trace_len // 2:
                aud = None
        vis = find_vis(self._trace, self._rate, audio=aud)
        if vis is not None:
            self._begin(vis)
            return
        self._searched_at = self._trace_len
        # Only roll the buffer AFTER looking. Trimming first could discard the
        # very header we were about to find.
        cap = int(LISTEN_SECONDS * self._rate)
        if self._trace_len > cap:
            drop = self._trace_len - cap
            self._set_trace(self._trace[drop:].copy(),
                            origin=self._trace_origin + drop)
            # Roll the audio to the same STREAM position — not by the same
            # index, which is only the same thing while the origins agree.
            keep = self._audio_for_trace(0)
            if keep is not None and keep > 0:
                self._set_audio(self._audio[keep:].copy(),
                                origin=self._audio_origin + keep)
            self._searched_at = max(0, self._searched_at - drop)

    def _begin(self, vis):
        # Cut to the header so the reception owns its own indices from here.
        cut = max(0, vis.leader_at)
        tr = self._trace[cut:].copy()
        new_trace_origin = self._trace_origin + cut
        # Keep a little audio in front of the header. A decoder run offline
        # has to find VIS for itself, and a recording that starts exactly on
        # the leader gives it nothing to lock onto.
        pre = int(0.5 * self._rate)
        # Whatever else happens, say out loud when the two buffers disagree.
        # One reception on 2026-08-26 saved a WAV that began in the middle of
        # the picture and root cause was never pinned down: there was nothing
        # in the log to work from. There is now.
        skew = (self._trace_origin + cut) - (self._audio_origin + cut)
        if skew:
            log.warning("SSTV audio/trace origins differ by %+d samples "
                        "(%.2f s) at header", skew, skew / float(self._rate))
        a0 = self._audio_for_trace(cut)
        if a0 is None:
            # The header is not inside the audio we still hold. Better to save
            # no WAV than one that silently starts in the middle of a picture.
            log.warning("SSTV raw audio does not cover the header — not kept")
            self._reset_audio()
        else:
            keep = max(0, a0 - pre)
            self._set_audio(self._audio[keep:].copy(),
                            origin=self._audio_origin + keep)
        # Centre the tones on the nominal map. VIS AFC may have locked 80 Hz
        # high; leaving that in the trace would wash black/white and miss sync.
        off = float(getattr(vis, "offset_hz", 0.0) or 0.0)
        if abs(off) >= 1.0:
            tr = tr - np.float32(off)
            self._afc_hz = off
        else:
            self._afc_hz = 0.0
        self._set_trace(tr, origin=new_trace_origin)
        start = vis.start - cut
        # Same helper the file decoder uses. The live path used to carry its
        # own test and it was never true, so Scottie kept a stray off-grid
        # pulse and auto-slant silently returned zero on every reception.
        start += lead_skip(self._trace, vis.mode, start, self._rate)
        self._rx = Reception(mode=vis.mode, start=start, rate=self._rate,
                             trace=self._trace)
        self._last_len = self._trace_len
        if abs(off) >= 1.0:
            log.info("SSTV receiving %s (AFC %+.0f Hz)", vis.mode.name, off)
        else:
            log.info("SSTV receiving %s", vis.mode.name)
        self._changed()

    def _finish(self, stalled: bool = False):
        rx = self._rx
        self._rx = None
        if rx is None:
            return
        # Whatever is still held back belongs to this picture, not the next.
        try:
            tail = self._demod.flush()
            if tail.size:
                if abs(self._afc_hz) >= 1.0:
                    tail = tail - np.float32(self._afc_hz)
                self._append_trace(tail)
                rx.trace = self._trace
        except Exception:
            log.exception("SSTV flush")
        if rx.lines_complete < 4:
            log.info("SSTV reception abandoned after %d lines", rx.lines_complete)
            self._reset_trace()
            self._reset_audio()
            self._afc_hz = 0.0
            self._changed()
            return
        # Own a copy before anything downstream resets the live buffers.
        audio = np.array(self._audio, copy=True) if self._audio_len else None
        fit = rx.autoslant()
        if fit.ok:
            log.info("SSTV autoslant %+d ppm (pulses=%d rms=%.2f ms)",
                     int(round(fit.slant * 1e6)), fit.pulses, fit.rms_ms)
        else:
            log.info("SSTV autoslant skipped (pulses=%d rms=%.2f ms) — "
                     "rendering with per-line sync",
                     fit.pulses, fit.rms_ms)
        # Own a copy of the trace BEFORE disk I/O. The live buffer is reset at
        # the end of this method, and saving a multi-MB npz can outlast the
        # test/UI poll that expects `last` to be ready.
        if rx.trace.nbytes <= KEEP_TRACE_BYTES:
            rx.trace = np.array(rx.trace, copy=True, dtype=np.float32)
            self.last = rx
        else:
            self.last = None
            log.info("SSTV trace too large to keep for re-rendering")
        img = rx.image(partial=not rx.complete)
        freq = None
        st = getattr(self._rig, "state", None)
        if st is not None:
            freq = getattr(st, "freq_a", None)
        meta = {
            "mode": rx.mode.name, "when": time.time(), "freq_hz": freq,
            "snr": rx.snr_estimate(), "slant_ppm": rx.slant * 1e6,
            "complete": rx.complete and not stalled,
            "afc_hz": round(float(self._afc_hz), 1),
            "autoslant_ok": bool(fit.ok),
            "autoslant_pulses": int(fit.pulses),
        }
        try:
            item = self._store.save(img, meta)
            self.saved = item.to_dict()
            # Raw audio first: it is the only artefact that survives a change
            # to the decoder, so it matters more than the re-renderable trace.
            try:
                wav = self._store.save_audio(item.id, audio, self._rate)
                if wav is not None:
                    self.saved["audio"] = f"/api/sstv/gallery/{item.id}.wav"
            except Exception:
                log.exception("SSTV save audio")
            # Persist the demod trace next to the PNG so a shredded picture
            # can be re-rendered offline after a restart.
            if self.last is not None:
                try:
                    self._store.save_trace(item.id, rx)
                except Exception:
                    log.exception("SSTV trace persist")
        except Exception as e:
            self.error = f"could not save: {e}"
            log.exception("SSTV save")
        self._reset_trace()
        self._reset_audio()
        self._afc_hz = 0.0
        self._demod.reset()
        self._changed()

    # ── operator adjustment ──────────────────────────────────────────────

    def status(self) -> dict:
        rx = self._rx
        return {
            "rx_running": self._running,
            "rx_active": rx is not None,
            "rx_mode": rx.mode.name if rx else None,
            "rx_lines": rx.lines_complete if rx else 0,
            "rx_total": rx.mode.lines_sent if rx else 0,
            "rx_progress": round(rx.progress, 3) if rx else 0.0,
            "rx_slant_ppm": round(rx.slant * 1e6) if rx else 0,
            "rx_snr": round(rx.snr_estimate(), 1) if rx else 0.0,
            "rx_last": self.saved,
            "rx_error": self.error,
            **self.last_info(),
            **(self.tuning() if self._running else
               {"tune_bins": [], "tune_offset_hz": None, "tune_signal": False}),
        }

    def adjust_last(self, slant_ppm=None, shift_ms=None,
                    auto: bool = False) -> dict:
        """Re-render the last picture at a different slant. No new audio."""
        rx = self.last
        if rx is None:
            return {"ok": False, "error": "no picture to adjust"}
        if auto:
            f = rx.autoslant()
            if not f.ok:
                why = ("not enough sync pulses to fit"
                       if f.pulses < 8 else "sync-pulse fit too loose")
                return {"ok": False, "error": why,
                        "pulses": f.pulses, "rms_ms": round(f.rms_ms, 3),
                        **self.last_info()}
        else:
            if slant_ppm is not None:
                rx.slant = float(slant_ppm) / 1e6
            if shift_ms is not None:
                rx.shift_ms = float(shift_ms)
        return {"ok": True, **self.last_info()}

    def last_image(self) -> Optional[np.ndarray]:
        rx = self.last
        return None if rx is None else rx.image(partial=not rx.complete)

    def last_info(self) -> dict:
        rx = self.last
        if rx is None:
            return {"last_adjustable": False}
        return {
            "last_adjustable": True,
            "last_mode": rx.mode.name,
            "last_slant_ppm": round(rx.slant * 1e6, 1),
            "last_shift_ms": round(rx.shift_ms, 2),
        }

    def resave_last(self) -> dict:
        """Overwrite the stored picture with the corrected render."""
        rx, item = self.last, self.saved
        if rx is None or not item:
            return {"ok": False, "error": "no picture to save"}

        p = self._store.path(item["id"])
        if p is None:
            return {"ok": False, "error": "the saved picture is gone"}
        to_display(rx.image(partial=not rx.complete), rx.mode.name).save(p)
        log.info("SSTV re-saved %s at %.0f ppm", item["id"], rx.slant * 1e6)
        return {"ok": True, "id": item["id"], **self.last_info()}

    def tuning(self) -> dict:
        """Where the received tones are sitting, and how far off that is.

        Sync is 1200 Hz by definition, so the offset of the received sync
        cluster from 1200 IS the tuning error — a number you can dial out,
        rather than a picture of a signal you have to interpret.
        """
        tr = self._trace
        n = int(TUNE_WINDOW_S * self._rate)
        seg = tr[-n:] if tr.size > n else tr
        if seg.size < 1000:
            return {"tune_bins": [], "tune_offset_hz": None, "tune_signal": False}
        hist, _ = np.histogram(seg, bins=TUNE_BINS, range=(TUNE_LO, TUNE_HI))
        peak = int(hist.max())
        bins = (hist / peak * 255).astype(int).tolist() if peak else [0] * TUNE_BINS
        # The sync cluster is the low tone. Nothing down there means no SSTV.
        # Report dial error, not the post-AFC residual: once VIS locks we shift
        # the live trace onto 1200/1900, and the strip would otherwise snap to
        # "0 Hz" and hide the correction the operator still needs on the VFO.
        low_lo, low_hi = 1050.0 + self._afc_hz, 1400.0 + self._afc_hz
        # After AFC the trace is already centred, so look in the nominal band.
        if abs(self._afc_hz) >= 1.0:
            low_lo, low_hi = 1050.0, 1400.0
        low = seg[(seg > low_lo) & (seg < low_hi)]
        offset = None
        if low.size > seg.size * 0.01:
            residual = float(np.median(low)) - 1200.0
            offset = round(residual + float(self._afc_hz), 1)
        return {
            "tune_bins": bins,
            "tune_offset_hz": offset,
            "tune_signal": bool(offset is not None),
        }

    def current_image(self) -> Optional[np.ndarray]:
        rx = self._rx
        if rx is None:
            return None
        return rx.image(partial=True)

    def _changed(self):
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("sstv rx on_change")
