"""CAT read-loop timing: an answer must release the serial lock immediately.

The old loop used read(256) with a 50 ms timeout, which blocks for the FULL
timeout whenever fewer than 256 bytes are waiting — so a 10 ms answer cost
~100 ms of held lock and every PTT press queued behind it (measured 2026-08-29:
40% of presses waited 90-100 ms).
"""
import threading
import time

import pytest

from cyberrig.cat.ftdx10 import FTdx10, FRAME_GRACE


class FakeSerial:
    """Minimal pyserial stand-in with a settable answer delay."""

    def __init__(self, answers=None, delay=0.005):
        self.is_open = True
        self.timeout = 0.5
        self._answers = answers or {}
        self._delay = delay
        self._pending = b""
        self._ready_at = 0.0
        self.written = []

    def reset_input_buffer(self):
        self._pending = b""

    def write(self, data):
        self.written.append(data)
        reply = self._answers.get(data.decode(), b"")
        if reply:
            self._pending = reply
            self._ready_at = time.monotonic() + self._delay
        return len(data)

    @property
    def in_waiting(self):
        if self._pending and time.monotonic() >= self._ready_at:
            return len(self._pending)
        return 0

    def read(self, n):
        if self.in_waiting:
            out, self._pending = self._pending[:n], self._pending[n:]
            return out
        time.sleep(min(n and self.timeout or 0, self.timeout))
        return b""

    def close(self):
        self.is_open = False


def _rig(fake):
    rig = FTdx10()
    rig._ser = fake
    rig.is_connected = True
    return rig


def test_answer_after_echo_returns_data_and_is_fast():
    fake = FakeSerial({"RM5;": b"RM5;RM5123000;"})
    rig = _rig(fake)
    t0 = time.monotonic()
    assert rig._cmd_raw("RM5;") == "RM5123000"
    assert time.monotonic() - t0 < 0.05


def test_echo_only_waits_just_the_grace_then_returns_it():
    fake = FakeSerial({"TX;": b"TX;"})
    rig = _rig(fake)
    t0 = time.monotonic()
    assert rig._cmd_raw("TX;") == "TX"
    assert time.monotonic() - t0 < FRAME_GRACE + 0.05


def test_no_answer_returns_none():
    fake = FakeSerial({})
    rig = _rig(fake)
    assert rig._cmd_raw("TX;") is None


def test_split_frames_are_reassembled():
    """Answer arriving in two chunks still parses (data frame wins over echo)."""
    fake = FakeSerial({"IF;": b"IF001039160"})
    rig = _rig(fake)

    def tail():
        time.sleep(0.01)
        fake._pending += b"00+000000000000;"

    threading.Thread(target=tail, daemon=True).start()
    assert rig._cmd_raw("IF;") == "IF00103916000+000000000000"


def test_ptt_write_is_not_stuck_behind_a_poll_read():
    """A query in flight must not delay set_ptt by more than a few ms."""
    fake = FakeSerial({"SM0;": b"SM0;SM0167;"}, delay=0.01)
    rig = _rig(fake)
    threading.Thread(target=lambda: rig._cmd_raw("SM0;"), daemon=True).start()
    time.sleep(0.002)  # query is mid-flight, holding the lock
    t0 = time.monotonic()
    rig.set_ptt(True)
    waited = time.monotonic() - t0
    assert waited < 0.05, f"PTT waited {waited*1000:.0f} ms on the serial lock"
    assert b"TX1;" in b"".join(fake.written)
