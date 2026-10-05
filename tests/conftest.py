"""Test-wide isolation.

Two things have to be true before any test module is imported, because
`cyberrig.web.app` builds its Settings and its SSTV gallery path at import
time and there is no second chance to redirect them afterwards.
"""

import atexit
import os
import shutil
import tempfile

import pytest

# Redirect CyberRig's entire on-disk state into a throwaway directory. This
# runs at conftest import, which pytest does before it imports any test
# module — a fixture would be too late for a module-level `settings`.
#
# Without it the suite reads and writes the RUNNING STATION's state: the API
# tests were asserting against whatever sstv_watts the operator last set in
# the UI (30 expected, 25 on this Pi), and three synthetic receptions had
# been written into ~/CyberRig/sstv/rx/ alongside real off-air pictures.
_TMP_HOME = tempfile.mkdtemp(prefix="cyberrig-test-")
os.environ["CYBERRIG_HOME"] = _TMP_HOME
atexit.register(shutil.rmtree, _TMP_HOME, True)

from cyberrig.modes import session as modes_session  # noqa: E402
from cyberrig.settings import _config_dir  # noqa: E402


def test_home() -> str:
    """Where this run's CyberRig state lives. Handy in a failing test."""
    return _TMP_HOME


@pytest.fixture(autouse=True, scope="session")
def _never_touch_the_real_station():
    """Fail loudly rather than quietly writing to the operator's directory."""
    real = os.path.expanduser("~/CyberRig")
    assert str(_config_dir()) != real, (
        f"tests are pointed at the real station directory {real}"
    )
    yield


@pytest.fixture(autouse=True)
def _clear_mode_registry():
    """Digital-mode sessions register globally so only one can be entered at
    a time. A test that enters and never exits would otherwise block every
    test after it."""
    modes_session._active.clear()
    yield
    modes_session._active.clear()
