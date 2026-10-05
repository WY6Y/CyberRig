"""CyberRig fixed-geometry control panel.

A 1080x534 instrument face, built to the spec frozen on 2026-08-24.
Deliberately not resizable and deliberately dock-free: see
cyberrig/panel/window.py for the geometry table and the reasoning behind each
rule.

The legacy dock-based UI in cyberrig/ui_legacy/ is untouched and still works;
`python desktop.py` runs that one, `python panel.py` runs this one.
"""
