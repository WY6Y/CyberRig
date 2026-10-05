"""Native EQ popout reads/writes the same EX 03 03 bands as the web EQ page."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


class FakeRig:
    def __init__(self):
        self.bands = {
            ("tx", 0): {"freq": 1, "level": -3, "bw": 4},
            ("tx", 1): {"freq": 2, "level": 0, "bw": 5},
            ("tx", 2): {"freq": 0, "level": 2, "bw": 6},
            ("mic", 0): {"freq": 3, "level": 1, "bw": 7},
            ("mic", 1): {"freq": 0, "level": 0, "bw": 5},
            ("mic", 2): {"freq": 8, "level": -10, "bw": 3},
        }
        self.writes = []

    def get_eq_band(self, section, band):
        return dict(self.bands[(section, band)])

    def set_eq_band(self, section, band, freq, level, bw):
        self.bands[(section, band)] = {"freq": freq, "level": level, "bw": bw}
        self.writes.append((section, band, freq, level, bw))


def test_eq_popout_reads_both_sections():
    _app()
    from cyberrig.panel.popouts import EqPopout

    rig = FakeRig()
    pop = EqPopout(rig)
    pop.read_all()
    tx0 = pop.sections["tx"][0].values()
    assert tx0 == {"freq": 1, "level": -3, "bw": 4}
    mic2 = pop.sections["mic"][2].values()
    assert mic2 == {"freq": 8, "level": -10, "bw": 3}


def test_eq_popout_write_goes_to_the_rig():
    _app()
    from cyberrig.panel.popouts import EqPopout

    rig = FakeRig()
    pop = EqPopout(rig)
    pop.read_all()
    band = pop.sections["tx"][0]
    band._writing = True
    band.level.setValue(5)
    band._writing = False
    band._write()
    assert rig.writes[-1] == ("tx", 0, 1, 5, 4)
