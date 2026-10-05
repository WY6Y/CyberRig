"""SSTV overlay templates: the model, the macros, and the on-disk store.

Templates are the one part of this feature the operator hand-edits, so a file
on disk is untrusted input. Most of these tests are about a bad template
failing safely rather than reaching the page.
"""

import datetime as dt
import json

import pytest

from cyberrig.sstv.templates import (
    BUILTINS,
    MAX_ELEMENTS,
    TemplateStore,
    band_for_hz,
    builtin_ids,
    clean_element,
    clean_template,
    expand_macros,
    macro_context,
    slug,
)


# ── macros ───────────────────────────────────────────────────────────────

def test_macros_expand_from_the_context():
    ctx = {"mycall": "K1ABC", "grid": "FN42aa", "band": "20m"}
    assert expand_macros("DE {mycall} {grid} {band}", ctx) == "DE K1ABC FN42aa 20m"


def test_unknown_and_empty_macros_vanish_rather_than_printing_raw():
    # Both empty, so the separating space survives but nothing renders.
    assert expand_macros("TO {call} {nonsense}", {}) == "TO  "
    assert expand_macros("{call}", {}) == ""
    assert expand_macros("{rst}", {"rst": ""}) == ""


def test_macro_syntax_that_is_not_a_macro_is_left_alone():
    assert expand_macros("100% {NOTLOWER} {}", {}) == "100% {NOTLOWER} {}"


def test_band_lookup_covers_the_hf_bands():
    assert band_for_hz(14_230_000) == "20m"
    assert band_for_hz(7_171_000) == "40m"
    assert band_for_hz(3_845_000) == "80m"
    assert band_for_hz(28_680_000) == "10m"


def test_band_lookup_is_blank_outside_a_band():
    assert band_for_hz(12_000_000) == ""
    assert band_for_hz(0) == ""
    assert band_for_hz(None) == ""
    assert band_for_hz("nonsense") == ""


def test_macro_context_does_not_depend_on_the_clock():
    now = dt.datetime(2026, 8, 26, 16, 30, tzinfo=dt.timezone.utc)
    c = macro_context(14_230_000, "DATA-U", "k1abc", "FN42aa", now=now)
    assert c["mycall"] == "K1ABC"
    assert c["band"] == "20m"
    assert c["freq"] == "14.230"
    assert c["mode"] == "DATA-U"
    assert c["date"] == "2026-08-26"
    assert c["utc"] == "16:30Z"


def test_macro_context_survives_a_disconnected_radio():
    c = macro_context(None, "", "", "")
    assert c["freq"] == ""
    assert c["band"] == ""


# ── validation ───────────────────────────────────────────────────────────

def test_junk_elements_are_dropped():
    assert clean_element(None) is None
    assert clean_element({"type": "wormhole"}) is None
    assert clean_element("not a dict") is None


def test_coordinates_are_clamped_not_trusted():
    e = clean_element({"type": "text", "x": 99, "y": -99, "size": 500})
    assert e["x"] == 1.5 and e["y"] == -0.5
    assert e["size"] == 0.5


def test_nan_and_garbage_numbers_fall_back_to_defaults():
    e = clean_element({"type": "text", "x": float("nan"), "size": "big", "alpha": None})
    assert e["x"] == 0.05
    assert e["size"] == 0.09
    assert e["alpha"] == 1.0


def test_only_real_hex_colours_get_through():
    assert clean_element({"type": "box", "color": "#ff0000"})["color"] == "#ff0000"
    assert clean_element({"type": "box", "color": "red"})["color"] == "#ffffff"
    assert clean_element({"type": "box", "color": "javascript:x"})["color"] == "#ffffff"


def test_text_is_length_capped():
    e = clean_element({"type": "text", "text": "x" * 5000})
    assert len(e["text"]) == 120


def test_text_elements_always_get_an_outline():
    """Unreadable text over a photo is the default failure of SSTV overlays."""
    e = clean_element({"type": "text", "text": "{mycall}"})
    assert e["outline_width"] > 0


def test_element_count_is_capped():
    t = clean_template({"elements": [{"type": "box"}] * 500})
    assert len(t["elements"]) == MAX_ELEMENTS


def test_a_template_of_pure_garbage_still_loads_as_empty():
    t = clean_template("not a template at all")
    assert t["elements"] == []
    assert t["name"] == "Untitled"


def test_slugs_are_filesystem_safe():
    assert slug("My Neon!") == "my-neon"
    assert slug("../../etc/passwd") == "etc-passwd"
    assert slug("") == "template"


# ── built-ins ────────────────────────────────────────────────────────────

def test_builtins_all_survive_validation_unchanged():
    for t in BUILTINS:
        c = clean_template(t, tid=t["id"])
        assert len(c["elements"]) == len(t["elements"]), t["id"]


def test_builtins_only_use_known_macros():
    from cyberrig.sstv.templates import MACROS, _MACRO_RE
    for t in BUILTINS:
        for e in t["elements"]:
            for m in _MACRO_RE.findall(e.get("text", "")):
                assert m in MACROS, f"{t['id']} uses unknown macro {m}"


# ── store ────────────────────────────────────────────────────────────────

def test_store_lists_builtins_even_with_no_directory(tmp_path):
    s = TemplateStore(tmp_path / "does-not-exist")
    ids = [t["id"] for t in s.list()]
    assert builtin_ids() <= set(ids)


def test_save_and_get_round_trip(tmp_path):
    s = TemplateStore(tmp_path)
    t = s.save({"name": "My Card", "elements": [
        {"type": "text", "text": "{mycall}", "x": 0.1, "y": 0.1}]})
    assert t["id"] == "my-card"
    assert t["builtin"] is False
    got = s.get("my-card")
    assert got["name"] == "My Card"
    assert len(got["elements"]) == 1


