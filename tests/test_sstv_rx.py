"""The live receiver: what happens around the decoder, not inside it.

The DSP has its own tests. These are about the plumbing — that our own
transmission is not received back as a picture, that a transmission cut short
still keeps what arrived, and that an idle receiver does not grow forever.
"""

import time

import numpy as np
import pytest

from cyberrig.sstv.rx import LISTEN_SECONDS, RxStore, SSTVReceiver
from cyberrig.sstv.modes import get_mode
from cyberrig.sstv.testcard import test_card as make_card
from fakes import FakeCodec, FakeRig

pysstv_color = pytest.importorskip("pysstv.color")
from PIL import Image  # noqa: E402

RATE = 48000


def audio_for(name, theirname):
    m = get_mode(name)
    img = make_card(m.width, m.height)
    cls = getattr(pysstv_color, theirname)
    s = cls(Image.fromarray(img), RATE, bits=16)
    return np.array(list(s.gen_samples()), dtype=np.float32) / 32768.0, img, m


def feed(rx, audio, chunk=4096, until=None):
    """Push audio through as the sound card would, then let the worker drain."""
    for i in range(0, len(audio), chunk):
        rx._on_input(audio[i:i + chunk])
        while len(rx._q) > 8:
            time.sleep(0.002)
        if until and until():
            break
    deadline = time.time() + 30
    while rx._q and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)


