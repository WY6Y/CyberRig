"""Native LARGE UI scale is 1.33 on, 1.0 off."""

from cyberrig.panel.theme import UI_SCALE, qt_scale_factor


def test_large_on_is_the_vnc_scale():
    assert UI_SCALE == 1.33
    assert qt_scale_factor(True) == "1.33"


def test_large_off_is_original_size():
    assert qt_scale_factor(False) == "1.0"
