"""Room display settings, room-scoped access and authenticated call-end reports."""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.rooms import Rooms

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "rooms", Rooms(tmp_path))
    monkeypatch.setattr(
        main.pages, "get", lambda page: {"id": page} if page == "atrium-room" else None
    )
    monkeypatch.setattr(main.hub, "devices", {})
    monkeypatch.setattr(main.hub, "device_keys", {})
    return tmp_path


def test_default_and_changed_display_survive_restart(isolated):
    assert main.rooms.display("atrium") == {
        "idle_page": "",
        "meeting_lead_minutes": 15,
        "last_meeting_end": 0,
    }
    r = client.put(
        "/api/rooms/atrium/display", json={"idle_page": "atrium-room", "meeting_lead_minutes": 25}
    )
    assert r.status_code == 200
    assert Rooms(isolated).display("atrium")["meeting_lead_minutes"] == 25
    assert (
        next(r for r in client.get("/api/rooms").json() if r["id"] == "atrium")["display"][
            "meeting_lead_minutes"
        ]
        == 25
    )


def test_only_this_rooms_remote_or_team_can_configure(monkeypatch):
    main.rooms.upsert("another", "Another", "hq")
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    body = {"idle_page": "atrium-room", "meeting_lead_minutes": 15}
    assert client.put("/api/rooms/atrium/display", json=body).status_code == 401
    main.hub.heartbeat(
        {"id": "room-ipad", "role": "tablet", "room": "atrium", "name": "Atrium iPad"}
    )
    key = main.hub.issue_key("room-ipad")
    headers = {"X-Xos-Device": "room-ipad", "X-Xos-Key": key}
    assert client.put("/api/rooms/atrium/display", json=body, headers=headers).status_code == 200
    assert client.put("/api/rooms/another/display", json=body, headers=headers).status_code == 403


def test_display_rejects_unknown_content_and_out_of_range_timing():
    assert (
        client.put(
            "/api/rooms/atrium/display",
            json={"idle_page": "not-a-page", "meeting_lead_minutes": 15},
        ).status_code
        == 400
    )
    for minutes in [-1, 121]:
        assert (
            client.put(
                "/api/rooms/atrium/display",
                json={"idle_page": "atrium-room", "meeting_lead_minutes": minutes},
            ).status_code
            == 422
        )


def test_only_enrolled_appliance_records_an_actual_end(monkeypatch, isolated):
    body = {
        "id": "room-computer",
        "role": "roompc",
        "arg": "atrium",
        "native": {"meeting": {"in_call": False, "ended": True}},
    }
    assert client.post("/api/devices/heartbeat", json=body).status_code == 200
    assert main.rooms.display("atrium")["last_meeting_end"] == 0
    main.hub.devices["room-computer"]["native"] = {"meeting": {"in_call": True}}
    key = main.hub.issue_key("room-computer")
    monkeypatch.setattr(main.appliances, "keyed", lambda did: did == "room-computer")
    assert client.post("/api/devices/heartbeat", json=body).status_code == 401
    headers = {"X-Xos-Key": key}
    assert client.post("/api/devices/heartbeat", json=body, headers=headers).status_code == 200
    ended = main.rooms.display("atrium")["last_meeting_end"]
    assert ended > 0 and Rooms(isolated).display("atrium")["last_meeting_end"] == ended
    assert client.post("/api/devices/heartbeat", json=body, headers=headers).status_code == 200
    assert main.rooms.display("atrium")["last_meeting_end"] == ended


def display_pc():
    return main.hub.heartbeat(
        {
            "id": "atrium-pc",
            "role": "roompc",
            "arg": "atrium",
            "client": "xosroom-linux-0.1.0",
            "native": {"meeting": {"in_call": False}},
        }
    )


def test_return_home_suppresses_current_booking_and_refreshes_computer(isolated):
    display_pc()
    main.rooms.set_display("atrium", "atrium-room", 15)
    r = client.post("/api/rooms/atrium/display/home")
    assert r.status_code == 200
    assert r.json()["display"]["last_meeting_end"] > 0
    assert Rooms(isolated).display("atrium")["last_meeting_end"] > 0
    assert r.json()["commands"][0]["type"] == "refresh"


def test_room_content_controls_use_existing_appliance_refresh():
    display_pc()
    r = client.post(
        "/api/devices/atrium-pc/commands",
        json={"type": "route", "args": {"route": "page/atrium-room"}},
    )
    assert r.status_code == 200
    assert r.json()["type"] == "refresh"
    assert main.rooms.display("atrium")["idle_page"] == "atrium-room"
    assert main.rooms.display("atrium")["meeting_lead_minutes"] == 15
    assert main.rooms.display("atrium")["last_meeting_end"] > 0
    assert (
        client.post(
            "/api/devices/atrium-pc/commands",
            json={"type": "route", "args": {"route": "page/missing"}},
        ).status_code
        == 400
    )


@pytest.mark.parametrize("state", ["in_call", "waiting"])
def test_display_controls_preserve_active_or_waiting_meeting(state):
    d = display_pc()
    d["native"]["meeting"] = {state: True}
    assert client.post("/api/rooms/atrium/display/home").status_code == 409
    assert (
        client.post(
            "/api/devices/atrium-pc/commands",
            json={"type": "route", "args": {"route": "page/atrium-room"}},
        ).status_code
        == 409
    )
    assert main.rooms.display("atrium")["last_meeting_end"] == 0


def test_return_home_requires_this_rooms_access(monkeypatch):
    display_pc()
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    assert client.post("/api/rooms/atrium/display/home").status_code == 401
    main.hub.heartbeat({"id": "other-ipad", "role": "tablet", "room": "another"})
    key = main.hub.issue_key("other-ipad")
    assert (
        client.post(
            "/api/rooms/atrium/display/home",
            headers={"X-Xos-Device": "other-ipad", "X-Xos-Key": key},
        ).status_code
        == 403
    )
