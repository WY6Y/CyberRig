"""The SSTV encoder, checked against the published mode specs.

Deliberately does NOT decode with our own decoder — see
feedback_roundtrip_tests_hide_ignored_fields. Everything here either compares
against numbers published independently of this code, or measures the audio
with an FFT, which shares no code with the NCO that generated it.
"""

import numpy as np
import pytest

from cyberrig.sstv.encode import DEFAULT_RATE, ToneRenderer, encode, encode_chunks, \
    encode_to_wav, prepare_image
from cyberrig.sstv.modes import (
    FREQ_BLACK,
    FREQ_SYNC,
    FREQ_WHITE,
    MODES,
    UI_ORDER,
    get_mode,
    level_to_freq,
    mode_list,
    vis_header,
)

RATE = DEFAULT_RATE


def tone_hz(sig, t0, t1, rate=RATE):
    """Dominant frequency in [t0, t1) seconds, by FFT with parabolic interpolation."""
    a = np.asarray(sig[int(t0 * rate):int(t1 * rate)], dtype=np.float64)
    assert a.size > 32, "window too short to measure"
    w = a * np.hanning(a.size)
    spec = np.abs(np.fft.rfft(w, n=1 << 15))
    k = int(np.argmax(spec))
    if 0 < k < len(spec) - 1:
        al, b, g = spec[k - 1], spec[k], spec[k + 1]
        k = k + 0.5 * (al - g) / (al - 2 * b + g)
    return k * rate / (1 << 15)


def decode_vis(sig, rate=RATE):
    """Read the VIS code back out of rendered audio, independent of vis_header()."""
    t = 0.300 + 0.010 + 0.300 + 0.030      # leader, break, leader, start bit
    bits = []
    for i in range(8):
        f = tone_hz(sig, t + i * 0.030 + 0.005, t + i * 0.030 + 0.025, rate)
        bits.append(1 if abs(f - 1100) < abs(f - 1300) else 0)
    code = sum(b << i for i, b in enumerate(bits[:7]))
    return code, bits[7]


# Image-portion durations (excluding the VIS header) as published for these
# modes. If a line got assembled with a separator missing, these drift.
PUBLISHED_BODY_S = {
    "Scottie 1": 109.60,
    "Scottie 2": 71.09,
    "Scottie DX": 268.88,
    "Martin 1": 114.29,
    "Martin 2": 58.06,
    "Robot 36": 36.00,
    "PD90": 89.99,
    "PD120": 126.10,
    "PD180": 187.05,
}


@pytest.mark.parametrize("name", UI_ORDER)
def test_line_assembly_matches_published_duration(name):
    m = get_mode(name)
    lead = m.sync_ms if m.family == "scottie" else 0.0
    body = (lead + m.lines_sent * m.line_ms) / 1000.0
    assert body == pytest.approx(PUBLISHED_BODY_S[name], abs=0.05)


@pytest.mark.parametrize("name", UI_ORDER)
def test_vis_code_survives_the_nco(name):
    m = get_mode(name)
    sig = ToneRenderer(RATE).feed(vis_header(m.vis_code))
    code, parity = decode_vis(sig)
    assert code == m.vis_code
    assert parity == bin(m.vis_code & 0x7F).count("1") % 2


def test_vis_uses_even_parity_and_lsb_first():
    # Scottie 1 = 0x3C = 0111100: four ones, so the parity bit is 0.
    segs = vis_header(0x3C)
    bits = [1 if hz == 1100 else 0 for hz, _ in segs[4:12]]
    assert bits == [0, 0, 1, 1, 1, 1, 0, 0]


def test_vis_codes_are_the_registered_ones():
    # Independently published VIS assignments — a typo here is a mode nobody decodes.
    assert get_mode("Scottie 1").vis_code == 0x3C
    assert get_mode("Scottie 2").vis_code == 0x38
    assert get_mode("Scottie DX").vis_code == 0x4C
    assert get_mode("Martin 1").vis_code == 0x2C
    assert get_mode("Martin 2").vis_code == 0x28
    assert get_mode("Robot 36").vis_code == 0x08
    assert get_mode("PD90").vis_code == 0x63
    assert get_mode("PD120").vis_code == 0x5F
    assert get_mode("PD180").vis_code == 0x60


