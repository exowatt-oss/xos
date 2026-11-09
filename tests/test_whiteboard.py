import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, hub
from app.whiteboard import Whiteboards

client = TestClient(app)
PATH = "/api/rooms/atrium/whiteboard"
STROKE = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def boards(tmp_path, monkeypatch):
    from app import main
    from app.whiteboard_exports import WhiteboardExports

    monkeypatch.setattr(main, "whiteboard_exports", WhiteboardExports(tmp_path))
    boards = Whiteboards(tmp_path)
    monkeypatch.setattr(main, "whiteboards", boards)
    monkeypatch.setattr(hub, "devices", {})
    monkeypatch.setattr(hub, "commands", {})
    return boards


def write(action, **extra):
    return client.post(PATH, json={"action": action, "epoch": 0, **extra})


def test_live_chunks_retry_reconnect_undo_and_exit_persist(boards, tmp_path):
    assert write("start").status_code == 200
    chunk = dict(id=STROKE, offset=0, points=[[10, 20], [500, 400]])
    assert write("stroke", **chunk).status_code == 200
    assert write("stroke", **chunk).status_code == 200  # lost response, retry same chunk
    assert write("stroke", id=STROKE, offset=2, points=[[1600, 900]], done=True).status_code == 200
    reloaded = Whiteboards(tmp_path).snapshot("atrium")
    assert reloaded["active"]
    assert reloaded["strokes"][0]["points"] == [[10, 20], [500, 400], [1600, 900]]
    queue = boards.subscribe("atrium")
    assert queue.get_nowait()["strokes"] == reloaded["strokes"]
    assert write("undo").json()["epoch"] == 1
    assert queue.get_nowait()["kind"] == "undo"
    assert write("stroke", **chunk).status_code == 409  # delayed input cannot resurrect undo
    assert write("exit", epoch=1).status_code == 200
    assert not Whiteboards(tmp_path).get("atrium")["active"]
    assert client.get(PATH).headers["cache-control"] == "no-store"
    boards.unsubscribe("atrium", queue)
    assert not boards.watchers["atrium"]


def test_clear_invalidates_input_and_exit_keeps_drawing_for_resume():
    write("start")
    write("stroke", id=STROKE, points=[[1, 2]], done=True)
    write("exit")
    assert len(client.get(PATH).json()["strokes"]) == 1
    write("start", epoch=1)
    assert write("clear", epoch=1).json()["epoch"] == 2
    assert client.get(PATH).json()["strokes"] == []
    assert write("stroke", id=STROKE, points=[[3, 4]], epoch=1).status_code == 409


def test_only_team_or_this_rooms_trusted_controller_can_draw(monkeypatch):
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    assert write("start").status_code == 401
    monkeypatch.setattr(settings, "XOS_DEV_USER", "person@example.com")
    assert write("start").status_code == 403
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    hub.devices["ipad-test"] = {"id": "ipad-test", "role": "tablet", "room": "atrium"}
    monkeypatch.setattr(hub, "device_keys", {})
    key = hub.issue_key("ipad-test")
    headers = {"X-Xos-Device": "ipad-test", "X-Xos-Key": key}
    assert (
        client.post(PATH, json={"action": "start", "epoch": 0}, headers=headers).status_code == 200
    )
    hub.devices["ipad-test"]["room"] = "other-room"
    assert (
        client.post(PATH, json={"action": "clear", "epoch": 0}, headers=headers).status_code == 403
    )
    hub.devices["ipad-test"].update(role="roompc", room="atrium")
    assert (
        client.post(PATH, json={"action": "clear", "epoch": 0}, headers=headers).status_code == 401
    )


def test_active_calls_are_preserved_and_whiteboard_blocks_new_calls(boards):
    hub.devices["room-test"] = {
        "id": "room-test",
        "role": "roompc",
        "arg": "atrium",
        "native": {"meeting": {"in_call": True}},
    }
    assert write("start").status_code == 409
    assert not boards.get("atrium")["active"]
    hub.devices["room-test"]["native"]["meeting"] = {"waiting": True}
    assert write("start").status_code == 409
    hub.devices["room-test"]["native"]["meeting"] = {}
    pending = hub.queue_command("room-test", "meeting", {"action": "meet_now"})
    assert write("start").status_code == 409
    hub.ack("room-test", pending["id"], False, "cancelled")
    assert write("start").status_code == 200
    assert hub.commands["room-test"][0]["type"] == "refresh"
    command = {"type": "meeting", "args": {"action": "meet_now"}}
    assert client.post("/api/devices/room-test/commands", json=command).status_code == 409
    assert client.post("/api/rooms/atrium/zoom", json={"action": "meet_now"}).status_code == 409