def test_saving_is_atomic_and_leaves_no_temp_file(tmp_path):
    s = TemplateStore(tmp_path)
    s.save({"name": "Thing", "elements": []})
    assert not list(tmp_path.glob("*.tmp"))
    assert (tmp_path / "thing.json").exists()


def test_a_user_template_cannot_take_a_builtin_name(tmp_path):
    """Shadowing a built-in is how you end up unable to get back to one."""
    s = TemplateStore(tmp_path)
    t = s.save({"name": "Clean Bar", "elements": []})
    assert t["id"] != "clean-bar"
    assert s.get("clean-bar")["builtin"] is True


def test_a_hand_written_file_cannot_shadow_a_builtin_either(tmp_path):
    (tmp_path / "neon.json").write_text(json.dumps(
        {"name": "Impostor", "elements": []}))
    s = TemplateStore(tmp_path)
    neon = [t for t in s.list() if t["id"] == "neon"]
    assert len(neon) == 1
    assert neon[0]["builtin"] is True
    assert any(t["id"] == "neon-user" for t in s.list())


def test_an_unreadable_file_is_skipped_not_fatal(tmp_path):
    (tmp_path / "broken.json").write_text("{ this is not json")
    (tmp_path / "fine.json").write_text(json.dumps({"name": "Fine", "elements": []}))
    s = TemplateStore(tmp_path)
    ids = [t["id"] for t in s.list()]
    assert "fine" in ids
    assert "broken" not in ids


def test_builtins_cannot_be_deleted(tmp_path):
    s = TemplateStore(tmp_path)
    r = s.delete("neon")
    assert not r["ok"]
    assert s.get("neon") is not None


def test_delete_removes_a_user_template(tmp_path):
    s = TemplateStore(tmp_path)
    s.save({"name": "Temp", "elements": []})
    assert s.delete("temp")["ok"]
    assert s.get("temp") is None


def test_deleting_something_that_is_not_there_says_so(tmp_path):
    assert not TemplateStore(tmp_path).delete("ghost")["ok"]


def test_a_malicious_id_cannot_escape_the_template_directory(tmp_path):
    s = TemplateStore(tmp_path / "tpl")
    s.save({"name": "../../pwned", "elements": []})
    assert not (tmp_path / "pwned.json").exists()
    assert list((tmp_path / "tpl").glob("*.json"))


# ── the received-picture inset ───────────────────────────────────────────

def test_an_rx_inset_survives_a_round_trip_through_disk():
    """Sending a picture back is a template element, so it has to persist."""
    from cyberrig.sstv.templates import clean_element
    e = clean_element({"type": "rx", "x": 0.14, "y": 0.2, "w": 0.72, "h": 0.54,
                       "border": "#33ff99", "border_width": 0.008})
    assert e is not None
    assert e["type"] == "rx"
    assert (e["x"], e["y"], e["w"], e["h"]) == (0.14, 0.2, 0.72, 0.54)
    assert e["border"] == "#33ff99"


def test_an_rx_inset_names_no_picture():
    """It must not carry a gallery id.

    A template outlives the pictures in the gallery; an id stored here would
    point at something deleted long before the template was.
    """
    from cyberrig.sstv.templates import clean_element
    e = clean_element({"type": "rx", "id": "20260826-181309-Martin1",
                       "src": "20260826-181309-Martin1"})
    assert "src" not in e and "id" not in e


def test_a_hostile_rx_inset_is_clamped_like_every_other_element():
    from cyberrig.sstv.templates import clean_element
    e = clean_element({"type": "rx", "border": "javascript:alert(1)",
                       "border_width": 99, "x": -50, "w": 900})
    assert e["border"] == "#00ff9c"
    assert e["border_width"] <= 0.05
    assert -0.5 <= e["x"] <= 1.5
    assert e["w"] <= 2.0


def test_the_send_back_builtin_carries_exactly_one_inset():
    from cyberrig.sstv.templates import BUILTINS, clean_template
    t = next(t for t in BUILTINS if t["id"] == "send-back")
    c = clean_template(t, "send-back")
    rx = [e for e in c["elements"] if e["type"] == "rx"]
    assert len(rx) == 1
    # Room for a header above and the callsign below, or it will not read at
    # 160 pixels wide.
    assert rx[0]["y"] > 0.1 and rx[0]["y"] + rx[0]["h"] < 0.78


def test_the_send_back_card_still_fits_the_narrowest_mode():
    """Text `size` is a fraction of HEIGHT, so it cannot see that Martin 2 is
    half as WIDE as Scottie 1 at the same 256 lines. A header sized for 320
    runs off both edges at 160 — which it did, caught by rendering the page
    headlessly at all three geometries. This is the cheap standing guard.
    """
    from cyberrig.sstv.templates import BUILTINS, clean_template, expand_macros
    t = clean_template(next(t for t in BUILTINS if t["id"] == "send-back"),
                       "send-back")
    vars = {"call": "WB4OFN", "mycall": "K1ABC", "rst": "599", "grid": "FN42aa"}
    # Share Tech Mono / VT323 advance is very close to half the em box.
    W, H, EM = 160, 256, 0.5
    for e in t["elements"]:
        if e["type"] != "text":
            continue
        txt = expand_macros(e["text"], vars)
        width = len(txt) * EM * e["size"] * H
        assert width <= W, (
            f"{txt!r} is {width:.0f}px wide at Martin 2's {W}px — shorten it "
            f"or drop its size below {W / (len(txt) * EM * H):.3f}")
