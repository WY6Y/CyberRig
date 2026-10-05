"""Shared fakes for the digital-mode session tests (FreeDV, SSTV).

A rig and a codec with just enough surface for DigitalSession. Kept in one
place so a new mode's tests do not drift from the mode they were copied from.
"""

import numpy as np

from cyberrig.modes.session import (
    EX_DATA_BPF,
    EX_DATA_HCUT,
    EX_DATA_LCUT,
    EX_DATA_MOD,
    EX_DATA_REAR,
)


class FakeState:
    def __init__(self, freq=14236000, mode="USB"):
        self.freq_a = freq
        self.mode = mode
        # Mirrors RigState: a fake that reports a dial the radio never gave us
        # would hide exactly the bug this flag exists to prevent. Tests that
        # care about the unverified case clear it explicitly.
        self.freq_verified = True
        self.is_tx = False
        self.power_meter = 0
        self.alc_meter = 0
        self.swr_meter = 0


class FakeRig:
    def __init__(self, freq=14236000, mode="USB"):
        self.state = FakeState(freq, mode)
        self.is_connected = True
        self.mode = mode
        self.sh = 13
        self.nr = True
        self.nb = True
        self.notch = True
        self.contour = True
        self.compressor = True
        self.dnf = False
        self.monitor = True
        self.roofing = "3K"
        self.ex = {
            EX_DATA_LCUT: "05",
            EX_DATA_HCUT: "10",
            EX_DATA_BPF: "3",
            EX_DATA_MOD: "0",
            EX_DATA_REAR: "0",
        }
        self.power = 100
        self.ptt_log = []
        self.tx_po_raw = 60      # ~15 W, enough to arm the SWR guard
        self.tx_swr_raw = 26     # SWR 1.2, a happy antenna
        self.tx_alc_raw = 32     # 50% of the ALC scale (full scale is 64)
        self.calls = []

    def get_mode(self):
        return self.mode

    def set_mode(self, mode):
        self.calls.append(("set_mode", mode))
        self.mode = mode
        self.state.mode = mode

    def get_freq(self):
        return self.state.freq_a

    def set_freq(self, hz):
        self.calls.append(("set_freq", int(hz)))
        self.state.freq_a = int(hz)
        self.state.freq_verified = True   # we commanded it, so we know it

    def get_sh(self):
        return self.sh

    def set_sh(self, code):
        self.calls.append(("set_sh", code))
        self.sh = code

    def get_nr(self):
        return self.nr

    def set_nr(self, on, level=None):
        self.calls.append(("set_nr", on))
        self.nr = on

    def get_nb(self):
        return self.nb

    def set_nb(self, on, level=None):
        self.calls.append(("set_nb", on))
        self.nb = on

    def get_notch(self):
        return self.notch

    def set_notch(self, on, position_hz=None):
        self.calls.append(("set_notch", on))
        self.notch = on

    def get_contour(self):
        return self.contour

    def set_contour(self, on):
        self.calls.append(("set_contour", on))
        self.contour = on

    def get_compressor(self):
        return self.compressor

    def set_compressor(self, on, level=None):
        self.calls.append(("set_compressor", on))
        self.compressor = on

    def get_dnf(self):
        return self.dnf

    def set_dnf(self, on):
        self.calls.append(("set_dnf", on))
        self.dnf = on

    def get_monitor(self):
        return self.monitor

    def set_monitor(self, on, level=None):
        self.calls.append(("set_monitor", on))
        self.monitor = on

    def set_ptt(self, on):
        self.calls.append(("set_ptt", bool(on)))
        self.ptt_log.append(bool(on))
        self.state.is_tx = bool(on)
        # A keyed radio reads power; an unkeyed one reads nothing.
        self.state.power_meter = self.tx_po_raw if on else 0
        self.state.swr_meter = self.tx_swr_raw if on else 0
        self.state.alc_meter = self.tx_alc_raw if on else 0

    def get_power(self):
        return self.power

    def set_power(self, watts):
        self.calls.append(("set_power", watts))
        self.power = watts

    def get_roofing(self):
        return self.roofing

    def set_roofing(self, name):
        self.calls.append(("set_roofing", name))
        self.roofing = name

    def ex_read(self, p1, p2, p3):
        return self.ex.get((p1, p2, p3))

    def ex_write(self, p1, p2, p3, value):
        self.calls.append(("ex_write", (p1, p2, p3), value))
        self.ex[(p1, p2, p3)] = value


class FakeCodec:
    def __init__(self, tx_holders=None, fail=False, rate=48000,
                 device_name="ftdx10_rx"):
        self.rate = rate
        self.device_name = device_name
        self.output_handler = None
        self.input_handler = None
        self.input_handlers = {}
        self.rx_holders = set()
        self.tx_holders = set(tx_holders or [])
        self.fail = fail
        self.holds = []
        self.releases = []

    def status(self):
        return {
            "ok": True,
            "running": bool(self.rx_holders or self.tx_holders),
            "tx_holders": sorted(self.tx_holders),
            "rx_holders": sorted(self.rx_holders),
            "sample_rate": self.rate,
            "device_name": self.device_name,
            "capture_only": True,
            "error": None,
        }

    @property
    def sample_rate(self):
        return self.rate

    def set_output_handler(self, cb):
        self.output_handler = cb

    def set_input_handler(self, cb):
        self.input_handler = cb

    def add_input_handler(self, tag, cb):
        self.input_handlers[tag] = cb

    def remove_input_handler(self, tag):
        self.input_handlers.pop(tag, None)

    def pump(self, blocks=200, frames=1024):
        """Stand in for the sound card: drain the transmitter as fast as we can.

        Faster than real time on purpose — the transmitter gates on buffered
        audio, not on wall clock, so a 36 s transmission runs in well under a
        second of test time.
        """
        out = np.zeros((frames, 2), dtype=np.float32)
        for _ in range(blocks):
            cb = self.output_handler
            if cb is None:
                return
            cb(out, frames)

    def pump_rx(self, mono):
        """Feed mono float32 capture samples to whatever is listening."""
        mono = np.asarray(mono, dtype=np.float32).reshape(-1)
        if self.input_handler is not None:
            self.input_handler(mono)
        for cb in list(self.input_handlers.values()):
            cb(mono)

    def hold(self, side, tag, device_index=None):
        self.holds.append((side, tag))
        if self.fail:
            return {"ok": False, "error": "No USB capture device found"}
        if side == "rx":
            self.rx_holders.add(tag)
        else:
            self.tx_holders.add(tag)
        return {"ok": True, **self.status()}

    def release(self, side, tag):
        self.releases.append((side, tag))
        if side == "rx":
            self.rx_holders.discard(tag)
        else:
            self.tx_holders.discard(tag)
        return {"ok": True, **self.status()}