def test_input_bounds_sequence_and_immutable_pen(boards):
    assert write("stroke", id=STROKE, points=[[1, 1]]).status_code == 409
    write("start")
    assert write("stroke", points=[[1, 1]]).status_code == 400
    for bad in ([[100001, 0]], [[0, -100001]]):
        assert write("stroke", id=STROKE, points=bad).status_code == 422
    assert write("stroke", id=STROKE, points=[[1, 1]] * 257).status_code == 422
    assert write("stroke", id=STROKE, points=[[1, 1]], width=100).status_code == 422
    from pydantic import ValidationError

    from app.main import WhiteboardOp

    with pytest.raises(ValidationError):
        WhiteboardOp(action="stroke", epoch=0, points=[[float("nan"), 0]])
    assert write("stroke", id=STROKE, points=[[1, 1]]).status_code == 200
    assert write("stroke", id=STROKE, offset=3, points=[[1, 1]]).status_code == 409
    assert write("stroke", id=STROKE, offset=0, points=[[2, 1]]).status_code == 409
    assert write("stroke", id=STROKE, offset=1, points=[[2, 1]], color="#2563eb").status_code == 409
    boards.MAX_POINTS = 1
    assert write("stroke", id=STROKE, offset=1, points=[[2, 1]]).status_code == 409
    assert client.get("/api/rooms/not-a-room/whiteboard").status_code == 404


def test_slow_tv_receives_a_complete_snapshot_instead_of_missing_chunks(boards):
    queue = boards.subscribe("atrium")
    for _ in range(130):
        boards.apply("atrium", {"action": "start", "epoch": 0})
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events[0]["kind"] == "snapshot"
    assert events[0]["active"] is True
    assert events[-1]["revision"] == boards.get("atrium")["revision"]


def test_mapped_zoom_room_cannot_be_replaced_during_a_call(monkeypatch):
    from app import main

    monkeypatch.setattr(main.zoom, "rooms", {"atrium": "zoom-test"})
    monkeypatch.setattr(type(main.zoom), "configured", property(lambda self: True))

    async def status(_):
        return {"status": "InMeeting"}

    monkeypatch.setattr(main.zoom, "room_status", status)
    assert write("start").status_code == 409


def test_entry_refreshes_only_this_rooms_displays_once():
    hub.devices.update(
        {
            "room-tv": {"id": "room-tv", "role": "tv", "arg": "atrium"},
            "room-ipad": {"id": "room-ipad", "role": "tablet", "arg": "atrium"},
            "other-tv": {"id": "other-tv", "role": "tv", "arg": "other"},
        }
    )
    assert write("start").status_code == 200
    assert list(hub.commands) == ["room-tv"]
    assert hub.commands["room-tv"][0]["type"] == "reload"
    assert write("start").status_code == 200
    assert len(hub.commands["room-tv"]) == 1


