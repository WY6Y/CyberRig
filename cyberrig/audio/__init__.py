"""Audio capture helpers.

RX (LISTEN) and TX (REMOTE TX) share one duplex open on the FTDX10 USB
codec — see ``rig_codec.py``. The waterfall FFT is a consumer of that RX
path, not an owner of the device.
"""
