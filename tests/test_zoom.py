import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main
from app import access
from app.zoom import Zoom

client = TestClient(main.app)
calls: list[httpx.Request] = []


def fake_zoom(request: httpx.Request) -> httpx.Response:
    calls.append(request)
    if request.url.path == "/oauth/token":
        assert request.url.params["grant_type"] == "account_credentials"
        assert request.url.params["account_id"] == "acct-1"
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    assert request.headers["authorization"] == "Bearer tok-1"
    if request.method == "GET" and request.url.path == "/v2/rooms":
        return httpx.Response(
            200,
            json={
                "rooms": [
                    {"id": "zr-atrium", "name": "Atrium", "status": "Available"},
                    {"id": "zr-lamar", "name": "Lamar", "status": "Offline"},
                ]
            },
        )
    if request.method == "PATCH" and request.url.path == "/v2/rooms/zr-atrium/events":
        return httpx.Response(202)
    if request.method == "PATCH":
        return httpx.Response(404, json={"code": 3001, "message": "Room does not exist"})
    return httpx.Response(500)


def settings(configured=True):
    return SimpleNamespace(
        ZOOM_ACCOUNT_ID="acct-1" if configured else "",
        ZOOM_CLIENT_ID="cid",
        ZOOM_CLIENT_SECRET=SecretStr("secret" if configured else ""),
        ZOOM_API_BASE="https://api.zoom.us/v2",
        ZOOM_OAUTH_URL="https://zoom.us/oauth/token",
    )


@pytest.fixture
def live_zoom(monkeypatch):
    calls.clear()
    z = Zoom(settings(), Path(tempfile.mkdtemp()), transport=httpx.MockTransport(fake_zoom))
    monkeypatch.setattr(main, "zoom", z)
    return z


def test_not_connected_is_clear_and_safe(monkeypatch):
    monkeypatch.setattr(main, "zoom", Zoom(settings(False), Path(tempfile.mkdtemp())))
    st = client.get("/api/zoom/status").json()
    assert st["configured"] is False and st["connected"] is False
    client.put("/api/zoom/map", json={"room": "atrium", "zoom_room_id": "zr-atrium"})
    r = client.post("/api/rooms/atrium/zoom", json={"action": "mute"})
    assert r.status_code == 409 and "isn't connected" in r.json()["detail"]
    logged = client.get("/api/zoom/log").json()[0]
    assert logged["action"] == "mute" and logged["sent"] is False


def test_status_list_and_map(live_zoom):
    st = client.get("/api/zoom/status").json()
    assert st["connected"] is True and st["zoom_rooms"] == 2
    assert [r["name"] for r in client.get("/api/zoom/rooms").json()] == ["Atrium", "Lamar"]
    assert client.put(
        "/api/zoom/map", json={"room": "atrium", "zoom_room_id": "zr-atrium"}
    ).json() == {"rooms": {"atrium": "zr-atrium"}}
    rs = client.get("/api/rooms/atrium/zoom").json()
    assert rs["mapped"] and rs["status"] == "Available" and rs["name"] == "Atrium"
    token_calls = [c for c in calls if c.url.path == "/oauth/token"]
    assert len(token_calls) == 1  # token cached across calls


def test_controls_send_real_zoom_events(live_zoom):
    client.put("/api/zoom/map", json={"room": "atrium", "zoom_room_id": "zr-atrium"})
    r = client.post(
        "/api/rooms/atrium/zoom",
        json={"action": "join", "meeting_number": "842 1337 7120", "passcode": "abc"},
    )
    assert r.status_code == 200 and r.json()["method"] == "zoomroom.meeting_join"
    body = json.loads(calls[-1].content)
    assert body == {
        "method": "zoomroom.meeting_join",
        "params": {"meeting_number": "84213377120", "passcode": "abc", "force_accept": True},
    }
    assert client.post("/api/rooms/atrium/zoom", json={"action": "mute"}).status_code == 200
    assert json.loads(calls[-1].content) == {"method": "zoomroom.mute"}
    assert client.post("/api/rooms/atrium/zoom", json={"action": "join"}).status_code == 400
    assert client.post("/api/rooms/nowhere/zoom", json={"action": "mute"}).status_code == 409
    log = client.get("/api/zoom/log").json()
    assert log[0]["room"] == "nowhere" and log[0]["sent"] is False
    assert log[2]["sent"] is True and log[2]["live"]["method"] == "zoomroom.mute"


