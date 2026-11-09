import json
import time

import pytest
from fastapi.testclient import TestClient

from app.catalog import SEED_ROOMS
from app.main import app, hub, rooms

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_hub():
    hub.devices.clear()
    hub.zoom_log.clear()
    hub.commands.clear()
    rooms.items = {r["id"]: {**r, "created": 0} for r in SEED_ROOMS}


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_config_lists_rooms_and_sites():
    body = client.get("/api/config").json()
    assert [r["id"] for r in body["rooms"]] == ["atrium"]
    assert {s["id"] for s in body["sites"]} == {"hq", "annex"}
    assert body["roles"]["signage"]["arg"] == "site"


def test_rooms_add_rename_and_delete():
    lamar = client.post("/api/rooms", json={"name": "Lamar", "site": "hq"}).json()
    assert lamar["id"] == "lamar"
    again = client.post("/api/rooms", json={"name": "Lamar", "site": "annex"}).json()
    assert again["id"] == "lamar-2"
    assert client.post("/api/rooms", json={"name": " ", "site": "hq"}).status_code == 400
    assert client.post("/api/rooms", json={"name": "X", "site": "mars"}).status_code == 400
    renamed = client.put("/api/rooms/lamar", json={"name": "Lamar Huddle", "site": "hq"}).json()
    assert renamed["name"] == "Lamar Huddle" and renamed["created"] == lamar["created"]
    assert client.put("/api/rooms/nope", json={"name": "A", "site": "hq"}).status_code == 404
    assert "lamar" in {r["id"] for r in client.get("/api/config").json()["rooms"]}

    dev = _register("firetv-lamar")
    client.put(f"/api/devices/{dev}", json={"room": "lamar"})
    assert client.delete("/api/rooms/lamar").status_code == 200
    assert client.delete("/api/rooms/lamar").status_code == 404
    assert "room" not in client.get("/api/devices").json()[0]
    assert "lamar" not in {r["id"] for r in client.get("/api/rooms").json()}


def test_rooms_seed_keeps_rooms_in_use(tmp_path):
    from app.rooms import Rooms

    seeded = Rooms(tmp_path, {"lamar", "atrium", "Bad Id"})
    assert [r["id"] for r in seeded.all()] == ["atrium", "lamar"]
    assert Rooms(tmp_path).all() == seeded.all()  # saved, not re-seeded


def test_demo_state_endpoints_are_gone():
    assert client.get("/api/state").status_code == 404
    adapter = client.post("/api/adapter", json={"room": "atrium", "action": "mute"})
    assert adapter.status_code in (404, 405)


def test_device_heartbeat_registry():
    beat = {"id": "dev-1234", "client": "firetv-0.2.0", "role": "tv", "arg": "lamar"}
    r = client.post("/api/devices/heartbeat", json=beat, headers={"x-forwarded-for": "203.0.113.7"})
    assert r.status_code == 200 and r.json()["ip"] == "203.0.113.7"
    listed = client.get("/api/devices").json()
    assert listed[0]["id"] == "dev-1234" and listed[0]["online"] is True
    assert client.post("/api/devices/heartbeat", json={"id": "x"}).status_code == 422


def test_heartbeat_reports_whether_the_server_holds_the_stream():
    beat = {"id": "dev-stream-1", "client": "firetv-0.5.0", "role": "page"}
    assert client.post("/api/devices/heartbeat", json=beat).json()["stream"] is False
    queue = hub.subscribe_device("dev-stream-1")
    try:
        assert client.post("/api/devices/heartbeat", json=beat).json()["stream"] is True
    finally:
        hub.unsubscribe_device("dev-stream-1", queue)
    assert "stream" not in hub.devices["dev-stream-1"]  # reply only, never saved


def test_site_and_kiosk_served_but_not_source():
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/app/"
    assert client.get("/vision").status_code == 404
    assert client.get("/app/").status_code == 200
    assert client.get("/app/js/state.js").headers["cache-control"] == "no-cache"
    # No APK is checked in: the route answers 404 until an operator builds one.
    assert client.get("/firetv/xos-tv.apk").status_code in (200, 404)
    for private in ("/app-manifest.yml", "/src/app/main.py", "/os/agent/xos_agent.py", "/.env"):
        assert client.get(private).status_code == 404


