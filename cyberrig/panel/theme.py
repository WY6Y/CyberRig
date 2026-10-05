"""Panel palette and stylesheet.

Three colours carry meaning and nothing else does:

    PHOS   green   an engaged control, or a live value
    AMBER  yellow  a group's identity, and station context
    DIM    grey    available but off

The legacy stylesheet defined four different greens across :hover, :pressed,
:checked and #modeBtn:checked, which encoded four states that do not exist.
Here hover moves the *border* only; colour changes exactly when a control is
actually engaged.
"""

# ── palette ──────────────────────────────────────────────────────────────
GROUND    = "#07090b"
PANEL     = "#0d1114"
PANEL_2   = "#11171a"
METER_BG  = "#000000"
RULE      = "#1e2a2c"
RULE_HOT  = "#2f4340"

PHOS      = "#3dff9a"
PHOS_DIM  = "#1c7d4d"
PHOS_DEEP = "#07301d"
AMBER     = "#ffb228"
RED       = "#ff4d4d"
RED_DEEP  = "#2a0e0e"

INK       = "#dfe7e4"
DIM       = "#7d908a"
DIMMER    = "#4a5a56"
OFF       = "#33403d"

BTN_BG    = "#161d20"
BTN_EDGE  = "#263134"

MONO = '"JetBrains Mono","DejaVu Sans Mono",Consolas,monospace'

# ── geometry, frozen 2026-08-24 ──────────────────────────────────────────
# Logical size stays 1080x534. panel.py applies UI_SCALE as QT_SCALE_FACTOR
# before QApplication so the on-screen face (and 9px labels) grow without
# rewriting every widget. 1.33 ≈ 1436x710 on the station PC's 1920x1080 display.
# PassThrough rounding is required — Qt's default Round would turn 1.33 into 1
# (no change) or 1.5 into 2 (almost fullscreen, title bar clips).
UI_SCALE     = 1.33
WIN_W, WIN_H = 1080, 534


def qt_scale_factor(large: bool) -> str:
    """QT_SCALE_FACTOR string. Must be applied before QApplication."""
    return str(UI_SCALE if large else 1.0)
HEAD_H       = 26
FOOT_H       = 32
PAD          = 6
GAP          = 5
ROW_A_H      = 132
ROW_B_H      = 130
BTN_H        = 20          #: the single button-height constant

STYLE = f"""
QWidget {{
    background: {GROUND};
    color: {INK};
    font-family: {MONO};
    font-size: 9px;
}}
QToolTip {{
    background: {PANEL_2};
    color: {INK};
    border: 1px solid {RULE_HOT};
    font-family: {MONO};
    font-size: 10px;
}}

/* ── buttons ─────────────────────────────────────────────────────────── */
QPushButton {{
    background: {BTN_BG};
    border: 1px solid {BTN_EDGE};
    color: #c3d2ce;
    font-family: {MONO};
    font-size: 9px;
    padding: 0px;
}}
QPushButton:hover  {{ border-color: {PHOS_DIM}; }}
QPushButton:focus  {{ border-color: {PHOS}; outline: none; }}
QPushButton:checked {{
    background: {PHOS_DEEP};
    border-color: {PHOS};
    color: {PHOS};
}}
QPushButton:disabled {{
    background: #0c1113;
    border-color: #1a2225;
    color: {OFF};
}}

QPushButton#ptt {{
    background: {RED_DEEP};
    border: 1px solid #5c1b1b;
    color: {RED};
    font-size: 13px;
    font-weight: bold;
    letter-spacing: 3px;
}}
QPushButton#ptt:hover   {{ border-color: {RED}; }}
QPushButton#ptt:checked {{
    background: #cc1111;
    color: #ffffff;
    border-color: {RED};
}}

QPushButton#popout {{
    background: #131a1c;
    color: #b7c8c3;
}}
QPushButton#popout:hover {{ border-color: {PHOS_DIM}; }}

/* ── group frame ─────────────────────────────────────────────────────── */
QFrame#group {{
    background: {PANEL};
    border: 1px solid {RULE};
}}

QLabel {{ background: transparent; }}
QLabel#grouplbl {{
    color: {AMBER};
    font-size: 9px;
    font-weight: bold;
}}
QLabel#dim    {{ color: {DIM}; font-size: 8px; }}
QLabel#dimmer {{ color: {DIMMER}; font-size: 8px; }}
QLabel#value  {{ color: {PHOS}; font-size: 8px; }}

/* ── sliders ─────────────────────────────────────────────────────────── */
QSlider {{ background: transparent; }}
QSlider::groove:vertical {{
    background: #070b0c;
    width: 3px;
    border-radius: 0px;
}}
QSlider::handle:vertical {{
    background: {PHOS};
    border: 1px solid #0a2a1a;
    height: 7px;
    margin: 0 -5px;
}}
QSlider::handle:vertical:hover {{ background: #7bffbd; }}
QSlider::groove:horizontal {{
    background: #070b0c;
    height: 3px;
}}
QSlider::handle:horizontal {{
    background: {PHOS};
    border: 1px solid #0a2a1a;
    width: 7px;
    margin: -4px 0;
}}
QSlider::handle:horizontal:hover {{ background: #7bffbd; }}

/* ── popout tables ───────────────────────────────────────────────────── */
QTableWidget {{
    background: #05080a;
    border: 1px solid {RULE};
    gridline-color: #121a1c;
    color: #b7c8c3;
    font-size: 10px;
    selection-background-color: #0c2318;
    selection-color: {PHOS};
}}
QHeaderView::section {{
    background: {PANEL};
    color: {DIMMER};
    border: 0px;
    border-bottom: 1px solid {RULE};
    padding: 3px 5px;
    font-size: 9px;
}}
QTableWidget QTableCornerButton::section {{ background: {PANEL}; border: 0px; }}
QScrollBar:vertical {{ background: {PANEL}; width: 9px; border: 0px; }}
QScrollBar::handle:vertical {{ background: {RULE_HOT}; min-height: 20px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; }}

QMenu {{
    background: #0b1113;
    border: 1px solid {RULE_HOT};
    color: #9db3ac;
    font-family: {MONO};
    font-size: 9px;
}}
QMenu::item:selected {{ background: {PHOS_DEEP}; color: {PHOS}; }}

QDialog {{ background: {GROUND}; }}

QComboBox, QSpinBox {{
    background: {BTN_BG};
    color: {INK};
    border: 1px solid {BTN_EDGE};
    font-family: {MONO};
    font-size: 9px;
    padding: 1px 4px;
    min-height: 18px;
}}
QComboBox::drop-down {{ border: 0px; width: 14px; }}
QComboBox QAbstractItemView {{
    background: #0b1113;
    color: {INK};
    selection-background-color: {PHOS_DEEP};
    selection-color: {PHOS};
    border: 1px solid {RULE_HOT};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background: {PANEL};
    border: 0px;
    width: 12px;
}}
"""
