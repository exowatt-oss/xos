"""Who may do what: Admins run Admin, anyone in the organization may take one free screen."""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, hub

client = TestClient(app)
TV = "firetv-access1"


@pytest.fixture(autouse=True)
def fresh():
    hub.devices.clear()
    hub.commands.clear()
    hub.controls.clear()
    hub.device_keys.clear()
    client.post("/api/devices/heartbeat", json={"id": TV, "role": "page", "arg": "welcome"})


def as_user(monkeypatch, email):
    monkeypatch.setattr(settings, "XOS_DEV_USER", email)


def command(**headers):
    return client.post(f"/api/devices/{TV}/commands", json={"type": "reload"}, headers=headers)


def test_admin_is_for_the_software_team(monkeypatch):
    as_user(monkeypatch, "")
    assert client.post("/api/rooms", json={"name": "Lamar", "site": "hq"}).status_code == 401
    assert client.put(f"/api/devices/{TV}", json={"name": "x"}).status_code == 401
    assert client.post("/api/pages", json={"name": "x"}).status_code == 401
    as_user(monkeypatch, "someone@example.com")
    assert client.post("/api/rooms", json={"name": "Lamar", "site": "hq"}).status_code == 403
    as_user(monkeypatch, "lee@example.com")
    assert client.post("/api/rooms", json={"name": "Lamar", "site": "hq"}).status_code == 200
    # Screens keep reading what they need without anyone signing in.
    as_user(monkeypatch, "")
    for path in ("/api/devices", "/api/config", "/api/pages", "/api/rooms", "/api/time"):
        assert client.get(path).status_code == 200, path


