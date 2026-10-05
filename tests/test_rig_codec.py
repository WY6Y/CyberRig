"""LISTEN + REMOTE TX share one duplex open — unless ftdx10_rx exists.

The 2026-08-25 hang was two streams on the same hw: node. the station PC's
dsnoop PCM is a different device, capture-only, and that is the one
LISTEN must pick so JTDX can keep the hardware.
"""

import numpy as np

from cyberrig.audio import devices
from cyberrig.audio.rig_codec import RigCodec
from cyberrig.audio.tx_audio import TxAudioEngine
from cyberrig.audio.waterfall import WaterfallEngine


class FakeStream:
    opens = []

    def __init__(self, **kwargs):
        FakeStream.opens.append(kwargs)
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class FakeInputStream:
    opens = []

    def __init__(self, **kwargs):
        FakeInputStream.opens.append(kwargs)
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class FakeOutputStream:
    opens = []

    def __init__(self, **kwargs):
        FakeOutputStream.opens.append(kwargs)
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class FakeSD:
    Stream = FakeStream
    InputStream = FakeInputStream
    OutputStream = FakeOutputStream

    DEVICES = [
        {
            "name": "HDA Intel PCH: ALC897 Analog (hw:0,0)",
            "max_input_channels": 2,
            "max_output_channels": 2,
            "default_samplerate": 44100.0,
            "hostapi": 0,
        },
        {
            "name": "USB Audio Device: Audio (hw:1,0)",
            "max_input_channels": 2,
            "max_output_channels": 2,
            "default_samplerate": 44100.0,
            "hostapi": 0,
        },
        {
            "name": "USB AUDIO  CODEC: Audio (hw:2,0)",
            "max_input_channels": 2,
            "max_output_channels": 2,
            "default_samplerate": 44100.0,
            "hostapi": 0,
        },
    ]

    def query_hostapis(self):
        return [{"name": "ALSA"}]

    def query_devices(self, idx=None):
        if idx is None:
            return list(self.DEVICES)
        return self.DEVICES[idx]


class FakeSDWithSnoop(FakeSD):
    """the station PC enumeration: raw codec still visible, plus the dsnoop PCM.

    While JTDX holds hw:2,0 the CODEC row actually disappears; keeping it
    here proves the picker prefers ftdx10_rx on merit, not because the
    hardware vanished.
    """
    DEVICES = FakeSD.DEVICES + [
        {
            "name": "ftdx10_rx",
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 48000.0,
            "hostapi": 0,
        },
    ]


class FakeSDNoDuplexCapture(FakeSD):
    """The exact enumeration proven live on the station PC 2026-09-11: once dsnoop
    owns the raw codec's capture side, `hw:2,0` itself reports zero input
    channels, so no device on the card can satisfy a duplex TX open."""
    DEVICES = [
        {
            "name": "HDA Intel PCH: ALC897 Analog (hw:0,0)",
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 44100.0,
            "hostapi": 0,
        },
        {
            "name": "USB AUDIO  CODEC: Audio (hw:2,0)",
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 44100.0,
            "hostapi": 0,
        },
        {
            "name": "ftdx10_rx",
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 48000.0,
            "hostapi": 0,
        },
    ]


def _codec(sd_cls=FakeSD):
    FakeStream.opens = []
    FakeInputStream.opens = []
    FakeOutputStream.opens = []
    return RigCodec(sd_module=sd_cls())


def test_extra_input_handler_fans_out_beside_the_primary():
    c = _codec()
    seen = []
    c.set_input_handler(lambda x: seen.append(("prim", len(x))))
    c.add_input_handler("freedv", lambda x: seen.append(("fdv", len(x))))
    c.hold("rx", "waterfall")
    import numpy as np
    c._callback(np.zeros((8, 2), dtype=np.float32), np.zeros((8, 2), dtype=np.float32), 8, None, None)
    assert ("prim", 8) in seen
    assert ("fdv", 8) in seen
    c.remove_input_handler("freedv")
    seen.clear()
    c._callback(np.zeros((4, 2), dtype=np.float32), np.zeros((4, 2), dtype=np.float32), 4, None, None)
    assert seen == [("prim", 4)]
    c.close()