def _register(device_id="firetv-abc123"):
    client.post("/api/devices/heartbeat", json={"id": device_id, "client": "firetv-0.4.0"})
    return device_id


def test_command_queued_until_device_connects_then_delivered():
    dev = _register()
    cmd = client.post(
        f"/api/devices/{dev}/commands", json={"type": "route", "args": {"route": "signage/hq"}}
    )
    assert cmd.status_code == 200 and cmd.json()["status"] == "pending"
    queue = hub.subscribe_device(dev)
    try:
        message = queue.get_nowait()
    finally:
        hub.unsubscribe_device(dev, queue)
    sent = json.loads(message.split("data: ", 1)[1])
    assert sent["type"] == "route" and sent["args"] == {"route": "signage/hq"}
    assert client.get(f"/api/devices/{dev}/commands").json()[0]["status"] == "sent"


def test_command_ack_and_validation():
    dev = _register()
    queue = hub.subscribe_device(dev)
    try:
        cid = client.post(f"/api/devices/{dev}/commands", json={"type": "reload"}).json()["id"]
        assert client.get("/api/devices").json()[0]["connected"] is True
    finally:
        hub.unsubscribe_device(dev, queue)
    done = client.post(f"/api/devices/{dev}/commands/{cid}/ack", json={"ok": True, "result": "ok"})
    assert done.json()["status"] == "done"
    assert client.post(f"/api/devices/{dev}/commands", json={"type": "explode"}).status_code == 422
    assert (
        client.post("/api/devices/nobody-here/commands", json={"type": "reload"}).status_code == 404
    )
    assert client.post(f"/api/devices/{dev}/commands/nope/ack", json={}).status_code == 404


def test_screenshot_round_trip():
    dev = _register()
    assert client.get(f"/api/devices/{dev}/screenshot").status_code == 404
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg"
    r = client.post(
        f"/api/devices/{dev}/screenshot", content=jpeg, headers={"content-type": "image/jpeg"}
    )
    assert r.status_code == 200
    got = client.get(f"/api/devices/{dev}/screenshot")
    assert got.content == jpeg and got.headers["content-type"] == "image/jpeg"
    assert client.get("/api/devices").json()[0]["screenshot"]["type"] == "image/jpeg"
    bad = client.post(
        f"/api/devices/{dev}/screenshot", content=b"x", headers={"content-type": "text/plain"}
    )
    assert bad.status_code == 415


def test_pages_upload_serve_and_delete():
    from app.main import pages

    assert client.get("/api/pages").json()[0]["id"] == "welcome"  # built in, listed first
    made = client.post(
        "/api/pages",
        json={"name": "Q4 Investor Update", "html": "<html><body><h1>Q4</h1></body></html>"},
    )
    assert made.status_code == 200 and made.json()["id"] == "q4-investor-update"
    served = client.get("/pages/q4-investor-update/")
    assert served.status_code == 200 and "<h1>Q4</h1>" in served.text
    assert 'm.xos === "scroll"' in served.text  # control script injected before </body>
    link = client.post(
        "/api/pages", json={"name": "Investor view", "url": "https://example.com/deck"}
    )
    assert link.json()["kind"] == "url"
    assert client.get("/pages/investor-view/").status_code == 404  # links aren't served by Xos
    assert client.delete("/api/pages/investor-view").json() == {"ok": True}
    assert (
        client.post("/api/pages", json={"name": "x", "url": "javascript:alert(1)"}).status_code
        == 400
    )
    assert client.delete("/api/pages/q4-investor-update").json() == {"ok": True}
    assert "q4-investor-update" not in pages.items


