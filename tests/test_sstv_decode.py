"""SSTV receive.

The headline tests decode audio produced by **PySSTV**, not by our own
encoder. Decoding our own output proves the two halves agree with each other
and nothing more — see feedback_roundtrip_tests_hide_ignored_fields. A couple
of self-decode tests are kept, clearly labelled, purely as diagnostics.
"""

import numpy as np
import pytest

from cyberrig.sstv.decode import (
    FMDemodulator,
    Reception,
    decode_audio,
    demodulate,
    find_sync_pulses,
    find_vis,
    fit_slant,
    freq_to_level,
    render,
)
from cyberrig.sstv.encode import ToneRenderer, encode
from cyberrig.sstv.modes import UI_ORDER, get_mode, vis_header
from cyberrig.sstv.testcard import test_card as make_card

RATE = 48000


def tone(hz, ms, rate=RATE):
    return ToneRenderer(rate).feed([(hz, ms)]).astype(np.float64)


# ── demodulator ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("hz", [1200.0, 1500.0, 1900.0, 2300.0])
def test_a_steady_tone_demodulates_to_its_own_frequency(hz):
    tr = demodulate(tone(hz, 200))
    mid = tr[len(tr) // 4: -len(tr) // 4]
    assert float(np.median(mid)) == pytest.approx(hz, abs=2.0)


def test_chunked_and_whole_demodulation_agree():
    """A live stream and a file must decode to the same picture."""
    sig = ToneRenderer(RATE).feed(
        [(1900.0, 300.0), (1200.0, 10.0), (1900.0, 300.0), (2300.0, 120.0)]
    ).astype(np.float64)
    whole = demodulate(sig)
    d = FMDemodulator(RATE)
    parts = [d.feed(sig[i:i + 997]) for i in range(0, len(sig), 997)]
    parts.append(d.flush())
    chunked = np.concatenate(parts)
    assert chunked.size == whole.size
    assert np.abs(chunked - whole).max() < 1.0


def test_block_seams_do_not_glitch():
    """One bad sample per seam was enough to split a VIS leader in two."""
    tr = demodulate(tone(1900.0, 3000))
    body = tr[2000:-2000]
    assert np.abs(body - 1900.0).max() < 25.0, "seam ringing is back"


def test_demodulator_refuses_an_overlap_it_cannot_honour():
    with pytest.raises(ValueError):
        FMDemodulator(RATE, block=1024, overlap=1024)


def test_empty_input_is_harmless():
    d = FMDemodulator(RATE)
    assert d.feed(np.zeros(0)).size == 0
    assert d.flush().size == 0


# ── VIS ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", UI_ORDER)
def test_every_mode_is_identified_from_its_header(name):
    m = get_mode(name)
    sig = ToneRenderer(RATE).feed(vis_header(m.vis_code) + [(1500.0, 200.0)])
    v = find_vis(demodulate(sig.astype(np.float64)))
    assert v is not None, f"{name} header not found"
    assert v.mode.name == m.name
    assert v.code == m.vis_code


def test_the_image_starts_where_the_header_ends():
    m = get_mode("Martin 1")
    sig = ToneRenderer(RATE).feed(vis_header(m.vis_code) + [(1500.0, 100.0)])
    v = find_vis(demodulate(sig.astype(np.float64)))
    assert v.start == pytest.approx(m.vis_ms / 1000.0 * RATE, abs=0.004 * RATE)


def test_silence_is_not_a_header():
    assert find_vis(demodulate(np.zeros(RATE))) is None


def test_noise_is_not_a_header():
    rng = np.random.RandomState(7)
    assert find_vis(demodulate(rng.normal(0, 0.3, RATE * 3))) is None


def test_a_bare_carrier_is_not_a_header():
    assert find_vis(demodulate(tone(1900.0, 3000))) is None


def test_a_corrupt_parity_bit_is_rejected():
    m = get_mode("Scottie 1")
    segs = vis_header(m.vis_code)
    segs[11] = (1100.0 if segs[11][0] == 1300.0 else 1300.0, segs[11][1])
    v = find_vis(demodulate(ToneRenderer(RATE).feed(segs).astype(np.float64)))
    assert v is None


def test_an_unknown_vis_code_is_reported_as_undecodable_not_guessed():
    # 0x25 is a real VIS code (Wraase SC2-60) that we do not implement.
    sig = ToneRenderer(RATE).feed(vis_header(0x25) + [(1500.0, 100.0)])
    assert find_vis(demodulate(sig.astype(np.float64))) is None


def _ssb_shift(x, hz):
    """Same analytic shift the RX tuning tests use — real mistuning, not AM."""
    n = len(x)
    X = np.fft.rfft(x.astype(np.float64))
    z = np.zeros(n, dtype=np.complex128)
    z[:len(X)] = X
    z[1:len(X) - (1 if n % 2 == 0 else 0)] *= 2.0
    analytic = np.fft.ifft(z)
    t = np.arange(n) / float(RATE)
    return np.real(analytic * np.exp(2j * np.pi * hz * t)).astype(np.float64)


@pytest.mark.parametrize("off", [60, 80, 100, 120, -80, -120])
def test_vis_still_locks_when_the_dial_is_off(off):
    """Live 20 m copy often sits 60-100 Hz high; ±70 used to be a hard cliff."""
    m = get_mode("Scottie 1")
    sig = ToneRenderer(RATE).feed(vis_header(m.vis_code) + [(1500.0, 200.0)])
    v = find_vis(demodulate(_ssb_shift(sig, off)))
    assert v is not None, f"VIS missed at {off:+d} Hz"
    assert v.mode.name == m.name
    # Even a lock inside the nominal ±70 gate must report the real mistune so
    # the receiver can centre black/white before rendering.
    assert v.offset_hz == pytest.approx(off, abs=30)


# ── cross-implementation decode ──────────────────────────────────────────

pysstv_color = pytest.importorskip("pysstv.color")
from PIL import Image  # noqa: E402

CROSS = [
    ("Scottie 1", "ScottieS1", 12.0),
    ("Scottie 2", "ScottieS2", 14.0),
    ("Martin 1", "MartinM1", 4.0),
    ("Martin 2", "MartinM2", 4.0),
    ("Robot 36", "Robot36", 8.0),
    ("PD90", "PD90", 5.0),
    ("PD120", "PD120", 5.0),
]


def pysstv_audio(theirname, mode):
    cls = getattr(pysstv_color, theirname)
    img = make_card(mode.width, mode.height)
    s = cls(Image.fromarray(img), RATE, bits=16)
    return np.array(list(s.gen_samples()), dtype=np.float64) / 32768.0, img


def mean_err(a, b, margin=6):
    a = a[margin:-margin, margin:-margin].astype(np.float64)
    b = b[margin:-margin, margin:-margin].astype(np.float64)
    return float(np.abs(a - b).mean())


@pytest.mark.parametrize("ours,theirname,tol", CROSS)
def test_decodes_a_picture_another_implementation_transmitted(ours, theirname, tol):
    m = get_mode(ours)
    audio, src = pysstv_audio(theirname, m)
    r = decode_audio(audio)
    assert r is not None, "no VIS found"
    assert r.mode.name == m.name
    err = mean_err(src, r.image())
    assert err < tol, f"{ours} decoded {err:.1f} levels off"


def test_scottie_error_is_pysstvs_layout_not_ours():
    """Scottie decodes several levels worse from PySSTV than from ourselves.

    That is expected and understood: PySSTV puts a separator on both sides of
    each channel and shortens the scan to compensate, so its pixel rate is
    about 1% from the spec's. This test exists so that if Scottie ever gets
    WORSE, nobody rediscovers the reason from scratch.
    """
    m = get_mode("Scottie 1")
    src = make_card(m.width, m.height)
    theirs = decode_audio(pysstv_audio("ScottieS1", m)[0]).image()
    ours = decode_audio(encode(src, m, fit="stretch").astype(np.float64)).image()
    assert mean_err(src, ours) < mean_err(src, theirs)
    assert mean_err(src, ours) < 5.0


# ── slant ────────────────────────────────────────────────────────────────

def resample(x, factor):
    """Stand in for a sender whose sample clock is off by `factor`."""
    n = int(round(len(x) * factor))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)


