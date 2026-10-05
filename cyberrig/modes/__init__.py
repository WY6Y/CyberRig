"""Shared machinery for CyberRig's exclusive digital operating modes.

FreeDV and SSTV both need the same thing: snapshot the radio, apply a
digital-clean CAT preset, take an exclusive hold on the USB codec, and put
everything back on the way out. That lives in `session.DigitalSession`;
each mode subclasses it for its own extras.
"""

from cyberrig.modes.session import DigitalSession, active_session, data_mode_for_hz

__all__ = ["DigitalSession", "active_session", "data_mode_for_hz"]