def test_rename_device_survives_heartbeats():
    dev = _register("firetv-atrium1")
    r = client.put(f"/api/devices/{dev}", json={"name": "Atrium room"})
    assert r.status_code == 200 and r.json()["name"] == "Atrium room"
    beat = client.post("/api/devices/heartbeat", json={"id": dev, "client": "firetv-0.4.0"})
    assert beat.json()["name"] == "Atrium room"  # heartbeats keep the admin-given name
    assert client.put("/api/devices/nobody-here", json={"name": "x"}).status_code == 404
    assert "name" not in client.put(f"/api/devices/{dev}", json={"name": ""}).json()


def test_pair_claim_and_forget():
    dev = "web-newscreen1"
    code = client.post("/api/devices/pair", json={"id": dev}).json()["code"]
    assert len(code) == 6 and code.isdigit()
    assert client.post("/api/devices/pair", json={"id": dev}).json()["code"] == code  # stable
    claimed = client.post(
        "/api/devices/claim", json={"code": code, "name": "Atrium room", "route": "signage/hq"}
    )
    assert claimed.status_code == 200 and claimed.json()["name"] == "Atrium room"
    sent = client.get(f"/api/devices/{dev}/commands").json()[0]
    assert sent["type"] == "route" and sent["args"] == {"route": "signage/hq"}
    assert client.post("/api/devices/claim", json={"code": code}).status_code == 404  # used up
    assert client.delete(f"/api/devices/{dev}").json() == {"ok": True}
    assert all(d["id"] != dev for d in client.get("/api/devices").json())
    assert client.delete(f"/api/devices/{dev}").status_code == 404


def test_playlist_pages():
    client.post("/api/pages", json={"name": "Slide A", "html": "<p>A</p>"})
    ok = client.post(
        "/api/pages",
        json={
            "name": "Lobby loop",
            "playlist": [
                {"page": "slide-a", "seconds": 15},
                {"page": "welcome", "seconds": 130},
            ],
        },
    )
    assert ok.status_code == 200 and ok.json()["kind"] == "playlist"
    bad = client.post("/api/pages", json={"name": "Broken", "playlist": [{"page": "nope"}]})
    assert bad.status_code == 400


def test_new_screen_defaults_to_the_story():
    code = client.post("/api/devices/pair", json={"id": "web-newtv"}).json()["code"]
    assert (
        client.post("/api/devices/claim", json={"code": code, "name": "Lobby"}).status_code == 200
    )
    sent = client.get("/api/devices/web-newtv/commands").json()[0]
    assert sent["type"] == "route" and sent["args"] == {"route": "page/welcome"}


def test_setup_short_link_and_remote_claim():
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/app/?kiosk=1#/setup"
    code = client.post("/api/devices/pair", json={"id": "web-ipad-atrium"}).json()["code"]
    ok = client.post(
        "/api/devices/claim",
        json={"code": code, "name": "Atrium iPad", "route": "remote/atrium-room"},
    )
    assert ok.status_code == 200
    sent = client.get("/api/devices/web-ipad-atrium/commands").json()[0]
    assert sent["args"] == {"route": "remote/atrium-room"}


def test_device_room_assignment():
    dev = _register("firetv-roomtest")
    assert client.put(f"/api/devices/{dev}", json={"room": "atrium"}).json()["room"] == "atrium"
    after = client.put(f"/api/devices/{dev}", json={"name": "Atrium room"}).json()
    assert after["room"] == "atrium" and after["name"] == "Atrium room"
    assert "room" not in client.put(f"/api/devices/{dev}", json={"room": ""}).json()


