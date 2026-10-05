"""Callsign -> DXCC entity, from AD1C's cty.dat.

This is the piece that makes roster filtering possible at all: a decode line gives
us "CQ RI1ANC KC93" and the roster needs to know that is Antarctica, entity 13,
and that we have never confirmed it.

Two lookups, in priority order, because cty.dat is built that way:

1. **Exact callsign overrides** (`=RI1ANC`). cty.dat carries these precisely for
   the callsigns whose prefix lies about where they are -- which is most of the
   interesting ones. A DXpedition operating as 3Y0J is not Norway. Check these
   FIRST or the prefix table will confidently give you the wrong entity for
   exactly the stations you care most about.
2. **Longest prefix match.** Longest wins, not first: `VP8` is Falklands but
   `VP8/h` variants and `VP8ANT`-style South Shetland calls hang off longer
   strings. A first-match-wins loop silently mislabels them.

Portable indicators are stripped before matching, with one asymmetry worth
knowing: in `EA8/KI5WKB` the LOCATION is the prefix (Canary Islands), while in
`KI5WKB/P` the suffix is noise. So we pick the *shortest* `/`-separated part as
the locating token when it looks like a prefix, and drop known junk suffixes
outright. This is the same heuristic WSJT-X and GridTracker use; it is not
perfect and cannot be -- `/MM` (maritime mobile) has no entity at all.

cty.dat ships with JTDX (`/usr/share/jtdx/cty.dat`) and ATNO keeps a copy at
`~/atno-dashboard/data/cty.dat`. Either is fine; they are the same AD1C file.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("cyberrig.ft8.cty")

# Suffixes that say something about the operator, not about where they are.
# /P /M /QRP etc. carry no entity information and must be discarded before
# matching or "K1ABC/QRP" tries to resolve against prefix "QRP".
JUNK_SUFFIXES = {
    "P", "M", "MM", "AM", "QRP", "A", "B", "J", "LH", "LGT", "R", "AG", "AE", "T",
}

# A callsign-ish token: at least one digit, letters around it.
CALL_RE = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]*$")


class Entity:
    __slots__ = ("name", "cq", "itu", "continent", "lat", "lon", "gmt", "primary")

    def __init__(self, name, cq, itu, continent, lat, lon, gmt, primary):
        self.name = name
        self.cq = cq
        self.itu = itu
        self.continent = continent
        self.lat = lat
        self.lon = lon
        self.gmt = gmt
        self.primary = primary

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Entity {self.primary} {self.name!r}>"


class CtyDatabase:
    """Parsed cty.dat with exact-call and longest-prefix lookup."""

    def __init__(self):
        self.entities: dict[str, Entity] = {}   # primary prefix -> Entity
        self.exact: dict[str, Entity] = {}      # =CALLSIGN -> Entity
        self.prefixes: dict[str, Entity] = {}   # prefix -> Entity
        self._max_prefix = 0

    # ---------------------------------------------------------------- parsing

    @classmethod
    def load(cls, path: Path) -> "CtyDatabase":
        db = cls()
        text = Path(path).read_text(encoding="utf-8", errors="replace")

        # Records are separated by ';'. Each starts with 8 colon-delimited header
        # fields on the first line, then a comma-separated alias list.
        for raw in text.split(";"):
            rec = raw.strip()
            if not rec:
                continue
            head, _, aliases = rec.partition("\n")
            fields = [f.strip() for f in head.split(":")]
            if len(fields) < 8:
                continue
            try:
                ent = Entity(
                    name=fields[0],
                    cq=int(fields[1] or 0),
                    itu=int(fields[2] or 0),
                    continent=fields[3],
                    lat=float(fields[4] or 0.0),
                    lon=float(fields[5] or 0.0),
                    gmt=float(fields[6] or 0.0),
                    primary=fields[7].lstrip("*"),
                )
            except ValueError:
                continue

            db.entities[ent.primary] = ent
            db._add_alias(ent.primary, ent)

            for alias in aliases.replace("\n", "").split(","):
                token = alias.strip()
                if not token:
                    continue
                # Strip per-alias CQ/ITU/lat-lon overrides: VE1(5)[9]<45/-60>{NA}~x~
                token = re.sub(r"[\(\[<\{~][^\)\]>\}~]*[\)\]>\}~]", "", token).strip()
                if not token:
                    continue
                if token.startswith("="):
                    db.exact[token[1:].upper()] = ent
                else:
                    db._add_alias(token.upper(), ent)

        log.info(
            "cty.dat loaded: %d entities, %d prefixes, %d exact-call overrides",
            len(db.entities), len(db.prefixes), len(db.exact),
        )
        return db

    def _add_alias(self, prefix: str, ent: Entity):
        if not prefix:
            return
        # Longest-match relies on knowing the longest key we hold.
        self.prefixes[prefix] = ent
        self._max_prefix = max(self._max_prefix, len(prefix))

    # ---------------------------------------------------------------- lookup

    @staticmethod
    def locating_token(callsign: str) -> str:
        """Reduce a possibly-portable callsign to the part that names a place.

        `EA8/KI5WKB` -> `EA8`   (prefix wins: the operator is in the Canaries)
        `KI5WKB/P`   -> `KI5WKB` (junk suffix dropped)
        `VP2E/K1ABC` -> `VP2E`
        """
        call = (callsign or "").upper().strip()
        if "/" not in call:
            return call

        parts = [p for p in call.split("/") if p]
        if not parts:
            return call
        parts = [p for p in parts if p not in JUNK_SUFFIXES] or parts

        if len(parts) == 1:
            return parts[0]

        # A bare digit reassigns the call area within the same entity (K1ABC/4);
        # it never changes the entity, so keep the real call.
        non_digit = [p for p in parts if not p.isdigit()]
        if len(non_digit) == 1:
            return non_digit[0]

        # Otherwise the SHORTER token is the added prefix and locates the operator.
        # (EA8/KI5WKB -> EA8). Ties fall back to the first part.
        non_digit.sort(key=len)
        return non_digit[0]

    def lookup(self, callsign: str) -> Optional[Entity]:
        call = (callsign or "").upper().strip()
        if not call:
            return None

        # 1. Exact overrides beat everything, including on the full portable form.
        hit = self.exact.get(call)
        if hit:
            return hit

        token = self.locating_token(call)
        hit = self.exact.get(token)
        if hit:
            return hit

        # 2. Longest prefix match.
        for n in range(min(len(token), self._max_prefix), 0, -1):
            hit = self.prefixes.get(token[:n])
            if hit:
                return hit
        return None


def find_cty_dat() -> Optional[Path]:
    """Locate a cty.dat without making the caller care which app installed it."""
    env = os.environ.get("CYBERRIG_CTY_DAT")
    if env and Path(env).is_file():
        return Path(env)
    for candidate in (
        Path("/usr/share/jtdx/cty.dat"),
        Path("/usr/share/wsjtx/cty.dat"),
        Path.home() / ".local" / "share" / "JTDX" / "cty.dat",
    ):
        if candidate.is_file():
            return candidate
    return None