@pytest.mark.parametrize("ppm", [-3000, -800, 800, 3000])
@pytest.mark.parametrize("ours,theirs", [("Martin 1", "MartinM1"),
                                         ("Scottie 1", "ScottieS1")])
def test_a_clock_error_is_measured_and_corrected(ours, theirs, ppm):
    """Scottie is here because it used to fail every time and nobody saw it.

    Only Martin was covered, and Scottie opens with a lone sync pulse that
    does not sit on the line grid — see fit_slant.
    """
    m = get_mode(ours)
    audio, src = pysstv_audio(theirs, m)
    skewed = resample(audio, 1.0 + ppm / 1e6)
    r = decode_audio(skewed)
    assert r is not None
    assert r.slant * 1e6 == pytest.approx(ppm, abs=250)
    assert mean_err(src, r.image()) < 14.0


def test_a_stray_first_pulse_does_not_poison_the_whole_fit():
    """One off-grid pulse used to reject all 256 good ones.

    The fit anchored on pulses[0]; a leading Scottie sync, another station's
    tail, or a noise burst landing first made every real pulse look like an
    outlier and auto-slant silently returned zero.
    """
    m = get_mode("Martin 1")
    good = 300.0 + np.arange(60) * m.line_ms * 1.001
    # Two thirds of a line early: far enough off the grid that anchoring on
    # it makes every real pulse look like an outlier. This is exactly where
    # Scottie's leftover VIS stop bit lands.
    stray = good[0] - m.line_ms * 0.67
    f = fit_slant(np.concatenate([[stray], good]), m)
    assert f.ok
    assert f.slant * 1e6 == pytest.approx(1000, abs=100)