def test_rtc_signalling_relay():
    cam = hub.rtc_join("cam-test", "camera")
    viewer = hub.rtc_join("cam-test", "v-1234")
    try:
        assert client.get("/api/rtc/cam-test/peers").json() == {
            "peers": ["camera", "v-1234"],
            "camera": True,
        }
        r = client.post(
            "/api/rtc/cam-test/signal", json={"from": "v-1234", "to": "camera", "type": "join"}
        )
        assert r.json() == {"delivered": 1}
        assert json.loads(cam.get_nowait().split("data: ", 1)[1])["type"] == "join"
        client.post(
            "/api/rtc/cam-test/signal",
            json={
                "from": "camera",
                "to": "v-1234",
                "type": "offer",
                "data": {"type": "offer", "sdp": "v=0"},
            },
        )
        assert json.loads(viewer.get_nowait().split("data: ", 1)[1])["data"]["sdp"] == "v=0"
        gone = client.post(
            "/api/rtc/cam-test/signal", json={"from": "camera", "to": "v-nobody", "type": "offer"}
        )
        assert gone.status_code == 409
    finally:
        hub.rtc_leave("cam-test", "v-1234", viewer)
    bye = json.loads(cam.get_nowait().split("data: ", 1)[1])
    assert bye == {"from": "v-1234", "to": "*", "type": "bye"}
    hub.rtc_leave("cam-test", "camera", cam)
    assert client.get("/api/rtc/cam-test/peers").json()["peers"] == []


def test_node_install_from_server():
    import io
    import tarfile

    assert client.get("/os/install.sh").text.startswith("#!/usr/bin/env bash")
    res = client.get("/os/xos-node.tar.gz")
    assert res.status_code == 200
    names = tarfile.open(fileobj=io.BytesIO(res.content)).getnames()
    assert "os/install.sh" in names and "app/index.html" in names
    assert not any("__pycache__" in n for n in names)
    assert client.get("/os/xos-node.sha256").text.strip() == res.headers["x-xos-bundle"]


def test_remotes_hear_about_changes_live():
    """GET /api/devices/events: remotes learn of changes and command results without polling."""
    queue = hub.watch()
    try:
        drain = lambda: [json.loads(queue.get_nowait()[6:]) for _ in range(queue.qsize())]  # noqa: E731
        client.post("/api/devices/heartbeat", json={"id": "tv-1", "role": "page", "arg": "a"})
        assert drain() == [{"device": "tv-1", "kind": "device"}]
        client.post("/api/devices/heartbeat", json={"id": "tv-1", "role": "page", "arg": "a"})
        assert drain() == []  # an unchanged heartbeat is not news
        cmd = client.post("/api/devices/tv-1/commands", json={"type": "reload"}).json()
        client.post(f"/api/devices/tv-1/commands/{cmd['id']}/ack", json={"ok": True})
        events = drain()
        assert [e["kind"] for e in events] == ["command", "command"]
        assert events[1]["command"]["status"] == "done"
        client.put("/api/devices/tv-1", json={"room": "atrium"})
        assert drain() == [{"device": "tv-1", "kind": "device"}]
    finally:
        hub.unwatch(queue)


def test_server_clock_for_synced_screens():
    before = time.time() * 1000
    now = client.get("/api/time").json()["now"]
    assert before - 1 <= now <= time.time() * 1000 + 1


def test_superseded_screenshot_does_not_overwrite_new_capture():
    dev = _register()
    path = f"/api/devices/{dev}"
    first = client.post(f"{path}/commands", json={"type": "screenshot"}).json()
    second = client.post(f"{path}/commands", json={"type": "screenshot"}).json()
    headers = {"content-type": "image/jpeg"}
    assert first["args"]["capture_id"] == first["id"]
    assert (
        client.post(
            f"{path}/screenshot?capture_id={second['id']}", content=b"new", headers=headers
        ).status_code
        == 200
    )
    before = client.get("/api/devices").json()
    assert (
        client.post(
            f"{path}/screenshot?capture_id={first['id']}", content=b"old", headers=headers
        ).status_code
        == 409
    )
    assert client.get(f"{path}/screenshot").content == b"new"
    assert client.get("/api/devices").json() == before


def test_uploaded_media_is_cached_for_good_and_app_files_revalidate():
    r = client.post(
        "/api/media?filename=still.png",
        content=b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        headers={"Content-Type": "image/png"},
    )
    assert r.status_code == 200, r.text
    got = client.get(r.json()["url"])
    assert got.status_code == 200
    assert got.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/app/").headers["cache-control"] == "no-cache"
    assert client.get("/media/missing.png").headers.get("cache-control") != (
        "public, max-age=31536000, immutable"
    )
    assert client.delete(f"/api/media/{r.json()['id']}").json() == {"ok": True}
