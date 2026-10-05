"""FTDX10 meter calibration — raw RM/SM counts to real-world units.

The FTDX10 CAT reference manual defines the RM answer payload as a bare
``P2: 0 - 255`` and stops there: no units, no full scale, no curve, for any of
the eight meter types. Yaesu documents none of it. These tables come from
Hamlib 4.5.4:

    PO, SWR                  rigs/yaesu/ftdx10.h  — FTDX10-specific
                                                    (PO via FLRig/W1HKJ,
                                                     SWR bench-tested by G3VPX)
    ALC, COMP, VDD, IDD      rigs/yaesu/newcat.c  — yaesu_default_*, applied
                                                    to every newcat radio
    S-meter                  rigs/yaesu/newcat.c  — yaesu_default_str_cal

Two traps this module exists to prevent:

* **Full scale is not 255.** ALC saturates at raw 64, so dividing by 255 shows
  a rig in hard ALC limiting as a comfortable 25%. PO reaches 100 W at raw 205
  (~24% low if scaled naively) and VDD reads 13.8 V at raw 196 (~23% low, which
  looks exactly like a failing power supply).
* **Every curve except IDD is piecewise and nonlinear.** COMP moves 2.5 dB over
  its first 40 counts and 2.5 dB over its next 20.

Interpolation matches Hamlib's ``rig_raw2val_float()``: piecewise linear
between breakpoints, clamped at both ends.

Only two of the six TX tables were measured on an FTDX10 — the rest are
Yaesu-wide defaults, and the SWR curve was actually taken on an FTDX101D. They
are correct in shape and far better than guesses, but if a reading ever
disagrees with the radio's own meter, fix the numbers here. That is the whole
point of keeping them in one place.
"""

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class MeterSpec:
    """One meter source: how to read it and how to draw it."""

    rm: int                       #: CAT RM type (RM1/RM3…RM8)
    key: str                      #: short name used in settings + the UI
    unit: str                     #: appended to the formatted reading
    decimals: int                 #: decimal places in the reading
    full_raw: int                 #: raw count that puts the bar at 100%
    ticks: Sequence[float]        #: values to label along the scale
    warn_raw: int                 #: bar turns amber at/above this raw count
    crit_raw: int                 #: bar turns red at/above this raw count
    cal: Sequence[tuple]          #: ((raw, value), …) ascending in raw
    tx_only: bool = True          #: meaningful only while transmitting

    # ── conversions ──────────────────────────────────────────────────────

    def to_value(self, raw: float) -> float:
        """Raw count → real-world value, piecewise linear, clamped."""
        return _interp(self.cal, raw, 0, 1)

    def to_raw(self, value: float) -> float:
        """Real-world value → raw count. The inverse, for placing ticks."""
        return _interp(self.cal, value, 1, 0)

    def fraction(self, raw: float) -> float:
        """Raw count → 0.0-1.0 bar fill, clamped."""
        if self.full_raw <= 0:
            return 0.0
        return max(0.0, min(1.0, raw / self.full_raw))

    def tick_fraction(self, value: float) -> float:
        """Where a scale label belongs, 0.0-1.0.

        Positioned through the curve, so a nonlinear meter gets a nonlinear
        face — the way the printed scale on the radio itself behaves. Even
        spacing here would be a lie the operator has to correct for.
        """
        if self.full_raw <= 0:
            return 0.0
        return max(0.0, min(1.0, self.to_raw(value) / self.full_raw))

    def format(self, raw: Optional[float]) -> str:
        if raw is None:
            return "--"
        return format_number(self.to_value(raw), self.decimals) + self.unit

    def zone(self, raw: float) -> str:
        """'ok' | 'warn' | 'crit' — drives the bar colour."""
        if raw >= self.crit_raw:
            return "crit"
        if raw >= self.warn_raw:
            return "warn"
        return "ok"


def _interp(cal: Sequence[tuple], x: float, xi: int, yi: int) -> float:
    """Piecewise-linear lookup through `cal`, reading column `xi` → `yi`.

    Clamps outside the table rather than extrapolating: past the last
    breakpoint the curve is unknown, and inventing a slope there is how a
    meter ends up confidently wrong at exactly the moment it matters.
    """
    if not cal:
        return 0.0
    first, last = cal[0], cal[-1]
    if x <= first[xi]:
        return float(first[yi])
    if x >= last[xi]:
        return float(last[yi])
    for i in range(len(cal) - 1):
        a, b = cal[i], cal[i + 1]
        if a[xi] <= x <= b[xi]:
            span = b[xi] - a[xi]
            if span == 0:
                return float(a[yi])
            return float(a[yi] + (b[yi] - a[yi]) * (x - a[xi]) / span)
    return float(last[yi])


