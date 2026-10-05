"""A CAT link that never came up must keep retrying (P163).

2026-09-25: the station PC booted off the new UPS before the FTDX10 answered, and
later a Shelly power cycle left the rig in standby. Both times connect() failed
once and nothing ever tried again — the panel sat Disconnected and rigctl kept
serving a cached frequency until someone pressed Connect by hand.
"""
import time

import pytest

from cyberrig.cat import ftdx10 as ftdx10_mod
from cyberrig.cat.ftdx10 import FTdx10


class FakeSerial:
    """pyserial stand-in: answers ID; only while the shared `rig_on` flag is set."""

    opened = []  # every port instance ever created, in order

    def __init__(self, port, baud, **kw):
        if FakeSerial.missing:
            raise OSError("no such device")
        self.port = port
        self.is_open = True
        self.timeout = 0.5
        self.written = []
        self._pending = b""
        FakeSerial.opened.append(self)

    def reset_input_buffer(self):
        self._pending = b""

    def write(self, data):
        self.written.append(data)
        if data == b"ID;" and FakeSerial.rig_on:
            self._pending = b"ID0761;"
        return len(data)

    @property
    def in_waiting(self):
        return len(self._pending)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self):
        self.is_open = False


@pytest.fixture
def fake_port(monkeypatch):
    FakeSerial.opened = []
    FakeSerial.missing = False
    FakeSerial.rig_on = False
    monkeypatch.setattr(ftdx10_mod.serial, "Serial", FakeSerial)
    monkeypatch.setattr(ftdx10_mod, "REOPEN_INTERVAL", 0.05)
    return FakeSerial


@pytest.fixture
def rig():
    r = FTdx10()
    r.sync_from_rig = lambda full=False: None  # full sync is irrelevant here
    yield r
    r.disconnect()


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_failed_first_connect_recovers_once_the_rig_answers(fake_port, rig):
    assert rig.connect("/dev/fake", 38400) is False
    rig.retry_until_connected("/dev/fake", 38400, interval=0.02)
    time.sleep(0.2)
    assert not rig.is_connected
    fake_port.rig_on = True
    assert _wait(lambda: rig.is_connected)


def test_a_missing_port_is_retried_too(fake_port, rig):
    fake_port.missing = True
    assert rig.connect("/dev/fake", 38400) is False
    assert rig._port == "/dev/fake"  # remembered even though open() raised
    rig.retry_until_connected("/dev/fake", 38400, interval=0.02)
    time.sleep(0.15)
    fake_port.missing = False
    fake_port.rig_on = True
    assert _wait(lambda: rig.is_connected)


def test_retrying_never_wakes_the_rig(fake_port, rig):
    rig.retry_until_connected("/dev/fake", 38400, interval=0.02)
    assert _wait(lambda: len(fake_port.opened) >= 3)
    sent = [w for s in fake_port.opened for w in s.written]
    assert b"PS1;" not in sent
    assert not rig.is_connected


def test_disconnect_stops_the_retry_loop(fake_port, rig):
    rig.retry_until_connected("/dev/fake", 38400, interval=0.02)
    assert _wait(lambda: len(fake_port.opened) >= 2)
    rig.disconnect()
    n = len(fake_port.opened)
    time.sleep(0.3)
    assert len(fake_port.opened) == n


def test_power_on_while_disconnected_sends_the_wake_pair(fake_port, rig):
    rig._port, rig._baud = "/dev/fake", 38400
    rig.set_radio_power(True)
    assert _wait(lambda: any(s.written == [b"PS1;", b"PS1;"] for s in fake_port.opened))
    wake = next(s for s in fake_port.opened if s.written == [b"PS1;", b"PS1;"])
    assert not wake.is_open  # its own short-lived handle, closed afterwards
    assert rig.state.radio_on is True
