"""SSTV overlay templates: a JSON layer list, stored and validated here.

Rendering happens in the browser, on the same canvas that gets transmitted,
so what the operator frames is literally the pixels that go out. This module
owns the *model*: what a template may contain, what the built-ins are, and how
they are read and written on disk.

Coordinates are fractions of the image, never pixels. One template has to look
right on Martin 2's 160x256 and PD180's 640x496 alike, so nothing here may
carry an absolute size.

Everything loaded from disk is clamped and type-checked before it reaches the
page. A hand-edited template file is untrusted input like any other.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("cyberrig.sstv")

# "rx" is the received-picture inset: the MMSSTV/YONIQ habit of sending a
# small copy of what you just decoded back to the other station, so they can
# see how their own signal actually landed. It carries no image reference —
# the page has ONE "picture to send back" selection, and this element only
# says where it goes and how big. Storing a gallery id here would rot the
# moment that picture was deleted.
ELEMENT_TYPES = ("text", "box", "bar", "rx")
ALIGNS = ("left", "center", "right")
# The three bundled faces, by role. These are drawn INTO the
# transmitted picture, so the choice is not cosmetic: VT323 in particular
# holds up at 320x256 where a fine-stroked face turns to mush.
FONTS = ("mono", "display", "sans")

MAX_ELEMENTS = 24
MAX_TEXT = 120

# Macros a text element may use. The page fills these from rig state plus the
# QSO fields; anything unknown is left empty rather than printed raw.
MACROS = ("mycall", "call", "rst", "grid", "date", "utc", "freq", "band", "mode")

_MACRO_RE = re.compile(r"\{([a-z]+)\}")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def expand_macros(text: str, ctx: dict) -> str:
    """Replace {macro} with its value. Unknown or empty macros vanish."""
    def sub(m):
        return str(ctx.get(m.group(1), "") or "")
    return _MACRO_RE.sub(sub, str(text or ""))


def slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    s = _SLUG_RE.sub("-", s.lower()).strip("-")
    return s or "template"


def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:                       # NaN
        return default
    return max(lo, min(hi, f))


def _color(v, default: str) -> str:
    s = str(v or "")
    return s if _HEX_RE.match(s) else default


def _pick(v, allowed, default):
    s = str(v or "").lower()
    return s if s in allowed else default


def clean_element(raw: Any) -> Optional[dict]:
    """One element, clamped into range. Returns None if it is not usable."""
    if not isinstance(raw, dict):
        return None
    kind = _pick(raw.get("type"), ELEMENT_TYPES, None)
    if kind is None:
        return None
    e = {
        "type": kind,
        # Allowed slightly outside 0-1 so an element can bleed off the edge.
        "x": _num(raw.get("x"), -0.5, 1.5, 0.05),
        "y": _num(raw.get("y"), -0.5, 1.5, 0.05),
        "color": _color(raw.get("color"), "#ffffff"),
        "alpha": _num(raw.get("alpha"), 0.0, 1.0, 1.0),
    }
    if kind == "text":
        e["text"] = str(raw.get("text") or "")[:MAX_TEXT]
        e["size"] = _num(raw.get("size"), 0.02, 0.5, 0.09)
        e["align"] = _pick(raw.get("align"), ALIGNS, "left")
        e["font"] = _pick(raw.get("font"), FONTS, "mono")
        e["bold"] = bool(raw.get("bold", True))
        # An outline is not decoration: text sits over an arbitrary photo and
        # is unreadable at SSTV resolution without one.
        e["outline"] = _color(raw.get("outline"), "#000000")
        e["outline_width"] = _num(raw.get("outline_width"), 0.0, 0.02, 0.006)
    else:
        e["w"] = _num(raw.get("w"), 0.0, 2.0, 1.0)
        e["h"] = _num(raw.get("h"), 0.0, 2.0, 0.12)
        if kind == "bar":
            e["color2"] = _color(raw.get("color2"), e["color"])
        if kind == "rx":
            # A frame is what makes an inset read as "their picture" rather
            # than as part of ours. Zero width is allowed for a bare inset.
            e["border"] = _color(raw.get("border"), "#00ff9c")
            e["border_width"] = _num(raw.get("border_width"), 0.0, 0.05, 0.008)
    return e


def clean_template(raw: Any, tid: Optional[str] = None) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    els = []
    for r in (raw.get("elements") or [])[:MAX_ELEMENTS]:
        e = clean_element(r)
        if e is not None:
            els.append(e)
    name = str(raw.get("name") or "Untitled")[:48]
    return {
        "id": tid or slug(raw.get("id") or name),
        "name": name,
        "elements": els,
        "builtin": bool(raw.get("builtin", False)),
    }


# ── built-ins ────────────────────────────────────────────────────────────
# Shipped in code so a fresh install always has something to send, and so the
# store can never end up empty. They are read-only; DUPLICATE to edit one.

def _t(tid, name, elements) -> dict:
    return {"id": tid, "name": name, "elements": elements, "builtin": True}


BUILTINS: list[dict] = [
    _t("blank", "Blank", []),

    _t("corner-call", "Corner Call", [
        {"type": "text", "text": "{mycall}", "x": 0.03, "y": 0.03,
         "size": 0.13, "color": "#ffffff", "align": "left"},
        {"type": "text", "text": "{grid}", "x": 0.03, "y": 0.17,
         "size": 0.06, "color": "#ffffff", "align": "left"},
    ]),

    _t("clean-bar", "Clean Bar", [
        {"type": "box", "x": 0.0, "y": 0.82, "w": 1.0, "h": 0.18,
         "color": "#000000", "alpha": 0.62},
        {"type": "text", "text": "{mycall}", "x": 0.03, "y": 0.845,
         "size": 0.11, "color": "#ffffff", "align": "left"},
        {"type": "text", "text": "{grid}  {freq}", "x": 0.97, "y": 0.865,
         "size": 0.065, "color": "#cfe8d8", "align": "right"},
    ]),

    _t("report-card", "Report Card", [
        {"type": "box", "x": 0.0, "y": 0.0, "w": 1.0, "h": 0.20,
         "color": "#000000", "alpha": 0.66},
        {"type": "text", "text": "{call}", "x": 0.03, "y": 0.025,
         "size": 0.14, "color": "#ffffff", "align": "left"},
        {"type": "text", "text": "{rst}", "x": 0.97, "y": 0.035,
         "size": 0.12, "color": "#ffd166", "align": "right"},
        {"type": "box", "x": 0.0, "y": 0.86, "w": 1.0, "h": 0.14,
         "color": "#000000", "alpha": 0.66},
        {"type": "text", "text": "DE {mycall} {grid}", "x": 0.03, "y": 0.885,
         "size": 0.085, "color": "#ffffff", "align": "left"},
    ]),

    _t("neon", "Neon", [
        {"type": "bar", "x": 0.0, "y": 0.0, "w": 1.0, "h": 0.035,
         "color": "#33ff99", "color2": "#44bbff", "alpha": 0.95},
        {"type": "box", "x": 0.0, "y": 0.78, "w": 1.0, "h": 0.22,
         "color": "#060b06", "alpha": 0.72},
        {"type": "bar", "x": 0.0, "y": 0.78, "w": 1.0, "h": 0.008,
         "color": "#33ff99", "color2": "#44bbff", "alpha": 0.95},
        {"type": "text", "text": "{mycall}", "x": 0.03, "y": 0.785,
         "size": 0.155, "color": "#33ff99", "align": "left", "font": "display",
         "outline": "#001b0e", "outline_width": 0.008},
        {"type": "text", "text": "{grid} · {band} · {mode}", "x": 0.03, "y": 0.935,
         "size": 0.055, "color": "#66cc88", "align": "left"},
        {"type": "text", "text": "{utc}", "x": 0.97, "y": 0.925,
         "size": 0.07, "color": "#44bbff", "align": "right", "font": "display"},
    ]),

    # The send-back card. Deliberately usable with NO photo loaded: the
    # received picture is the content, and the point of transmitting it is to
    # show the other station how their own signal actually landed here. It is
    # laid out for 4:3, and the inset is centred with room for a header and a
    # footer so it still reads on a 160-pixel-wide Martin 2.
    # Text `size` is a fraction of HEIGHT, which is the same 256 on Martin 2
    # as on Scottie 1 — but Martin 2 is only 160 pixels WIDE. A header that
    # fits at 320 runs off both edges at 160, so every string here is kept
    # short and small enough to fit the narrowest mode we send. Checked by
    # rendering at 160x256, 320x256 and 640x496.
    _t("send-back", "Send Back", [
        {"type": "box", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0,
         "color": "#04070a", "alpha": 1.0},
        {"type": "bar", "x": 0.0, "y": 0.0, "w": 1.0, "h": 0.03,
         "color": "#33ff99", "color2": "#44bbff", "alpha": 0.95},
        {"type": "text", "text": "{call}", "x": 0.5, "y": 0.055,
         "size": 0.085, "color": "#33ff99", "align": "center", "font": "display",
         "outline": "#001b0e", "outline_width": 0.008},
        {"type": "text", "text": "AS RECEIVED", "x": 0.5, "y": 0.145,
         "size": 0.05, "color": "#66cc88", "align": "center", "font": "mono",
         "outline": "#000000", "outline_width": 0.006},
        {"type": "rx", "x": 0.10, "y": 0.215, "w": 0.80, "h": 0.53,
         "border": "#33ff99", "border_width": 0.008},
        {"type": "text", "text": "{rst}", "x": 0.5, "y": 0.775,
         "size": 0.075, "color": "#44bbff", "align": "center", "font": "display",
         "outline": "#00121b", "outline_width": 0.008},
        {"type": "text", "text": "DE {mycall}", "x": 0.5, "y": 0.88,
         "size": 0.062, "color": "#eaf6ff", "align": "center", "font": "mono",
         "outline": "#000000", "outline_width": 0.007},
    ]),
]


def builtin_ids() -> set:
    return {t["id"] for t in BUILTINS}


# ── store ────────────────────────────────────────────────────────────────

class TemplateStore:
    """Built-ins plus whatever the operator has saved. Built-ins always win."""

    def __init__(self, directory):
        self.dir = Path(directory)

    def _path(self, tid: str) -> Path:
        return self.dir / f"{slug(tid)}.json"

    def list(self) -> list[dict]:
        out = [dict(t) for t in BUILTINS]
        seen = builtin_ids()
        try:
            files = sorted(self.dir.glob("*.json"))
        except OSError:
            files = []
        for f in files:
            try:
                raw = json.loads(f.read_text())
            except (OSError, ValueError):
                log.warning("SSTV template %s is unreadable, skipping", f.name)
                continue
            t = clean_template(raw, tid=f.stem)
            t["builtin"] = False
            if t["id"] in seen:
                # A user file must never shadow a built-in — that is how you
                # end up unable to get back to a working template.
                t["id"] = t["id"] + "-user"
            seen.add(t["id"])
            out.append(t)
        return out

    def get(self, tid: str) -> Optional[dict]:
        for t in self.list():
            if t["id"] == tid:
                return t
        return None

    def save(self, raw: Any) -> dict:
        t = clean_template(raw)
        if t["id"] in builtin_ids():
            # Saving over a built-in silently would lose the original.
            t["id"] = t["id"] + "-user"
        t["builtin"] = False
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(t["id"]).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(t, indent=2))
        tmp.replace(self._path(t["id"]))
        log.info("SSTV template saved: %s", t["id"])
        return t

    def delete(self, tid: str) -> dict:
        if tid in builtin_ids():
            return {"ok": False, "error": "built-in templates cannot be deleted"}
        p = self._path(tid)
        if not p.exists():
            return {"ok": False, "error": "no such template"}
        p.unlink()
        log.info("SSTV template deleted: %s", tid)
        return {"ok": True}


# ── macro context ────────────────────────────────────────────────────────

# Band names come from cyberrig.bands, which owns the band-edge table for the
# whole app. Re-exported here because callers (and tests) import band_for_hz
# from this module.
from cyberrig.bands import band_for_hz  # noqa: F401,E402


def macro_context(freq_hz=None, mode: str = "", callsign: str = "",
                  grid: str = "", now=None) -> dict:
    """The macros the server can fill. The page adds {call} and {rst} itself.

    `now` is injectable so tests do not depend on the clock.
    """
    import datetime as _dt

    t = now or _dt.datetime.now(_dt.timezone.utc)
    mhz = (int(freq_hz) / 1e6) if freq_hz else 0.0
    return {
        "mycall": (callsign or "").upper(),
        "grid": grid or "",
        "freq": f"{mhz:.3f}" if mhz else "",
        "band": band_for_hz(freq_hz),
        "mode": (mode or "").upper(),
        "date": t.strftime("%Y-%m-%d"),
        "utc": t.strftime("%H:%MZ"),
    }