def test_signed_in_path_and_me(monkeypatch):
    as_user(monkeypatch, "someone@example.com")
    me = client.get("/u/api/me").json()
    assert me["email"] == me["name"] == "someone@example.com"
    assert me["team"] is False and me["role"] == "member" and me["preview"] is None
    assert me["perms"] == ["screens.take_shared"]
    as_user(monkeypatch, "outsider@gmail.com")
    assert client.get("/u/api/me").json()["email"] == ""
    r = client.get("/u/signin?next=/app/%23/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/#/admin"
    r = client.get("/u/signin?next=//evil.example", follow_redirects=False)
    assert r.headers["location"] == "/app/"


def test_take_control_one_person_at_a_time(monkeypatch):
    as_user(monkeypatch, "")
    assert command().status_code == 401
    assert client.get("/api/control").status_code == 401

    as_user(monkeypatch, "ana@example.com")
    assert command().status_code == 403  # take it first
    screens = client.get("/api/control").json()["screens"]
    assert [s["id"] for s in screens] == [TV] and screens[0]["controlled_by"] is None
    assert client.post(f"/api/control/{TV}").status_code == 200
    assert command().status_code == 200
    bad = client.post(f"/api/devices/{TV}/commands", json={"type": "return_min", "args": {}})
    assert bad.status_code == 403
    assert client.get("/api/devices").json()[0]["controlled_by"]["name"] == "ana@example.com"

    as_user(monkeypatch, "ben@example.com")
    taken = client.post(f"/api/control/{TV}")
    assert taken.status_code == 409 and "ana@example.com" in taken.json()["detail"]
    assert command().status_code == 409
    assert client.delete(f"/api/control/{TV}").status_code == 403
    assert client.get("/api/control").json()["screens"][0]["mine"] is False

    as_user(monkeypatch, "ana@example.com")
    assert client.delete(f"/api/control/{TV}").status_code == 200
    as_user(monkeypatch, "ben@example.com")
    assert client.post(f"/api/control/{TV}").status_code == 200

    # A held screen still obeys Admins.
    as_user(monkeypatch, "root@example.com")
    assert command().status_code == 200


def test_control_only_screens_with_sharing_on(monkeypatch):
    client.put(f"/api/devices/{TV}/share", json={"enabled": False})
    as_user(monkeypatch, "ana@example.com")
    assert hub.devices[TV].get("share_off")
    assert client.get("/api/control").json()["screens"] == []
    assert client.post(f"/api/control/{TV}").status_code == 403


def test_trusted_remote_gets_a_key_once(monkeypatch):
    as_user(monkeypatch, "")
    beat = {"id": "web-remote01", "role": "remote", "arg": "atrium"}
    hub.devices["web-remote01"] = {"id": "web-remote01", "first_seen": 2e9, "last_seen": 2e9}
    assert "key" not in client.post("/api/devices/heartbeat", json=beat).json()  # unnamed, new
    hub.devices["web-remote01"]["name"] = "Atrium iPad"
    key = client.post("/api/devices/heartbeat", json=beat).json()["key"]
    assert "key" not in client.post("/api/devices/heartbeat", json=beat).json()
    assert all("key" not in d for d in client.get("/api/devices").json())
    assert command(**{"X-Xos-Device": "web-remote01", "X-Xos-Key": "wrong"}).status_code == 401
    assert command(**{"X-Xos-Device": "web-remote01", "X-Xos-Key": key}).status_code == 200


def test_mcp_is_for_the_software_team(monkeypatch):
    from types import SimpleNamespace

    from app import mcp_server

    def as_user(email):
        user = SimpleNamespace(email=email, is_bot=False)
        monkeypatch.setattr(mcp_server, "user_from_headers", lambda _h: user)

    as_user("root@example.com")
    assert mcp_server.authorized({"authorization": "Bearer id-token"})
    as_user("someone@example.com")
    assert not mcp_server.authorized({"authorization": "Bearer id-token"})


@pytest.mark.parametrize("reply_lost", [False, True])
def test_remote_persists_its_key_before_enrollment(monkeypatch, tmp_path, reply_lost):
    from app.hub import Hub

    as_user(monkeypatch, "")
    remote = "web-persisted-remote"
    proposed = "a" * 48
    beat = {"id": remote, "role": "tablet", "arg": "atrium", "enrollment_key": proposed}
    hub.devices[remote] = {"id": remote, "name": "Atrium tablet", "first_seen": 2e9}
    monkeypatch.setattr(hub, "data_dir", tmp_path)
    response = client.post("/api/devices/heartbeat", json=beat)
    if not reply_lost:
        assert response.json()["key"] == proposed
    assert response.json()["remote_access"] == "ready"
    # Even without reading the reply, a restart retains the hash and the tablet knows its key.
    restarted = Hub(tmp_path)
    assert restarted.check_key(remote, proposed)
    assert command(**{"X-Xos-Device": remote, "X-Xos-Key": proposed}).status_code == 200
    assert "key" not in client.post("/api/devices/heartbeat", json=beat).json()
    assert proposed not in client.get("/api/devices").text
    assert proposed not in (tmp_path / "devices.json").read_text()
    assert proposed not in (tmp_path / "device_keys.json").read_text()


def test_enrollment_cannot_replace_an_existing_key_or_trust_an_unnamed_remote(monkeypatch):
    as_user(monkeypatch, "")
    remote = "web-not-trusted"
    beat = {"id": remote, "role": "tablet", "enrollment_key": "b" * 48}
    assert "key" not in client.post("/api/devices/heartbeat", json=beat).json()
    assert not hub.check_key(remote, beat["enrollment_key"])
    hub.devices[remote]["name"] = "Named tablet"
    assert client.post("/api/devices/heartbeat", json=beat).json()["key"] == beat["enrollment_key"]
    assert (
        "key"
        not in client.post(
            "/api/devices/heartbeat", json={**beat, "enrollment_key": "c" * 48}
        ).json()
    )
    assert hub.check_key(remote, beat["enrollment_key"])
    assert not hub.check_key(remote, "c" * 48)


def test_storage_failure_does_not_consume_an_enrollment_key(monkeypatch):
    as_user(monkeypatch, "")
    remote = "web-no-storage"
    hub.devices[remote] = {"id": remote, "name": "Atrium tablet", "first_seen": 2e9}
    beat = {"id": remote, "role": "tablet", "enrollment_key": ""}
    assert "key" not in client.post("/api/devices/heartbeat", json=beat).json()
    assert remote not in hub.device_keys
    for bad in ["short", "x" * 129, "<script>" * 8]:
        assert (
            client.post("/api/devices/heartbeat", json={**beat, "enrollment_key": bad}).status_code
            == 422
        )


def test_only_admin_can_reset_remote_access_without_losing_settings(monkeypatch):
    remote = "web-reset-remote"
    hub.devices[remote] = {
        "id": remote,
        "name": "Atrium tablet",
        "role": "tablet",
        "arg": "atrium",
        "room": "atrium",
        "share_code": "abcde",
        "first_seen": 2e9,
    }
    original = dict(hub.devices[remote])
    old = hub.issue_key(remote)
    path = f"/api/devices/{remote}/remote-access/reset"
    as_user(monkeypatch, "")
    assert client.post(path).status_code == 401
    assert client.post(path, headers={"X-Xos-Device": remote, "X-Xos-Key": old}).status_code == 401
    as_user(monkeypatch, "someone@example.com")
    assert client.post(path).status_code == 403
    assert hub.check_key(remote, old)
    as_user(monkeypatch, "root@example.com")
    r = client.post(path)
    assert r.status_code == 200 and r.json()["command"]["type"] == "reload"
    assert hub.devices[remote] == {**original, "remote_access": "needs_reset"}
    assert not hub.check_key(remote, old)
    assert client.post(f"/api/devices/{TV}/remote-access/reset").status_code == 400
    assert client.post("/api/devices/missing/remote-access/reset").status_code == 404
    # The actual tablet's next heartbeat can enroll and command the room again.
    as_user(monkeypatch, "")
    new = "d" * 48
    client.post(
        "/api/devices/heartbeat",
        json={
            "id": remote,
            "role": "tablet",
            "arg": "atrium",
            "enrollment_key": new,
        },
    )
    assert command(**{"X-Xos-Device": remote, "X-Xos-Key": new}).status_code == 200