@pytest.mark.parametrize("ours,theirs", [("Martin 1", "MartinM1"),
                                         ("Scottie 1", "ScottieS1")])
def test_the_fitted_line_phase_lands_on_line_zero(ours, theirs):
    """Scottie's sync sits mid-line, so line numbering must start from the
    line, not the pulse. It used to round line 0 up to line 1 and hand back
    an intercept a whole line early — invisible while auto-slant never
    succeeded on Scottie at all, and wrong the moment it did."""
    m = get_mode(ours)
    audio, _src = pysstv_audio(theirs, m)
    r = decode_audio(resample(audio, 1.0012))
    assert r.slant * 1e6 == pytest.approx(1200, abs=250)
    assert abs(r.shift_ms) < m.line_ms * 0.1


@pytest.mark.parametrize("ours,theirs", [("Martin 1", "MartinM1"),
                                         ("Scottie 1", "ScottieS1")])
def test_the_slant_slider_alone_straightens_a_picture(ours, theirs):
    """No per-line snap: this is the path the SLANT slider drives, and a
    wrong intercept shows up here even when sync-snap hides it."""
    from cyberrig.sstv.decode import render
    m = get_mode(ours)
    audio, src = pysstv_audio(theirs, m)
    r = decode_audio(resample(audio, 1.0012))
    got = render(r.trace, m, r.start, r.rate, slant=r.slant,
                 shift_ms=r.shift_ms, line_sync=False)
    assert mean_err(src, got) < 10.0


def test_the_live_receiver_and_the_file_decoder_pick_the_same_start():
    """They used to disagree, and only the live one was wrong.

    rx.py tested `mean |f - 1200| < 200` over a window that is half stop bit
    and half video — never true — so Scottie kept a stray pulse the file
    decoder skipped, and auto-slant failed on air while every test passed.
    """
    from cyberrig.sstv.decode import lead_skip
    m = get_mode("Scottie 1")
    audio, _src = pysstv_audio("ScottieS1", m)
    tr = demodulate(audio)
    # Same inputs as decode_audio: the spectral header path needs the audio,
    # and comparing it against the trace-only path compares two methods
    # rather than the two callers this test is about.
    v = find_vis(tr, RATE, audio=np.asarray(audio, dtype=np.float32))
    assert v is not None
    r = decode_audio(audio)
    assert r.start == v.start + lead_skip(tr, m, v.start, RATE)


def test_per_line_sync_recovers_a_clock_error_without_autoslant():
    """Live HF failed when free-run timing drifted; sync-snap is the fix."""
    m = get_mode("Martin 1")
    audio, src = pysstv_audio("MartinM1", m)
    r = decode_audio(resample(audio, 1.003), autoslant=False)
    # No global slant applied — per-line sync alone must keep the picture.
    assert abs(r.slant) < 1e-6
    assert mean_err(src, r.image()) < 10.0