def test_level_maps_black_and_white_to_the_standard_tones():
    assert level_to_freq(0) == pytest.approx(1500.0)
    assert level_to_freq(255) == pytest.approx(2300.0)
    assert level_to_freq(128) == pytest.approx(1901.6, abs=1.0)


def test_black_and_white_images_land_on_1500_and_2300():
    # Scottie 1: VIS 910 ms, 9 ms lead sync, 1.5 ms separator, then green scan.
    g0 = 0.910 + 0.009 + 0.0015
    mid = g0 + 0.069
    black = encode(np.zeros((256, 320, 3), np.uint8), "Scottie 1")
    white = encode(np.full((256, 320, 3), 255, np.uint8), "Scottie 1")
    assert tone_hz(black, mid, mid + 0.02) == pytest.approx(FREQ_BLACK, abs=8)
    assert tone_hz(white, mid, mid + 0.02) == pytest.approx(FREQ_WHITE, abs=8)


def test_scottie_sync_pulse_is_where_the_spec_says():
    m = get_mode("Scottie 1")
    sig = encode(np.zeros((256, 320, 3), np.uint8), m)
    # Leading sync sits between the VIS stop bit and the first separator.
    assert tone_hz(sig, 0.9125, 0.9175) == pytest.approx(FREQ_SYNC, abs=15)
    # In-line sync comes after green and blue, not at the start of the line.
    t = 0.910 + 0.009 + 2 * (0.0015 + 0.13824)
    assert tone_hz(sig, t + 0.002, t + 0.007) == pytest.approx(FREQ_SYNC, abs=15)


def test_timing_does_not_drift_over_a_whole_transmission():
    m = get_mode("Scottie 1")
    sig = encode(np.zeros((256, 320, 3), np.uint8), m)
    # Exact, not approximate: cumulative rounding is the point.
    assert sig.size == round(m.duration_s * RATE)


def test_martin_1_also_lands_exactly():
    m = get_mode("Martin 1")
    sig = encode(np.zeros((256, 320, 3), np.uint8), m)
    assert sig.size == round(m.duration_s * RATE)


def test_chunks_concatenate_to_the_same_signal():
    img = (np.arange(256 * 320 * 3, dtype=np.uint8).reshape(256, 320, 3))
    m = get_mode("Robot 36")
    img = prepare_image(img, m)
    joined = np.concatenate(list(encode_chunks(img, m)))
    whole = encode(img, m)
    assert joined.size == whole.size
    assert np.array_equal(joined, whole)


def test_first_chunk_is_the_vis_header():
    m = get_mode("Martin 1")
    chunks = encode_chunks(np.zeros((256, 320, 3), np.uint8), m)
    first = next(chunks)
    assert first.size == round(m.vis_ms / 1000.0 * RATE)


def test_no_phase_discontinuity_anywhere():
    """A phase jump is an audible click and splatter either side of the signal."""
    sig = encode(np.random.RandomState(0).randint(0, 256, (240, 320, 3), dtype=np.uint8),
                 "Robot 36")
    # Largest legal sample-to-sample step is one period of the highest tone.
    limit = 2.0 * np.sin(np.pi * FREQ_WHITE / RATE) * 1.02
    assert np.abs(np.diff(sig)).max() <= limit


def test_transmission_starts_and_ends_at_silence():
    sig = encode(np.zeros((256, 320, 3), np.uint8), "Scottie 2")
    assert abs(float(sig[0])) < 0.02
    assert abs(float(sig[-1])) < 0.02
    assert abs(sig).max() == pytest.approx(1.0, abs=0.01)


def test_amplitude_scales_the_output():
    sig = encode(np.zeros((256, 320, 3), np.uint8), "Scottie 2", amplitude=0.5)
    assert abs(sig).max() == pytest.approx(0.5, abs=0.01)


def test_every_tone_stays_inside_the_ssb_passband():
    """Nothing may fall outside 1100-2300 Hz, or the 3 kHz filter clips it."""
    for name in UI_ORDER:
        m = get_mode(name)
        img = np.random.RandomState(1).randint(0, 256, (m.height, m.width, 3), dtype=np.uint8)
        for seg in m.segments(img):
            for hz, _ms in seg:
                assert 1100.0 <= hz <= 2300.0, f"{name}: {hz} Hz"
            break  # header only; scan tones are bounded by level_to_freq


