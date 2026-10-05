from cyberrig.freedv.reporter import (
    Reporter,
    apply_event,
    stations_to_rows,
    _parse_sio_packets,
    _hz,
)


def test_auth_is_view_without_grid():
    r = Reporter()
    r.configure("k1abc", "")
    assert r.auth_payload()["role"] == "view"


def test_auth_is_report_with_call_and_grid():
    r = Reporter()
    r.configure("k1abc", "FN42aa", "Example Op - Anytown - CyberRig")
    a = r.auth_payload()
    assert a["role"] == "report"
    assert a["callsign"] == "K1ABC"
    assert a["grid_square"] == "FN42aa"
    assert a["rx_only"] is False


def test_hz_accepts_mhz_or_hz():
    assert _hz(7_181_000) == 7_181_000
    assert _hz(7.181) == 7_181_000
    assert _hz(7181) == 7_181_000


def test_tx_station_sorts_first():
    st = {}
    apply_event(st, "new_connection", {"sid": "a", "callsign": "K1AA", "grid_square": "FN42"})
    apply_event(st, "freq_change", {"sid": "a", "freq": 14236000})
    apply_event(st, "new_connection", {"sid": "b", "callsign": "W5JDX", "grid_square": "EM42"})
    apply_event(st, "freq_change", {"sid": "b", "freq": 7181000})
    apply_event(st, "tx_report", {"sid": "b", "mode": "RADEV1", "transmitting": True})
    rows = stations_to_rows(st)
    assert rows[0]["callsign"] == "W5JDX"
    assert rows[0]["tx"] is True
    assert rows[0]["hz"] == 7_181_000
    assert rows[1]["callsign"] == "K1AA"


def test_bulk_update_replays_events():
    st = {}
    apply_event(st, "bulk_update", [
        ["new_connection", {"sid": "x", "callsign": "N7AA"}],
        ["freq_change", {"sid": "x", "freq": 7177000}],
        ["tx_report", {"sid": "x", "mode": "RADEV1", "transmitting": False}],
    ])
    rows = stations_to_rows(st)
    assert len(rows) == 1
    assert rows[0]["mhz"] == 7.177
    assert rows[0]["mode"] == "RADEV1"


def test_parse_glued_engineio_packets():
    body = '40{"sid":"abc"}42["freq_change",{"sid":"s1","freq":7177000}]'
    ev = _parse_sio_packets(body)
    assert ev[0][0] == "freq_change"
    assert ev[0][1]["freq"] == 7177000


def test_later_bulk_does_not_wipe_snapshot():
    st = {}
    apply_event(st, "bulk_update", [
        ["new_connection", {"sid": "x", "callsign": "W5JDX"}],
        ["freq_change", {"sid": "x", "freq": 7181000}],
    ])
    apply_event(st, "bulk_update", [
        ["rx_report", {"sid": "x", "receiver_callsign": "W5JDX", "snr": 8}],
    ])
    rows = stations_to_rows(st)
    assert len(rows) == 1
    assert rows[0]["mhz"] == 7.181
    assert rows[0]["rx_snr"] == 8