def test_org_canvas_can_draw_without_turning_on_the_room_tv(monkeypatch):
    monkeypatch.setattr(settings, "XOS_DEV_USER", "teammate@example.com")
    assert write("start").status_code == 403
    result = client.post(
        PATH + "/drawing",
        json={
            "action": "stroke",
            "epoch": 0,
            "id": STROKE,
            "points": [[100, 200]],
            "done": True,
        },
    )
    assert result.status_code == 200
    assert not result.json()["active"]
    assert len(client.get(PATH).json()["strokes"]) == 1
    assert client.post(PATH + "/drawing", json={"action": "start", "epoch": 0}).status_code == 403
    assert client.get(PATH + "/access").json() == {"editable": True}
    link = client.get(PATH + "/share").json()["org_url"]
    assert link.endswith("/u/whiteboards/atrium")
    assert "board_key" not in link
    response = client.get("/u/whiteboards/atrium", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/app/#/whiteboard/atrium"


def test_copper_key_is_board_only_revocable_and_never_in_read_state(monkeypatch, tmp_path):
    from urllib.parse import parse_qs, urlsplit

    connection = {"canvas_id": STROKE, "web_url": "https://cloud.example.com/join/test"}
    response = client.put(PATH + "/copper", json=connection)
    assert response.status_code == 200
    key = parse_qs(urlsplit(response.json()["embed_url"]).fragment.split("?")[1])["board_key"][0]
    assert key not in (tmp_path / "whiteboards" / "copper.json").read_text()
    assert Whiteboards(tmp_path).drawing_key("atrium", key)
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    assert client.get(PATH + "/access").json() == {"editable": False}
    assert client.get(PATH + "/share").status_code == 401
    headers = {"X-Xos-Board": key}
    assert client.get(PATH + "/access", headers=headers).json() == {"editable": True}
    assert (
        client.get("/api/rooms/lakeside/whiteboard/access", headers=headers).json().get("editable")
        is not True
    )
    op = {"action": "stroke", "epoch": 0, "id": STROKE, "points": [[1, 2]], "done": True}
    assert client.post(PATH + "/drawing", json=op).status_code == 401
    assert client.post(PATH + "/drawing", json=op, headers=headers).status_code == 200
    assert (
        client.post(PATH, json={"action": "start", "epoch": 0}, headers=headers).status_code == 401
    )
    for action in ["start", "exit"]:
        assert (
            client.post(
                PATH + "/drawing", json={"action": action, "epoch": 0}, headers=headers
            ).status_code
            == 403
        )
    for route in [PATH, PATH + "/share", "/api/config"]:
        text = client.get(route, headers=headers).text
        assert key not in text
        assert "key_hash" not in text
    monkeypatch.setattr(settings, "XOS_DEV_USER", "root@example.com")
    assert client.put(PATH + "/copper", json=connection).status_code == 200
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    assert client.get(PATH + "/access", headers=headers).json() == {"editable": False}
    assert client.post(PATH + "/drawing", json=op, headers=headers).status_code == 401


def test_copper_connection_requires_admin_and_safe_urls(monkeypatch):
    connection = {"canvas_id": STROKE, "web_url": "https://cloud.example.com/join/test"}
    for url in [
        "javascript:alert(1)",
        "http://cloud.example.com/join/a",
        "https://name:password@cloud.example.com",
        "https://cloud.example.com/#secret",
    ]:
        assert client.put(PATH + "/copper", json={**connection, "web_url": url}).status_code == 400
    monkeypatch.setattr(settings, "XOS_DEV_USER", "teammate@example.com")
    assert client.put(PATH + "/copper", json=connection).status_code == 403


def test_native_copper_invitation_is_kept_for_easy_sharing():
    invitation = "copper://canvas/join/" + "a" * 43 + "?cloud=cloud.example.com:443&n=Room"
    response = client.put(PATH + "/copper", json={"canvas_id": STROKE, "invite_url": invitation})
    assert response.status_code == 200
    assert client.get(PATH + "/share").json()["copper_invite_url"] == invitation
    for invalid in ["javascript:alert(1)", "copper://settings/cloud", "copper://canvas/join/short"]:
        assert (
            client.put(
                PATH + "/copper", json={"canvas_id": STROKE, "invite_url": invalid}
            ).status_code
            == 400
        )


PNG_1PX = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mP8/x8AAusB9Wl6W1sAAAAASUVORK5CYII="
)


def snapshot_export(**extra):
    return client.post(
        PATH + "/exports",
        json={"png": PNG_1PX, "width": 1, "height": 1, "epoch": 0, "revision": 0, **extra},
    )


def test_native_snapshot_is_immutable_expiring_and_only_reports_an_actual_receipt(tmp_path):
    from app.whiteboard_exports import WhiteboardExports

    response = snapshot_export()
    assert response.status_code == 200
    source = response.json()["source"].replace(settings.XOS_PUBLIC_URL, "")
    payload = client.get(source).json()
    assert payload["version"] == 1 and payload["room"] == "atrium"
    assert payload["png"] == PNG_1PX
    assert "board_key" not in str(payload) and "key_hash" not in str(payload)
    assert client.get(source).headers["cache-control"] == "no-store"
    assert client.get(source + "/status").json()["status"] == "pending"
    write("start")
    write("stroke", id=STROKE, points=[[-200, 1200]], done=True)
    assert client.get(source).json() == payload
    receipt = {
        "canvas_id": "personal",
        "canvas_name": "Personal",
        "shape_id": "xos-" + payload["id"],
    }
    bad = {**receipt, "shape_id": "xos-" + STROKE}
    assert client.post(source + "/receipt", json=bad).status_code == 400
    assert client.get(source + "/status").json()["status"] == "pending"
    assert client.post(source + "/receipt", json=receipt).status_code == 200
    result = client.get(source + "/status").json()
    assert result["status"] == "inserted" and result["receipt"]["canvas_id"] == "personal"
    token = source.rsplit("/", 1)[1]
    assert WhiteboardExports(tmp_path).get(token)["receipt"]["shape_id"] == receipt["shape_id"]