def test_prepare_image_cover_crops_to_the_mode_geometry():
    m = get_mode("PD120")
    wide = np.zeros((100, 900, 3), np.uint8)
    out = prepare_image(wide, m)
    assert out.shape == (496, 640, 3)
    assert out.dtype == np.uint8


def test_prepare_image_contain_letterboxes_onto_black():
    m = get_mode("Scottie 1")
    tall = np.full((400, 40, 3), 255, np.uint8)
    out = prepare_image(tall, m, fit="contain")
    assert out.shape == (256, 320, 3)
    assert out[:, 0].max() == 0        # black bar on the left


def test_wrong_size_image_is_refused_not_silently_stretched():
    m = get_mode("Scottie 1")
    with pytest.raises(ValueError):
        list(m.segments(np.zeros((100, 100, 3), np.uint8)))


def test_unknown_mode_raises():
    with pytest.raises(KeyError):
        get_mode("Scottie 9")


def test_mode_aliases_resolve():
    assert get_mode("s1") is get_mode("Scottie 1")
    assert get_mode("SCOTTIE1") is get_mode("Scottie 1")
    assert get_mode("r36") is get_mode("Robot 36")


def test_mode_list_carries_duration_for_the_ui():
    rows = mode_list()
    assert [r["name"] for r in rows] == UI_ORDER
    dx = next(r for r in rows if r["name"] == "Scottie DX")
    assert dx["seconds"] > 265        # the UI must be able to warn about this one
    assert next(r for r in rows if r["name"] == "PD120")["width"] == 640


def test_renderer_is_chunk_invariant():
    segs = [(1500.0, 7.3), (1900.0, 11.1), (2300.0, 5.5), (1200.0, 9.0)]
    whole = ToneRenderer(RATE).feed(segs)
    r = ToneRenderer(RATE)
    split = np.concatenate([r.feed(segs[:2]), r.feed(segs[2:])])
    assert np.array_equal(whole, split)


def test_renderer_does_not_accumulate_rounding_error():
    # 0.4320 ms is Scottie 1's pixel time and is not a whole number of samples.
    segs = [(1700.0, 0.4320)] * 100_000
    n = ToneRenderer(RATE).feed(segs).size
    assert n == round(0.4320 / 1000.0 * 100_000 * RATE)


def test_encode_to_wav_writes_a_real_file(tmp_path):
    import wave

    p = tmp_path / "test.wav"
    m = get_mode("Robot 36")
    r = encode_to_wav(str(p), np.zeros((240, 320, 3), np.uint8), m)
    assert r["ok"]
    with wave.open(str(p)) as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == RATE
        assert w.getnframes() == round(m.duration_s * RATE)


# ── FSK ID ───────────────────────────────────────────────────────────────

def test_fsk_id_lengthens_the_transmission_by_exactly_its_own_length():
    from cyberrig.sstv.modes import fsk_id_ms

    m = get_mode("Robot 36")
    img = np.zeros((240, 320, 3), np.uint8)
    plain = encode(img, m, fit="stretch").size
    withid = encode(img, m, fit="stretch", fsk_id="WY6Y").size
    assert withid - plain == pytest.approx(fsk_id_ms("WY6Y") / 1000.0 * RATE, abs=2)


def test_fsk_id_tones_are_1900_and_2100():
    from cyberrig.sstv.modes import fsk_id_segments

    tones = {hz for hz, _ms in fsk_id_segments("WY6Y")}
    assert tones == {1900.0, 2100.0}


def test_fsk_text_is_cleaned_not_trusted():
    from cyberrig.sstv.modes import FSK_MAX_CHARS, clean_fsk_text

    assert clean_fsk_text("wy6y") == "WY6Y"
    assert clean_fsk_text("WY6Y\n\t{}") == "WY6Y"      # outside the 6-bit alphabet
    assert len(clean_fsk_text("A" * 99)) == FSK_MAX_CHARS
    assert clean_fsk_text("") == ""
    assert clean_fsk_text(None) == ""


def test_no_fsk_id_means_no_extra_audio():
    m = get_mode("Robot 36")
    img = np.zeros((240, 320, 3), np.uint8)
    assert encode(img, m, fit="stretch").size == encode(img, m, fit="stretch", fsk_id="").size
