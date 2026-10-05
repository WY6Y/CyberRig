"""SSTV receive: audio in, picture out.

The design decision that matters here is that the whole demodulated
instantaneous-frequency trace for an image is kept in memory. Slant and line
phase are then corrected by RE-RENDERING from that trace, so the SLANT slider
fixes a picture you have already received instead of asking you to catch the
transmission again. That is the difference between a toy decoder and one you
would actually operate, and it is why `Reception` holds a float array rather
than streaming pixels straight out.

numpy only. the station PC has no scipy, so the filtering is done with FFTs.

Chain: real audio -> bandpassed analytic signal (overlap-save FFT) -> phase
difference -> instantaneous frequency -> VIS header -> per-pixel averaging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from cyberrig.sstv.modes import (
    FREQ_BLACK,
    FREQ_LEADER,
    FREQ_SYNC,
    FREQ_VIS_0,
    FREQ_VIS_1,
    FREQ_WHITE,
    MODES,
    SSTVMode,
    VIS_BIT_MS,
    VIS_BREAK_MS,
    VIS_LEADER_MS,
)

log = logging.getLogger("cyberrig.sstv")

DEFAULT_RATE = 48000

# Everything SSTV lives in 1100-2300 Hz. Filtering wider buys nothing but
# noise; filtering tighter rings on the 1200 Hz sync edges.
BAND_LO = 900.0
BAND_HI = 2600.0

# Anything outside this is not a tone we care about, and averaging it into a
# pixel would drag the whole line. Used to reject dropouts, not to clip video.
FREQ_FLOOR = 500.0
FREQ_CEIL = 3200.0


# ── demodulation ─────────────────────────────────────────────────────────

class FMDemodulator:
    """Real audio to instantaneous frequency, in Hz, sample for sample.

    Overlap-save so a live stream and a whole file give the same answer. The
    bandpass is free: it is the same FFT that builds the analytic signal.
    """

    # 2048 samples of overlap, not 512. A brick-wall bandpass built by zeroing
    # FFT bins has an impulse response as long as the block, and the ringing
    # showed up as one wrong sample at every seam — enough to split a VIS
    # leader in two and lose the header entirely. The cosine roll-off below
    # shortens the kernel; this is the belt to its braces.
    def __init__(self, rate: int = DEFAULT_RATE, block: int = 16384,
                 overlap: int = 2048):
        self.rate = int(rate)
        self.block = int(block)
        self.overlap = int(overlap)
        if self.block <= 2 * self.overlap:
            raise ValueError("block must exceed twice the overlap")
        self._tail = np.zeros(0, dtype=np.float64)
        self._last_z: Optional[complex] = None
        self._first = True
        self._mask_n = -1
        self._mask: Optional[np.ndarray] = None

    def _band(self, n: int) -> np.ndarray:
        """Cosine-tapered bandpass. A hard edge in frequency is a long tail in
        time, and the tail is what corrupts block seams."""
        if self._mask_n == n:
            return self._mask
        f = np.fft.rfftfreq(n, 1.0 / self.rate)
        t = 250.0
        w = np.zeros(len(f))
        w[(f >= BAND_LO) & (f <= BAND_HI)] = 1.0
        m = (f >= BAND_LO - t) & (f < BAND_LO)
        w[m] = 0.5 * (1.0 - np.cos(np.pi * (f[m] - (BAND_LO - t)) / t))
        m = (f > BAND_HI) & (f <= BAND_HI + t)
        w[m] = 0.5 * (1.0 + np.cos(np.pi * (f[m] - BAND_HI) / t))
        self._mask, self._mask_n = w, n
        return w

    @property
    def pending(self) -> int:
        """Samples held back waiting for a full block.

        A live caller needs this: the tail is up to one block of audio that
        `feed` has taken but not yet returned, and a receiver that forgets it
        will decide a picture ended a third of a second before it did.
        """
        return max(0, len(self._tail) - self.overlap)

    def reset(self):
        self._tail = np.zeros(0, dtype=np.float64)
        self._last_z = None
        self._first = True

    def _analytic(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        X = np.fft.rfft(x) * self._band(n)
        # Hilbert: double the positive bins, drop the negatives. rfft already
        # holds only the non-negative half, so an irfft-style reconstruction
        # into a complex signal is just a full ifft of the doubled spectrum.
        full = np.zeros(n, dtype=np.complex128)
        full[:len(X)] = X
        full[1:len(X) - (1 if n % 2 == 0 else 0)] *= 2.0
        return np.fft.ifft(full)

    def feed(self, x: np.ndarray) -> np.ndarray:
        """Instantaneous frequency for the samples fed so far, in Hz."""
        x = np.asarray(x, dtype=np.float64).ravel()
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)
        buf = np.concatenate([self._tail, x])
        out = []
        pos = 0
        B, O = self.block, self.overlap
        # The analytic transform is not causal — its kernel is two-sided — so
        # the FFT's circular wrap contaminates BOTH ends of every block, not
        # just the front. Discard O samples from each end and advance by
        # B - 2*O so what is emitted stays exactly contiguous.
        while len(buf) - pos >= B:
            z = self._analytic(buf[pos:pos + B])
            lo = 0 if self._first else O
            out.append(self._freq(z[lo:B - O]))
            self._first = False
            pos += B - 2 * O
        self._tail = buf[pos:]
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out)

    def flush(self) -> np.ndarray:
        """Demodulate whatever is left. Call once at the end of a transmission."""
        tail = self._tail
        self._tail = np.zeros(0, dtype=np.float64)
        if tail.size < 64:
            return np.zeros(0, dtype=np.float32)
        z = self._analytic(tail)
        lo = 0 if self._first else self.overlap
        # A flush empties the tail, so whatever arrives next begins a fresh
        # stream with no emitted history in front of it. Leaving this False
        # made every flush after the first discard `overlap` samples of real
        # audio — which is how a live picture ended up permanently one line
        # short of finished.
        self._first = True
        return self._freq(z[lo:])

    def _freq(self, z: np.ndarray) -> np.ndarray:
        if z.size == 0:
            return np.zeros(0, dtype=np.float32)
        prev = self._last_z if self._last_z is not None else z[0]
        seq = np.concatenate([[prev], z])
        d = np.angle(seq[1:] * np.conj(seq[:-1]))
        self._last_z = complex(z[-1])
        f = d * (self.rate / (2.0 * np.pi))
        return np.clip(f, 0.0, FREQ_CEIL).astype(np.float32)


def demodulate(audio: np.ndarray, rate: int = DEFAULT_RATE) -> np.ndarray:
    d = FMDemodulator(rate)
    return np.concatenate([d.feed(audio), d.flush()])


# ── VIS header ───────────────────────────────────────────────────────────

@dataclass
class VISResult:
    mode: SSTVMode
    code: int
    start: int            # sample index of the first image sample
    leader_at: int        # where the header began, for diagnostics
    offset_hz: float = 0.0  # dial/audio mistune; subtract from the trace to centre tones


# How far off the dial we still try to lock. ±70 Hz used to be a hard cliff
# inside `_near`; live 20 m signals often sit 60-100 Hz high/low, which is
# exactly how you hear a perfect VIS and never start a picture.
VIS_AFC_MAX_HZ = 150.0
VIS_AFC_STEP_HZ = 25.0

# Smoothing applied ONLY when hunting the 300 ms leader tones.
#
# A sample-by-sample instantaneous-frequency reading is very spiky on real HF
# audio, and the leader test wants 225 ms where the reading stays inside a
# +-70 Hz gate. Noise keeps punching holes in that, so an operator hears a
# perfectly clear header and no picture ever starts — measured on air
# 2026-08-26, and reproduced by adding noise to real recordings: a signal
# that still renders a GOOD picture at +3 dB produces nothing at all at 0 dB.
#
# The leader is 300 ms of one tone, so it can be averaged heavily. Everything
# downstream must keep reading the RAW trace: the VIS bits are 30 ms and the
# sync pulses 5-9 ms, and this window would erase them.
#
# Swept against 21 real off-air recordings down a noise ladder (locks out of
# 147): 0 ms -> 44, 5-15 ms -> 53, 20-30 ms -> 54. Past 15 ms it buys one
# lock in 147, which is noise, while the slack every index below needs keeps
# growing. Sitting inside the flat region rather than at its edge.
LEADER_SMOOTH_MS = 15.0


def _smooth(trace: np.ndarray, rate: int, ms: float) -> np.ndarray:
    """Centred moving average, via a running sum so it stays O(n)."""
    n = max(1, int(round(ms * rate / 1000.0)))
    if n <= 1 or trace.size < n:
        return trace
    c = np.concatenate([[0.0], np.cumsum(trace, dtype=np.float64)])
    out = np.empty(trace.size, dtype=np.float32)
    half = n // 2
    a = np.clip(np.arange(trace.size) - half, 0, trace.size)
    b = np.clip(a + n, 0, trace.size)
    a = np.minimum(a, np.maximum(b - 1, 0))
    out[:] = (c[b] - c[a]) / np.maximum(b - a, 1)
    return out


def _near(trace: np.ndarray, hz: float, tol: float = 70.0) -> np.ndarray:
    return np.abs(trace - hz) <= tol


# A VIS hit is only believed if the sync train underneath it agrees with the
# mode it claims. `_spectral_leaders` ranks candidates by leader strength, not
# by time, so a long steady tone INSIDE somebody's video can outrank the real
# header at the top of the file and `find_vis` — which returned the first hit
# — would take it.
#
# Corpus recording 20260826-170846 is the case: 115.7 s of audio, a false
# Robot 36 (a 36-second mode) accepted 19.9 s in, while the true Martin 1
# header sat at 0.448 s as the 8th-ranked candidate. Result was 240 lines of
# garbage. The evidence to reject it was already there and nothing looked:
# 94.8% of the sync gaps in that file match Martin 1's 446.45 ms line, and
# 0% match Robot 36's 150 ms.
#
# Two questions, because neither alone is enough.
#
# DENSITY — pulses per line — catches the real failure: over the first
# SYNC_SUPPORT_LINES lines of every corpus file the right mode yields 36-83
# pulses per 40 lines (0.9-2.1) and the wrong one on 170846 yields zero. But
# density alone cannot tell "sparse because the mode is wrong" from "sparse
# because the signal faded", and both matter: a Martin 1 train measured on a
# Robot 36 grid gives 0.35, and so does a genuine reception that lost two
# lines in three.
#
# PERIOD settles those. A gap between real sync pulses is a whole number of
# the sender's lines, however many were dropped — so a faded Martin 1 scores
# 1.0 on the Martin 1 grid, while a Martin 1 train on a Robot 36 grid scores
# ~0 because 446.45 ms is not a multiple of 150 ms. It is only consulted when
# density is poor: a picture with spurious pulses all through it (163055,
# density 2.1) scores 0.17 on period and is perfectly fine.
#
# This can only ever REJECT a candidate in favour of another one. If nothing
# validates, the first hit is returned exactly as before — a weak header that
# produces few pulses still starts a picture, because a bad picture beats no
# picture and that trade is not this check's to make.
SYNC_SUPPORT_LINES = 40
SYNC_SUPPORT_DENSITY = 0.5
SYNC_SUPPORT_PERIOD = 0.5
SYNC_SUPPORT_MIN_PULSES = 6


def _sync_supports_mode(trace: np.ndarray, mode: SSTVMode, start: int,
                        rate: int) -> bool:
    """Does the sync train under `start` look like this mode's line rate?"""
    st = start + lead_skip(trace, mode, start, rate)
    span = int(SYNC_SUPPORT_LINES * mode.line_ms * rate / 1000.0)
    end = min(len(trace), st + span)
    if end - st < span * 0.5:            # not enough audio left to judge
        return True
    p = find_sync_pulses(trace[:end], mode, st, rate)
    lines = max(1.0, (end - st) / (mode.line_ms * rate / 1000.0))
    if p.size / lines >= SYNC_SUPPORT_DENSITY:
        return True
    if p.size < SYNC_SUPPORT_MIN_PULSES:
        return False                     # too sparse to believe or to test
    # Every gap between real pulses is a whole number of the sender's lines.
    k = np.diff(p) / mode.line_ms
    whole = (np.abs(k - np.round(k)) < SYNC_WINDOW_FRAC) & (k > 0.5) & (k < 4.5)
    return float(whole.mean()) >= SYNC_SUPPORT_PERIOD


