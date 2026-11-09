"""An Xos box from the USB, paired in Admin as the room's meeting computer (Meet, Zoom, Teams):
its helper (os/appliance/xos-box) collects the setup code with the box's own secret."""

import pytest
from fastapi.testclient import TestClient

from app.main import app, appliances, boxes, hub

client = TestClient(app)
BOX = "xos-9cce5b-a1b2c3"
SECRET = "s" * 64  # a test box secret, not a credential


@pytest.fixture(autouse=True)
def clean():
    for store in (appliances.items, boxes.items, hub.devices, hub.pair_codes, hub.commands):
        store.clear()
    hub.device_keys.clear()


def helper(secret=SECRET, box=BOX):
    return client.post("/api/appliances/handoff", json={"id": box, "secret": secret})


def waiting_box(client_name="xos-display-f108013ab1c2"):
    client.post("/api/devices/heartbeat", json={"id": BOX, "client": client_name, "role": "setup"})
    return client.post("/api/devices/pair", json={"id": BOX}).json()["code"]


def make_room_computer(code, **kw):
    return client.post(
        "/api/devices/claim", json={"code": code, "room": "atrium", "use": "room", **kw}
    )


def test_a_paired_box_becomes_the_rooms_meeting_computer():
    assert helper().json() == {"code": ""}  # first boot: registers, nothing to do yet
    r = make_room_computer(waiting_box(), name="Atrium meetings")
    assert r.status_code == 200, r.text
    pc = r.json()
    assert pc["role"] == "roompc" and pc["room"] == "atrium" and pc["name"] == "Atrium meetings"
    assert hub.commands[BOX][-1]["args"] == {"route": "tv/atrium"}  # the room screen meanwhile
    got = helper().json()
    assert got["role"] == "room" and got["room"] == "atrium" and len(got["code"]) == 10
    # The box's installer redeems it: the room computer's id and key; the screen entry goes.
    out = client.post("/api/appliances/enroll", json={"code": got["code"]}).json()
    assert out["device_id"] == pc["id"] and out["device_key"]
    assert BOX not in hub.devices and helper().json() == {"code": ""}


def test_only_the_box_with_its_secret_gets_the_code():
    helper()
    make_room_computer(waiting_box())
    assert helper(secret="x" * 64).status_code == 403
    # Someone else tried to be this box: no code for anyone until it's reinstalled.
    assert helper().status_code == 403


def test_a_box_that_never_registered_or_isnt_a_box_is_refused():
    r = make_room_computer(waiting_box())
    assert r.status_code == 409 and "Xos USB" in r.json()["detail"]
    hub.devices.clear()
    hub.pair_codes.clear()
    helper()
    r = make_room_computer(waiting_box("firetv-0.6.0"))
    assert r.status_code == 400 and "Xos box" in r.json()["detail"]


def test_a_meeting_computer_needs_a_room():
    helper()
    r = client.post("/api/devices/claim", json={"code": waiting_box(), "use": "room"})
    assert r.status_code == 400 and "room" in r.json()["detail"]


def test_pairing_again_replaces_the_earlier_choice():
    helper()
    first = make_room_computer(waiting_box()).json()
    second = make_room_computer(waiting_box()).json()
    assert first["id"] not in appliances.items and second["id"] in appliances.items
    assert helper().json()["code"] == appliances.items[second["id"]]["code"]


def test_the_helper_is_public_but_bounded():
    assert helper(secret="short").status_code == 422
    assert helper(box="../etc").status_code == 422
    assert helper(box="firetv-abc123").status_code == 403  # only Xos box ids register
