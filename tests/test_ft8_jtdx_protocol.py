"""WSJT-X/JTDX protocol codec, checked against real captured packets.

The decoder is validated against bytes that came off the wire from the JTDX this
build actually talks to (tests/ft8_packets.py). That matters more than it sounds:
QDataStream has no field delimiters, so a single misplaced read shifts everything
after it and yields plausible values rather than an error. A round-trip against
our own encoder would agree with itself and catch none of that.

The encoders are checked structurally AND by the property that actually governs
whether JTDX accepts a Reply -- that every field is echoed from the Decode
verbatim. JTDX matches a Reply against a decode it still holds and logs "UDP
Reply request ignored, decode not found" when they disagree.
"""

from __future__ import annotations

import pytest

from cyberrig.ft8 import jtdx
import ft8_packets as pkts   # flat import, matching tests/fakes.py


# ------------------------------------------------- decoding, against real bytes

def test_real_status_packet_decodes_field_for_field():
    m = jtdx.decode(pkts.STATUS)
    assert m["type"] == jtdx.STATUS
    assert m["id"] == "JTDX"
    assert m["dial_frequency"] == 14_074_000
    assert m["mode"] == "FT8"
    assert m["tx_mode"] == "FT8"
    assert m["de_call"] == "WY6Y"
    assert m["de_grid"] == "EM15fo"
    # NOTE: rx_df and tx_df are read in the published WSJT-X order (Rx then Tx),
    # and that order is NOT verified against this station -- they have read
    # identically in every observation. Tried to force them apart on 2026-09-01
    # by setting RxFreq=1500 / TxFreq=2000 in the ini and restarting: JTDX
    # reported BOTH as 2000, i.e. it snaps RX to the TX slot at startup. So a
    # swapped decoder would currently be harmless AND undetectable here.
    # If they ever diverge, verify before trusting tx_df for the XIT offset.
    assert m["rx_df"] == 1813
    assert m["tx_df"] == 1813
    assert m["rx_df"] == m["tx_df"], (
        "if these ever differ, the Rx-before-Tx field order becomes load-bearing "
        "and must be confirmed against a real packet first")
    assert m["tx_enabled"] is False
    assert m["transmitting"] is False
    assert m["decoding"] is True
    assert m["tx_watchdog"] is False
    assert m["special_op"] == "NONE"


def test_status_distinguishes_null_from_empty_string():
    """0xFFFFFFFF is NULL and is NOT ''.

    Treating them the same shifts nothing on the wire but silently turns "no
    sub-mode" into "sub-mode is blank", and the real packet carries both cases:
    dx_call is empty, sub_mode is null.
    """
    m = jtdx.decode(pkts.STATUS)
    assert m["dx_call"] == "", "dx_call is an EMPTY string in this capture"
    assert m["sub_mode"] is None, "sub_mode is NULL in this capture, not ''"


def test_real_decode_packet_decodes_field_for_field():
    m = jtdx.decode(pkts.DECODE)
    assert m["type"] == jtdx.DECODE
    assert m["id"] == "JTDX"
    assert m["new"] is True
    assert isinstance(m["time"], int) and 0 <= m["time"] < 86_400_000
    assert m["delta_frequency"] > 0
    assert m["message"] and m["message"] == m["message"].strip()
    assert m["mode"] == "~", (
        "JTDX reports FT8 as the mode CHARACTER '~' in Decode, not 'FT8'. A Reply "
        "must echo whatever arrived or JTDX will not match it.")
    assert -30 <= m["snr"] <= 30
    assert -3.0 <= m["delta_time"] <= 3.0


def test_real_heartbeat_is_short_and_still_parses():
    """JTDX 2.2 omits the trailing `revision` field the spec describes."""
    m = jtdx.decode(pkts.HEARTBEAT)
    assert m["type"] == jtdx.HEARTBEAT
    assert m["version"] == "2.2.159"
    assert m["max_schema"] == 3
    assert "error" not in m, "a short heartbeat must not be reported as truncated"


def test_foreign_and_junk_datagrams_are_rejected_not_guessed():
    assert jtdx.decode(b"") is None
    assert jtdx.decode(b"hello world, not wsjtx") is None
    assert jtdx.decode(b"\x00\x00\x00\x00" * 8) is None


