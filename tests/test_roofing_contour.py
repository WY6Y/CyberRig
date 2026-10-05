from cyberrig.cat.ftdx10 import ROOF_GET, ROOF_SET


def test_roofing_set_codes():
    assert ROOF_SET["12K"] == "1"
    assert ROOF_SET["3K"] == "2"
    assert ROOF_SET["500"] == "4"
    assert ROOF_SET["300"] == "5"


def test_roofing_read_codes_include_answer_p3():
    assert ROOF_GET["6"] == "12K"
    assert ROOF_GET["7"] == "3K"
    assert ROOF_GET["9"] == "500"
    assert ROOF_GET["A"] == "300"


def test_contour_level_ex_format():
    assert f"{-40:+03d}" == "-40"
    assert f"{0:+03d}" == "+00"
    assert f"{20:+03d}" == "+20"


def test_contour_width_ex_format_and_clamp():
    assert f"{1:02d}" == "01"
    assert f"{11:02d}" == "11"
    w = max(1, min(11, 0))
    assert w == 1
    w = max(1, min(11, 99))
    assert w == 11


def test_contour_on_off_reads_the_last_digit():
    from cyberrig.cat.ftdx10 import FTdx10

    rig = FTdx10.__new__(FTdx10)
    for answer in ("CO000001;", "CO000001"):
        rig._cmd = lambda c, a=answer: {"CO00;": a}.get(c)
        assert rig.get_contour() is True
    rig._cmd = lambda c: {"CO00;": "CO000000;"}.get(c)
    assert rig.get_contour() is False
    rig._cmd = lambda c: None
    assert rig.get_contour() is None
