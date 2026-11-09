import pytest
from fastapi.testclient import TestClient

from app.main import app, hub

client = TestClient(app)


@pytest.fixture(autouse=True)
def tv(monkeypatch):
    hub.devices.clear()
    hub.commands.clear()
    hub.shares.clear()
    client.post("/api/devices/heartbeat", json={"id": "firetv-1", "role": "tv", "arg": "atrium"})
    client.put("/api/devices/firetv-1", json={"name": "Atrium room", "room": "atrium"})
    # The screen has its command stream open (what "online" means for sharing).
    monkeypatch.setattr(hub, "connected", lambda device_id: device_id == "firetv-1")
    return hub.devices["firetv-1"]["share_code"]


def test_every_screen_gets_a_stable_share_link(tv):
    assert len(tv) == 5
    client.post("/api/devices/heartbeat", json={"id": "firetv-1", "role": "tv", "arg": "atrium"})
    assert hub.devices["firetv-1"]["share_code"] == tv
    status = client.get(f"/api/share/{tv.upper()}").json()
    assert status["name"] == "Atrium room" and status["online"] and status["enabled"]
    assert status["link"].endswith(f"/s/{tv}")
    r = client.get(f"/s/{tv}", follow_redirects=False)
    assert r.headers["location"] == f"/app/#/share/{tv}"
    assert client.get("/api/share/zzzzz").status_code == 404
    svg = client.get(f"/api/share/{tv}/qr.svg")
    assert svg.status_code == 200 and svg.text.lstrip().startswith("<svg")


def test_share_a_link_and_stop_it(tv):
    bad = client.post(f"/api/share/{tv}", json={"kind": "url", "url": "javascript:alert(1)"})
    assert bad.status_code == 400
    s = client.post(
        f"/api/share/{tv}", json={"kind": "url", "url": "https://example.org", "name": "Ana"}
    ).json()
    assert s["kind"] == "url" and s["from"] == "Ana" and len(s["session"]) == 16
    cmd = hub.commands["firetv-1"][0]
    assert cmd["type"] == "share" and cmd["args"]["url"] == "https://example.org"
    assert client.get(f"/api/share/{tv}").json()["sharing"]["session"] == s["session"]
    # A stale session id (e.g. an old tab) doesn't stop someone else's share.
    assert client.post(f"/api/share/{tv}/stop", json={"session": "old"}).json()["stopped"] is False
    assert client.post(f"/api/share/{tv}/stop", json={"session": s["session"]}).json()["stopped"]
    assert hub.commands["firetv-1"][0]["type"] == "share_stop"
    assert client.get(f"/api/share/{tv}").json()["sharing"] is None


def test_share_a_photo(tv):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    r = client.post(
        f"/api/share/{tv}/image?name=Ana", content=png, headers={"content-type": "image/png"}
    )
    assert r.status_code == 200 and r.json()["kind"] == "image"
    media = client.get(r.json()["url"])
    assert media.content == png and media.headers["content-type"] == "image/png"
    wrong = client.post(
        f"/api/share/{tv}/image", content=b"x", headers={"content-type": "text/plain"}
    )
    assert wrong.status_code == 415


def test_offline_or_turned_off_screens_refuse(tv, monkeypatch):
    off = client.put("/api/devices/firetv-1/share", json={"enabled": False}).json()
    assert off["enabled"] is False
    r = client.post(f"/api/share/{tv}", json={"kind": "screen"})
    assert r.status_code == 409 and "turned off" in r.json()["detail"]
    client.put("/api/devices/firetv-1/share", json={"enabled": True})
    monkeypatch.setattr(hub, "connected", lambda device_id: False)
    r = client.post(f"/api/share/{tv}", json={"kind": "screen"})
    assert r.status_code == 409 and "offline" in r.json()["detail"]


def test_new_link_replaces_the_old_one_and_stops_sharing(tv):
    client.post(f"/api/share/{tv}", json={"kind": "screen"})
    fresh = client.put("/api/devices/firetv-1/share", json={"new_code": True}).json()
    assert fresh["code"] != tv and fresh["sharing"] is None
    assert [c["type"] for c in hub.commands["firetv-1"][:2]] == ["refresh", "share_stop"]
    assert client.get(f"/api/share/{tv}").status_code == 404
    assert client.get(f"/api/share/{fresh['code']}").status_code == 200


def test_airplay_name_is_reported(tv):
    client.post(
        "/api/devices/heartbeat",
        json={"id": "firetv-1", "role": "tv", "arg": "atrium", "airplay": "Atrium TV"},
    )
    assert client.get(f"/api/share/{tv}").json()["airplay"] == "Atrium TV"


def test_short_link_lists_the_screens_you_can_share_to(tv, monkeypatch):
    assert client.get("/s", follow_redirects=False).headers["location"] == "/app/#/share"
    # Not listed: a remote, an unnamed screen, and a screen with sharing turned off.
    client.post("/api/devices/heartbeat", json={"id": "web-remote", "role": "remote", "arg": "z"})
    client.put("/api/devices/web-remote", json={"name": "Atrium remote"})
    client.post("/api/devices/heartbeat", json={"id": "firetv-2", "role": "signage", "arg": "aus"})
    monkeypatch.setattr(hub, "connected", lambda device_id: True)
    assert [s["name"] for s in client.get("/api/share").json()] == ["Atrium room"]
    client.put("/api/devices/firetv-1/share", json={"enabled": False})
    assert client.get("/api/share").json() == []
    client.put("/api/devices/firetv-1/share", json={"enabled": True})
    client.post(f"/api/share/{tv}", json={"kind": "screen"})
    (screen,) = client.get("/api/share").json()
    assert screen == {
        "code": tv,
        "name": "Atrium room",
        "room": "atrium",
        "airplay": "",
        "busy": True,
    }


def test_rtc_config_lists_the_ice_servers(monkeypatch):
    from app.main import settings

    urls = "stun:stun.example:3478, turn:turn.example:3478"
    monkeypatch.setattr(settings, "XOS_ICE_SERVERS", urls)
    monkeypatch.setattr(settings, "XOS_TURN_USERNAME", "xos")
    servers = client.get("/api/rtc/config").json()["iceServers"]
    assert servers[0] == {"urls": "stun:stun.example:3478"}
    assert servers[1]["urls"] == "turn:turn.example:3478" and servers[1]["username"] == "xos"
    monkeypatch.setattr(settings, "XOS_ICE_SERVERS", "")
    assert client.get("/api/rtc/config").json() == {"iceServers": []}
