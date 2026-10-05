"""The web UI's fallback meter curves must match meter_cal.py.

The page fetches /api/meter_cal, but carries literal curves for the case
where that fetch fails. A fallback that has drifted from the source is
worse than none: it renders confidently and wrongly, and only on the one
path nobody exercises.

Found the hard way on 2026-08-26 — the S-meter never used its curve at
all. `drawSMeter` scaled the raw 0-255 SM0 count on an invented 0-50
scale, so anything above raw 50 (about S3) clamped to full deflection and
the needle sat pinned, dead still, for every real signal. /api/meter_cal
had been serving the correct S curve the whole time; the page merged only
`j.meters` and dropped `j.smeter` on the floor.
"""

import json
import re
from pathlib import Path

from cyberrig.cat import meter_cal

INDEX = Path(__file__).resolve().parent.parent / "cyberrig/web/static/index.html"


def _js_object(name: str) -> dict:
    """Pull a `let NAME = {...};` literal out of the page as JSON."""
    src = INDEX.read_text()
    m = re.search(rf"let {name} = (\{{.*?\n\}});", src, re.S)
    assert m, f"{name} literal not found in index.html"
    body = m.group(1)
    body = re.sub(r"//[^\n]*", "", body)          # strip comments
    body = re.sub(r"(\w+):", r'"\1":', body)      # quote keys
    body = body.replace("'", '"')
    body = re.sub(r",(\s*[}\]])", r"\1", body)    # trailing commas
    return json.loads(body)


def test_smeter_fallback_matches_the_python_curve():
    js = _js_object("SMETER_CAL")
    spec = meter_cal.SMETER
    assert js["full_raw"] == spec.full_raw
    assert js["warn_raw"] == spec.warn_raw
    assert js["crit_raw"] == spec.crit_raw
    assert [tuple(p) for p in js["cal"]] == [tuple(p) for p in spec.cal]
    assert js["ticks"] == list(spec.ticks)


def test_smeter_full_scale_is_not_the_invented_fifty():
    """The exact bug: raw >= 50 pinned the needle at S9+60."""
    js = _js_object("SMETER_CAL")
    assert js["full_raw"] == 255
    # Raw 147 — a real reading measured off the FTDX10 — must not be full scale.
    assert 147 / js["full_raw"] < 0.6
    assert meter_cal.SMETER.fraction(147) < 0.6


def test_ops_dock_puts_alc_between_po_and_swr():
    """The Signal/TX strip already had ALC. The bottom dock next to PTT is
    what you watch while keyed, and it was PO | SWR with a gap."""
    src = INDEX.read_text()
    po = src.find('id="po-badge"')
    alc = src.find('id="alc-badge"')
    swr = src.find('id="swr-badge"')
    assert po != -1 and alc != -1 and swr != -1
    assert po < alc < swr
    assert 'id="dock-alc-val"' in src
    assert "grid-template-columns: 1fr 1fr 1fr" in src


def test_tx_meter_fallbacks_match_too():
    """Only the three the page actually draws are carried — check those.

    COMP/IDD/VDD have no fallback on purpose; the page never renders them.
    """
    js = _js_object("METER_CAL")
    by_key = {m.key: m for m in meter_cal.TX_METERS}
    assert set(js) == {"PO", "ALC", "SWR"}, set(js)
    for key, got in js.items():
        spec = by_key[key]
        assert got["full_raw"] == spec.full_raw, key
        assert [tuple(p) for p in got["cal"]] == [tuple(p) for p in spec.cal], key


# ── AGC buttons must not depend on their captions (2026-08-26) ───────────

def test_agc_buttons_carry_the_mode_they_set():
    """The captions are abbreviated — F/M/S — so matching state against
    button text could only ever light AUTO and OFF. With AGC on MID the web
    UI showed no state at all. Each button now carries data-mode.
    """
    import re as _re

    src = INDEX.read_text()
    m = _re.search(r'id="agc-btns"(.*?)</div>', src, _re.S)
    assert m, "agc-btns group not found"
    btns = _re.findall(r'<button[^>]*>', m.group(1))
    assert len(btns) == 5, btns

    from cyberrig.cat.ftdx10 import AGC_CODES

    for b in btns:
        mode = _re.search(r'data-mode="([^"]+)"', b)
        sets = _re.search(r"setAGC\('([^']+)'\)", b)
        assert mode, f"no data-mode: {b}"
        assert sets, f"no setAGC: {b}"
        # What it advertises and what it sends must agree...
        assert mode.group(1) == sets.group(1), b
        # ...and the driver must accept it.
        assert sets.group(1) in AGC_CODES, sets.group(1)


def test_agc_paint_matches_on_data_mode_not_text():
    src = INDEX.read_text()
    assert "b.dataset.mode" in src, "AGC paint must key off data-mode"
    # The old caption comparison must be gone.
    assert "b.textContent === state.agc" not in src
