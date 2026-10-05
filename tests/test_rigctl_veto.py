"""A vetoed beacon must never reach the client as an error.

Regression for 2026-09-04. The WSPR gate refused one PTT at 22:04:00 and JTDX
answered by raising "Rig control error", un-checking Enable Tx and dropping the
rigctld socket; the link stayed dead 8h51m, through an entire unattended
overnight run, because nobody was awake to click Retry. The veto itself was
correct -- a tune was in flight -- but `RPRT -1` is escalated by the client,
so the only safe way to decline a key is to swallow it.

These drive the proxy, not the gate: `allow_ptt` already had tests and they all
passed while the bug was live. What was untested was the translation from a
False into bytes on the wire, which is where the whole failure lived.
"""

from __future__ import annotations

from cyberrig.server.rigctl import RigctlServer
from tests.fakes import FakeRig


class VetoGate:
    """Refuses every key, like a tune in flight. Records what it was asked."""

    def __init__(self, allow=False):
        self.allow = allow
        self.asked = []
        self.clients = []

    def allow_ptt(self, on):
        self.asked.append(on)
        return True if not on else self.allow

    def note_ptt(self, on):
        pass

    def note_rigctl_clients(self, n):
        self.clients.append(n)


def server(gate=None, rig=None):
    return RigctlServer(rig or FakeRig(), port=0, gate=gate)


def test_vetoed_key_reports_success_and_does_not_key():
    rig = FakeRig()
    gate = VetoGate(allow=False)
    resp = server(gate, rig)._dispatch("T 1")
    # The client must see a clean result. "RPRT -1" here is what cost the night.
    assert resp == "RPRT 0\n"
    assert "-1" not in resp
    # ...and the radio must not have been keyed anyway.
    assert rig.ptt_log == []
    assert gate.asked == [True]


def test_vetoed_key_does_not_count_as_a_transmission_we_permitted():
    """note_ptt is how the watchdog learns which TX is its business."""
    seen = []
    gate = VetoGate(allow=False)
    gate.note_ptt = lambda on: seen.append(on)
    server(gate, FakeRig())._dispatch("T 1")
    assert seen == []


def test_permitted_key_still_keys_the_radio():
    rig = FakeRig()
    resp = server(VetoGate(allow=True), rig)._dispatch("T 1")
    assert resp == "RPRT 0\n"
    assert rig.ptt_log == [True]


def test_jtdx_data_mode_ptt_value_is_still_vetoed():
    """JTDX sends `T 3` (PTT via data port), not `T 1`."""
    rig = FakeRig()
    assert server(VetoGate(allow=False), rig)._dispatch("T 3") == "RPRT 0\n"
    assert rig.ptt_log == []


def test_unkey_is_never_vetoed():
    rig = FakeRig()
    gate = VetoGate(allow=False)
    assert server(gate, rig)._dispatch("T 0") == "RPRT 0\n"
    assert rig.ptt_log == [False]


def test_unknown_command_still_errors():
    """Swallowing is for a *vetoed key*, not a blanket ban on RPRT -1."""
    assert server(None, FakeRig())._dispatch("\\nonsense_command") == "RPRT -1\n"


def test_client_count_is_reported_to_the_gate():
    gate = VetoGate()
    srv = server(gate, FakeRig())
    assert srv.client_count == 0
    srv._note_clients(+1)
    assert srv.client_count == 1
    srv._note_clients(-1)
    assert srv.client_count == 0
    assert gate.clients == [1, 0]
    # Never negative, however the accounting is entered.
    srv._note_clients(-1)
    assert srv.client_count == 0


def test_gate_without_the_hook_is_tolerated():
    """Older gates have no note_rigctl_clients; that must not kill a connect."""

    class Bare:
        def allow_ptt(self, on):
            return True

    srv = server(Bare(), FakeRig())
    srv._note_clients(+1)
    assert srv.client_count == 1


# ── the startup placeholder must never be served as a reading ────────────

def test_get_freq_waits_for_a_real_reading_rather_than_serving_the_default():
    """2026-09-05: JTDX connected one second after a panel restart, read
    `RigState.freq_a`'s hardcoded 14200000 as truth and put the radio on
    14.200 DATA-U, where no FT8 exists. Seven minutes of an FT8 session
    decoded nothing while every indicator looked healthy. A client that polls
    us is FOLLOWING us; a default is not a reading."""
    import threading
    import time as _time

    from cyberrig.server import rigctl as rigctlmod

    rig = FakeRig(freq=28_074_000)
    rig.state.freq_verified = False
    srv = rigctlmod.RigctlServer(rig, port=0, gate=None)

    out = []
    t = threading.Thread(target=lambda: out.append(srv._dispatch("f")),
                         daemon=True)
    t.start()
    _time.sleep(0.2)
    assert not out, "answered before the radio had reported anything"

    rig.state.freq_verified = True
    t.join(timeout=3)
    assert out and out[0].startswith("28074000"), out


def test_get_freq_refuses_only_when_the_radio_never_answers(monkeypatch):
    """A stall is cheap; a wrong frequency is not. But a radio that never
    reports has to produce an error rather than an invented dial."""
    from cyberrig.server import rigctl as rigctlmod

    monkeypatch.setattr(rigctlmod, "FIRST_READ_WAIT_S", 0.2)
    rig = FakeRig(freq=28_074_000)
    rig.state.freq_verified = False
    srv = rigctlmod.RigctlServer(rig, port=0, gate=None)
    assert srv._dispatch("f").strip() == "RPRT -1"


def test_a_commanded_frequency_counts_as_verified():
    rig = FakeRig(freq=14_074_000)
    rig.state.freq_verified = False
    rig.set_freq(21_074_000)
    assert rig.state.freq_verified is True
