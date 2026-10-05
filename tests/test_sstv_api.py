"""The SSTV web routes, driven through the real app.

Nothing here transmits: the rig in a test process has no CAT link, and the
transmitter refuses to key blind. That refusal is itself worth asserting.
"""

import base64
import io

import numpy as np
import pytest

from fastapi.testclient import TestClient
from PIL import Image

from cyberrig.web import app as app_mod


@pytest.fixture()
def client():
    with TestClient(app_mod.app) as c:
        yield c
    app_mod.sstv_tx.abort("test teardown")
    app_mod.sstv_session.exit()


def png_data_url(w=64, h=48) -> str:
    buf = io.BytesIO()
    Image.fromarray(np.full((h, w, 3), 90, np.uint8)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_status_route_answers_before_anything_is_entered(client):
    r = client.get("/api/sstv").json()
    assert r["ok"]
    assert r["active"] is False
    assert r["tx_active"] is False
    assert r["watts"] == 30
    # The ceiling is where the ALC threshold may go, not what we radiate —
    # raised 50 -> 100 on 2026-08-29 so the drive controls output. The guard
    # on actual output is SSTVTransmitter's measured-PO trip.
    assert r["max_watts"] == 100


def test_mode_table_is_served_with_durations(client):
    r = client.get("/api/sstv/modes").json()
    assert r["ok"]
    names = [m["name"] for m in r["modes"]]
    assert "Scottie 1" in names and "PD120" in names
    assert all(m["seconds"] > 0 for m in r["modes"])


def test_the_page_is_served(client):
    r = client.get("/sstv")
    assert r.status_code == 200
    assert "CYBERRIG SSTV" in r.text


def test_transmitting_without_entering_is_refused(client):
    r = client.post("/api/sstv/tx", json={"image": png_data_url()}).json()
    assert not r["ok"]
    assert "not entered" in r["error"]
    assert r["tx_active"] is False


def test_a_corrupt_image_is_rejected_cleanly(client):
    r = client.post("/api/sstv/tx", json={"image": "data:image/png;base64,!!!!"}).json()
    assert not r["ok"]
    assert r["tx_active"] is False


def test_abort_is_safe_when_nothing_is_transmitting(client):
    r = client.post("/api/sstv/abort", json={}).json()
    assert r["ok"]
    assert r["tx_active"] is False


def test_watts_route_clamps_to_the_ceiling(client):
    r = client.post("/api/sstv/watts", json={"watts": 500}).json()
    assert r["ok"]
    assert r["watts"] == 100
    r = client.post("/api/sstv/watts", json={"watts": 25}).json()
    assert r["watts"] == 25


def test_data_url_decoder_accepts_both_forms():
    raw = b"\x89PNG\r\n\x1a\n"
    b64 = base64.b64encode(raw).decode()
    assert app_mod._decode_data_url(b64) == raw
    assert app_mod._decode_data_url("data:image/png;base64," + b64) == raw
    with pytest.raises(ValueError):
        app_mod._decode_data_url("data:image/png,notbase64")


def test_meters_read_zero_with_the_radio_unkeyed(client):
    m = app_mod._sstv_meters()
    assert m["is_tx"] is False
    assert m["watts_out"] == 0
    assert m["swr_out"] is None


# ── templates ────────────────────────────────────────────────────────────

def test_template_list_route_serves_the_builtins(client):
    r = client.get("/api/sstv/templates").json()
    assert r["ok"]
    ids = [t["id"] for t in r["templates"]]
    assert "neon" in ids and "blank" in ids
    assert all(t["builtin"] for t in r["templates"] if t["id"] in ("blank", "neon"))


def test_saving_and_deleting_a_template_through_the_api(client, tmp_path):
    app_mod.sstv_templates.dir = tmp_path
    r = client.post("/api/sstv/templates", json={"template": {
        "name": "API Test", "elements": [
            {"type": "text", "text": "{mycall}", "x": 0.1, "y": 0.1}]}}).json()
    assert r["ok"]
    assert r["template"]["id"] == "api-test"
    assert any(t["id"] == "api-test" for t in r["templates"])

    d = client.delete("/api/sstv/templates/api-test").json()
    assert d["ok"]
    assert not any(t["id"] == "api-test" for t in d["templates"])


def test_the_api_refuses_to_delete_a_builtin(client):
    r = client.delete("/api/sstv/templates/neon").json()
    assert not r["ok"]
    assert any(t["id"] == "neon" for t in r["templates"])


def test_a_hostile_template_is_sanitised_on_the_way_in(client, tmp_path):
    app_mod.sstv_templates.dir = tmp_path
    r = client.post("/api/sstv/templates", json={"template": {
        "name": "Nasty",
        "elements": [
            {"type": "text", "text": "ok", "color": "javascript:alert(1)", "x": 9e9},
            {"type": "not-a-thing"},
        ]}}).json()
    assert r["ok"]
    els = r["template"]["elements"]
    assert len(els) == 1
    assert els[0]["color"] == "#ffffff"
    assert els[0]["x"] <= 1.5


def test_status_carries_the_macro_context(client):
    m = client.get("/api/sstv").json()["macros"]
    assert set(m) >= {"mycall", "grid", "freq", "band", "mode", "date", "utc"}


# ── receive and gallery ──────────────────────────────────────────────────

def test_status_carries_receiver_state(client):
    r = client.get("/api/sstv").json()
    assert r["rx_running"] is False
    assert r["rx_active"] is False
    assert r["rx_progress"] == 0.0


def test_rx_image_is_empty_when_nothing_is_arriving(client):
    assert client.get("/api/sstv/rx/image").status_code == 204


def test_gallery_is_empty_and_says_so(client, tmp_path):
    app_mod.sstv_store.dir = tmp_path
    r = client.get("/api/sstv/gallery").json()
    assert r["ok"] and r["items"] == []


def test_a_saved_picture_appears_in_the_gallery_and_can_be_deleted(client, tmp_path):
    app_mod.sstv_store.dir = tmp_path
    img = np.full((256, 320, 3), 40, np.uint8)
    item = app_mod.sstv_store.save(img, {"mode": "Scottie 1", "snr": 12.0})

    items = client.get("/api/sstv/gallery").json()["items"]
    assert len(items) == 1
    assert items[0]["mode"] == "Scottie 1"

    png = client.get(items[0]["url"])
    assert png.status_code == 200
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"

    d = client.delete(f"/api/sstv/gallery/{item.id}").json()
    assert d["ok"]
    assert d["items"] == []


def test_a_missing_picture_is_a_404_not_a_traceback(client, tmp_path):
    app_mod.sstv_store.dir = tmp_path
    assert client.get("/api/sstv/gallery/nope.png").status_code == 404


def test_gallery_ids_cannot_escape_the_directory(client, tmp_path):
    app_mod.sstv_store.dir = tmp_path / "rx"
    (tmp_path / "secret.png").write_bytes(b"nope")
    assert app_mod.sstv_store.path("../secret") is None


def test_rx_adjust_routes_refuse_cleanly_with_nothing_received(client):
    assert client.get("/api/sstv/rx/last.png").status_code == 204
    r = client.post("/api/sstv/rx/adjust", json={"slant_ppm": 100}).json()
    assert not r["ok"]
    r = client.post("/api/sstv/rx/resave", json={}).json()
    assert not r["ok"]


def test_status_reports_whether_a_picture_can_be_straightened(client):
    assert client.get("/api/sstv").json()["last_adjustable"] is False


def test_saved_png_is_written_at_the_display_aspect(tmp_path):
    """The file on disk has to be right on its own.

    Both places the UI shows a received picture correct the aspect on the way
    past — the RX canvas is stretched to a 4:3 wrapper, the gallery thumbnails
    are object-fit: cover — so a squashed PNG looked fine everywhere except
    when you opened the file itself.
    """
    from cyberrig.sstv.modes import get_mode
    from cyberrig.sstv.rx import RxStore

    store = RxStore(tmp_path)
    for name in ("Scottie 1", "Martin 2", "Robot 36"):
        m = get_mode(name)
        item = store.save(np.zeros((m.height, m.width, 3), np.uint8), {"mode": name})
        w, h = Image.open(tmp_path / f"{item.id}.png").size
        assert abs(w / h - m.display_aspect) < 0.01, f"{name} saved {w}x{h}"
        # Never resample the long axis down: that would trade decoded detail
        # for framing.
        assert w >= m.width and h >= m.height