def test_slant_is_re_applied_from_the_trace_not_re_received():
    """The whole reason the trace is kept: fix a picture already received."""
    from cyberrig.sstv.decode import render
    m = get_mode("Martin 1")
    audio, src = pysstv_audio("MartinM1", m)
    r = decode_audio(resample(audio, 1.002), autoslant=False)
    # Force free-run (no pulse snap) to show global slant still matters there.
    bad = mean_err(src, render(r.trace, m, r.start, r.rate, slant=0.0,
                               shift_ms=0.0, line_sync=False))
    r.autoslant()                       # same trace, no new audio
    good = mean_err(src, r.image())
    assert good < bad / 4


def test_a_fit_with_too_few_pulses_is_refused_rather_than_guessed():
    m = get_mode("Martin 1")
    f = fit_slant(np.array([0.0, 446.4, 892.9]), m)
    assert not f.ok
    assert f.slant == 0.0


def test_sync_pulses_are_found_one_per_line():
    m = get_mode("Martin 1")
    audio, _src = pysstv_audio("MartinM1", m)
    r = decode_audio(audio)
    p = find_sync_pulses(r.trace, m, r.start)
    assert abs(len(p) - m.lines_sent) <= 3


# ── reception state ──────────────────────────────────────────────────────

def test_progress_tracks_a_transmission_arriving():
    m = get_mode("Martin 1")
    audio, _src = pysstv_audio("MartinM1", m)
    half = audio[: len(audio) // 2]
    r = decode_audio(half)
    assert r is not None
    assert 0.3 < r.progress < 0.7
    assert not r.complete


def test_a_partial_picture_renders_what_arrived():
    m = get_mode("Martin 1")
    audio, src = pysstv_audio("MartinM1", m)
    r = decode_audio(audio[: len(audio) // 2])
    img = r.image(partial=True)
    assert img.shape == (m.height, m.width, 3)
    done = r.lines_complete
    assert mean_err(src[:done - 4], img[:done - 4]) < 6.0
    assert img[done + 8:].max() == 0, "lines that never arrived must stay blank"


def test_a_completed_reception_says_so():
    m = get_mode("Robot 36")
    audio, _src = pysstv_audio("Robot36", m)
    r = decode_audio(audio)
    assert r.complete
    assert r.progress == 1.0


def test_levels_map_back_from_the_standard_tones():
    lv = freq_to_level(np.array([1500.0, 1900.0, 2300.0, 900.0, 2900.0]))
    assert lv[0] == pytest.approx(0.0)
    assert lv[1] == pytest.approx(127.5, abs=0.6)
    assert lv[2] == pytest.approx(255.0)
    assert lv[3] == 0.0 and lv[4] == 255.0


def test_render_of_an_empty_trace_does_not_explode():
    m = get_mode("Scottie 1")
    img = render(np.zeros(10, dtype=np.float32), m, 0)
    assert img.shape == (m.height, m.width, 3)


# ── the sync snap window ─────────────────────────────────────────────────
#
# `find_sync_pulses` threshold-detects 1200 Hz runs on the raw frequency
# trace, so at HF signal levels it reports far more pulses than there are
# lines: dark video sits at 1500 Hz and noise walks it down through the sync
# window for milliseconds at a time. A live Martin 1 reception on 2026-08-29
# gave 713 pulses for 256 lines, and a Scottie 1 the same afternoon gave 140
# — too many and too few in the same conditions.
#
# That is a decoder problem worth fixing on its own. What turned it into
# shredded pictures was `line_origins_ms` believing any of those pulses
# within +-40% of a line, which is +-178 ms on Martin 1, or +-390 pixels of a
# 320-pixel-wide picture. Measured on that reception: 113 px RMS displacement
# between adjacent lines, worst line 633 px out, 110 of 255 lines past a
# pixel. The same audio at +-2% gives 1.7 px RMS.

def _plant(tr, mode, centre_ms, rate=RATE):
    half = mode.sync_ms * rate / 2000.0
    a = int(centre_ms * rate / 1000.0 - half)
    b = int(centre_ms * rate / 1000.0 + half)
    if 0 <= a < b <= len(tr):
        tr[a:b] = 1200.0


def _black_trace(mode, n_lines, rate=RATE):
    """Pure black video — 1500 Hz — with no sync pulses in it yet."""
    total = int((n_lines + 2) * mode.line_ms * rate / 1000.0)
    return np.full(total, 1500.0, dtype=np.float32)


def test_a_false_pulse_train_cannot_capture_the_whole_picture():
    """The +-40% window locked onto the wrong train from line zero.

    With a false 1200 Hz run a third of a line in and the real pulse missing
    on every third line, the old window snapped line 0 to the false pulse and
    then tracked it perfectly for 256 lines: a picture displaced sideways by
    a third of its width, with no per-line jitter to give it away.
    """
    from cyberrig.sstv.decode import line_origins_ms, sync_center_offset_ms
    m = get_mode("Martin 1")
    n = m.lines_sent
    tr = _black_trace(m, n)
    for li in range(n):
        if li % 3:                       # a third of the real pulses are lost
            _plant(tr, m, li * m.line_ms + sync_center_offset_ms(m))
        _plant(tr, m, li * m.line_ms + m.line_ms * 0.33)
    org = line_origins_ms(tr, m, 0, RATE, n)
    truth = np.arange(n, dtype=np.float64) * m.line_ms
    assert float(np.abs(org - truth).max()) < m.pixel_ms


def test_scattered_false_pulses_do_not_shred_the_picture():
    """The live failure mode: every line yanked somewhere different."""
    from cyberrig.sstv.decode import (
        SYNC_WINDOW_FRAC, line_origins_ms, sync_center_offset_ms,
    )
    m = get_mode("Scottie 1")
    n = m.lines_sent
    tr = _black_trace(m, n)
    rng = np.random.default_rng(20260829)
    for li in range(n):
        if li % 3:
            _plant(tr, m, li * m.line_ms + sync_center_offset_ms(m))
        for off in rng.uniform(0.05, 0.95, size=2):
            _plant(tr, m, (li + off) * m.line_ms)
    assert find_sync_pulses(tr, m, 0, RATE).size > 2 * n
    org = line_origins_ms(tr, m, 0, RATE, n)
    err = np.abs(org - np.arange(n, dtype=np.float64) * m.line_ms)
    # A false pulse INSIDE the window is indistinguishable from a real one and
    # always will be. What the window buys is a bound on the damage: no line
    # can be displaced further than the window itself. At +-40% this same
    # fixture puts the worst line 1371 px out with a 37 px median; the bound
    # is the entire fix.
    assert float(err.max()) <= m.line_ms * SYNC_WINDOW_FRAC + 1.0
    assert float(np.median(err)) < m.pixel_ms
    assert int((err > m.pixel_ms).sum()) < n // 4


def test_the_snap_window_still_tracks_a_real_clock_error():
    """The window is bounded below by the drift it has to follow.

    The expectation advances one nominal period per line, so a sender fast by
    more than SYNC_WINDOW_FRAC of a line each line falls out of the window.
    fit_slant refuses anything past 20,000 ppm, so the window must cover that.
    """
    from cyberrig.sstv.decode import (
        SYNC_WINDOW_FRAC, line_origins_ms, sync_center_offset_ms,
    )
    m = get_mode("Martin 1")
    assert SYNC_WINDOW_FRAC >= 0.02

    ppm = 15000e-6                       # inside fit_slant's own sanity limit
    real_period = m.line_ms * (1.0 + ppm)
    tr = np.full(int((m.lines_sent + 2) * real_period * RATE / 1000.0),
                 1500.0, dtype=np.float32)
    for li in range(m.lines_sent):
        _plant(tr, m, li * real_period + sync_center_offset_ms(m))

    org = line_origins_ms(tr, m, 0, RATE, m.lines_sent)
    # Tracked, not free-run: the last line sits where the sender put it, and
    # the WHOLE accumulated error is followed rather than half of it — which
    # is what blending the tracked origin with the nominal grid used to do.
    assert org[-1] == pytest.approx((m.lines_sent - 1) * real_period, abs=1.0)


# ── the AFC the whole trace is shifted by ────────────────────────────────
#
# `offset_hz` is subtracted from the entire frequency trace, so it moves the
# video map: 1500 Hz is black, 2300 Hz is white, and 170 Hz of error is a
# fifth of that span rendered as a brightness and colour cast.
#
# It used to be measured as the median of the raw trace over the leader —
# a wideband read of a narrowband quantity, and the same mistake the leader
# SEARCH was rebuilt to avoid. The window is right; the trace over it is not.
# At the SNR pictures arrive at only 11-17% of those trace samples are within
# 1900+-70 Hz, so the median measures the 900-2600 Hz passband instead of the
# leader, and that passband centres low. Across 16 live receptions
# it reported a median -75 Hz where -7.6 Hz was actually present, worst case
# -172 Hz reported against a true +13 Hz — and it got worse as the signal got
# weaker, which is backwards for something meant to help a weak signal.

def _vis_audio(mode, offset_hz=0.0, rate=RATE, noise=0.0, seed=7):
    """A VIS header at a known frequency offset, then a little video."""
    from cyberrig.sstv.modes import vis_header
    r = ToneRenderer(rate)
    plan = [(hz + offset_hz, ms) for hz, ms in vis_header(mode.vis_code)]
    plan += [(1500.0 + offset_hz, 200.0)]
    a = r.feed(plan).astype(np.float64)
    if noise:
        rng = np.random.default_rng(seed)
        n = rng.standard_normal(a.size)
        N = np.fft.rfft(n); f = np.fft.rfftfreq(a.size, 1.0 / rate)
        N[(f < 300) | (f > 2700)] = 0
        n = np.fft.irfft(N, a.size)
        a = a + n * np.sqrt((a ** 2).mean() / (n ** 2).mean()) * (10 ** (-noise / 20.0))
    return a


@pytest.mark.parametrize("off", [0.0, -60.0, +45.0])
def test_the_reported_afc_is_the_offset_that_is_actually_there(off):
    m = get_mode("Martin 1")
    a = _vis_audio(m, off)
    v = find_vis(demodulate(a, RATE), RATE, audio=a)
    assert v is not None and v.mode.name == m.name
    assert v.offset_hz == pytest.approx(off, abs=8.0)


@pytest.mark.parametrize("snr", [20.0, 10.0])
def test_a_weak_header_does_not_invent_an_offset(snr):
    """The bias grew with noise, which is the opposite of what is wanted."""
    m = get_mode("Martin 1")
    a = _vis_audio(m, 0.0, noise=snr)
    v = find_vis(demodulate(a, RATE), RATE, audio=a)
    assert v is not None
    assert abs(v.offset_hz) < 25.0


def test_the_offset_is_measured_narrowband_not_off_the_trace():
    """Guard the actual regression.

    The leader window is found correctly — the FFT says the tone is there and
    dominant. The trap is that the instantaneous-frequency TRACE over that
    same window is mostly noise excursions at the SNR pictures arrive at:
    measured on live receptions, only 11-17% of the trace samples inside a
    correctly-located leader lie within 1900+-70 Hz. So a median over it is a
    median of the passband, not of the leader, and the 900-2600 Hz passband
    centres well below 1900. That is where -148 and -243 Hz came from.
    """
    from cyberrig.sstv.decode import leader_offset_hz
    rate = RATE
    lead = ToneRenderer(rate).feed([(1900.0, 600.0)]).astype(np.float64)
    rng = np.random.default_rng(11)
    n = rng.standard_normal(lead.size)
    N = np.fft.rfft(n); f = np.fft.rfftfreq(lead.size, 1.0 / rate)
    N[(f < 300) | (f > 2700)] = 0
    n = np.fft.irfft(N, lead.size)
    # 3 dB: the tone still dominates the spectrum, the trace does not know it.
    audio = lead + n * np.sqrt((lead ** 2).mean() / (n ** 2).mean()) * 10 ** (-3 / 20)
    trace = demodulate(audio, rate)

    near = float(((trace > 1830) & (trace < 1970)).mean())
    assert near < 0.5, "fixture is not noisy enough to exercise the bug"
    assert float(np.median(trace)) - 1900.0 < -20.0     # the old way is biased low
    got = leader_offset_hz(audio, rate, 0, len(audio), fallback=0.0)
    assert abs(got) < 15.0                              # the new way is not


def test_an_unmeasurable_leader_keeps_the_coarse_estimate():
    from cyberrig.sstv.decode import leader_offset_hz
    assert leader_offset_hz(np.zeros(10, dtype=np.float64), RATE, 0, 10,
                            fallback=-33.0) == -33.0
    silence = np.zeros(RATE // 2, dtype=np.float64)
    assert leader_offset_hz(silence, RATE, 0, silence.size,
                            fallback=-12.0) == -12.0


# A transmitter/audio timing STEP is not jitter and not clock error, and the
# tight snap window cannot follow one: the tracker builds each expectation
# from the previous line, so once the train moves further than the window the
# phase is stale for every remaining line. Measured on corpus recording
# 20260826-163650 — 20.3 dB, the cleanest one there is — the sync train jumps
# 164.56 ms during a 13 dB fade at 58 s and is perfectly coherent again after
# it (MAD 0.12 ms). Before REACQUIRE_AFTER, that one step cost the bottom
# half of the picture; every corpus decode had the same shape.

def test_a_mid_picture_timing_step_is_re_acquired():
    from cyberrig.sstv.decode import line_origins_ms, sync_center_offset_ms
    m = get_mode("Martin 1")
    n = m.lines_sent
    step_at, step_ms = 130, 164.56          # the measured off-air numbers
    tr = _black_trace(m, n + 2)
    truth = np.arange(n, dtype=np.float64) * m.line_ms
    truth[step_at:] += step_ms
    for li in range(n):
        _plant(tr, m, truth[li] + sync_center_offset_ms(m))

    org = line_origins_ms(tr, m, 0, RATE, n)
    # The lines inside the step are lost — nothing can recover those — but the
    # picture below it must come back, not free-run to the bottom.
    settled = np.abs(org[step_at + 8:] - truth[step_at + 8:])
    assert float(settled.max()) < m.pixel_ms, f"worst {settled.max():.2f} ms"
    assert float(np.abs(org[:step_at] - truth[:step_at]).max()) < m.pixel_ms


def test_a_lone_spur_during_a_dropout_does_not_steal_the_phase():
    """Re-acquire must need corroboration, or it chases noise.

    Holding a stale phase through a gap is recoverable; re-anchoring the rest
    of the picture onto a single noise spike is not.
    """
    from cyberrig.sstv.decode import line_origins_ms, sync_center_offset_ms
    m = get_mode("Martin 1")
    n = m.lines_sent
    tr = _black_trace(m, n + 2)
    dead = range(60, 70)                    # ten lines of lost sync
    for li in range(n):
        if li not in dead:
            _plant(tr, m, li * m.line_ms + sync_center_offset_ms(m))
    # one spur, a third of a line off, in the middle of the dropout
    _plant(tr, m, 65 * m.line_ms + m.line_ms * 0.33 + sync_center_offset_ms(m))

    org = line_origins_ms(tr, m, 0, RATE, n)
    truth = np.arange(n, dtype=np.float64) * m.line_ms
    assert float(np.abs(org[70:] - truth[70:]).max()) < m.pixel_ms


# `_spectral_leaders` ranks candidates by leader strength, not by time, so a
# steady tone inside somebody's video can outrank the real header at the top
# of the file — and `find_vis` returned the first hit. Corpus recording
# 20260826-170846 is 115.7 s of Martin 1 that was read as Robot 36, a
# 36-second mode, from a false leader 19.9 s in. The true header was the 8th
# candidate, at 0.448 s. The sync train says which one is right: 94.8% of the
# gaps in that file match Martin 1's line, 0% match Robot 36's.

def test_a_sync_train_rejects_a_mode_it_cannot_belong_to():
    from cyberrig.sstv.decode import _sync_supports_mode, sync_center_offset_ms
    m = get_mode("Martin 1")
    n = m.lines_sent
    tr = _black_trace(m, n)
    for li in range(n):
        _plant(tr, m, li * m.line_ms + sync_center_offset_ms(m))

    assert _sync_supports_mode(tr, m, 0, RATE)
    # Robot 36 lines are 150 ms; a Martin 1 train cannot fill that grid.
    assert not _sync_supports_mode(tr, get_mode("Robot 36"), 0, RATE)


def test_a_weak_header_still_starts_a_picture():
    """The mode check may only prefer one candidate over another.

    If nothing corroborates, find_vis must still return what it found: a
    doubtful picture beats no picture, and that trade is the operator's.
    """
    from cyberrig.sstv.decode import _sync_supports_mode
    m = get_mode("Martin 1")
    # Sync pulses on only a third of the lines — a badly faded reception.
    tr = _black_trace(m, m.lines_sent)
    from cyberrig.sstv.decode import sync_center_offset_ms
    for li in range(0, m.lines_sent, 3):
        _plant(tr, m, li * m.line_ms + sync_center_offset_ms(m))
    assert _sync_supports_mode(tr, m, 0, RATE)
