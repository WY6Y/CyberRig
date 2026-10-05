"""Cross-check the encoder against PySSTV, an independent implementation.

This is the conformance test that our own tests cannot be: PySSTV was written
by someone else from the same specs, so agreement is evidence and disagreement
is a question worth answering. Every disagreement below has been read in both
sources and adjudicated — see the docstring in cyberrig/sstv/modes.py.

PySSTV is a dev-only dependency (`pip install PySSTV`); these skip without it.
"""

import numpy as np
import pytest

from cyberrig.sstv.modes import get_mode
from cyberrig.sstv.testcard import test_card as make_card

pysstv_color = pytest.importorskip("pysstv.color")
from PIL import Image  # noqa: E402

RGB_MODES = [("Martin 1", "MartinM1"), ("Martin 2", "MartinM2")]
YCC_MODES = [("Robot 36", "Robot36"), ("PD90", "PD90"),
             ("PD120", "PD120"), ("PD180", "PD180")]
SCOTTIE_MODES = [("Scottie 1", "ScottieS1"), ("Scottie 2", "ScottieS2"),
                 ("Scottie DX", "ScottieDX")]

# One 0-255 level is 800/255 Hz. PIL rounds YCbCr to uint8; we keep floats.
ONE_LEVEL_HZ = 800.0 / 255.0


def theirs(name: str, mode):
    cls = getattr(pysstv_color, name)
    img = make_card(mode.width, mode.height)
    return list(cls(Image.fromarray(img), 48000, bits=16).gen_freq_bits()), img


def mine(mode, img):
    return [t for seg in mode.segments(img) for t in seg]


@pytest.mark.parametrize("ours,theirname", RGB_MODES)
def test_rgb_modes_match_pysstv_exactly(ours, theirname):
    m = get_mode(ours)
    them, img = theirs(theirname, m)
    us = mine(m, img)
    assert len(us) == len(them)
    a, b = np.array(us, dtype=np.float64), np.array(them, dtype=np.float64)
    # Not bit-identical: we compute 1500 + v/255*800, they use a lookup. The
    # residual is float representation noise, ~1e-13 Hz.
    assert np.abs(a[:, 0] - b[:, 0]).max() < 1e-9
    assert np.abs(a[:, 1] - b[:, 1]).max() < 1e-9


@pytest.mark.parametrize("ours,theirname", YCC_MODES)
def test_ycc_modes_match_pysstv_within_one_quantisation_step(ours, theirname):
    """Robot/PD agree to within PIL's uint8 rounding — i.e. the colour
    conversion, channel order and chroma-line pairing all match."""
    m = get_mode(ours)
    them, img = theirs(theirname, m)
    us = mine(m, img)
    assert len(us) == len(them)
    a, b = np.array(us, dtype=np.float64), np.array(them, dtype=np.float64)
    assert np.abs(a[:, 1] - b[:, 1]).max() < 1e-9, "timing must be identical"
    assert np.abs(a[:, 0] - b[:, 0]).max() <= ONE_LEVEL_HZ + 1e-9


@pytest.mark.parametrize("ours,theirname", SCOTTIE_MODES)
def test_scottie_line_timing_matches_despite_a_different_decomposition(ours, theirname):
    """We and PySSTV lay a Scottie line out differently but must still agree on
    how long it is — that is what a decoder's slant correction locks onto."""
    m = get_mode(ours)
    them, img = theirs(theirname, m)
    us = mine(m, img)
    ours_total = sum(d for _f, d in us)
    their_total = sum(d for _f, d in them)
    # The only permitted difference is our spec-mandated leading sync pulse.
    # Loose tolerance: these are sums of a quarter-million Python floats.
    assert ours_total - their_total == pytest.approx(m.sync_ms, abs=1e-3)
    assert m.line_ms == pytest.approx(
        (their_total - m.vis_ms) / m.lines_sent, abs=1e-6)


@pytest.mark.parametrize("ours,theirname", SCOTTIE_MODES)
def test_scottie_divergence_is_only_the_two_documented_ones(ours, theirname):
    """If PySSTV or we ever change, this trips instead of silently drifting."""
    m = get_mode(ours)
    them, img = theirs(theirname, m)
    us = mine(m, img)
    # 1. Our leading sync pulse.
    assert us[13] == (1200.0, m.sync_ms)
    assert them[13][0] == 1500
    # 2. They emit a separator after each channel too, so three more per line.
    assert len(them) - (len(us) - 1) == 3 * m.lines_sent


@pytest.mark.parametrize("ours,theirname",
                         RGB_MODES + YCC_MODES + SCOTTIE_MODES)
def test_vis_codes_agree(ours, theirname):
    assert get_mode(ours).vis_code == getattr(pysstv_color, theirname).VIS_CODE


@pytest.mark.parametrize("ours,theirname", RGB_MODES + YCC_MODES + SCOTTIE_MODES)
def test_geometry_agrees(ours, theirname):
    m = get_mode(ours)
    cls = getattr(pysstv_color, theirname)
    assert (m.width, m.height) == (cls.WIDTH, cls.HEIGHT)


# ── FSK ID ───────────────────────────────────────────────────────────────

def test_fsk_id_matches_pysstv_tone_for_tone():
    """The callsign trailer decoders show as text. Same source of truth."""
    from cyberrig.sstv.modes import fsk_id_segments

    m = get_mode("Martin 1")
    cls = getattr(pysstv_color, "MartinM1")
    img = make_card(m.width, m.height)
    s = cls(Image.fromarray(img), 48000, bits=16)
    s.add_fskid_text("WY6Y")
    theirs = list(s.gen_freq_bits())
    plain = len(mine(m, img))
    tail = theirs[plain:]
    ours = fsk_id_segments("WY6Y")
    assert len(ours) == len(tail)
    a, b = np.array(ours, dtype=np.float64), np.array(tail, dtype=np.float64)
    assert np.abs(a - b).max() < 1e-9