def format_number(v: float, decimals: int) -> str:
    if decimals <= 0:
        return str(int(round(v)))
    return f"{v:.{decimals}f}"


# ── TX meter sources, in the order the selector cycles them ──────────────

SWR = MeterSpec(
    rm=6, key="SWR", unit="", decimals=1,
    full_raw=236, ticks=(1, 1.5, 2, 3, 5),
    warn_raw=89, crit_raw=126,
    cal=((0, 1.0), (26, 1.2), (52, 1.5), (89, 2.0), (126, 3.0),
         (173, 4.0), (236, 5.0), (255, 25.0)),
)

ALC = MeterSpec(
    # Full scale is raw 64, NOT 255. Do not "fix" this to 255.
    rm=4, key="ALC", unit="%", decimals=0,
    full_raw=64, ticks=(0, 25, 50, 75, 100),
    warn_raw=58, crit_raw=64,
    cal=((0, 0.0), (64, 100.0)),
)

COMP = MeterSpec(
    rm=3, key="COMP", unit=" dB", decimals=1,
    full_raw=220, ticks=(0, 5, 10, 15, 20),
    warn_raw=150, crit_raw=195,
    cal=((0, 0.0), (40, 2.5), (60, 5.0), (85, 7.5), (135, 10.0),
         (150, 12.5), (175, 15.0), (195, 17.5), (220, 20.0)),
)

IDD = MeterSpec(
    rm=7, key="IDD", unit=" A", decimals=1,
    full_raw=255, ticks=(0, 5, 10, 15, 20, 25),
    warn_raw=200, crit_raw=240,
    cal=((0, 0.0), (100, 10.0), (255, 25.5)),
)

VDD = MeterSpec(
    # 13.8 V sits at raw 196. A naive raw/255*18 renders a healthy supply as
    # 10.6 V, which looks exactly like a PSU about to fail.
    rm=8, key="VDD", unit=" V", decimals=1,
    full_raw=255, ticks=(0, 5, 10, 13.8, 18),
    warn_raw=999, crit_raw=999,
    cal=((0, 0.0), (196, 13.8), (255, 17.95)),
    tx_only=False,
)

PO = MeterSpec(
    # 100 W is raw 205, and the curve is compressive below that.
    rm=5, key="PO", unit=" W", decimals=0,
    full_raw=205, ticks=(0, 5, 25, 50, 75, 100),
    warn_raw=176, crit_raw=999,
    cal=((0, 0.0), (27, 5.0), (94, 25.0), (147, 50.0), (176, 75.0), (205, 100.0)),
)

#: Selector order in the meter popup menu.
TX_METERS = (SWR, ALC, COMP, IDD, VDD, PO)

#: Lookup by short name (what gets persisted in settings).
BY_KEY = {m.key: m for m in TX_METERS}

#: Lookup by CAT RM type.
BY_RM = {m.rm: m for m in TX_METERS}


# ── S-meter (SM0, not RM) ────────────────────────────────────────────────
#
# Raw is 0-255 with S9 at raw 130, per yaesu_default_str_cal. The legacy
# arc widget assumed 0-30, which is why it pinned at the slightest signal.

SMETER = MeterSpec(
    rm=1, key="S", unit=" dB", decimals=0,
    full_raw=255, ticks=(-48, -36, -24, -12, 0, 20, 40, 60),
    warn_raw=157, crit_raw=203,
    cal=((0, -54), (26, -42), (51, -30), (81, -18), (105, -9), (130, 0),
         (157, 12), (186, 25), (203, 35), (237, 50), (255, 60)),
    tx_only=False,
)

#: dB-over-S9 → the label printed on the scale.
_S_LABELS = {
    -48: "1", -36: "3", -24: "5", -12: "7", 0: "9",
    20: "+20", 40: "+40", 60: "+60",
}


def smeter_tick_label(db: float) -> str:
    return _S_LABELS.get(int(db), f"{db:+.0f}")


def smeter_reading(raw: Optional[float]) -> str:
    """Raw SM count → 'S7' or 'S9+20'."""
    if raw is None:
        return "--"
    db = SMETER.to_value(raw)
    if db >= 0:
        # Round to the nearest 10 dB the way a real scale is marked.
        over = int(round(db / 10.0) * 10)
        return "S9" if over <= 0 else f"S9+{over}"
    s = 9 + db / 6.0          # 6 dB per S-unit below S9
    return f"S{max(0, int(round(s)))}"