def test_native_snapshot_requires_drawing_access_and_a_valid_png(monkeypatch):
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    assert snapshot_export().status_code == 401
    monkeypatch.setattr(settings, "XOS_DEV_USER", "teammate@example.com")
    assert snapshot_export().status_code == 200
    assert snapshot_export(png="data:image/png;base64,invalid").status_code == 400
    assert snapshot_export(width=2).status_code == 400
    assert snapshot_export(width=4097).status_code == 422
    assert snapshot_export(epoch=12).status_code == 409
    assert snapshot_export(revision=100).status_code == 409
    assert snapshot_export(meeting_id="made-up").status_code == 409
    assert client.get("/api/whiteboard-exports/not-a-token").status_code == 410


def test_snapshot_expiry_and_capacity_are_bounded(monkeypatch):
    from app.whiteboard_exports import WhiteboardExports

    monkeypatch.setattr(WhiteboardExports, "MAX_ITEMS", 1)
    assert snapshot_export().status_code == 200
    assert snapshot_export().status_code == 429
    monkeypatch.setattr(WhiteboardExports, "MAX_ITEMS", 64)
    monkeypatch.setattr(WhiteboardExports, "TTL", 0)
    source = snapshot_export().json()["source"].replace(settings.XOS_PUBLIC_URL, "")
    assert client.get(source).status_code == 410
    assert (
        client.post(
            source + "/receipt",
            json={"canvas_id": "personal", "canvas_name": "Personal", "shape_id": "xos-" + STROKE},
        ).status_code
        == 410
    )


def test_meeting_context_is_resolved_on_the_server(monkeypatch):
    from app import main

    monkeypatch.setattr(type(main.calendars), "configured", property(lambda _: True))
    monkeypatch.setattr(main.rooms, "calendar", lambda _: "room@example.com")

    async def bookings(_):
        return [
            {
                "id": "booking-1",
                "title": "Design <review>",
                "start": 1,
                "end": 9999999999,
                "join_url": "private-meeting-link",
            }
        ]

    monkeypatch.setattr(main.calendars, "bookings", bookings)
    context = client.get(PATH + "/context").json()
    assert context["person"]["email"] == "root@example.com"
    assert "private-meeting-link" not in str(context)
    assert context["meetings"][0]["id"] == "booking-1"
    response = snapshot_export(meeting_id="booking-1")
    payload = client.get(response.json()["source"].replace(settings.XOS_PUBLIC_URL, "")).json()
    assert payload["meeting_id"] == "booking-1" and payload["meeting_title"] == "Design <review>"


def test_native_ink_colours_widths_and_replace(boards):
    write("start")
    marker = dict(id=STROKE, points=[[1, 1], [9, 9]], color="#f5c51866", width=22.5, done=True)
    assert write("stroke", **marker).status_code == 200
    for bad in ({"color": "red"}, {"color": "#ABCDEF"}, {"width": 0.5}, {"width": 65}):
        assert write("stroke", id=STROKE, points=[[1, 1]], **bad).status_code == 422
    queue = boards.subscribe("atrium")
    queue.get_nowait()
    kept = {"id": STROKE, "color": "#f5c51866", "width": 22.5, "points": [[1, 1], [5, 5]]}
    other = {"id": STROKE.replace("1", "2"), "color": "#000000", "width": 2, "points": [[3, 3]]}
    event = write("replace", strokes=[kept, other]).json()
    assert event["kind"] == "snapshot" and event["epoch"] == 1
    assert [s["points"] for s in event["strokes"]] == [[[1, 1], [5, 5]], [[3, 3]]]
    assert all(s["done"] for s in event["strokes"])
    assert queue.get_nowait()["strokes"] == event["strokes"]  # screens get the whole drawing
    assert write("replace", strokes=[kept]).status_code == 409  # drawn on a stale board
    assert write("replace", epoch=1, strokes=[kept, kept]).status_code == 409
    assert write("replace", epoch=1, strokes=[{**kept, "points": []}]).status_code == 422
    boards.MAX_POINTS = 2
    assert write("replace", epoch=1, strokes=[kept, other]).status_code == 409
    assert write("replace", epoch=1, strokes=[]).json()["strokes"] == []