def test_zoom_errors_are_reported(live_zoom):
    client.put("/api/zoom/map", json={"room": "atrium", "zoom_room_id": "zr-gone"})
    r = client.post("/api/rooms/atrium/zoom", json={"action": "mute"})
    assert r.status_code == 400 and "Room does not exist" in r.json()["detail"]
    assert "isn't in this Zoom account" in client.get("/api/rooms/atrium/zoom").json()["error"]


def test_meeting_links_and_codes_are_understood():
    from app.zoom import ZoomError, parse_meeting

    assert parse_meeting("842 1337 7120") == {
        "kind": "zoom",
        "number": "84213377120",
        "passcode": "",
    }
    assert parse_meeting("https://example.zoom.us/j/84213377120?pwd=AbC123") == {
        "kind": "zoom",
        "number": "84213377120",
        "passcode": "AbC123",
    }
    assert parse_meeting("abc-defg-hij") == {
        "kind": "meet",
        "url": "https://meet.google.com/abc-defg-hij",
    }
    assert parse_meeting("https://meet.google.com/abc-defg-hij?authuser=0")["url"].endswith(
        "/abc-defg-hij"
    )
    with pytest.raises(ZoomError):
        parse_meeting("tomorrow")


def test_join_meet_camera_volume_and_meet_now(live_zoom):
    client.put("/api/zoom/map", json={"room": "atrium", "zoom_room_id": "zr-atrium"})
    zoom = lambda body: client.post("/api/rooms/atrium/zoom", json=body)  # noqa: E731
    r = zoom({"action": "join", "meeting": "meet.google.com/abc-defg-hij"})
    assert r.status_code == 200 and r.json()["method"] == "zoomroom.thirdparty_meeting_join"
    assert json.loads(calls[-1].content)["params"] == {
        "meeting_source_type": "GOOGLE_MEET",
        "join_type": "url",
        "join_url": "https://meet.google.com/abc-defg-hij",
    }
    assert (
        zoom({"action": "join", "meeting": "https://zoom.us/j/84213377120?pwd=x1"}).status_code
        == 200
    )
    assert json.loads(calls[-1].content)["params"] == {
        "meeting_number": "84213377120",
        "passcode": "x1",
        "force_accept": True,
    }
    assert zoom({"action": "camera_off"}).status_code == 200
    assert json.loads(calls[-1].content) == {"method": "zoomroom.video_mute"}
    assert zoom({"action": "volume", "volume": 40}).status_code == 200
    assert json.loads(calls[-1].content) == {
        "method": "zoomroom.volume_level",
        "params": {"volume_level": 40},
    }
    assert zoom({"action": "volume"}).status_code == 400
    assert zoom({"action": "meet_now"}).status_code == 200
    assert json.loads(calls[-1].content) == {
        "method": "zoomroom.meeting_join",
        "params": {"force_accept": True},
    }
    assert zoom({"action": "join", "meeting": "soon"}).status_code == 400


def test_setup_creates_links_and_returns_activation_code(monkeypatch):
    calls.clear()
    created = []

    def fake(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        if request.method == "POST" and request.url.path == "/v2/rooms":
            created.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "zr-new", "name": "Test bed"})
        calls.append(request)
        if request.method == "GET" and request.url.path == "/v2/rooms/zr-new":
            return httpx.Response(
                200, json={"basic": {"name": "Test bed", "activation_code": "1234-5678-9012"}}
            )
        return fake_zoom(request)

    z = Zoom(settings(), Path(tempfile.mkdtemp()), transport=httpx.MockTransport(fake))
    monkeypatch.setattr(main, "zoom", z)
    r = client.post("/api/zoom/setup", json={"room": "test-bed"}).json()
    assert r == {
        "room": "test-bed",
        "zoom_room_id": "zr-new",
        "created": True,
        "activation_code": "1234-5678-9012",
    }
    assert created == [{"name": "test-bed", "type": "ZoomRoom"}] and z.rooms["test-bed"] == "zr-new"
    again = client.post("/api/zoom/setup", json={"room": "test-bed"}).json()
    assert (
        again["created"] is False and len(created) == 1
    )  # a linked room is reused, never duplicated
    # Its PC isn't signed in yet, so the code may have expired: Zoom is asked for a new one.
    codes = [c for c in calls if c.url.path == "/v2/rooms/zr-new"]
    assert codes and codes[-1].url.params.get("regenerate_activation_code") == "true"