def test_picks_the_yaesu_codec_not_the_digirig_or_onboard():
    c = _codec()
    r = c.hold("rx", "waterfall")
    assert r["ok"], r
    assert r["device_index"] == 2
    assert "CODEC" in r["device_name"]
    assert r["sample_rate"] == 48000
    assert FakeStream.opens[0]["channels"] == (2, 2)
    c.close()


def test_denied_device_refused_even_by_index():
    c = _codec()
    r = c.hold("tx", "tx", device_index=1)
    assert r["ok"] is False
    assert "deny" in (r.get("error") or "").lower()
    assert c.running is False
    assert FakeStream.opens == []


def test_listen_then_remote_tx_is_one_open():
    """The 2026-08-25 failure: LISTEN at 44100 then TX at 48000 on hw:2,0."""
    c = _codec()
    wf = WaterfallEngine()
    wf._codec = c
    tx = TxAudioEngine()
    tx._codec = c

    a = wf.start()
    assert a["ok"], a
    assert c._opens == 1
    assert c.running

    b = tx.start()
    assert b["ok"], b
    assert c._opens == 1, "REMOTE TX must not open a second PortAudio stream"
    assert sorted(c.status()["rx_holders"]) == ["waterfall"]
    assert sorted(c.status()["tx_holders"]) == ["tx"]
    assert b["sample_rate"] == a["sample_rate"]

    wf.stop()
    assert c.running, "LISTEN dropping must not kill an armed TX stream"
    assert c._opens == 1

    tx.stop()
    assert c.running is False
    assert c._opens == 1


def test_tx_first_then_listen_still_one_open():
    c = _codec()
    wf = WaterfallEngine()
    wf._codec = c
    tx = TxAudioEngine()
    tx._codec = c

    assert tx.start()["ok"]
    assert wf.start()["ok"]
    assert c._opens == 1
    tx.stop()
    assert c.running
    wf.stop()
    assert c.running is False


def test_callback_silence_until_tx_holder_then_plays_ring():
    c = _codec()
    heard = []

    def on_in(mono):
        heard.append(len(mono))

    c.set_input_handler(on_in)
    assert c.hold("rx", "waterfall")["ok"]
    stream = c._stream
    frames = 8
    indata = np.ones((frames, 2), dtype=np.float32)
    outdata = np.full((frames, 2), 9.0, dtype=np.float32)
    stream.kwargs["callback"](indata, outdata, frames, None, None)
    assert heard == [frames]
    assert np.all(outdata == 0)  # no TX holder → silence

    tx = TxAudioEngine()
    tx._codec = c
    assert tx.start()["ok"]
    tx._ring.clear()  # drop the 40 ms prime of near-silence
    tone = np.full(frames, 0.5, dtype=np.float32)
    tx._ring.write(tone)
    out2 = np.zeros((frames, 2), dtype=np.float32)
    stream.kwargs["callback"](indata, out2, frames, None, None)
    assert np.allclose(out2[:, 0], 0.5)
    assert np.allclose(out2[:, 1], 0.5)
    c.close()
    tx._codec_held = False
    tx._running = False


# ---------------------------------------------------------------- ftdx10_rx


def test_ftdx10_rx_beats_raw_codec_for_capture_not_for_tx():
    codec = "USB AUDIO  CODEC: Audio (hw:2,0)"
    snoop = "ftdx10_rx"
    assert devices.score(snoop, "ALSA", side="rx") > devices.score(codec, "ALSA", side="rx")
    assert devices.score(snoop, "ALSA", side="rx") >= devices.MIN_AUTO_SCORE
    assert devices.score(snoop, "ALSA", side="tx") < devices.MIN_AUTO_SCORE
    assert devices.score(codec, "ALSA", side="tx") >= devices.MIN_AUTO_SCORE


# ------------------------------------------------------- TX output-only fallback


def test_tx_duplex_open_fails_when_raw_codec_has_no_input():
    """Reproduces the live 2026-09-11 failure directly: with no fallback,
    `_open_duplex_unlocked` alone finds nothing on this enumeration."""
    c = _codec(FakeSDNoDuplexCapture)
    r = c._open_duplex_unlocked(None)
    assert r["ok"] is False
    assert "no duplex" in r["error"].lower()
    c.close()