def wait_for(pred, timeout=15.0):
    """Wait for the worker's finish path, not just for the queue to drain.

    An empty queue means the last chunk was dequeued, NOT that the picture
    was written: autoslant, render, PNG, WAV and a multi-megabyte .npz all
    run afterwards on the worker thread. rx.py:597 says as much in a comment
    ("saving a multi-MB npz can outlast the test/UI poll"). `feed`'s flat
    0.2 s was enough on an idle machine and not on a loaded one, which made
    every save-asserting test fail roughly one run in five.

    `rx.last` is set by that same finish path, so an assertion on it races
    exactly like one on the store — three tests did, and are the likeliest
    source of the ~1-in-14 failure that survived the first two passes here.

    Only for tests asserting something WAS saved or decoded. The ones asserting the
    store stays empty must keep the fast path, or each would sit here for
    the full timeout on every run.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def setup(tmp_path):
    rig, codec = FakeRig(), FakeCodec()
    rx = SSTVReceiver(codec, RxStore(tmp_path), rig)
    rx.start()
    yield rig, codec, rx, RxStore(tmp_path)
    rx.stop()


def test_a_whole_transmission_is_received_and_saved(setup):
    rig, _c, rx, store = setup
    audio, src, m = audio_for("Robot 36", "Robot36")
    feed(rx, audio)
    assert wait_for(lambda: len(store.list()) == 1), rx.status()
    items = store.list()
    assert len(items) == 1, rx.status()
    assert items[0]["mode"] == "Robot 36"
    assert items[0]["complete"] is True
    got = np.asarray(Image.open(store.path(items[0]["id"])))
    assert got.shape == (m.height, m.width, 3)
    err = np.abs(got[6:-6, 6:-6].astype(float) - src[6:-6, 6:-6].astype(float)).mean()
    assert err < 10.0


def test_our_own_transmission_is_not_received_back(setup):
    """A duplex codec hears what we send. The gallery must not fill with it."""
    rig, _c, rx, store = setup
    rig.state.is_tx = True
    audio, _src, _m = audio_for("Robot 36", "Robot36")
    feed(rx, audio)
    assert store.list() == []
    assert rx.status()["rx_active"] is False


def test_a_transmission_cut_short_still_keeps_what_arrived(setup):
    rig, _c, rx, store = setup
    audio, _src, m = audio_for("Robot 36", "Robot36")
    feed(rx, audio[: int(len(audio) * 0.6)])
    rx.stop()                      # operator exits, or the band drops out
    items = store.list()
    assert len(items) == 1
    assert items[0]["complete"] is False
    got = np.asarray(Image.open(store.path(items[0]["id"])))
    assert got.shape[0] == m.height


def test_noise_alone_never_produces_a_picture(setup):
    _rig, _c, rx, store = setup
    rng = np.random.RandomState(3)
    feed(rx, rng.normal(0, 0.2, RATE * 6).astype(np.float32))
    assert store.list() == []


def test_an_idle_receiver_does_not_grow_without_bound(setup):
    _rig, _c, rx, _store = setup
    feed(rx, np.zeros(RATE * 12, dtype=np.float32))
    assert len(rx._trace) <= LISTEN_SECONDS * RATE + RATE


def test_starting_twice_is_harmless(setup):
    _rig, _c, rx, _store = setup
    assert rx.start().get("already")


def test_stopping_detaches_from_the_codec(setup):
    _rig, codec, rx, _store = setup
    rx.stop()
    assert rx.running is False


def test_status_is_safe_before_anything_arrives(setup):
    _rig, _c, rx, _store = setup
    st = rx.status()
    assert st["rx_running"] is True
    assert st["rx_active"] is False
    assert st["rx_mode"] is None


def test_store_ids_are_unique_even_within_a_second(tmp_path):
    s = RxStore(tmp_path)
    img = np.zeros((16, 16, 3), np.uint8)
    a = s.save(img, {"mode": "Scottie 1", "when": 1000.0})
    b = s.save(img, {"mode": "Scottie 1", "when": 1000.0})
    assert a.id != b.id
    assert s.path(a.id) and s.path(b.id)


def test_deleting_something_that_is_not_there_says_so(tmp_path):
    assert not RxStore(tmp_path).delete("ghost")["ok"]


# ── straightening a picture that already arrived ─────────────────────────

def skewed_audio(name, theirname, ppm):
    audio, src, m = audio_for(name, theirname)
    n = int(round(len(audio) * (1.0 + ppm / 1e6)))
    out = np.interp(np.linspace(0, len(audio) - 1, n),
                    np.arange(len(audio)), audio).astype(np.float32)
    return out, src, m


def err(a, b, k=6):
    return float(np.abs(a[k:-k, k:-k].astype(float)
                        - b[k:-k, k:-k].astype(float)).mean())


def test_the_last_picture_is_kept_so_it_can_be_re_rendered(setup):
    _rig, _c, rx, _store = setup
    audio, _src, _m = audio_for("Robot 36", "Robot36")
    feed(rx, audio)
    assert wait_for(lambda: rx.last is not None), rx.status()
    assert rx.status()["last_adjustable"] is True


def test_slant_is_corrected_from_the_recording_not_a_re_send(setup):
    """The promise the whole decoder is built around."""
    from cyberrig.sstv.decode import render
    _rig, _c, rx, store = setup
    audio, src, m = skewed_audio("Robot 36", "Robot36", 2500)
    feed(rx, audio)
    assert wait_for(lambda: rx.last is not None), rx.status()
    # Free-run with zero slant is the old broken path; auto re-render fixes it.
    bad = err(src, render(rx.last.trace, m, rx.last.start, rx.last.rate,
                          slant=0.0, shift_ms=0.0, line_sync=False))
    r = rx.adjust_last(auto=True)        # same trace, no new audio
    assert r["ok"], r
    good = err(src, rx.last_image())
    assert good < bad / 3, f"{bad:.1f} -> {good:.1f}"


def test_a_manual_slant_actually_changes_the_picture(setup):
    from cyberrig.sstv.decode import render
    _rig, _c, rx, _store = setup
    feed(rx, audio_for("Robot 36", "Robot36")[0])
    assert wait_for(lambda: rx.last is not None), rx.status()
    # With per-line sync on, global slant barely moves a clean picture — so
    # exercise the free-run path the slider still uses as a fallback.
    a = render(rx.last.trace, rx.last.mode, rx.last.start, rx.last.rate,
               slant=0.0, line_sync=False)
    b = render(rx.last.trace, rx.last.mode, rx.last.start, rx.last.rate,
               slant=0.003, line_sync=False)
    assert not np.array_equal(a, b)
    rx.adjust_last(slant_ppm=3000)
    assert rx.status()["last_slant_ppm"] == 3000


def test_saving_the_correction_overwrites_the_stored_picture(setup):
    from cyberrig.sstv.decode import render
    _rig, _c, rx, store = setup
    audio, src, m = skewed_audio("Robot 36", "Robot36", 2500)
    feed(rx, audio)
    assert wait_for(lambda: len(store.list()) == 1), rx.status()
    rid = store.list()[0]["id"]
    bad_img = render(rx.last.trace, m, rx.last.start, rx.last.rate,
                     slant=0.0, shift_ms=0.0, line_sync=False)
    from PIL import Image as _Image
    _Image.fromarray(bad_img).save(store.path(rid))
    bad = err(src, np.asarray(Image.open(store.path(rid))))
    rx.adjust_last(auto=True)
    assert rx.resave_last()["ok"]
    good = err(src, np.asarray(Image.open(store.path(rid))))
    assert good < bad / 3
    assert len(store.list()) == 1, "correcting must not create a second copy"


def test_adjusting_with_nothing_received_is_refused_not_a_crash(setup):
    _rig, _c, rx, _store = setup
    r = rx.adjust_last(slant_ppm=100)
    assert not r["ok"]
    assert rx.last_image() is None
    assert rx.status()["last_adjustable"] is False


def test_resaving_with_nothing_received_is_refused(setup):
    _rig, _c, rx, _store = setup
    assert not rx.resave_last()["ok"]


# ── tuning display ───────────────────────────────────────────────────────

def ssb_shift(x, hz):
    """Move every tone by `hz` — what mistuning the radio actually does.

    Multiplying by a cosine is NOT this: it produces both sidebands, and the
    demodulator sees the sum and difference at once and lands back where it
    started. Shift the analytic signal instead.
    """
    n = len(x)
    X = np.fft.rfft(x.astype(np.float64))
    z = np.zeros(n, dtype=np.complex128)
    z[:len(X)] = X
    z[1:len(X) - (1 if n % 2 == 0 else 0)] *= 2.0
    z = np.fft.ifft(z)
    t = np.arange(n) / RATE
    return np.real(z * np.exp(2j * np.pi * hz * t)).astype(np.float32)


def test_tuning_reports_no_signal_on_silence(setup):
    _rig, _c, rx, _store = setup
    feed(rx, np.zeros(RATE * 3, dtype=np.float32))
    t = rx.tuning()
    assert t["tune_signal"] is False
    assert t["tune_offset_hz"] is None


def test_tuning_offset_is_zero_on_a_correctly_tuned_signal(setup):
    _rig, _c, rx, _store = setup
    audio, _src, _m = audio_for("Robot 36", "Robot36")
    feed(rx, audio[: RATE * 4])
    t = rx.tuning()
    assert t["tune_signal"] is True
    assert abs(t["tune_offset_hz"]) < 12, t


def test_a_mistuned_signal_reports_the_error_it_actually_has(setup):
    """The whole point: the number is the correction to dial into the rig."""
    _rig, _c, rx, _store = setup
    audio, _src, _m = audio_for("Robot 36", "Robot36")
    feed(rx, ssb_shift(audio[: RATE * 4], 120.0))
    t = rx.tuning()
    assert t["tune_signal"] is True
    assert t["tune_offset_hz"] == pytest.approx(120, abs=25), t


def test_the_histogram_is_bounded_and_json_safe(setup):
    _rig, _c, rx, _store = setup
    feed(rx, audio_for("Robot 36", "Robot36")[0][: RATE * 3])
    bins = rx.tuning()["tune_bins"]
    assert len(bins) == 56
    assert all(isinstance(b, int) and 0 <= b <= 255 for b in bins)


def test_the_raw_audio_is_kept_and_decodes_again_from_disk(setup):
    """The point of the WAV: re-run a CHANGED decoder on what really arrived.

    Two on-air failures had to be diagnosed against synthetic audio because
    the real audio existed nowhere — only the trace, which can be re-rendered
    but not re-decoded, and only by the code under suspicion.
    """
    import wave

    from cyberrig.sstv.decode import decode_audio

    _rig, _c, rx, store = setup
    audio, src, m = audio_for("Robot 36", "Robot36")
    feed(rx, audio)
    assert wait_for(lambda: len(store.list()) == 1), rx.status()
    # The WAV is written after the PNG — wait for the sidecar, not the entry.
    assert wait_for(
        lambda: (store.list()[0].get("audio") or "").endswith(".wav")
    ), rx.status()

    items = store.list()
    assert len(items) == 1
    assert items[0].get("audio", "").endswith(".wav")

    p = store.path(items[0]["id"], ext="wav")
    assert p is not None and p.stat().st_size > 0

    with wave.open(str(p), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == RATE
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    # Some lead-in before the header, and the whole picture after it.
    assert len(pcm) >= len(audio) * 0.9

    r = decode_audio(pcm.astype(np.float32) / 32768.0, RATE)
    assert r is not None
    assert r.mode.name == m.name
    assert r.complete


def test_deleting_a_picture_takes_its_audio_with_it(setup):
    _rig, _c, rx, store = setup
    audio, _src, _m = audio_for("Robot 36", "Robot36")
    feed(rx, audio)
    assert wait_for(lambda: len(store.list()) == 1), rx.status()
    rid = store.list()[0]["id"]
    # The store entry appears with the PNG; the WAV is written after it, so
    # the picture landing is NOT proof the audio has. Wait for the sidecar
    # itself. (Missed on the first pass at this: hammering one test 20 times
    # proved nothing about the four other save-asserting ones.)
    assert wait_for(lambda: store.path(rid, ext="wav") is not None), rx.status()
    assert store.delete(rid)["ok"]
    assert store.path(rid, ext="wav") is None
    assert store.path(rid) is None


def test_the_saved_audio_starts_at_the_header_after_a_long_idle_listen(setup):
    """A WAV that starts mid-picture is worse than no WAV.

    The audio buffer and the demodulated trace are trimmed by different
    rules — the trace rolls to LISTEN_SECONDS while idle, the audio has its
    own ceiling — and `_begin` has to convert a trace index into the audio
    buffer. When that conversion assumed a shared origin, a reception after
    a long idle stretch saved audio starting in the middle of the picture,
    with the VIS header missing entirely. Found on air, 2026-08-26.
    """
    import wave

    from cyberrig.sstv.decode import demodulate, find_vis

    _rig, _c, rx, store = setup
    # Idle first: silence long enough for the listening buffer to roll many
    # times over, so any origin drift has room to accumulate.
    quiet = np.zeros(RATE * 2, dtype=np.float32)
    for _ in range(12):
        feed(rx, quiet)
    audio, _src, m = audio_for("Robot 36", "Robot36")
    feed(rx, audio)
    assert wait_for(lambda: len(store.list()) == 1), rx.status()
    # ...and the WAV lands after the picture, so wait for the sidecar too.
    assert wait_for(
        lambda: (store.list()[0].get("audio") or "").endswith(".wav")
    ), rx.status()

    items = store.list()
    assert len(items) == 1, rx.status()
    p = store.path(items[0]["id"], ext="wav")
    assert p is not None, "no WAV kept"
    with wave.open(str(p), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    a = pcm.astype(np.float32) / 32768.0
    v = find_vis(demodulate(a[: RATE * 6], RATE), RATE)
    assert v is not None, "saved audio does not contain the VIS header"
    assert v.mode.name == m.name
    # And it should sit near the half-second of deliberate lead-in.
    assert v.leader_at / RATE < 1.0
