"""Prove a keyed over that carried no mic audio is reported, not swallowed.

On 2026-09-04 remote SSB on 3916 transmitted overs that were partly or wholly
silent -- the browser mic needed up to ~2 s to start while the radio was
already keyed -- and nothing anywhere said so. The panel logged a clean TX,
the meters looked normal, and the only evidence was a hand-run probe watching
`bytes_in` crawl. `_log_over_fill` is the permanent version of that probe.

The math under test: mic PCM is s16le mono at `client_sr`, so realtime is
`client_sr * 2` bytes/sec, and fill is delivered-audio-seconds over held
seconds. Under 50% is a warning, because that is an over people could not hear.
"""

from __future__ import annotations

import logging
import time

import pytest

from cyberrig.web import app as webapp


class FakeTxEngine:
    def __init__(self, bytes_in):
        self._bytes_in = bytes_in

    def status(self):
        return {"bytes_in": self._bytes_in}


@pytest.fixture
def engine(monkeypatch):
    eng = FakeTxEngine(0)
    monkeypatch.setattr(webapp, "tx_engine", eng)
    return eng


def run_over(engine, caplog, held_s, audio_s, client_sr=48000):
    """Simulate an over of `held_s` that delivered `audio_s` of mic audio."""
    key_bytes = 1000
    engine._bytes_in = key_bytes + int(audio_s * client_sr * 2)
    key_t = time.time() - held_s
    with caplog.at_level(logging.INFO, logger=webapp.log.name):
        webapp._log_over_fill(key_t, key_bytes, client_sr)
    return caplog.records[-1] if caplog.records else None


def test_silent_over_warns(engine, caplog):
    """The 0% case: 2.1 s keyed, nothing sent. This is the one that hurt."""
    rec = run_over(engine, caplog, held_s=2.1, audio_s=0.0)
    assert rec.levelno == logging.WARNING
    assert "mostly silence" in rec.message
    assert "0% fill" in rec.message


def test_partial_over_warns(engine, caplog):
    """The measured 28% case: 3.1 s keyed, 0.9 s of audio."""
    rec = run_over(engine, caplog, held_s=3.1, audio_s=0.9)
    assert rec.levelno == logging.WARNING
    assert "29% fill" in rec.message or "28% fill" in rec.message


def test_healthy_over_is_info_not_warning(engine, caplog):
    """A good over must not cry wolf, or the warning stops meaning anything."""
    rec = run_over(engine, caplog, held_s=4.0, audio_s=3.95)
    assert rec.levelno == logging.INFO
    assert "mostly silence" not in rec.message


def test_fill_uses_client_sample_rate(engine, caplog):
    """A 24 kHz client sends half the bytes for the same audio -- still 100%."""
    rec = run_over(engine, caplog, held_s=3.0, audio_s=3.0, client_sr=24000)
    assert rec.levelno == logging.INFO
    assert "100% fill" in rec.message


def test_never_keyed_logs_nothing(engine, caplog):
    """No key_t means the over never started; there is nothing to report."""
    with caplog.at_level(logging.INFO, logger=webapp.log.name):
        webapp._log_over_fill(None, 0, 48000)
    assert not caplog.records


def test_key_tap_is_ignored(engine, caplog):
    """A sub-50 ms tap is a fumbled button, not an over. Don't log noise."""
    engine._bytes_in = 0
    with caplog.at_level(logging.INFO, logger=webapp.log.name):
        webapp._log_over_fill(time.time() - 0.01, 0, 48000)
    assert not caplog.records