def test_tx_hold_falls_back_to_output_only_when_no_duplex_device_exists():
    c = _codec(FakeSDNoDuplexCapture)
    r = c.hold("tx", "ft8")
    assert r["ok"], r
    assert r["tx_output_only"] is True
    assert c._stream is None
    assert c._output_stream is not None
    assert FakeOutputStream.opens[0]["channels"] == 2
    assert FakeStream.opens == []  # no duplex candidates -- no Stream ever constructed
    c.close()


def test_output_only_stream_plays_tx_ring_and_ignores_capture_holders():
    c = _codec(FakeSDNoDuplexCapture)
    tx = TxAudioEngine()
    tx._codec = c
    assert tx.start()["ok"]

    stream = c._output_stream
    tx._ring.clear()
    tone = np.full(8, 0.5, dtype=np.float32)
    tx._ring.write(tone)
    outdata = np.zeros((8, 2), dtype=np.float32)
    stream.kwargs["callback"](outdata, 8, None, None)
    assert np.allclose(outdata[:, 0], 0.5)
    assert np.allclose(outdata[:, 1], 0.5)

    tx.stop()
    assert c._output_stream is None
    c.close()


def test_output_only_closes_once_all_tx_holders_release():
    c = _codec(FakeSDNoDuplexCapture)
    assert c.hold("tx", "a")["ok"]
    assert c.hold("tx", "b")["ok"]
    assert c._output_stream is not None

    c.release("tx", "a")
    assert c._output_stream is not None, "b still holds it"
    c.release("tx", "b")
    assert c._output_stream is None
    assert c.running is False


def test_failed_duplex_start_closes_the_orphaned_stream_before_fallback():
    """Guard against the other hazard nerd-grok flagged: a Stream object
    that constructs fine but raises in start() must be closed, not left
    open, before the output-only fallback opens on the same device index."""
    instances = []

    class BoomStream(FakeStream):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            instances.append(self)

        def start(self):
            raise RuntimeError("codec busy")

    class SD(FakeSDWithSnoop):
        Stream = BoomStream

    c = _codec(SD)
    r = c.hold("tx", "tx")
    assert r["ok"], r
    assert r["tx_output_only"] is True
    assert instances, "duplex was attempted"
    assert all(inst.closed for inst in instances), \
        "every constructed-but-failed duplex Stream must be closed, not orphaned"
    assert c._stream is None
    c.close()


def test_rx_duplex_skips_device_already_held_by_tx_output_only():
    """Guard against the dual-open hazard nerd-grok flagged in review: if TX
    already holds an output-only stream on a device, RX must not also try a
    duplex Stream on that same index -- that is the exact same-hw:-node
    hang this module's own docstring warns about."""
    c = _codec(FakeSD)  # single duplex-capable raw CODEC at index 2, no ftdx10_rx
    info = FakeSD.DEVICES[2]
    opened = c._start_output(2, info)
    assert opened is not None
    assert c._output_stream is not None
    assert c._output_device_index == 2

    r = c.hold("rx", "waterfall")
    assert r["ok"] is False
    assert "busy with tx output-only" in r["error"].lower()
    assert FakeStream.opens == [], "must never attempt a duplex Stream on TX's device"
    assert c._output_stream is not None, "RX failing must not disturb the live TX stream"
    c.close()


def test_rx_capture_only_and_tx_output_only_coexist():
    """The FT8 capture shape: capture_cycle holds RX on ftdx10_rx (InputStream)
    while transmit_message holds TX on the raw codec (OutputStream) -- two
    independent streams on two different device entries, same as the
    existing RX-capture-only + TX-duplex case this file already covers."""
    c = _codec(FakeSDNoDuplexCapture)
    rx = c.hold("rx", "ft8-capture")
    assert rx["ok"], rx
    assert c._input_stream is not None
    assert c._stream is None

    tx = c.hold("tx", "ft8-tx")
    assert tx["ok"], tx
    assert c._output_stream is not None
    assert c._stream is None
    assert c.running

    c.release("tx", "ft8-tx")
    assert c._output_stream is None
    assert c._input_stream is not None, "releasing TX must not touch RX"
    assert c.running

    c.release("rx", "ft8-capture")
    assert c._input_stream is None
    assert c.running is False


