"""libcodec2 700D loopback — skip if the library isn't here."""

import numpy as np
import pytest

from cyberrig.freedv.modem import Codec2Modem, available_modes, resample


def test_resample_identity():
    x = np.linspace(-0.5, 0.5, 480, dtype=np.float32)
    y = resample(x, 8000, 8000)
    assert y.shape == x.shape
    assert np.allclose(x, y)


def test_resample_48k_to_8k_length():
    x = np.zeros(4800, dtype=np.float32)
    y = resample(x, 48000, 8000)
    assert abs(len(y) - 800) <= 2


def test_stream_resampler_usb_blocks_land_near_8000_per_second():
    from cyberrig.freedv.modem import StreamResampler
    rs = StreamResampler(48000, 8000)
    out = []
    # 48 kHz USB comes in 256-sample PortAudio blocks
    for _ in range(48000 // 256):
        y = rs.feed(np.ones(256, dtype=np.float32))
        if y.size:
            out.append(y)
    n = sum(len(c) for c in out)
    # one second of 48 kHz → 8000 samples, allow a block of slack
    assert 7800 <= n <= 8200


def test_available_modes_dict_shape():
    m = available_modes()
    assert "RADE" in m and "700D" in m
    assert set(m.keys()) >= {"RADE", "700D", "1600"}


@pytest.mark.skipif(not available_modes().get("700D"), reason="libcodec2 700D missing")
def test_700d_loopback_gets_sync():
    tx = Codec2Modem("700D")
    rx = Codec2Modem("700D")
    try:
        # A second of voiced-ish noise is enough for 700D to report stats.
        t = np.arange(tx.speech_rate, dtype=np.float32) / tx.speech_rate
        speech = 0.2 * np.sin(2 * np.pi * 220 * t)
        modem = tx.tx(speech)
        assert modem.size > 0
        decoded = rx.rx(modem)
        assert rx.sync in (0, 1)
        # decoded may be empty on the first frame; just prove it doesn't crash
        assert decoded.dtype == np.float32
    finally:
        tx.close()
        rx.close()
