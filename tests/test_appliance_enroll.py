"""Xos Linux boxes set up from Admin: one-time setup codes, the flash preseed, device keys."""

import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.main import app, appliances, hub

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean():
    appliances.items.clear()
    hub.devices.clear()
    hub.commands.clear()
    hub.device_keys.clear()


def create(**kw):
    body = {"role": "room", "room": "atrium", "name": "Atrium room computer", **kw}
    r = client.post("/api/appliances", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_admin_sets_a_box_up_and_it_shows_named_in_its_room():
    b = create()
    assert len(b["code"]) == 10 and b["user"] == "xosadmin" and len(b["password"]) == 19
    assert b["hostname"] == "xos-atrium-room-computer" and b["preseed_url"].endswith(
        f"/os/p/{b['code']}"
    )
    d = hub.devices[b["device_id"]]
    assert d["name"] == "Atrium room computer" and d["room"] == "atrium" and d["role"] == "roompc"
    assert not next(x for x in hub.device_list(90) if x["id"] == b["device_id"])["online"]


def test_the_preseed_has_the_box_but_no_secrets():
    b = create()
    text = client.get(f"/os/p/{b['code']}").text
    assert "netcfg/hostname string xos-atrium-room-computer" in text
    assert "time/zone string America/Denver" in text
    assert f"--enroll {b['code']}" in text and "--no-start" in text
    assert b["password"] not in text and "user-password-crypted password !" in text
    assert client.get("/os/p/WRONGCODE1").status_code == 404


def test_a_setup_code_works_once():
    b = create()
    r = client.post(
        "/api/appliances/enroll", json={"code": b["code"].lower()[:5] + "-" + b["code"][5:]}
    )
    assert r.status_code == 200
    out = r.json()
    assert (
        out["device_id"] == b["device_id"]
        and out["password"] == b["password"]
        and out["device_key"]
    )
    assert out["role"] == "room" and out["room"] == "atrium" and out["hostname"] == b["hostname"]
    assert client.post("/api/appliances/enroll", json={"code": b["code"]}).status_code == 404
    assert client.get(f"/os/p/{b['code']}").status_code == 404  # used: no more preseed either


def test_an_expired_code_is_refused():
    b = create()
    appliances.items[b["device_id"]]["expires"] = time.time() - 1
    assert client.post("/api/appliances/enroll", json={"code": b["code"]}).status_code == 404


def test_an_enrolled_room_computer_must_prove_it_is_that_box():
    b = create()
    key = client.post("/api/appliances/enroll", json={"code": b["code"]}).json()["device_key"]
    beat = {
        "id": b["device_id"],
        "client": "xosroom-linux-0.1.0",
        "role": "roompc",
        "arg": "atrium",
    }
    assert client.post("/api/devices/heartbeat", json=beat).status_code == 401
    assert (
        client.post("/api/devices/heartbeat", json=beat, headers={"X-Xos-Key": "wrong"}).status_code
        == 401
    )
    ok = client.post(
        "/api/devices/heartbeat",
        json=beat,
        headers={"X-Xos-Device": b["device_id"], "X-Xos-Key": key},
    )
    assert ok.status_code == 200 and ok.json()["name"] == "Atrium room computer"
    cmd = client.post(
        f"/api/devices/{b['device_id']}/commands",
        json={"type": "meeting", "args": {"action": "mute"}},
    ).json()
    ack = f"/api/devices/{b['device_id']}/commands/{cmd['id']}/ack"
    assert client.post(ack, json={"ok": True}).status_code == 401
    assert client.post(ack, json={"ok": True}, headers={"X-Xos-Key": key}).status_code == 200


def test_an_appliance_key_does_not_make_a_trusted_remote(monkeypatch):
    b = create()
    key = client.post("/api/appliances/enroll", json={"code": b["code"]}).json()["device_key"]
    hub.heartbeat({"id": "firetv-tv1", "role": "page", "arg": "x"})
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    r = client.post(
        "/api/devices/firetv-tv1/commands",
        json={"type": "reload"},
        headers={"X-Xos-Device": b["device_id"], "X-Xos-Key": key},
    )
    assert r.status_code == 401


def test_other_devices_check_in_as_before():
    assert (
        client.post(
            "/api/devices/heartbeat", json={"id": "mac-exomac-1", "role": "roompc", "arg": "atrium"}
        ).status_code
        == 200
    )


def test_only_the_team_sees_or_makes_boxes_and_updates(monkeypatch):
    b = create()
    hub.heartbeat({"id": "web-tablet1", "role": "tablet", "arg": "atrium"})
    monkeypatch.setattr(settings, "XOS_DEV_USER", "someone@example.com")
    assert client.get("/api/appliances").status_code == 403
    assert (
        client.post(
            "/api/appliances", json={"role": "room", "room": "atrium", "name": "x"}
        ).status_code
        == 403
    )
    assert client.post(
        f"/api/devices/{b['device_id']}/commands", json={"type": "update"}
    ).status_code in (403, 401)


def test_a_new_code_to_reflash_revokes_the_old_key():
    b = create()
    key = client.post("/api/appliances/enroll", json={"code": b["code"]}).json()["device_key"]
    renewed = client.post(f"/api/appliances/{b['device_id']}/renew").json()
    assert renewed["code"] != b["code"] and not renewed["enrolled"]
    assert not hub.check_key(b["device_id"], key)
    assert client.post("/api/appliances/enroll", json={"code": renewed["code"]}).status_code == 200


def test_a_room_computer_needs_a_real_room():
    assert (
        client.post(
            "/api/appliances", json={"role": "room", "room": "nowhere", "name": "x"}
        ).status_code
        == 400
    )
    d = create(role="display", room="", name="Lobby TV", show="page/atrium-room")
    assert (
        hub.devices[d["device_id"]]["role"] == "page"
        and hub.devices[d["device_id"]]["arg"] == "atrium-room"
    )
    assert main.appliances.keyed(d["device_id"]) is False


def test_wifi_for_boxes_is_team_only_ordered_and_goes_to_the_box_once(monkeypatch):
    nets = [
        {"ssid": "Test OT", "psk": "test-ot-psk-1"},
        {"ssid": "Test IOT", "psk": "test-iot-psk-2"},
    ]
    assert client.put("/api/appliances/wifi", json={"networks": nets}).json()["networks"] == nets
    assert client.get("/api/appliances/wifi").json()["networks"][0]["ssid"] == "Test OT"
    b = create()
    assert "test-ot-psk-1" not in client.get(f"/os/p/{b['code']}").text  # never in the preseed
    out = client.post("/api/appliances/enroll", json={"code": b["code"]}).json()
    assert out["wifi"] == nets
    assert (
        client.put(
            "/api/appliances/wifi", json={"networks": [{"ssid": "x", "psk": "short"}]}
        ).status_code
        == 400
    )
    monkeypatch.setattr(settings, "XOS_DEV_USER", "someone@example.com")
    assert client.get("/api/appliances/wifi").status_code == 403
    assert client.put("/api/appliances/wifi", json={"networks": []}).status_code == 403
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    assert client.get("/api/appliances/wifi").status_code == 401


def test_wifi_profiles_for_networkmanager(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    tool = Path(__file__).resolve().parents[1] / "os/appliance/xos-wifi"
    (tmp_path / "xos-wifi-9.nmconnection").write_text("old")
    (tmp_path / "Office.nmconnection").write_text("someone else's")
    data = {
        "wifi": [
            {"ssid": "Test OT", "psk": "a\\b-ot-psk"},
            {"ssid": "Test IOT", "psk": "iot-psk-22"},
        ]
    }
    subprocess.run(
        [sys.executable, str(tool), str(tmp_path)],
        input=json.dumps(data),
        text=True,
        check=True,
        capture_output=True,
    )
    ot = (tmp_path / "xos-wifi-1.nmconnection").read_text()
    assert "ssid=Test OT" in ot and "autoconnect-priority=20" in ot and "psk=a\\\\b-ot-psk" in ot
    assert "autoconnect-priority=10" in (tmp_path / "xos-wifi-2.nmconnection").read_text()
    assert not (tmp_path / "xos-wifi-9.nmconnection").exists()  # earlier Xos profiles replaced
    assert (tmp_path / "Office.nmconnection").exists()  # anything else left alone
    assert oct((tmp_path / "xos-wifi-1.nmconnection").stat().st_mode & 0o777) == "0o600"