def test_rx_hold_opens_inputstream_on_ftdx10_rx():
    c = _codec(FakeSDWithSnoop)
    r = c.hold("rx", "waterfall")
    assert r["ok"], r
    assert r["device_name"] == "ftdx10_rx"
    assert r["capture_only"] is True
    assert r["device_index"] == 3
    assert FakeStream.opens == []
    assert len(FakeInputStream.opens) == 1
    opened = FakeInputStream.opens[0]
    assert opened["device"] == 3
    assert opened["channels"] == 1
    assert opened["samplerate"] == 48000
    assert c._opens == 1
    c.close()


def test_tx_hold_still_opens_duplex_codec_when_ftdx10_rx_exists():
    c = _codec(FakeSDWithSnoop)
    r = c.hold("tx", "tx")
    assert r["ok"], r
    assert "CODEC" in r["device_name"]
    assert r["capture_only"] is False
    assert len(FakeStream.opens) == 1
    assert FakeStream.opens[0]["channels"] == (2, 2)
    assert FakeInputStream.opens == []
    c.close()


def test_listen_then_remote_tx_is_two_opens_when_dsnoop_exists():
    """Additive: capture-only RX plus duplex TX, not a second open on hw:2,0."""
    c = _codec(FakeSDWithSnoop)
    wf = WaterfallEngine()
    wf._codec = c
    tx = TxAudioEngine()
    tx._codec = c

    a = wf.start()
    assert a["ok"], a
    assert c._opens == 1
    assert c.status()["capture_only"] is True

    b = tx.start()
    assert b["ok"], b
    assert c._opens == 2
    assert sorted(c.status()["rx_holders"]) == ["waterfall"]
    assert sorted(c.status()["tx_holders"]) == ["tx"]
    assert len(FakeInputStream.opens) == 1
    assert len(FakeStream.opens) == 1
    assert FakeInputStream.opens[0]["device"] == 3
    assert FakeStream.opens[0]["device"] == 2

    wf.stop()
    assert c.running, "LISTEN dropping must not kill an armed TX stream"
    assert c._input_stream is None
    assert c._stream is not None

    tx.stop()
    assert c.running is False
    assert c._opens == 2


def test_tx_first_then_listen_opens_capture_beside_duplex():
    c = _codec(FakeSDWithSnoop)
    wf = WaterfallEngine()
    wf._codec = c
    tx = TxAudioEngine()
    tx._codec = c

    assert tx.start()["ok"]
    assert c._opens == 1
    assert wf.start()["ok"]
    assert c._opens == 2
    assert c._input_stream is not None
    tx.stop()
    assert c.running
    assert c._stream is None
    wf.stop()
    assert c.running is False


def test_capture_callback_fans_out_without_outdata():
    c = _codec(FakeSDWithSnoop)
    heard = []
    c.set_input_handler(lambda mono: heard.append(len(mono)))
    assert c.hold("rx", "waterfall")["ok"]
    stream = c._input_stream
    frames = 8
    indata = np.ones((frames, 1), dtype=np.float32)
    stream.kwargs["callback"](indata, frames, None, None)
    assert heard == [frames]
    c.close()


def test_tx_open_failure_does_not_drop_rx_capture():
    """Since the 2026-09-11 output-only fallback, a broken duplex Stream no
    longer means TX has no options -- the raw codec's playback side is a
    separate device object underneath and still opens fine here. What this
    test actually protects (RX capture surviving a TX-side failure/retry)
    is unchanged either way, so it is asserted directly rather than via
    "tx hold failed"."""
    class BoomStream(FakeStream):
        def start(self):
            raise RuntimeError("codec busy")

    class SD(FakeSDWithSnoop):
        Stream = BoomStream

    c = _codec(SD)
    assert c.hold("rx", "waterfall")["ok"]
    r = c.hold("tx", "tx")
    assert r["ok"] is True
    assert r["tx_output_only"] is True
    assert c.running
    assert "waterfall" in c.status()["rx_holders"]
    assert "tx" in c.status()["tx_holders"]
    assert c._input_stream is not None, "TX falling back must not touch RX capture"
    c.close()
