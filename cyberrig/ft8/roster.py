"""Roster: decide what a decoded callsign is WORTH to this operator.

This is the GridTracker half. A decode list that shows everything is a firehose;
the useful question is "is this one I need?" ATNO already knows the answer -- it
parses the ADIF logs into a confirmed-entity set and publishes needed/confirmed/
watchlist over HTTP -- so this module is a join, not a new data model.

Classification, highest priority first:

    watchlist  -- a call ATNO is actively hunting (calls_of_interest), or any
                  station in a watchlisted entity's DXCC. These are the ones
                  worth interrupting a bath for.
    needed     -- an entity not in the confirmed set. An ATNO.
    confirmed  -- already worked and confirmed. Still shown, just quiet.
    unknown    -- callsign did not resolve to an entity at all.

The join runs on two keys, because neither alone is sufficient. Names agree most
of the time but disagree in ways that are invisible until you count them: `&` vs
`and`, `Is.` vs `Islands`, `Cote d'Ivoire` vs `Côte d'Ivoire`. A name-only join
left 71 of 346 cty entities unmatched here -- 20% of the world silently absent
from the needed filter. So names are pass one, and pass two intersects cty's
primary prefix against ATNO's `prefixes` list, which is exact where names are
fuzzy. ATNO's scalar `prefix` field is NOT usable for this (Canada is "CF" there,
"VE" in cty.dat); the `prefixes` LIST is.

`unmatched_names` is exposed deliberately: a name join that quietly drops entities
would make needed-DX vanish from the filter, which is the exact failure the filter
exists to prevent. Check it rather than trusting the join.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata
import time
from pathlib import Path
from typing import Any, Optional

import urllib.request
import json
import ssl

from cyberrig.ft8.cty import CtyDatabase

log = logging.getLogger("cyberrig.ft8.roster")

# Optional. Any service that serves an ATNO-style /api/bootstrap JSON (buckets
# "confirmed", "needed", "watchlist" of {dxcc, name, ...}). Empty disables it.
ATNO_URL = ""
REFRESH_S = 300.0

# If that service sits behind a private CA, point ATNO_CA_BUNDLE at its root
# certificate rather than disabling verification.
CA_BUNDLE_ENV = "ATNO_CA_BUNDLE"

# Abbreviation expansions applied before folding. These are the differences that
# actually occur between AD1C and ADIF spellings, not a general dictionary.
_ABBREV = [
    (r"&", " and "),
    (r"\bis\b\.?", " islands "),
    (r"\bi\b\.?", " island "),
    (r"\bst\b\.?", " saint "),
    (r"\bmt\b\.?", " mount "),
    (r"\brep\b\.?", " republic "),
    (r"\bdem\b\.?", " democratic "),
    (r"\bfed\b\.?", " federal "),
    (r"\bdpr\b\.?", " democratic peoples republic "),
]

# Names where no amount of normalizing helps because the two sources genuinely
# chose different words. Keyed by folded cty name -> folded ATNO name.
NAME_ALIASES = {
    "unitedstates": "unitedstatesofamerica",
    "fedrepofgermany": "germany",
    "unitedkingdom": "england",
    "slovakrepublic": "slovakia",
    "czechrepublic": "czechia",
}


def _norm(name: str) -> str:
    """Fold a country name to a comparison key.

    Accents stripped (Côte d'Ivoire), abbreviations expanded (Is. -> islands),
    then everything but letters removed. Articles are dropped last so that
    "Dem. Rep. of the Congo" and "Democratic Republic of the Congo" converge.
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    for pat, rep in _ABBREV:
        text = re.sub(pat, rep, text)
    text = re.sub(r"\b(the|of|and)\b", " ", text)
    key = re.sub(r"[^a-z]", "", text)
    return NAME_ALIASES.get(key, key)


class Roster:
    """ATNO's confirmed/needed/watchlist sets, joined to cty.dat resolution."""

    def __init__(self, cty: CtyDatabase, url: str = ATNO_URL,
                 ca_bundle: Optional[str] = None):
        self._cty = cty
        self._url = url
        self._ctx: Optional[ssl.SSLContext] = None
        bundle = ca_bundle or os.getenv(CA_BUNDLE_ENV) or ""
        if bundle and url.startswith("https://"):
            if Path(bundle).is_file():
                self._ctx = ssl.create_default_context(cafile=bundle)
                log.info("ATNO TLS pinned to %s", bundle)
            else:
                log.warning("%s=%s does not exist -- default trust store used",
                            CA_BUNDLE_ENV, bundle)
        self._lock = threading.Lock()
        self._by_name: dict[str, dict] = {}      # normalized name -> ATNO entity
        self._by_prefix: dict[str, dict] = {}    # cty primary prefix -> ATNO entity
        self._needed: set[str] = set()           # dxcc ids
        self._watch_dxcc: set[str] = set()
        self._watch_calls: set[str] = set()
        self._loaded_at = 0.0
        self._error: Optional[str] = None
        self.unmatched_names: list[str] = []

    # ------------------------------------------------------------------ load

    def refresh(self, force: bool = False) -> bool:
        with self._lock:
            if not force and (time.time() - self._loaded_at) < REFRESH_S:
                return True
        if not self._url:
            # No needed-entity service configured: cty.dat resolution only.
            return False
        try:
            with urllib.request.urlopen(self._url, timeout=8, context=self._ctx) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log.warning("ATNO unreachable (%s) -- roster falls back to 'unknown'", e)
            with self._lock:
                self._error = str(e)
            return False

        by_name: dict[str, dict] = {}
        by_prefix: dict[str, dict] = {}
        needed: set[str] = set()
        watch_dxcc: set[str] = set()
        watch_calls: set[str] = set()

        for bucket in ("confirmed", "needed", "watchlist", "recent"):
            for ent in data.get(bucket) or []:
                dxcc = str(ent.get("dxcc") or "")
                name = ent.get("name") or ""
                if not dxcc or not name:
                    continue
                rec = {
                    "dxcc": dxcc,
                    "name": name,
                    "continent": ent.get("continent") or "",
                    "flag": ent.get("flag") or "",
                    "distance_km": ent.get("distance_km"),
                    "bearing_deg": ent.get("bearing_deg"),
                }
                by_name.setdefault(_norm(name), rec)
                # Pass two key: every prefix ATNO lists for this entity. cty's
                # primary prefix is looked up against this when the name misses.
                for pfx in ent.get("prefixes") or []:
                    by_prefix.setdefault(str(pfx).upper().strip(), rec)

        for ent in data.get("needed") or []:
            if ent.get("dxcc"):
                needed.add(str(ent["dxcc"]))
        for ent in data.get("watchlist") or []:
            if ent.get("dxcc"):
                watch_dxcc.add(str(ent["dxcc"]))
            for c in ent.get("calls_of_interest") or []:
                watch_calls.add(str(c).upper().strip())

        # Which cty.dat entities failed BOTH passes? Surfaced, not swallowed --
        # see module docstring.
        unmatched = sorted({
            e.name for e in self._cty.entities.values()
            if _norm(e.name) not in by_name and e.primary.upper() not in by_prefix
        })

        with self._lock:
            self._by_name = by_name
            self._by_prefix = by_prefix
            self._needed = needed
            self._watch_dxcc = watch_dxcc
            self._watch_calls = watch_calls
            self._loaded_at = time.time()
            self._error = None
            self.unmatched_names = unmatched

        log.info(
            "roster: %d entities joined, %d needed, %d watchlist entities, "
            "%d watchlist calls, %d cty names unmatched",
            len(by_name), len(needed), len(watch_dxcc), len(watch_calls), len(unmatched),
        )
        return True

    # -------------------------------------------------------------- classify

    def classify(self, callsign: str) -> dict[str, Any]:
        """Resolve a callsign and say what it is worth. Never raises."""
        call = (callsign or "").upper().strip()
        out = {
            "call": call,
            "dxcc": None,
            "entity": None,
            "continent": "",
            "flag": "",
            "status": "unknown",
            "distance_km": None,
            "bearing_deg": None,
        }
        if not call:
            return out

        ent = self._cty.lookup(call)
        if ent is None:
            return out

        out["entity"] = ent.name
        out["continent"] = ent.continent

        with self._lock:
            atno = (self._by_name.get(_norm(ent.name))
                    or self._by_prefix.get(ent.primary.upper()))
            watch_calls = self._watch_calls
            watch_dxcc = self._watch_dxcc
            needed = self._needed

        if call in watch_calls:
            out["status"] = "watchlist"

        if atno:
            out["dxcc"] = atno["dxcc"]
            out["entity"] = atno["name"]
            out["flag"] = atno.get("flag") or ""
            out["continent"] = atno.get("continent") or out["continent"]
            out["distance_km"] = atno.get("distance_km")
            out["bearing_deg"] = atno.get("bearing_deg")
            if out["status"] != "watchlist":
                if atno["dxcc"] in watch_dxcc:
                    out["status"] = "watchlist"
                elif atno["dxcc"] in needed:
                    out["status"] = "needed"
                else:
                    out["status"] = "confirmed"
        return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entities": len(self._by_name),
                "prefix_keys": len(self._by_prefix),
                "needed": len(self._needed),
                "watchlist_entities": len(self._watch_dxcc),
                "watchlist_calls": sorted(self._watch_calls),
                "unmatched_cty_names": len(self.unmatched_names),
                "loaded_at": self._loaded_at,
                "error": self._error,
            }