def _believable(hit: Optional["VISResult"], trace: np.ndarray,
                rate: int) -> bool:
    if hit is None:
        return False
    off = float(hit.offset_hz or 0.0)
    t = trace - np.float32(off) if abs(off) >= 1.0 else trace
    return _sync_supports_mode(t, hit.mode, hit.start, rate)


def find_vis(trace: np.ndarray, rate: int = DEFAULT_RATE,
             search_from: int = 0,
             audio: Optional[np.ndarray] = None) -> Optional[VISResult]:
    """Locate a VIS header and identify the mode.

    Anchored on the two leader runs rather than walked through tone by tone.
    Tone-walking looked simpler and was not: every transition rings for a
    millisecond or so, so a walk that starts inside a transition immediately
    concludes it is lost. The two long 1900 Hz runs separated by a short break
    are the one unmistakable feature in the whole header — find those, then
    everything after is at a fixed offset.

    Also searches a small AFC window around the nominal tones. A header that
    lands 80 Hz high is still a header; refusing it because 1900±70 missed is
    how an operator hears VIS after VIS with an empty gallery.
    """
    # Zero first (cheap, common case), then a leader-based estimate, then a
    # coarse grid. Dedup so a lucky estimate does not re-run the same offset.
    # Spectral first when the audio is available: it finds headers roughly
    # 18 dB weaker than the tone-run search can, and it hands over a measured
    # frequency offset instead of a 25 Hz grid guess.
    fallback: Optional[VISResult] = None
    if audio is not None and audio.size:
        blur = int(LEADER_FFT_MS * rate / 2000.0)
        for lead_start, lead_end, off in _spectral_leaders(audio, rate, search_from):
            if lead_end >= len(trace):
                continue
            hit = _vis_spectral(audio, rate, lead_start, lead_end, off)
            if hit is None:
                hit = _vis_after_leader(trace, rate, lead_end, off,
                                        lead_start, lead_end, lead_end,
                                        slack=blur, audio=audio)
            if hit is not None:
                # Candidates arrive strongest-first, not earliest-first, so
                # keep looking when the sync train disagrees with the mode.
                if _believable(hit, trace, rate):
                    return hit
                if fallback is None:
                    fallback = hit

    lead = _smooth(trace, rate, LEADER_SMOOTH_MS)
    candidates: list[float] = [0.0]
    est = _estimate_leader_offset(trace, rate, search_from, lead=lead)
    if est is not None:
        candidates.append(est)
    step = VIS_AFC_STEP_HZ
    n = int(VIS_AFC_MAX_HZ // step)
    for i in range(1, n + 1):
        candidates.append(i * step)
        candidates.append(-i * step)

    seen: set[int] = set()
    for off in candidates:
        key = int(round(off))
        if key in seen:
            continue
        seen.add(key)
        hit = _find_vis_at(trace, rate, search_from, offset=float(off), lead=lead)
        if hit is not None:
            if _believable(hit, trace, rate):
                return hit
            if fallback is None:
                fallback = hit
    # Nothing corroborated. Better a picture in a doubtful mode than none.
    return fallback


# ── spectral leader detection ────────────────────────────────────────────
#
# Reading frequency sample by sample and demanding a long unbroken run is a
# WIDEBAND measurement: every bit of noise across the whole passband lands in
# it. An FFT is narrowband — it sums the leader's own energy and discards
# everything outside its bin. On 21 real off-air recordings down a noise
# ladder that is the difference between finding 54 headers and finding 181,
# roughly 18 dB. It is why an operator can hear a header perfectly clearly
# and watch nothing happen.
#
# This needs the AUDIO, not the demodulated trace, which is why `find_vis`
# takes an optional `audio` argument. Everything after the leader still reads
# the trace exactly as before.

LEADER_FFT_MS = 64.0        # ~16 Hz resolution; the leader is 300 ms
LEADER_HOP_DIV = 4
LEADER_DOMINANCE = 0.5      # leader energy as a share of the 900-2600 band

# Half-width of the band `leader_offset_hz` measures the leader in. It has to
# cover every offset the leader test would have ACCEPTED (VIS_AFC_MAX_HZ + 90),
# or a genuinely far-off header gets measured against the edge of the band and
# under-reported; past that, more bandwidth is only more noise.
LEADER_MEASURE_BW = VIS_AFC_MAX_HZ + 100.0


def _leader_frames(audio: np.ndarray, rate: int):
    """(frame_is_leader, peak_hz, hop) over the audio."""
    n = max(64, int(LEADER_FFT_MS * rate / 1000.0))
    hop = max(1, n // LEADER_HOP_DIV)
    if audio.size < n:
        return np.zeros(0, dtype=bool), np.zeros(0), hop
    f = np.fft.rfftfreq(n, 1.0 / rate)
    band = (f >= BAND_LO) & (f <= BAND_HI)
    near = np.abs(f - FREQ_LEADER) <= VIS_AFC_MAX_HZ + 90.0
    win = np.hanning(n)
    starts = np.arange(0, audio.size - n, hop)
    ok = np.zeros(starts.size, dtype=bool)
    pk = np.zeros(starts.size)
    for i, st in enumerate(starts):
        S = np.abs(np.fft.rfft(audio[st:st + n] * win)) ** 2
        tot = S[band].sum()
        if tot <= 0:
            continue
        pk[i] = f[band][int(np.argmax(S[band]))]
        # Both tests matter: the peak must BE the leader, and the leader must
        # dominate. Peak alone fires on any tone; dominance alone fires on a
        # loud carrier anywhere in the band.
        ok[i] = (abs(pk[i] - FREQ_LEADER) <= VIS_AFC_MAX_HZ + 90.0
                 and S[near].sum() / tot > LEADER_DOMINANCE)
    return ok, pk, hop


def _spectral_leaders(audio: np.ndarray, rate: int, search_from: int = 0):
    """Candidate (leader_end_sample, measured_offset_hz), strongest first.

    The 10 ms break between the two leader halves is far shorter than the FFT
    window, so a header usually reads as ONE long leader region. That is fine:
    all the caller needs is where it ends and how far off frequency it sits.
    """
    ok, pk, hop = _leader_frames(audio, rate)
    if not ok.any():
        return []
    need = max(2, int(VIS_LEADER_MS * 0.75 / 1000.0 * rate / hop))
    out = []
    for a, b in _runs(ok, min_len=need, start=0, bridge=2):
        end = int(b * hop + LEADER_FFT_MS * rate / 2000.0)
        if end < search_from:
            continue
        tones = pk[a:b]
        tones = tones[tones > 0]
        if tones.size == 0:
            continue
        start = max(0, int(a * hop - LEADER_FFT_MS * rate / 2000.0))
        out.append((start, end, float(np.median(tones)) - FREQ_LEADER, int(b - a)))
    out.sort(key=lambda t: -t[3])
    return [(st, e, o) for st, e, o, _ in out]


def leader_offset_hz(audio: np.ndarray, rate: int, lead_start: int,
                     lead_end: int, fallback: float) -> float:
    """How far off 1900 Hz the VIS leader actually sits, from the AUDIO.

    This is the AFC that gets subtracted from the whole trace, so an error
    here slides the entire video map: 1500 Hz is black and 2300 Hz is white,
    and 170 Hz of it is a fifth of that span.

    It used to be `median(trace[lead_start:lead_end]) - 1900`, and that is a
    WIDEBAND read of a narrowband quantity — the same mistake the leader
    SEARCH was rebuilt to avoid, reintroduced one step later as a
    "refinement". The window is not the problem: the FFT locates the leader
    correctly and the tone genuinely dominates the spectrum there. The
    problem is that the instantaneous-frequency TRACE over that same window
    is mostly noise excursions at the SNR pictures actually arrive at —
    measured on live receptions, only 11-17% of the trace samples inside a
    correctly-located leader lie within 1900+-70 Hz. So the median is a
    median of the PASSBAND, and 900-2600 Hz centres well below 1900, which is
    why the error was always negative and always grew as the signal weakened.
    Across 16 live receptions it reported a median -75 Hz where -7.6 Hz was
    present, worst case -172 Hz against a true +13 Hz.

    An amplitude-weighted phase slope over a narrow band around the leader is
    the same narrowband argument as the search itself. Weighting by envelope
    keeps a noise-only stretch from voting.
    """
    pad = int(LEADER_FFT_MS * rate / 2000.0)
    a, b = int(lead_start) + pad, int(lead_end) - pad
    a = max(0, a)
    b = min(len(audio), b)
    if b - a < int(0.05 * rate):        # under 50 ms is not worth measuring
        return float(fallback)
    x = np.asarray(audio[a:b], dtype=np.float64)
    n = x.size
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / rate)
    lo, hi = FREQ_LEADER - LEADER_MEASURE_BW, FREQ_LEADER + LEADER_MEASURE_BW
    X[(f < lo) | (f > hi)] = 0.0
    full = np.zeros(n, dtype=np.complex128)
    full[:len(X)] = X
    full[1:len(X) - (1 if n % 2 == 0 else 0)] *= 2.0
    z = np.fft.ifft(full)
    d = np.angle(z[1:] * np.conj(z[:-1]))
    w = np.abs(z[1:]) * np.abs(z[:-1])
    tot = float(np.sum(w))
    if tot <= 0.0:
        return float(fallback)
    hz = float(np.sum(d * w) / tot) * rate / (2.0 * np.pi)
    off = hz - FREQ_LEADER
    # A measurement outside the window we would have searched is not a
    # measurement, it is a lock on something else. Keep the coarse spectral
    # value, which at least came from a peak that passed the leader test.
    if not np.isfinite(off) or abs(off) > VIS_AFC_MAX_HZ + 90.0:
        return float(fallback)
    return float(off)


def _goertzel(x: np.ndarray, rate: int, hz: float) -> float:
    """Energy at one frequency. Cheaper than a whole FFT for a few bins."""
    if x.size < 4:
        return 0.0
    k = 2.0 * np.cos(2.0 * np.pi * hz / rate)
    s1 = s2 = 0.0
    for v in x:
        s0 = float(v) + k * s1 - s2
        s2, s1 = s1, s0
    return float(s1 * s1 + s2 * s2 - k * s1 * s2)


def _tone_energy(audio: np.ndarray, rate: int, a: int, b: int,
                 freqs) -> list:
    """Energy at each of `freqs` over audio[a:b], via one FFT."""
    a, b = max(0, int(a)), min(audio.size, int(b))
    if b - a < 32:
        return [0.0] * len(freqs)
    seg = audio[a:b] * np.hanning(b - a)
    S = np.abs(np.fft.rfft(seg)) ** 2
    f = np.fft.rfftfreq(b - a, 1.0 / rate)
    out = []
    for hz in freqs:
        # Sum a small neighbourhood: the window is short, so the tone is not
        # confined to one bin, and a single-bin read throws away most of it.
        m = np.abs(f - hz) <= 40.0
        out.append(float(S[m].sum()) if m.any() else 0.0)
    return out


def _vis_spectral(audio: np.ndarray, rate: int,
                  lead_start: int, lead_end: int,
                  offset: float) -> Optional[VISResult]:
    """Read the VIS bits from the AUDIO rather than the frequency trace.

    Finding the leader spectrally was only half the job: the 30 ms bits still
    had to be read, and reading them as a median of the instantaneous-frequency
    trace fails at exactly the signal levels where the spectral leader test
    starts paying off. Comparing energy at 1100 Hz against 1300 Hz over each
    bit is the same narrowband argument applied one step further along.
    """
    ms = rate / 1000.0
    bit_n = int(round(VIS_BIT_MS * ms))
    sync = FREQ_SYNC + offset
    one, zero = FREQ_VIS_1 + offset, FREQ_VIS_0 + offset

    # The start bit is 30 ms of 1200 Hz immediately after the leader. The FFT
    # window blurs the leader's end, so slide to wherever 1200 dominates.
    span = int(LEADER_FFT_MS * rate / 1000.0)

    def start_score(p: int) -> float:
        e_sync, e_lead = _tone_energy(audio, rate, p, p + bit_n,
                                      [sync, FREQ_LEADER + offset])
        return e_sync / max(e_sync + e_lead, 1e-30)

    # Coarse then fine. A coarse-only search quantises the start bit to its
    # own step, which put this path a couple of milliseconds off the tone-run
    # path — about five pixels of horizontal shift on Scottie 1.
    coarse = max(1, bit_n // 8)
    best_p, best_score = None, 0.0
    for p in range(max(0, lead_end - span), lead_end + span, coarse):
        sc = start_score(p)
        if sc > best_score:
            best_p, best_score = p, sc
    if best_p is None or best_score < 0.6:
        return None
    fine = max(1, coarse // 12)
    for p in range(max(0, best_p - coarse), best_p + coarse, fine):
        sc = start_score(p)
        if sc > best_score:
            best_p, best_score = p, sc

    bits = []
    for i in range(1, 9):
        q = best_p + i * bit_n
        e1, e0 = _tone_energy(audio, rate, q, q + bit_n, [one, zero])
        if e1 <= 0.0 and e0 <= 0.0:
            return None
        bits.append(1 if e1 > e0 else 0)
    code = sum(bits[i] << i for i in range(7))
    if sum(bits[:7]) % 2 != bits[7]:
        log.debug("VIS parity failed for 0x%02X (spectral, offset %+.0f)",
                  code, offset)
        return None
    mode = _mode_for_code(code)
    if mode is None:
        log.info("VIS 0x%02X is a mode we do not decode", code)
        return None
    stop = best_p + 9 * bit_n
    e_sync, e_one = _tone_energy(audio, rate, stop, stop + bit_n, [sync, one])
    if e_sync <= e_one:
        return None
    refined = leader_offset_hz(audio, rate, lead_start, lead_end, offset)
    if abs(refined) >= 8.0:
        log.info("VIS locked %s with AFC %+.0f Hz", mode.name, refined)
    return VISResult(mode=mode, code=code, start=stop + bit_n,
                     leader_at=lead_start, offset_hz=refined)


def _estimate_leader_offset(trace: np.ndarray, rate: int,
                            search_from: int = 0,
                            lead: Optional[np.ndarray] = None) -> Optional[float]:
    """Best-guess dial error from a long stable tone near the VIS leader.

    A 300 ms dwell around 1900 Hz is rare in noise and common in a header.
    Its median minus 1900 is the correction to try first.
    """
    ms = rate / 1000.0
    lead_n = int(VIS_LEADER_MS * ms * 0.75)
    t = trace if lead is None else lead
    # Wide gate: a ±150 Hz mistune still lands inside here.
    band = (t > 1750.0) & (t < 2050.0)
    runs = _runs(band, min_len=lead_n, start=search_from)
    best = None
    best_len = 0
    for a, b in runs:
        seg = trace[a:b]
        if seg.size < lead_n:
            continue
        # Reject sweeps / video: a real leader is a tight cluster.
        if float(np.std(seg)) > 80.0:
            continue
        if seg.size > best_len:
            best_len = seg.size
            best = float(np.median(seg)) - FREQ_LEADER
    if best is None or abs(best) < 8.0:
        return None
    if abs(best) > VIS_AFC_MAX_HZ:
        return None
    return best


def _find_vis_at(trace: np.ndarray, rate: int, search_from: int,
                 offset: float,
                 lead: Optional[np.ndarray] = None) -> Optional[VISResult]:
    """VIS search with every nominal tone shifted by `offset` Hz.

    `lead` is a smoothed copy of `trace` used ONLY to locate the two 300 ms
    leader tones. Every test after that reads the raw trace — the bits and
    the sync pulses are far too short to survive the smoothing window.
    """
    ms = rate / 1000.0
    lead_n = int(VIS_LEADER_MS * ms * 0.75)     # allow a clipped leader
    bit_n = int(round(VIS_BIT_MS * ms))
    brk_n = int(VIS_BREAK_MS * ms)
    leader = FREQ_LEADER + offset
    sync = FREQ_SYNC + offset
    bit1 = FREQ_VIS_1 + offset
    bit0 = FREQ_VIS_0 + offset

    runs = _runs(_near(lead if lead is not None else trace, leader),
                 min_len=lead_n, start=search_from)
    # A smoothed run's ends are blurred by half the window either way, so
    # every index derived from them gets that much slack below.
    blur = int(LEADER_SMOOTH_MS * ms) if lead is not None else 0
    for (a0, a1), (b0, b1) in zip(runs, runs[1:]):
        # The two leaders sit either side of one short 1200 Hz break.
        gap = b0 - a1
        if not (brk_n * 0.4 <= gap - blur <= brk_n * 4 + 2 * blur):
            continue
        # Look for the break as a real 1200 Hz run somewhere between the two
        # leaders rather than as a fraction of a window whose ends are fuzzy.
        # `_mostly` over blurred bounds fails on a break that is plainly there.
        if not _has_run(trace, a1 - blur, b0 + blur, sync,
                        min_len=max(8, int(brk_n * 0.4))):
            continue

        hit = _vis_after_leader(trace, rate, b1, offset, a0, a1, b0,
                                slack=blur)
        if hit is not None:
            return hit
    return None


def _vis_after_leader(trace: np.ndarray, rate: int, lead_end: int,
                      offset: float, a0: int, a1: int, b0: int,
                      slack: int = 0,
                      audio: Optional[np.ndarray] = None) -> Optional[VISResult]:
    """Decode the VIS bits that follow a leader ending at `lead_end`.

    Split out so a leader found spectrally and a leader found by tone-running
    reach exactly the same bit decisions. `a0`/`a1`/`b0` bound the leader tone
    itself and are used only to refine the reported frequency offset.

    `audio` is optional because this path is also reached from the tone-run
    search, which has no audio. With it, the offset is measured narrowband
    like everywhere else; without it, the median of the two leader RUNS is
    the honest fallback — those runs were selected for being near 1900, so it
    can under-report an offset but it cannot invent one, which is the failure
    the padded-window median had.
    """
    ms = rate / 1000.0
    bit_n = int(round(VIS_BIT_MS * ms))
    sync = FREQ_SYNC + offset
    bit1 = FREQ_VIS_1 + offset
    bit0 = FREQ_VIS_0 + offset
    for _once in (0,):
        # Run ends land a transition early, so find the real edge into the
        # start bit instead of trusting the leader's end. The search has to
        # cover the smoothing blur too, or a correct header is missed by ms.
        p = _edge_to(trace, max(0, lead_end - slack), sync,
                     limit=int(6 * ms) + 2 * slack)
        if p is None:
            continue
        if not _mostly(trace, p, p + bit_n, sync):
            continue

        bits = []
        for i in range(1, 9):
            q = p + i * bit_n
            mid = trace[q + bit_n // 4: q + 3 * bit_n // 4]
            if mid.size == 0:
                break
            f = float(np.median(mid))
            bits.append(1 if abs(f - bit1) < abs(f - bit0) else 0)
        if len(bits) < 8:
            continue
        code = sum(bits[i] << i for i in range(7))
        if sum(bits[:7]) % 2 != bits[7]:
            log.debug("VIS parity failed for 0x%02X (offset %+.0f)", code, offset)
            continue
        mode = _mode_for_code(code)
        if mode is None:
            log.info("VIS 0x%02X is a mode we do not decode", code)
            continue
        stop = p + 9 * bit_n
        if not _mostly(trace, stop, stop + bit_n, sync, frac=0.5):
            continue
        # Refine from the leaders we actually locked. A header 60 Hz high still
        # matches the nominal 1900±70 gate at grid offset 0, and leaving that
        # uncorrected would wash the picture even though VIS "succeeded".
        if audio is not None and audio.size:
            refined = leader_offset_hz(audio, rate, a0, lead_end, offset)
        else:
            leaders = np.concatenate([trace[a0:a1], trace[b0:lead_end]])
            refined = (float(np.median(leaders)) - FREQ_LEADER
                       if leaders.size else float(offset))
        if abs(refined) >= 8.0:
            log.info("VIS locked %s with AFC %+.0f Hz", mode.name, refined)
        return VISResult(mode=mode, code=code, start=stop + bit_n,
                         leader_at=a0, offset_hz=refined)
    return None


def _edge_to(trace: np.ndarray, a: int, hz: float, limit: int) -> Optional[int]:
    """First index at or after `a` where the tone settles on `hz` and stays."""
    b = min(len(trace), a + limit)
    for i in range(a, b):
        if _near(trace[i:i + 16], hz).all():
            return i
    return None


def _mode_for_code(code: int) -> Optional[SSTVMode]:
    for m in set(MODES.values()):
        if m.vis_code == code:
            return m
    return None


def _runs(mask: np.ndarray, min_len: int, start: int = 0, bridge: int = 48):
    """Start/end index pairs of True runs at least `min_len` long.

    Gaps shorter than `bridge` are closed first. A single bad sample in the
    middle of a 300 ms leader is noise, not the end of the leader, and
    treating it as the end is how a whole header gets missed.
    """
    m = mask.astype(bool)
    if start:
        m = m.copy()
        m[:start] = False
    d = np.diff(np.concatenate([[0], m.view(np.int8), [0]]))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    spans = []
    for a, b in zip(starts, ends):
        if spans and a - spans[-1][1] <= bridge:
            spans[-1][1] = b
        else:
            spans.append([int(a), int(b)])
    return [(a, b) for a, b in spans if b - a >= min_len]


def _has_run(trace: np.ndarray, a: int, b: int, hz: float, min_len: int) -> bool:
    """Is there a sustained run of `hz` anywhere in [a, b)?

    Tolerant of fuzzy bounds in a way `_mostly` is not: widening the window
    to cover a blurred edge drags a fraction test down with off-tone samples,
    while a run test simply keeps looking.
    """
    a, b = max(0, int(a)), min(len(trace), int(b))
    if b - a < min_len:
        return False
    return bool(_runs(_near(trace[a:b], hz), min_len=min_len))


def _mostly(trace: np.ndarray, a: int, b: int, hz: float, frac: float = 0.6) -> bool:
    a, b = max(0, a), min(len(trace), b)
    if b <= a:
        return False
    return float(_near(trace[a:b], hz).mean()) >= frac


def _skip_while(trace: np.ndarray, a: int, hz: float, limit: int) -> int:
    b = min(len(trace), a + limit)
    seg = _near(trace[a:b], hz)
    bad = np.flatnonzero(~seg)
    # Tolerate a few stray samples; stop at the first sustained departure.
    for i in bad:
        if not seg[i:i + 32].any():
            return a + int(i)
    return b


# ── where each pixel lives in time ───────────────────────────────────────

def channel_plan(mode: SSTVMode) -> list:
    """(offset_ms_within_line, channel_key) for one line of this mode.

    Mirrors the encoder's line layout. Kept as one small table rather than
    spread through the decoder so the two can be read side by side.
    """
    sep, scan, sync = mode.sep_ms, mode.scan_ms, mode.sync_ms
    if mode.family == "scottie":
        # sep G sep B sync sep R  — sync sits mid-line, not at the start.
        return [(sep, "g"), (2 * sep + scan, "b"), (3 * sep + 2 * scan + sync, "r")]
    if mode.family == "martin":
        # sync sep G sep B sep R sep
        return [(sync + sep, "g"),
                (sync + 2 * sep + scan, "b"),
                (sync + 3 * sep + 2 * scan, "r")]
    if mode.family == "robot36":
        # sync porch Y sep porch C
        t = sync + mode.porch_ms
        return [(t, "y"), (t + scan + sep + 1.5, "c")]
    if mode.family == "pd":
        t = sync + mode.porch_ms
        return [(t, "y0"), (t + scan, "cr"), (t + 2 * scan, "cb"), (t + 3 * scan, "y1")]
    raise ValueError(mode.family)


def _sample(csum: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """Mean of the trace over each [start, end) window, via a running sum.

    Averaging the whole pixel window rather than point-sampling its centre is
    what makes a noisy signal decode as noise instead of as confetti.
    """
    # csum has one more entry than the trace, so index n is valid as an END
    # but not as a START. Clamping both to the same bound was an off-by-one
    # that only fired on the very last pixel of the very last line.
    n = len(csum) - 1
    starts = np.clip(starts, 0, n - 1)
    ends = np.clip(np.maximum(ends, starts + 1), 1, n)
    starts = np.minimum(starts, ends - 1)
    return (csum[ends] - csum[starts]) / (ends - starts)


def freq_to_level(f: np.ndarray) -> np.ndarray:
    lv = (f - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK) * 255.0
    return np.clip(lv, 0, 255)


def ycrcb_to_rgb(y: np.ndarray, cr: np.ndarray, cb: np.ndarray) -> np.ndarray:
    """Inverse of modes.rgb_to_ycrcb. Must track YCC_FULL_RANGE there."""
    from cyberrig.sstv.modes import YCC_FULL_RANGE
    y = y.astype(np.float64)
    cr = cr.astype(np.float64) - 128.0
    cb = cb.astype(np.float64) - 128.0
    if YCC_FULL_RANGE:
        r = y + 1.402 * cr
        g = y - 0.344136 * cb - 0.714136 * cr
        b = y + 1.772 * cb
    else:
        yy = (y - 16.0) * (255.0 / 219.0)
        r = yy + 1.596 * cr
        g = yy - 0.391 * cb - 0.813 * cr
        b = yy + 2.018 * cb
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)


def render(trace: np.ndarray, mode: SSTVMode, start: int,
           rate: int = DEFAULT_RATE, slant: float = 0.0,
           shift_ms: float = 0.0, lines: Optional[int] = None,
           line_sync: bool = True) -> np.ndarray:
    """Render an image from a frequency trace.

    `slant` is a fractional clock error: positive means the sender's line came
    out longer than nominal. `shift_ms` slides the whole image sideways. Both
    are applied here rather than at capture, which is the entire point of
    keeping the trace — re-render, do not re-receive.

    `line_sync` (default on) re-anchors each line to its 1200 Hz sync pulse
    when pulses are findable. That is what stops a small clock error or a
    brief audio hitch from shredding the rest of the picture.
    """
    csum = np.concatenate([[0.0], np.cumsum(trace, dtype=np.float64)])
    # Origins are real milliseconds on the trace (from sync sample indices, or
    # free-run with slant already folded into the period). Do NOT also scale by
    # (1+slant) here — that double-counted and sheared good pictures.
    spl = rate / 1000.0
    W = mode.width
    n_lines = mode.lines_sent if lines is None else max(0, min(lines, mode.lines_sent))
    plan = channel_plan(mode)
    px = mode.pixel_ms
    cpx = (mode.chroma_scan_ms / W) if mode.family == "robot36" else px

    # Prefer per-line sync re-anchor when pulses are visible. Free-running from
    # VIS alone is what turned a good top quarter into shredded colour once
    # any gap or clock error accumulated — every live QSO today failed that way.
    if line_sync:
        origins = line_origins_ms(trace, mode, start, rate, n_lines,
                                  slant=slant, shift_ms=shift_ms)
    else:
        period = mode.line_ms * (1.0 + slant)
        origins = shift_ms + np.arange(n_lines, dtype=np.float64) * period

    # Sample every channel of every line first, then assemble. Robot 36 needs
    # two lines in hand before either can be coloured, and doing that with
    # state carried between calls is how you get a decoder that works once.
    scanned: list = []
    for li in range(n_lines):
        line_t0 = origins[li]
        row = {}
        for off, key in plan:
            step = cpx if (mode.family == "robot36" and key == "c") else px
            edges = (line_t0 + off + np.arange(W + 1) * step) * spl + start
            e = edges.astype(np.int64)
            row[key] = freq_to_level(_sample(csum, e[:-1], e[1:]))
        scanned.append(row)

    img = np.zeros((mode.height, W, 3), dtype=np.uint8)
    if mode.family in ("scottie", "martin"):
        for li, row in enumerate(scanned):
            img[li, :, 0] = row["r"]
            img[li, :, 1] = row["g"]
            img[li, :, 2] = row["b"]
    elif mode.family == "robot36":
        neutral = np.full(W, 128.0)
        for li in range(0, len(scanned), 2):
            even = scanned[li]
            odd = scanned[li + 1] if li + 1 < len(scanned) else None
            cr = even["c"]                      # R-Y rides the even line
            cb = odd["c"] if odd else neutral    # B-Y the odd one
            img[li] = ycrcb_to_rgb(even["y"], cr, cb)
            if odd is not None:
                img[li + 1] = ycrcb_to_rgb(odd["y"], cr, cb)
    elif mode.family == "pd":
        for li, row in enumerate(scanned):
            img[2 * li] = ycrcb_to_rgb(row["y0"], row["cr"], row["cb"])
            img[2 * li + 1] = ycrcb_to_rgb(row["y1"], row["cr"], row["cb"])
    return img


# ── sync tracking and auto-slant ─────────────────────────────────────────

@dataclass
class SlantFit:
    slant: float = 0.0          # fractional clock error
    shift_ms: float = 0.0       # line-phase correction
    pulses: int = 0
    rms_ms: float = 0.0         # fit residual; large means do not trust it
    ok: bool = False


def sync_center_offset_ms(mode: SSTVMode) -> float:
    """Where the sync pulse CENTRE sits relative to the start of a line."""
    if mode.family == "scottie":
        # sep G sep B sync … — sync starts after two (sep+scan) blocks.
        return 2.0 * (mode.sep_ms + mode.scan_ms) + mode.sync_ms / 2.0
    # Martin / PD / Robot: sync opens the line.
    return mode.sync_ms / 2.0


def find_sync_pulses(trace: np.ndarray, mode: SSTVMode, start: int,
                     rate: int = DEFAULT_RATE) -> np.ndarray:
    """Centre time (ms from `start`) of every 1200 Hz sync pulse after `start`."""
    ms = rate / 1000.0
    # Martin sync is only ~5 ms; 0.45 of that still needs a real dwell, but
    # the old 0.55 gate plus a wide <1320 mask ate pulses on live HF audio.
    want = max(8, int(mode.sync_ms * ms * 0.45))
    lo, hi = FREQ_SYNC - 100.0, FREQ_SYNC + 90.0
    mask = (trace > lo) & (trace < hi)
    out = []
    for a, b in _runs(mask, min_len=want, start=start):
        # A leader-length run of low tone is not a sync pulse; it is somebody
        # else's header or a dropout.
        if (b - a) > mode.sync_ms * ms * 3.5:
            continue
        seg = trace[a:b]
        med = float(np.median(seg))
        if abs(med - FREQ_SYNC) > 85.0:
            continue
        out.append(((a + b) / 2.0 - start) / ms)
    return np.asarray(out, dtype=np.float64)


# How far from where a line is EXPECTED a sync pulse may be and still be
# believed, as a fraction of the line period.
#
# This was 0.40, and on real HF audio that was the single largest source of
# damage in a received picture. `find_sync_pulses` threshold-detects 1200 Hz
# runs on the raw frequency trace, and at the SNR pictures actually arrive at
# (~10 dB in 2.4 kHz) dark video — black is 1500 Hz — dips into the sync
# window for milliseconds at a time. Measured on a live Martin 1: 713 pulses
# reported for 256 lines. A +-40% window is +-178 ms on Martin 1, or +-390
# pixels of a 320-pixel-wide picture, so roughly every other line was captured
# by a false pulse. Measured adjacent-line displacement was 113 px RMS with a
# worst line 633 px out; at 0.02 the same recordings give 1.7 px RMS.
#
# The ceiling on this number is the clock error it must still track: the
# expectation advances one nominal period per line, so a sender running fast
# by more than SYNC_WINDOW_FRAC per line falls out of the window and the
# picture free-runs. 0.02 tolerates 20,000 ppm, which is `fit_slant`'s own
# sanity limit — anything past that is not a clock error, it is a bad decode.
# Do not widen this to "recover more pulses". More pulses is the problem.
SYNC_WINDOW_FRAC = 0.02

# How many consecutive lines may free-run before the tracker stops believing
# its own phase and goes looking for the train again.
#
# The tracker follows a pulse only within +-SYNC_WINDOW_FRAC of a line, and
# builds every expectation from the previous line. That is right for jitter
# and clock error, and wrong for a STEP. Measured on the corpus recording
# 20260826-163650 (20.3 dB, the cleanest one there is): during a 13 dB QSB
# fade at 58 s the sync train jumps 164.56 ms — 36.9% of a Martin 1 line —
# and is then perfectly coherent again on the far side, MAD 0.12 ms. That is
# 18x outside the +-8.9 ms window, so without this the tracker free-runs on
# the stale phase for the whole rest of the picture. Every corpus decode
# showed the same shape: clean top, abrupt break, permanent garbage below.
#
# Across the 22-file corpus this took lines actually snapped to their own
# sync pulse from 1747/5522 (31.6%) to 4228/5522 (76.6%).
REACQUIRE_AFTER = 3

# A re-acquire is only believed if the candidate phase is corroborated by
# this many of the following lines. A lone spur in the middle of a fade is
# exactly what a wide search would otherwise lock onto, and re-anchoring to
# it would be worse than free-running: free-run keeps the phase it had.
REACQUIRE_CONFIRM = 2
REACQUIRE_LOOKAHEAD = 5


def line_origins_ms(trace: np.ndarray, mode: SSTVMode, start: int,
                    rate: int, n_lines: int, slant: float = 0.0,
                    shift_ms: float = 0.0, with_lock: bool = False):
    """Start time (ms from `start`) of each line, sync-snapped when possible.

    Falls back to free-run (`li * line_ms * (1+slant) + shift_ms`) when no
    pulse is near the expected place — so clean lab audio behaves as before.

    The expectation for each line is built from the PREVIOUS line's origin,
    never blended with the free-run grid. Blending the two was defensible
    while the window was wide, but it fights the tracker exactly when it is
    needed: at 3000 ppm the free-run grid has drifted the better part of a
    whole line by the bottom of the picture, and averaging against it pulls
    the target far enough off that a tight window snaps to nothing.

    After REACQUIRE_AFTER consecutive lines find nothing, the phase is no
    longer trusted and the search widens to half a line once, to catch a step
    the narrow window cannot follow. See REACQUIRE_AFTER for the measurement.

    `with_lock` additionally returns a boolean per line: did this line get its
    position from its own sync pulse, or is it free-running on a remembered
    phase? That fraction is what `tests/bench_rx.py` reports, because it is
    the number that tracks whether the PICTURE is right. Header detection —
    all `bench_vis` ever measured — only says a picture started.
    """
    period = mode.line_ms * (1.0 + slant)
    free = shift_ms + np.arange(n_lines, dtype=np.float64) * period
    locked = np.zeros(n_lines, dtype=bool)
    pulses = find_sync_pulses(trace, mode, start, rate)
    if pulses.size < 4:
        return (free, locked) if with_lock else free
    sync_at = sync_center_offset_ms(mode)
    # Pulse centres → line starts.
    starts = pulses - sync_at
    window = period * SYNC_WINDOW_FRAC
    out = free.copy()
    misses = 0
    # No "already used" bookkeeping. Consecutive expectations are a full
    # period apart and the window is a small fraction of one, so a single
    # pulse cannot be in range of two lines — while the old greedy marking
    # could starve a real line whose pulse a false one had already claimed.
    for li in range(n_lines):
        expect = free[li] if li == 0 else out[li - 1] + period
        d = starts - expect
        j = int(np.argmin(np.abs(d)))
        if abs(d[j]) <= window:
            out[li] = expect + d[j]
            locked[li] = True
            misses = 0
            continue
        misses += 1
        cand = (_reacquire(starts, expect, period, window, n_lines - li)
                if misses >= REACQUIRE_AFTER else None)
        if cand is None:
            out[li] = expect
        else:
            out[li] = cand
            locked[li] = True
            misses = 0
    return (out, locked) if with_lock else out


def _reacquire(starts: np.ndarray, expect: float, period: float,
               window: float, lines_left: int) -> Optional[float]:
    """Best corroborated line origin within half a period of `expect`.

    Returns None unless some candidate is backed by REACQUIRE_CONFIRM of the
    next few lines landing on the same phase — a single pulse is not evidence
    of a step, and re-anchoring to noise is worse than holding the old phase.
    """
    near = starts[np.abs(starts - expect) <= period * 0.5]
    if near.size == 0:
        return None
    look = min(REACQUIRE_LOOKAHEAD, max(0, lines_left - 1))
    best, best_votes = None, 0
    for c in near:
        votes = 0
        for k in range(1, look + 1):
            if np.min(np.abs(starts - (c + k * period))) <= window:
                votes += 1
        # Ties go to the candidate closest to where we already were.
        if votes > best_votes or (votes == best_votes and best is not None
                                  and abs(c - expect) < abs(best - expect)):
            best, best_votes = c, votes
    if best is None or best_votes < min(REACQUIRE_CONFIRM, look):
        return None
    return float(best)


def lead_skip(trace: np.ndarray, mode: SSTVMode, start: int,
              rate: int = DEFAULT_RATE) -> int:
    """Samples to advance past a 1200 Hz remnant sitting on `start`.

    Two things land here. The spec puts a lone sync pulse before Scottie's
    first line and PySSTV omits it, so it has to be looked for rather than
    assumed. The VIS stop bit also bleeds a millisecond or two past where the
    header nominally ends. Either leaves a low-tone run that
    `find_sync_pulses` reports as a pulse — and for Scottie, whose real sync
    sits 284 ms into the line, that stray pulse is far enough off the line
    grid to wreck `fit_slant`.

    This lives here, and both callers use it, because the live receiver used
    to carry its own copy of the test (`mean |f - 1200| < 200`) which was
    never true: the window is half stop bit and half video, so the mean
    deviation sits near 400. Scottie auto-slant therefore failed on every
    live reception while every file-decode test passed.
    """
    if mode.family != "scottie":
        return 0
    lead = int(mode.sync_ms * rate / 1000.0)
    w = trace[start:start + int(lead * 1.6)]
    if w.size and _near(w, FREQ_SYNC).mean() > 0.4:
        return lead
    return 0


def fit_slant(pulses: np.ndarray, mode: SSTVMode,
              max_ppm: float = 20000.0) -> SlantFit:
    """Least squares through the sync pulses, with the obvious outliers gone.

    This is the whole of auto-slant: sync pulses should be exactly one line
    apart, so the slope of time-against-line-number IS the sender's clock
    relative to ours.
    """
    f = SlantFit(pulses=int(pulses.size))
    if pulses.size < 8:
        return f
    # Assign line numbers using the OBSERVED spacing, not the nominal one.
    # Using the nominal period assumes the answer: at 3000 ppm the cumulative
    # drift across a picture exceeds a quarter of a line, so the later pulses
    # get the wrong index and are then thrown out as outliers — the fit fails
    # precisely when there is something worth measuring.
    d = np.diff(pulses)
    d = d[d > mode.line_ms * 0.5]
    period = float(np.median(d)) if d.size else mode.line_ms
    if not (mode.line_ms * 0.8 < period < mode.line_ms * 1.2):
        period = mode.line_ms
    # Absolute line numbers, but SPACED by the observed period: rebasing on
    # one pulse keeps large slants from mis-indexing the tail, and adding
    # that pulse's own line number back keeps the intercept meaning "time of
    # line zero" rather than "time of whichever pulse we happened to see".
    #
    # The anchor is CHOSEN, not assumed to be pulses[0]. A single off-grid
    # pulse at the front — a Scottie leading sync, a VIS stop-bit remnant,
    # another station's tail, a noise burst — used to become the origin, and
    # then all 256 real pulses measured a fixed fraction of a line away from
    # it and were discarded as outliers. Auto-slant returned a silent zero
    # for every Scottie picture we ever received. Trying a handful of
    # candidates and keeping whichever explains the most pulses costs
    # nothing and cannot be poisoned by what happens to arrive first.
    # Line numbers must be counted from the start of a LINE, not from the
    # pulse. Scottie's sync sits 284 ms into its 428 ms line, so dividing the
    # pulse time by the period rounded line 0 up to line 1 and every later
    # line with it. The slope was unaffected, which is why nothing caught it,
    # but the intercept came out a whole line early — and the intercept is
    # what the SLANT slider and the free-run fallback both steer by.
    sync_off = sync_center_offset_ms(mode)
    best_keep = None
    for a in range(min(8, pulses.size)):
        base_a = np.round((pulses[a] - sync_off) / period)
        idx_a = base_a + np.round((pulses - pulses[a]) / period)
        keep_a = np.abs(pulses - (pulses[a] + (idx_a - base_a) * period)) < period * 0.25
        if best_keep is None or keep_a.sum() > best_keep[0]:
            best_keep = (int(keep_a.sum()), idx_a, keep_a)
    _, idx, keep = best_keep
    idx, t = idx[keep], pulses[keep]
    if t.size < 8:
        return f
    _, uniq = np.unique(idx, return_index=True)
    idx, t = idx[uniq], t[uniq]
    if t.size < 8:
        return f

    for _ in range(3):
        A = np.vstack([idx, np.ones_like(idx)]).T
        (slope, off), *_ = np.linalg.lstsq(A, t, rcond=None)
        resid = t - (slope * idx + off)
        rms = float(np.sqrt(np.mean(resid ** 2)))
        if rms <= 0.5 or t.size <= 8:
            break
        keep = np.abs(resid) <= 2.5 * rms
        if keep.all() or keep.sum() < 8:
            break
        idx, t = idx[keep], t[keep]

    slant = float(slope / mode.line_ms - 1.0)
    if abs(slant) > max_ppm / 1e6 * 1000:       # sanity, not a real limit
        return f
    f.slant = slant
    f.shift_ms = float(off)
    f.rms_ms = rms
    # A fit that scatters by a large fraction of a pixel is worse than none.
    f.ok = rms < max(0.5, mode.pixel_ms * 4)
    return f


# ── one reception ────────────────────────────────────────────────────────

@dataclass
class Reception:
    """A received (or in-progress) picture, and the trace it came from."""

    mode: SSTVMode
    start: int
    rate: int = DEFAULT_RATE
    trace: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    slant: float = 0.0
    shift_ms: float = 0.0
    auto: bool = True

    @property
    def total_samples(self) -> int:
        lead = self.mode.sync_ms if self.mode.family == "scottie" else 0.0
        return int((lead + self.mode.lines_sent * self.mode.line_ms)
                   / 1000.0 * self.rate)

    @property
    def lines_complete(self) -> int:
        have = max(0, len(self.trace) - self.start)
        per = self.mode.line_ms / 1000.0 * self.rate * (1.0 + self.slant)
        # 2% of a line of grace. VIS detection lands a few samples late and
        # senders trim the tail, so demanding the very last sample of the very
        # last line would report almost every real transmission as unfinished.
        return int(min(self.mode.lines_sent, (have + per * 0.02) // max(1.0, per)))

    @property
    def progress(self) -> float:
        return min(1.0, self.lines_complete / max(1, self.mode.lines_sent))

    @property
    def complete(self) -> bool:
        return self.lines_complete >= self.mode.lines_sent

    def autoslant(self) -> SlantFit:
        f = fit_slant(find_sync_pulses(self.trace, self.mode, self.start, self.rate),
                      self.mode)
        if f.ok:
            self.slant = f.slant
            # Fitted intercept is the pulse CENTRE of line zero. Convert to a
            # line-start shift. Scottie is mid-line; the others open with sync.
            self.shift_ms = f.shift_ms - sync_center_offset_ms(self.mode)
        return f

    def image(self, partial: bool = False) -> np.ndarray:
        return render(self.trace, self.mode, self.start, self.rate,
                      slant=self.slant, shift_ms=self.shift_ms,
                      lines=self.lines_complete if partial else None)

    def snr_estimate(self) -> float:
        """Crude but honest: how tightly the sync pulses sit on 1200 Hz."""
        seg = self.trace[self.start:self.start + self.total_samples]
        if seg.size < 1000:
            return 0.0
        sync = seg[seg < FREQ_SYNC + 120.0]
        if sync.size < 100:
            return 0.0
        spread = float(np.std(sync))
        return float(max(0.0, min(40.0, 20.0 * np.log10(800.0 / max(spread, 1.0)))))


def decode_audio(audio: np.ndarray, rate: int = DEFAULT_RATE,
                 autoslant: bool = True) -> Optional[Reception]:
    """Whole-file decode. The live receiver uses the same pieces incrementally."""
    trace = demodulate(audio, rate)
    vis = find_vis(trace, rate, audio=np.asarray(audio, dtype=np.float32))
    if vis is None:
        return None
    off = float(vis.offset_hz or 0.0)
    if abs(off) >= 1.0:
        trace = trace - np.float32(off)
    start = vis.start + lead_skip(trace, vis.mode, vis.start, rate)
    r = Reception(mode=vis.mode, start=start, rate=rate, trace=trace)
    if autoslant:
        r.autoslant()
    return r
