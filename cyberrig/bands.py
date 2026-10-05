"""HF/6m band edges, in one place.

This table lived in `cyberrig/sstv/templates.py` because the SSTV macro
context was the first thing that needed to name a band. The WSPR auto-tuner
needs the same table plus the EDGES (to keep a tuning carrier inside the
band), and a second copy of the band plan is exactly the sort of thing that
drifts silently, so it moved here and `sstv.templates` imports it.
"""

from typing import Optional, Tuple

#: Ham bands by edge, in Hz, with the metre name. Only what the FTDX10 covers.
BANDS = (
    (1_800_000, 2_000_000, "160m"), (3_500_000, 4_000_000, "80m"),
    (5_330_000, 5_410_000, "60m"), (7_000_000, 7_300_000, "40m"),
    (10_100_000, 10_150_000, "30m"), (14_000_000, 14_350_000, "20m"),
    (18_068_000, 18_168_000, "17m"), (21_000_000, 21_450_000, "15m"),
    (24_890_000, 24_990_000, "12m"), (28_000_000, 29_700_000, "10m"),
    (50_000_000, 54_000_000, "6m"),
)

#: FT8 watering holes, dial frequencies in Hz. NOT the same as the SSB calling
#: frequencies `FTdx10.go_band()` targets (20m -> 14.200), which is not where
#: anyone runs FT8. 60m is omitted deliberately: it is channelised, so there is
#: nowhere to move a tuning carrier to and HUNT must not hop there.
FT8_DIAL_HZ = {
    "160m": 1_840_000, "80m": 3_573_000, "40m": 7_074_000, "30m": 10_136_000,
    "20m": 14_074_000, "17m": 18_100_000, "15m": 21_074_000, "12m": 24_915_000,
    "10m": 28_074_000, "6m": 50_313_000,
}

#: 60 metres is five fixed USB channels, not a band you may roam inside.
#: Nothing here may pick a "nearby quiet frequency" on it.
CHANNELISED = ("60m",)


def band_for_hz(hz) -> str:
    """Band name for a frequency, or "" if it is outside every ham band."""
    try:
        f = int(hz or 0)
    except (TypeError, ValueError):
        return ""
    for lo, hi, name in BANDS:
        if lo <= f <= hi:
            return name
    return ""


def band_edges(band: str) -> Optional[Tuple[int, int]]:
    """(low, high) in Hz for a band name, or None if unknown."""
    for lo, hi, name in BANDS:
        if name == band:
            return lo, hi
    return None
