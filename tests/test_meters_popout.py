"""Native meters popout shows PO, ALC and SWR in that order."""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# PySide6 is installed on the station PC, where the native panel actually runs, but
# not on the Pi where the suite is usually run. Skip rather than error at
# collection, so this file does not have to live on an --ignore list that every
# caller has to remember.
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QVBoxLayout

from cyberrig.cat import meter_cal
from cyberrig.panel.popouts import METER_OWNER, MetersPopout


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


class FakeRig(QObject):
    meters_update = Signal(object)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.releases = []

    def request_meters(self, owner, wanted):
        self.requests.append((owner, set(wanted)))

    def release_meters(self, owner):
        self.releases.append(owner)


def test_popout_draws_po_alc_swr_in_that_order():
    _app()
    pop = MetersPopout(FakeRig())
    widgets = [
        pop.layout().itemAt(i).widget()
        for i in range(pop.layout().count())
        if pop.layout().itemAt(i).widget() is not None
    ]
    assert widgets[0] is pop.po
    assert widgets[1] is pop.alc
    assert widgets[2] is pop.swr
    assert pop.po.spec is meter_cal.PO
    assert pop.alc.spec is meter_cal.ALC
    assert pop.swr.spec is meter_cal.SWR
    assert "ALC" in pop.windowTitle()


def test_popout_feeds_alc_from_the_same_rm_dict():
    _app()
    pop = MetersPopout(FakeRig())
    pop._on_meters({
        meter_cal.PO.rm: 205,
        meter_cal.ALC.rm: 32,
        meter_cal.SWR.rm: 26,
    })
    assert pop.po._raw == 205
    assert pop.alc._raw == 32
    assert pop.swr._raw == 26


def test_popout_registers_alc_as_its_own_meter_owner():
    """Reusing the panel's owner id would let either window kill the other's
    meters. ALC has to be in the request or the popout sits at zero."""
    _app()
    rig = FakeRig()
    pop = MetersPopout(rig)
    pop._register(True)
    assert rig.requests
    owner, wanted = rig.requests[-1]
    assert owner == METER_OWNER
    assert wanted == {meter_cal.PO.rm, meter_cal.ALC.rm, meter_cal.SWR.rm}
    pop._register(False)
    assert rig.releases == [METER_OWNER]


def test_layout_is_a_column():
    _app()
    pop = MetersPopout(FakeRig())
    assert isinstance(pop.layout(), QVBoxLayout)
