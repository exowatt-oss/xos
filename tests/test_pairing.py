"""Adding a screen: waiting screens in Admin, claim into a room, the pairing QR."""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, hub

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean():
    hub.devices.clear()
    hub.pair_codes.clear()
    hub.commands.clear()


def waiting_screen(device="xos-9cce5b-a1b2c3"):
    client.post(
        "/api/devices/heartbeat",
        json={
            "id": device,
            "client": "xos-display-f108013ab1c2",
            "role": "setup",
            "screen": "1920x1080",
        },
    )
    return client.post("/api/devices/pair", json={"id": device}).json()["code"]


def test_admin_sees_screens_waiting_with_their_codes():
    code = waiting_screen()
    rows = client.get("/api/devices/pairing").json()
    assert rows[0]["code"] == code and rows[0]["device"] == "xos-9cce5b-a1b2c3"
    assert rows[0]["client"].startswith("xos-display") and rows[0]["screen"] == "1920x1080"


def test_waiting_screens_are_team_only(monkeypatch):
    waiting_screen()
    monkeypatch.setattr(settings, "XOS_DEV_USER", "someone@example.com")
    assert client.get("/api/devices/pairing").status_code == 403
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    assert client.get("/api/devices/pairing").status_code == 401


def test_a_screen_that_went_away_is_not_waiting():
    waiting_screen()
    hub.devices["xos-9cce5b-a1b2c3"]["last_seen"] -= 600
    assert client.get("/api/devices/pairing").json() == []


def test_add_puts_it_in_its_room_names_it_and_uses_the_code():
    code = waiting_screen()
    r = client.post(
        "/api/devices/claim",
        json={
            "code": code,
            "name": "Atrium lobby TV",
            "room": "atrium",
            "route": "page/welcome",
        },
    )
    assert r.status_code == 200
    d = hub.devices["xos-9cce5b-a1b2c3"]
    assert d["name"] == "Atrium lobby TV" and d["room"] == "atrium"
    assert hub.commands["xos-9cce5b-a1b2c3"][0]["args"] == {"route": "page/welcome"}
    assert client.get("/api/devices/pairing").json() == []  # code used
    again = waiting_screen("xos-7f00aa-d4e5f6")
    assert (
        client.post("/api/devices/claim", json={"code": again, "room": "nowhere"}).status_code
        == 400
    )


def test_pairing_qr_opens_admin_with_the_code():
    r = client.get("/api/pair/qr.svg?code=123456")
    assert (
        r.status_code == 200
        and r.headers["content-type"].startswith("image/svg+xml")
        and "<svg" in r.text
    )
    assert client.get("/api/pair/qr.svg?code=12ab56").status_code == 400
    assert client.get("/api/pair/qr.svg").status_code == 400