def test_truncated_packet_reports_error_rather_than_inventing_fields():
    m = jtdx.decode(pkts.STATUS[:40])
    assert m is not None and m["type"] == jtdx.STATUS
    assert "error" in m, "a short read must surface, not produce plausible values"


# --------------------------------------------------------------------- encoding

def test_reply_echoes_every_field_from_the_decode():
    """The property JTDX actually enforces before it will act on a Reply."""
    src = jtdx.decode(pkts.DECODE)
    raw = jtdx.encode_reply(
        "JTDX", time_ms=src["time"], snr=src["snr"], delta_time=src["delta_time"],
        delta_frequency=src["delta_frequency"], mode=src["mode"],
        message=src["message"], low_confidence=src["low_confidence"])

    r = jtdx.Reader(raw)
    assert r.u32() == jtdx.MAGIC
    assert r.u32() == jtdx.SCHEMA
    assert r.u32() == jtdx.REPLY
    assert r.string() == "JTDX"
    assert r.qtime() == src["time"]
    assert r.i32() == src["snr"]
    assert r.f64() == pytest.approx(src["delta_time"])
    assert r.u32() == src["delta_frequency"]
    assert r.string() == src["mode"]
    assert r.string() == src["message"]
    assert r.boolean() == src["low_confidence"]
    assert r.u8() == 0
    assert r.remaining == 0, "Reply must not carry trailing bytes"


def test_halt_tx_defaults_to_immediate_not_end_of_over():
    """This is the abort path. An abort that waits politely is not an abort."""
    r = jtdx.Reader(jtdx.encode_halt_tx("JTDX"))
    r.u32(); r.u32()
    assert r.u32() == jtdx.HALT_TX
    assert r.string() == "JTDX"
    assert r.boolean() is False, "auto must default False = halt NOW"

    r2 = jtdx.Reader(jtdx.encode_halt_tx("JTDX", auto=True))
    r2.u32(); r2.u32(); r2.u32(); r2.string()
    assert r2.boolean() is True


def test_heartbeat_encodes_with_a_schema_jtdx_accepts():
    """JTDX advertises max_schema 3; we must not send higher than it supports."""
    live_max = jtdx.decode(pkts.HEARTBEAT)["max_schema"]
    assert jtdx.SCHEMA <= live_max
    m = jtdx.decode(jtdx.encode_heartbeat("JTDX"))
    assert m["type"] == jtdx.HEARTBEAT and m["id"] == "JTDX"


def test_no_encoder_exists_for_the_messages_jtdx_cannot_handle():
    """Configure(15) and SwitchConfiguration(14) are absent from this build.

    Their moc signal names are not in the binary. Shipping encoders for them
    would invite a future session to 'just send Configure' to set the TX slot and
    then debug silence — the slot workaround is XIT over CAT instead.
    """
    assert not hasattr(jtdx, "encode_configure")
    assert not hasattr(jtdx, "encode_switch_configuration")


# -------------------------------------------------------------------- transport

@pytest.mark.parametrize("host,expect", [
    ("239.255.0.1", True), ("224.0.0.1", True), ("239.0.0.1", True),
    ("127.0.0.1", False), ("172.16.0.52", False), ("240.0.0.1", False),
    ("localhost", False),
])
def test_multicast_detection(host, expect):
    assert jtdx.JtdxClient(host=host).is_multicast is expect


def test_client_refuses_to_send_before_it_has_seen_jtdx():
    """Without a wsjtx id we would be guessing at the key JTDX matches on."""
    c = jtdx.JtdxClient(host="239.255.0.1")
    assert c.halt_tx() is False
    assert c.reply(jtdx.decode(pkts.DECODE)) is False


def test_client_status_surfaces_hound_state():
    """We cannot SET Fox/Hound over UDP on this build, but we can always read it."""
    c = jtdx.JtdxClient()
    c.last_status = jtdx.decode(pkts.STATUS)
    c.wsjtx_id = "JTDX"
    st = c.status()
    assert st["hound"] is False and st["special_op"] == "NONE"
    assert st["tx_enabled"] is False
    assert st["tx_df"] == 1813

    hound = dict(c.last_status, special_op="HOUND", special_op_mode=7)
    c.last_status = hound
    assert c.status()["hound"] is True