def test_setup_without_a_zoom_rooms_licence_says_so(monkeypatch):
    def fake(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v2/rooms":
            return httpx.Response(
                400, json={"code": 200, "message": "Zoom Room subscription not found."}
            )
        return fake_zoom(request)

    z = Zoom(settings(), Path(tempfile.mkdtemp()), transport=httpx.MockTransport(fake))
    monkeypatch.setattr(main, "zoom", z)
    r = client.post("/api/zoom/setup", json={"room": "test-bed"})
    assert r.status_code == 409 and "no Zoom Rooms licence" in r.json()["detail"]
    assert "test-bed" not in z.rooms


def test_mcp_zoom_control_reaches_zoom(live_zoom):
    import asyncio

    from app import mcp_server

    client.put("/api/zoom/map", json={"room": "atrium", "zoom_room_id": "zr-atrium"})
    with pytest.raises(ValueError, match="no verified MCP caller"):  # fails closed when unset
        asyncio.run(mcp_server.zoom_control("atrium", "mute"))
    token = mcp_server.current_caller.set(access.APP_TOKEN)  # as CheckToolPermission does
    try:
        out = asyncio.run(mcp_server.zoom_control("atrium", "join", meeting="abc-defg-hij"))
        assert out["method"] == "zoomroom.thirdparty_meeting_join"
        assert (
            asyncio.run(mcp_server.zoom_control("atrium", "camera_off"))["method"]
            == "zoomroom.video_mute"
        )
    finally:
        mcp_server.current_caller.reset(token)


@pytest.mark.parametrize("path", ["j", "w", "s", "wc/join"])
@pytest.mark.parametrize(
    "meeting_id", ["12345678", "123456789012", "1234567890123", "123456789abc"]
)
def test_zoom_link_rejects_partial_meeting_ids(path, meeting_id):
    from app.zoom import ZoomError, parse_meeting

    with pytest.raises(ZoomError):
        parse_meeting(f"https://zoom.us/{path}/{meeting_id}?pwd=example")


@pytest.mark.parametrize("meeting_id", ["123456789", "1234567890", "12345678901"])
@pytest.mark.parametrize("suffix", ["", "/", "?pwd=example", "#details"])
def test_zoom_link_preserves_complete_meeting_id(meeting_id, suffix):
    from app.zoom import parse_meeting

    assert parse_meeting(f"https://zoom.us/j/{meeting_id}{suffix}")["number"] == meeting_id


@pytest.mark.parametrize(
    ("encoded", "decoded"),
    [("%41bC123", "AbC123"), ("a%26b", "a&b"), ("a%2Bb", "a+b"), ("a%2526b", "a%26b")],
)
def test_zoom_join_decodes_invitation_passcode_once(live_zoom, encoded, decoded):
    client.put("/api/zoom/map", json={"room": "atrium", "zoom_room_id": "zr-atrium"})
    response = client.post(
        "/api/rooms/atrium/zoom",
        json={"action": "join", "meeting": f"https://zoom.us/j/84213377120?pwd={encoded}"},
    )
    assert response.status_code == 200
    assert json.loads(calls[-1].content)["params"]["passcode"] == decoded


def test_manual_zoom_passcode_is_not_url_decoded():
    from app.zoom import parse_meeting

    assert (
        parse_meeting("https://zoom.us/j/84213377120?pwd=link", "manual%41+")["passcode"]
        == "manual%41+"
    )
