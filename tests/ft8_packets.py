"""Real JTDX v2.2.159 UDP packets, captured off loopback 2026-09-01.

Frozen as fixtures deliberately. A round-trip against our own encoder only
proves self-consistency; these bytes came off the wire from the actual
decoder this build talks to, so they catch a field in the wrong place.
Captured passively with tcpdump so GridTracker2, which owns the port, was
never disturbed.
"""

# HEARTBEAT: id='JTDX', max_schema=3, version='2.2.159'
HEARTBEAT = bytes.fromhex("adbccbda0000000200000000000000044a5444580000000300000007322e322e313539")

# STATUS: id='JTDX', dial_frequency=14074000, mode='FT8', dx_call=''
STATUS = bytes.fromhex("adbccbda0000000200000001000000044a5444580000000000d6c0900000000346543800000000000000032d3135000000034654380000010000071500000715000000045759365900000006454d3135666f0000000000ffffffff0000")

# DECODE: id='JTDX', new=True, time=58650000, snr=0
DECODE = bytes.fromhex("adbccbda0000000200000002000000044a54445801037eed90000000003fb99999a00000000000017a000000017e000000104e39564b56204b53344f5420522d31390000")
