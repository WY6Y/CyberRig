"""FTDX10 EX menu definitions — corrected against CAT OM Table 2 (page 10-12).

EX command format: EX{p1:02d}{p2:02d}{p3:02d}{value};
P1 = category (01-04), P2 = sub-category (01-07), P3 = item (01-23)
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MenuItem:
    p1:      int
    p2:      int
    p3:      int
    label:   str
    vtype:   str          # 'int', 'bool', 'select'
    vmin:    int = 0
    vmax:    int = 255
    unit:    str = ""
    digits:  int = 2      # P4 digit count
    options: Optional[dict] = None
    group:   str = "General"

    @property
    def key(self) -> tuple:
        return (self.p1, self.p2, self.p3)


# fmt: off
MENU_ITEMS: list[MenuItem] = [

    # ── P1=01 RADIO SETTING / P2=01 MODE SSB ─────────────────────────────────
    MenuItem(1,1,1,  "SSB AF Treble Gain",    "int",  -10, 10, "dB", 3, group="Mode SSB"),
    MenuItem(1,1,2,  "SSB AF Mid Gain",        "int",  -10, 10, "dB", 3, group="Mode SSB"),
    MenuItem(1,1,3,  "SSB AF Bass Gain",       "int",  -10, 10, "dB", 3, group="Mode SSB"),
    MenuItem(1,1,4,  "SSB AGC Fast Delay",     "int",  20, 4000, "ms", 4, group="Mode SSB"),
    MenuItem(1,1,5,  "SSB AGC Mid Delay",      "int",  20, 4000, "ms", 4, group="Mode SSB"),
    MenuItem(1,1,6,  "SSB AGC Slow Delay",     "int",  20, 4000, "ms", 4, group="Mode SSB"),
    MenuItem(1,1,7,  "SSB LCUT Freq",          "select", 0, 19, "Hz", 2,
             options={0:"OFF",1:"100",2:"150",3:"200",4:"250",5:"300",6:"350",7:"400",
                      8:"450",9:"500",10:"600",11:"700",12:"800",13:"900",14:"1000",
                      15:"1100",16:"1200",17:"1350",18:"1500",19:"1700"}, group="Mode SSB"),
    MenuItem(1,1,8,  "SSB LCUT Slope",         "select", 0, 1, "", 1,
             options={0:"6dB/oct",1:"18dB/oct"}, group="Mode SSB"),
    MenuItem(1,1,9,  "SSB HCUT Freq",          "select", 0, 67, "Hz", 2,
             options={0:"OFF",1:"700",2:"750",67:"4000"}, group="Mode SSB"),
    MenuItem(1,1,10, "SSB HCUT Slope",         "select", 0, 1, "", 1,
             options={0:"6dB/oct",1:"18dB/oct"}, group="Mode SSB"),
    MenuItem(1,1,11, "SSB Output Level",       "int", 0, 100, "", 3, group="Mode SSB"),
    MenuItem(1,1,12, "SSB TX BPF Select",      "select", 0, 4, "", 1,
             options={0:"50~3050",1:"100~2900",2:"200~2800",3:"300~2700",4:"400~2600"},
             group="Mode SSB"),
    MenuItem(1,1,13, "SSB Mod Source",         "select", 0, 1, "", 1,
             options={0:"MIC",1:"REAR"}, group="Mode SSB"),
    MenuItem(1,1,14, "SSB Rear Select",        "select", 0, 1, "", 1,
             options={0:"DATA",1:"USB"}, group="Mode SSB"),
    MenuItem(1,1,15, "SSB Rport Gain",         "int", 0, 100, "", 3, group="Mode SSB"),
    MenuItem(1,1,16, "SSB RPTT Select",        "select", 0, 2, "", 1,
             options={0:"DAKY",1:"RTS",2:"DTR"}, group="Mode SSB"),

    # ── P1=01 / P2=02 MODE AM ────────────────────────────────────────────────
    MenuItem(1,2,1,  "AM AF Treble Gain",      "int",  -10, 10, "dB", 3, group="Mode AM"),
    MenuItem(1,2,2,  "AM AF Mid Gain",         "int",  -10, 10, "dB", 3, group="Mode AM"),
    MenuItem(1,2,3,  "AM AF Bass Gain",        "int",  -10, 10, "dB", 3, group="Mode AM"),
    MenuItem(1,2,4,  "AM AGC Fast Delay",      "int",  20, 4000, "ms", 4, group="Mode AM"),
    MenuItem(1,2,5,  "AM AGC Mid Delay",       "int",  20, 4000, "ms", 4, group="Mode AM"),
    MenuItem(1,2,6,  "AM AGC Slow Delay",      "int",  20, 4000, "ms", 4, group="Mode AM"),
    MenuItem(1,2,11, "AM Output Level",        "int",  0, 100, "", 3, group="Mode AM"),
    MenuItem(1,2,15, "AM MIC Gain",            "int", 0, 100, "", 4, group="Mode AM"),

    # ── P1=02 CW SETTING / P2=01 MODE CW ─────────────────────────────────────
    MenuItem(2,1,1,  "CW AF Treble Gain",      "int",  -10, 10, "dB", 3, group="CW Mode"),
    MenuItem(2,1,2,  "CW AF Mid Gain",         "int",  -10, 10, "dB", 3, group="CW Mode"),
    MenuItem(2,1,3,  "CW AF Bass Gain",        "int",  -10, 10, "dB", 3, group="CW Mode"),
    MenuItem(2,1,4,  "CW AGC Fast Delay",      "int",  20, 4000, "ms", 4, group="CW Mode"),
    MenuItem(2,1,5,  "CW AGC Mid Delay",       "int",  20, 4000, "ms", 4, group="CW Mode"),
    MenuItem(2,1,6,  "CW AGC Slow Delay",      "int",  20, 4000, "ms", 4, group="CW Mode"),
    MenuItem(2,1,11, "CW Output Level",        "int",  0, 100, "", 3, group="CW Mode"),
    MenuItem(2,1,12, "CW Auto Mode",           "select", 0, 2, "", 1,
             options={0:"OFF",1:"50MHz",2:"ON"}, group="CW Mode"),
    MenuItem(2,1,13, "CW BK-IN Type",          "select", 1, 2, "", 1,
             options={1:"SEMI",2:"FULL"}, group="CW Mode"),
    MenuItem(2,1,14, "CW Wave Shape",          "select", 1, 4, "ms", 1,
             options={1:"1ms",2:"2.4ms",3:"6ms"}, group="CW Mode"),
    MenuItem(2,1,15, "CW Freq Display",        "select", 0, 1, "", 1,
             options={0:"DIRECT FREQ",1:"PITCH OFFSET"}, group="CW Mode"),
    MenuItem(2,1,17, "CW QSK Delay",           "select", 0, 3, "", 1,
             options={0:"15ms",1:"20ms",2:"25ms",3:"30ms"}, group="CW Mode"),
    MenuItem(2,1,18, "CW Indicator",           "select", 0, 1, "", 1,
             options={0:"OFF",1:"ON"}, group="CW Mode"),

    # ── P1=02 / P2=02 KEYER ──────────────────────────────────────────────────
    MenuItem(2,2,1,  "Keyer Type",             "select", 0, 5, "", 1,
             options={0:"Bug",1:"BUG",2:"ELEKEY-A",3:"ELEKEY-B",4:"ELEKEY-Y",5:"ACS"},
             group="Keyer"),
    MenuItem(2,2,2,  "Keyer Dot/Dash",         "select", 0, 1, "", 1,
             options={0:"NOR",1:"REV"}, group="Keyer"),
    MenuItem(2,2,3,  "CW Weight",              "int",   25, 45, "", 2, group="Keyer"),
    MenuItem(2,2,4,  "Number Style",           "select", 0, 6, "", 1,
             options={0:"1290",1:"AUNO",2:"AUNT",3:"A2NO",4:"A2NT",5:"12NO",6:"12NT"},
             group="Keyer"),
    MenuItem(2,2,5,  "Contest Number",         "int",   1, 9999, "", 4, group="Keyer"),
    MenuItem(2,2,11, "Repeat Interval",        "int",   1, 60, "s", 2, group="Keyer"),

    # ── P1=03 OPERATION SETTING / P2=01 GENERAL ──────────────────────────────
    MenuItem(3,1,1,  "NB Width",               "select", 0, 2, "", 1,
             options={0:"1ms",1:"3ms",2:"10ms"}, group="General"),
    MenuItem(3,1,2,  "NB Rejection",           "select", 0, 2, "", 1,
             options={0:"10dB",1:"30dB",2:"50dB"}, group="General"),
    MenuItem(3,1,3,  "Beep Level",             "int",  0, 100, "", 3, group="General"),
    MenuItem(3,1,5,  "Tuner Select",           "select", 0, 3, "", 1,
             options={0:"INT",1:"EXT1",2:"EXT2",3:"EXT3"}, group="General"),
    MenuItem(3,1,6,  "232C Rate",              "select", 0, 3, "", 1,
             options={0:"4800",1:"9600",2:"19200",3:"38400"}, group="General"),
    MenuItem(3,1,8,  "CAT Rate",               "select", 0, 3, "", 1,
             options={0:"4800",1:"9600",2:"19200",3:"38400"}, group="General"),
    MenuItem(3,1,9,  "CAT Time-Out",           "select", 0, 3, "", 1,
             options={0:"10ms",1:"100ms",2:"1000ms",3:"3000ms"}, group="General"),
    MenuItem(3,1,10, "CAT RTS",                "select", 0, 1, "", 1,
             options={0:"DISABLE",1:"ENABLE"}, group="General"),
    MenuItem(3,1,14, "Quick Split Freq",        "int", -20, 20, "kHz", 3, group="General"),
    MenuItem(3,1,16, "TX Time-Out",            "int",   0, 30, "min", 2, group="General"),

    # ── P1=03 / P2=02 RX-DSP ─────────────────────────────────────────────────
    MenuItem(3,2,1,  "APF Width",              "select", 0, 2, "", 1,
             options={0:"NARROW",1:"MEDIUM",2:"WIDE"}, group="RX DSP"),
    MenuItem(3,2,2,  "Contour Level",          "int", -40, 20, "dB", 3, group="RX DSP"),
    MenuItem(3,2,3,  "Contour Width",          "int",   1, 11, "", 2, group="RX DSP"),
    MenuItem(3,2,4,  "IF Notch Width",         "select", 0, 1, "", 1,
             options={0:"NARROW",1:"WIDE"}, group="RX DSP"),

    # ── P1=03 / P2=03 TX AUDIO — PRMTRC EQ (RX DSP EQ) ─────────────────────
    # EQ1 Freq options: 00=OFF, 01-07=100-700Hz
    MenuItem(3,3,2,  "TX EQ1 Freq",           "select", 0, 7, "Hz", 2,
             options={0:"OFF",1:"100",2:"200",3:"300",4:"400",5:"500",6:"600",7:"700"},
             group="TX DSP EQ"),
    MenuItem(3,3,3,  "TX EQ1 Level",          "int", -20, 10, "dB", 3, group="TX DSP EQ"),
    MenuItem(3,3,4,  "TX EQ1 BW",             "int",   1, 10, "", 2, group="TX DSP EQ"),
    # EQ2 Freq options: 00=OFF, 01-09=700-1500Hz
    MenuItem(3,3,5,  "TX EQ2 Freq",           "select", 0, 9, "Hz", 2,
             options={0:"OFF",1:"700",2:"800",3:"900",4:"1000",5:"1100",6:"1200",
                      7:"1300",8:"1400",9:"1500"}, group="TX DSP EQ"),
    MenuItem(3,3,6,  "TX EQ2 Level",          "int", -20, 10, "dB", 3, group="TX DSP EQ"),
    MenuItem(3,3,7,  "TX EQ2 BW",             "int",   1, 10, "", 2, group="TX DSP EQ"),
    # EQ3 Freq options: 00=OFF, 01-18=1500-3200Hz
    MenuItem(3,3,8,  "TX EQ3 Freq",           "select", 0, 18, "Hz", 2,
             options={0:"OFF",1:"1500",2:"1600",3:"1700",4:"1800",5:"1900",6:"2000",
                      7:"2100",8:"2200",9:"2300",10:"2400",11:"2500",12:"2600",
                      13:"2700",14:"2800",15:"2900",16:"3000",17:"3100",18:"3200"},
             group="TX DSP EQ"),
    MenuItem(3,3,9,  "TX EQ3 Level",          "int", -20, 10, "dB", 3, group="TX DSP EQ"),
    MenuItem(3,3,10, "TX EQ3 BW",             "int",   1, 10, "", 2, group="TX DSP EQ"),

    # ── P1=03 / P2=03 TX AUDIO — P PRMTRC EQ (Parametric MIC EQ) ───────────
    MenuItem(3,3,11, "MIC EQ1 Freq",          "select", 0, 7, "Hz", 2,
             options={0:"OFF",1:"100",2:"200",3:"300",4:"400",5:"500",6:"600",7:"700"},
             group="MIC P-EQ"),
    MenuItem(3,3,12, "MIC EQ1 Level",         "int", -20, 10, "dB", 3, group="MIC P-EQ"),
    MenuItem(3,3,13, "MIC EQ1 BW",            "int",   1, 10, "", 2, group="MIC P-EQ"),
    MenuItem(3,3,14, "MIC EQ2 Freq",          "select", 0, 9, "Hz", 2,
             options={0:"OFF",1:"700",2:"800",3:"900",4:"1000",5:"1100",6:"1200",
                      7:"1300",8:"1400",9:"1500"}, group="MIC P-EQ"),
    MenuItem(3,3,15, "MIC EQ2 Level",         "int", -20, 10, "dB", 3, group="MIC P-EQ"),
    MenuItem(3,3,16, "MIC EQ2 BW",            "int",   1, 10, "", 2, group="MIC P-EQ"),
    MenuItem(3,3,17, "MIC EQ3 Freq",          "select", 0, 18, "Hz", 2,
             options={0:"OFF",1:"1500",2:"1600",3:"1700",4:"1800",5:"1900",6:"2000",
                      7:"2100",8:"2200",9:"2300",10:"2400",11:"2500",12:"2600",
                      13:"2700",14:"2800",15:"2900",16:"3000",17:"3100",18:"3200"},
             group="MIC P-EQ"),
    MenuItem(3,3,18, "MIC EQ3 Level",         "int", -20, 10, "dB", 3, group="MIC P-EQ"),
    MenuItem(3,3,19, "MIC EQ3 BW",            "int",   1, 10, "", 2, group="MIC P-EQ"),

    # ── P1=03 / P2=03 TX AUDIO — General ────────────────────────────────────
    MenuItem(3,3,1,  "AMC Release Time",       "select", 0, 2, "", 1,
             options={0:"FAST",1:"MID",2:"SLOW"}, group="TX Audio"),

    # ── P1=03 / P2=04 TX GENERAL ─────────────────────────────────────────────
    MenuItem(3,4,1,  "HF Max Power",           "int",  5, 100, "W", 3, group="TX General"),
    MenuItem(3,4,2,  "50M Max Power",          "int",  5, 100, "W", 3, group="TX General"),
    MenuItem(3,4,3,  "70M Max Power",          "int",  5,  50, "W", 3, group="TX General"),
    MenuItem(3,4,4,  "AM Max Power",           "int",  5,  25, "W", 3, group="TX General"),
    MenuItem(3,4,5,  "VOX Select",             "select", 0, 1, "", 1,
             options={0:"MIC",1:"DATA"}, group="TX General"),
    MenuItem(3,4,6,  "DATA VOX Gain",          "int",  0, 100, "", 3, group="TX General"),
    MenuItem(3,4,7,  "Emergency Freq TX",      "select", 0, 1, "", 1,
             options={0:"DISABLE",1:"ENABLE"}, group="TX General"),

    # ── P1=04 DISPLAY SETTING / P2=01 DISPLAY ────────────────────────────────
    MenuItem(4,1,1,  "RBW",                    "select", 0, 0, "", 12,
             options={}, group="Display"),
    MenuItem(4,1,2,  "My Call Time",           "select", 0, 5, "s", 1,
             options={0:"OFF",1:"0.5",2:"1",3:"2",4:"3",5:"5"}, group="Display"),
    MenuItem(4,1,3,  "Screen Saver",           "select", 0, 3, "min", 1,
             options={0:"OFF",1:"15",2:"30",3:"60"}, group="Display"),
    MenuItem(4,1,4,  "Dimmer LED",             "int",   0, 20, "", 2, group="Display"),

    # ── P1=04 / P2=02 SCOPE ──────────────────────────────────────────────────
    MenuItem(4,2,1,  "Scope Center",           "select", 0, 1, "", 1,
             options={0:"HIGH",1:"MID",2:"LOW"}, group="Scope"),
    MenuItem(4,2,2,  "Scope CTR",              "select", 0, 1, "", 1,
             options={0:"FILTER",1:"CAR POINT"}, group="Scope"),
    MenuItem(4,2,3,  "2D Display Sensitivity", "select", 0, 1, "", 1,
             options={0:"NORMAL",1:"HI"}, group="Scope"),
    MenuItem(4,2,4,  "3DSS Display Sensitivity","select", 0, 1, "", 1,
             options={0:"NORMAL",1:"HI"}, group="Scope"),
]
# fmt: on

# ── Lookup dicts ─────────────────────────────────────────────────────────────

MENU_BY_KEY: dict[tuple, MenuItem] = {m.key: m for m in MENU_ITEMS}

MENU_GROUPS: dict[str, list[MenuItem]] = {}
for _item in MENU_ITEMS:
    MENU_GROUPS.setdefault(_item.group, []).append(_item)

# Legacy compat — old code used MENU_BY_NUM with a flat number
# Build a synthetic number: p1*10000 + p2*100 + p3 for sorting
def _menu_num(m: MenuItem) -> int:
    return m.p1 * 10000 + m.p2 * 100 + m.p3

MENU_BY_NUM: dict[int, MenuItem] = {_menu_num(m): m for m in MENU_ITEMS}
