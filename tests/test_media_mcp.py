import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import app, hub, media, pages, settings

TOKEN = "test-mcp-token"

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture(scope="module")
def running():
    # Runs the app lifespan (once: the MCP session manager can only start once per process).
    settings.XOS_MCP_TOKEN = SecretStr(TOKEN)
    with TestClient(app) as c:
        yield c
    settings.XOS_MCP_TOKEN = SecretStr("")


@pytest.fixture(autouse=True)
def sample_builtin(monkeypatch):
    """Xos ships no built-in media; a deployment can add some. Stand one in."""
    monkeypatch.setitem(
        media.builtin,
        "sample-clip",
        {
            "id": "sample-clip",
            "name": "Sample clip",
            "kind": "video",
            "type": "video/mp4",
            "url": "/static/builtin-media/sample.mp4",
            "builtin": True,
        },
    )


@pytest.fixture
def client(running):
    c = running
    hub.devices.clear()
    hub.commands.clear()
    hub.shares.clear()
    c.post("/api/devices/heartbeat", json={"id": "firetv-1", "role": "tv", "arg": "atrium"})
    c.put("/api/devices/firetv-1", json={"name": "Atrium room", "room": "atrium"})
    yield c
    for item in list(media.items):
        media.delete(item)
    for page in list(pages.items):
        pages.delete(page)


def upload(client, name="Lobby photo", data=PNG, media_type="image/png"):
    return client.post(
        f"/api/media?name={name}", content=data, headers={"Content-Type": media_type}
    )


def test_upload_list_serve_rename_delete(client):
    item = upload(client).json()
    assert item["kind"] == "image" and item["bytes"] == len(PNG)
    listed = client.get("/api/media").json()
    assert listed["items"][0]["id"] == item["id"] and listed["used_bytes"] == len(PNG)
    assert any(m["builtin"] for m in listed["items"] if m.get("builtin"))
    assert client.get(item["url"]).content == PNG
    assert client.put(f"/api/media/{item['id']}", json={"name": "Lobby"}).json()["name"] == "Lobby"
    assert client.delete(f"/api/media/{item['id']}").json() == {"ok": True}
    assert client.get(item["url"]).status_code == 404


def test_upload_rejects_unsafe_types_and_builtins_are_read_only(client):
    r = upload(client, data=b"<svg onload=alert(1)>", media_type="image/svg+xml")
    assert r.status_code == 415
    assert client.delete("/api/media/sample-clip").status_code == 409
    assert client.get("/static/welcome/index.html").status_code == 200


def test_video_is_served_with_ranges(client):
    item = upload(client, "Clip", b"\x00" * 5000, "video/mp4").json()
    r = client.get(item["url"], headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100


def test_slideshow_announcement_and_builtin_media(client):
    photo = upload(client).json()
    r = client.post(
        "/api/pages",
        json={
            "name": "Lobby",
            "slides": [{"media": photo["id"], "seconds": 8}, {"media": "sample-clip"}],
            "fit": "contain",
        },
    )
    assert r.status_code == 200 and r.json()["kind"] == "slides"
    page = client.get("/api/pages/lobby").json()
    assert page["media"][photo["id"]]["url"] == photo["url"]
    assert (
        client.post("/api/pages", json={"name": "X", "slides": [{"media": "nope-x"}]}).status_code
        == 400
    )
    assert client.get("/api/media").json()["items"][0]["used_by"] == ["Lobby"]

    a = client.post(
        "/api/pages",
        json={
            "name": "Flights",
            "announcement": {
                "title": "Offsite <flights>",
                "body": "Book by Friday",
                "tag": "People",
            },
        },
    ).json()
    assert a["kind"] == "announcement" and a["announcement"]["style"] == "dark"
    html = client.get("/pages/flights/").text
    assert "Offsite &lt;flights&gt;" in html and "logo-white.svg" in html

    ids = [p["id"] for p in client.get("/api/pages").json()]
    assert ids[0] == "welcome"
    assert client.delete("/api/pages/welcome").status_code == 409
    assert client.put("/api/pages/welcome", json={"name": "x", "url": "/"}).status_code == 409
    r = client.post(
        "/api/pages",
        json={
            "name": "Loop",
            "playlist": [
                {"page": "welcome", "seconds": 120},
                {"page": "lobby", "seconds": 30},
            ],
        },
    )
    assert r.status_code == 200


def test_share_badge_setting(client):
    r = client.put("/api/devices/firetv-1/share", json={"badge": True}).json()
    assert r["badge"] is True and r["enabled"]
    beat = client.post("/api/devices/heartbeat", json={"id": "firetv-1", "role": "tv"}).json()
    assert beat.get("share_badge") is True or hub.devices["firetv-1"]["share_badge"] is True


def mcp(client, method, params=None, id_=1):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    }
    if getattr(client, "mcp_session", None):
        headers["mcp-session-id"] = client.mcp_session
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id_ is not None:
        body["id"] = id_
    r = client.post("/mcp", json=body, headers=headers, follow_redirects=False)
    assert r.status_code in (200, 202), r.text
    if "mcp-session-id" in r.headers:
        client.mcp_session = r.headers["mcp-session-id"]
    if id_ is None:
        return None
    text = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Bearer "])
def test_mcp_needs_the_token(client, auth):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    r = client.post("/mcp", json=body, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.json()["error"]["code"] == -32001
    # The rest of the app (kiosks, Admin) is unaffected.
    assert client.get("/api/health").status_code == 200


def test_mcp_lists_and_calls_tools(client):
    init = mcp(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert init["result"]["serverInfo"]["name"] == "xos"
    mcp(client, "notifications/initialized", id_=None)
    tools = {t["name"] for t in mcp(client, "tools/list", id_=2)["result"]["tools"]}
    assert {"list_screens", "show_on_screen", "create_slideshow", "take_screenshot"} <= tools

    call = mcp(
        client,
        "tools/call",
        {"name": "show_on_screen", "arguments": {"screen": "Atrium room", "what": "welcome"}},
        id_=3,
    )
    assert not call["result"].get("isError"), call
    assert hub.commands["firetv-1"][0]["args"] == {"route": "page/welcome"}

    call = mcp(
        client,
        "tools/call",
        {"name": "create_announcement", "arguments": {"title": "Hello"}},
        id_=4,
    )
    assert pages.get("hello")["kind"] == "announcement"
    bad = mcp(
        client,
        "tools/call",
        {"name": "show_on_screen", "arguments": {"screen": "nowhere", "what": "wall"}},
        id_=5,
    )
    assert bad["result"]["isError"]
