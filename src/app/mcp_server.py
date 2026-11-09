"""Xos over MCP (/mcp): the same screens, content, sharing, rooms and Zoom controls as Admin.

Tools call the same hub, pages, media, rooms and Zoom objects the REST routes use (app.main),
so an agent and a person in Admin always see the same thing. A caller sends the app's token
(XOS_MCP_TOKEN: full Admin) or their Google identity token; then each tool needs the same
permission its Admin button does (TOOL_PERMS, roles in src/app/access.py). See README.md.
"""

from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import re
import time
from typing import Any, Literal

from forwarded_auth import ConfigurationError, InvalidIdentityToken, user_from_headers
from forwarded_auth.mcp import build_transport_security
from fastapi import HTTPException
from mcp.server.mcpserver import Image, MCPServer
from mcp.shared.exceptions import MCPError
from starlette.types import ASGIApp, Receive, Scope, Send

from .access import APP_TOKEN, Caller, allowed_email

INSTRUCTIONS = """Xos runs office screens: Fire TV room TVs, lobby signage, iPad remotes.

Start with list_screens. A screen can be named by its id, its name ("Atrium room"), or a room id
(the room's TV). Put things on a screen with show_on_screen: a page id from list_content, a
media id (one photo or video full-screen), or a view such as "signage/hq" or "tv/atrium".
Make new content with create_slideshow (photos/videos from list_content or add_media_from_url),
create_announcement, create_link_page or create_playlist, then show it. take_screenshot shows
what a Fire TV is displaying right now. Never change many screens at once unless asked."""

ROUTE = re.compile(r"^[a-z]+(/[a-z0-9-]+){0,2}$")
DISPLAY_ROLES = {"page", "tv", "signage", "wall", "feed", "setup", "media", ""}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _m():
    from app import main  # the app module; imported late because it builds this server

    return main


def _fail(exc: HTTPException) -> ValueError:
    return ValueError(str(exc.detail))


def _screen_id(screen: str) -> str:
    """Device id for an id, a name, a name's slug, or a room id (that room's first display)."""
    hub = _m().hub
    if screen in hub.devices:
        return screen
    want = _slug(screen)
    devices = sorted(hub.devices.values(), key=lambda d: d.get("last_seen", 0), reverse=True)
    for d in devices:
        if want and want in (_slug(d.get("name", "")), _slug(d.get("label", ""))):
            return d["id"]
    for d in devices:
        if d.get("room") == want and d.get("role", "") in DISPLAY_ROLES:
            return d["id"]
    raise ValueError(f"no screen called {screen!r}; use list_screens")


def _summary(d: dict) -> dict:
    m = _m()
    online = hub_online(d)
    return {
        "id": d["id"], "name": d.get("name") or d.get("label") or d["id"],
        "room": d.get("room", ""), "online": online,
        "showing": "/".join(x for x in (d.get("role"), d.get("arg")) if x) or "",
        "app": d.get("client", ""), "model": (d.get("native") or {}).get("model", ""),
        "display_on": (d.get("native") or {}).get("displayOn"),
        "share_link": f"{m.settings.XOS_PUBLIC_URL}/s/{d.get('share_code', '')}",
        "sharing_enabled": not d.get("share_off"), "share_badge": bool(d.get("share_badge")),
        "being_shared": m.hub.shares.get(d["id"]),
    }


def hub_online(d: dict) -> bool:
    m = _m()
    return m.hub.connected(d["id"]) or time.time() - d["last_seen"] < m.settings.DEVICE_ONLINE_S


# Who is calling the tool running now (set by CheckToolPermission for each tools/call). Unset
# means no verified caller, and every tool that acts on a screen or room refuses.
current_caller: contextvars.ContextVar[Caller | None] = contextvars.ContextVar(
    "xos_mcp_caller", default=None
)


def _caller() -> Caller:
    caller = current_caller.get()
    if caller is None:
        raise ValueError("no verified MCP caller")
    return caller


def _may_command(device_id: str, kind: str) -> None:
    """The same rule as the REST command route: holds, sharing and roles (app.main.may_command)."""
    try:
        _m().may_command(_caller(), device_id, kind)
    except HTTPException as exc:
        raise _fail(exc) from exc


def _command(device_id: str, kind: str, args: dict | None = None) -> dict:
    _may_command(device_id, kind)
    queued = _m().command_device(device_id, kind, args or {})
    return {"screen": device_id, "command": kind, "status": queued["status"]}


# ---------- screens ----------


def list_screens(include_offline: bool = True) -> list[dict]:
    """Every Xos screen: id, name, room, online, what it shows, its share link and settings."""
    hub = _m().hub
    rows = [_summary(d) for d in hub.devices.values()]
    return [r for r in rows if include_offline or r["online"]]


def show_on_screen(screen: str, what: str) -> dict:
    """Put something on a screen.

    `what` is a page id (from list_content), a media id (one photo or video full-screen), or a
    view route: "signage/<site>" (lobby signage, plays the welcome page), "tv/<room>" (room TV),
    "panel/<room>", "wall", "camera", "remote/<screen-name>".
    """
    m = _m()
    device_id = _screen_id(screen)
    if m.pages.get(what):
        route = f"page/{what}"
    elif m.media.get(what):
        route = f"media/{what}"
    elif ROUTE.match(what):
        route = what
    else:
        raise ValueError("give a page id, a media id, or a view such as signage/hq")
    return {**_command(device_id, "route", {"route": route}), "route": route}


def show_message(screen: str, text: str, seconds: int = 15) -> dict:
    """Show a short message over whatever a screen displays, for a few seconds."""
    text = text.strip()[:200]
    if not text:
        raise ValueError("the message is empty")
    seconds = max(3, min(int(seconds), 600))
    return _command(_screen_id(screen), "message", {"text": text, "seconds": seconds})


def reload_screen(screen: str) -> dict:
    """Reload a screen's page (e.g. after an Xos update)."""
    return _command(_screen_id(screen), "reload")


def identify_screen(screen: str) -> dict:
    """Flash a screen's name on it, to find which physical TV it is."""
    return _command(_screen_id(screen), "identify")


def set_display(screen: str, on: bool) -> dict:
    """Turn a Fire TV's display on or off (needs the Xos TV app and provisioning)."""
    return _command(_screen_id(screen), "display_on" if on else "display_off")


async def switch_tv_to_xos(screen: str) -> dict:
    """Switch a room TV back to its Xos input after someone changed HDMI input: Display off, then
    Display on, so HDMI-CEC switches the TV to the stick. Needs Xos TV 0.6.0+ with display
    control. Takes about 10 s; list_screens shows the TV on again when it's done."""
    m = _m()
    device_id = _screen_id(screen)
    try:
        started = m.switch_input(device_id)
    except HTTPException as exc:
        raise _fail(exc) from exc
    return {"screen": device_id, "command": "switch_input", "status": "running",
            "id": started["id"]}


def step_page(
    screen: str, action: Literal["next", "previous", "scroll_up", "scroll_down", "top"]
) -> dict:
    """Next / previous slide (slideshows, playlists, decks) or scroll a page on a screen."""
    device_id = _screen_id(screen)
    if action in ("next", "previous"):
        key = "ArrowRight" if action == "next" else "ArrowLeft"
        return _command(device_id, "page_key", {"key": key})
    direction = {"scroll_up": "up", "scroll_down": "down", "top": "top"}[action]
    return _command(device_id, "page_scroll", {"dir": direction})


def update_screen(screen: str, name: str | None = None, room: str | None = None) -> dict:
    """Rename a screen and/or put it in a room (room id from list_rooms, "" for none)."""
    m = _m()
    device_id = _screen_id(screen)
    if room and room not in {r["id"] for r in m.rooms.all()}:
        raise ValueError(f"no room {room!r}; use list_rooms")
    if name is not None:
        m.hub.rename(device_id, name.strip()[:60])
    if room is not None:
        m.hub.set_room(device_id, room)
    return _summary(m.hub.devices[device_id])


async def take_screenshot(screen: str) -> Image:
    """A picture of what a screen is showing now (Fire TV with the Xos TV app)."""
    m = _m()
    device_id = _screen_id(screen)
    _may_command(device_id, "screenshot")
    before = (m.hub.devices[device_id].get("screenshot") or {}).get("at", 0)
    m.hub.queue_command(device_id, "screenshot", {})
    for _ in range(40):
        await asyncio.sleep(0.5)
        shot = m.hub.devices[device_id].get("screenshot") or {}
        if shot.get("at", 0) > before and m.hub.screenshot_path(device_id).exists():
            fmt = "png" if "png" in shot.get("type", "") else "jpeg"
            return Image(data=m.hub.screenshot_path(device_id).read_bytes(), format=fmt)
    raise ValueError("the screen didn't send a screenshot in 20 s (offline, or not the TV app)")


# ---------- content: pages and media ----------


def list_content() -> dict:
    """Pages (ids for show_on_screen and playlists) and media (ids for slideshows)."""
    m = _m()
    keep = ("id", "name", "kind", "url", "note", "builtin", "items", "fit")
    return {
        "pages": [{k: p[k] for k in keep if k in p} for p in m.pages.all()],
        "media": [{k: x[k] for k in ("id", "name", "kind", "type", "bytes", "url", "builtin")
                   if k in x} for x in m.media.all()],
        **m.media.usage(),
    }


async def add_media_from_url(url: str, name: str = "") -> dict:
    """Download a photo (JPEG, PNG, GIF, WebP) or video (MP4, WebM, MOV) into the library."""
    from app.media import MediaError

    try:
        return await _m().media.save_from_url(url.strip(), name.strip())
    except MediaError as exc:
        raise ValueError(str(exc)) from exc


def _save_page(page_id: str | None, name: str, **kw: Any) -> dict:
    try:
        return _m().pages.upsert(page_id, name.strip()[:80] or "Untitled", **kw)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - shown to the agent as a tool error
        raise ValueError(str(exc)) from exc


def create_slideshow(
    name: str, media_ids: list[str], seconds_per_photo: int = 10,
    fit: Literal["cover", "contain"] = "cover", page_id: str | None = None,
) -> dict:
    """A slideshow page of photos and videos (videos play to the end). Pass page_id to update."""
    seconds = max(2, min(int(seconds_per_photo), 3600))
    slides = [{"media": i, "seconds": seconds} for i in media_ids][:200]
    return _save_page(page_id, name, slides=slides, fit=fit)


def create_announcement(
    title: str, body: str = "", tag: str = "", name: str = "",
    style: Literal["dark", "gradient", "light"] = "dark", page_id: str | None = None,
) -> dict:
    """An Xos-styled announcement page: optional tag pill, headline and message."""
    a = {"title": title[:120], "body": body[:600], "tag": tag[:30], "style": style}
    return _save_page(page_id, name or title, announcement=a)


def create_link_page(name: str, url: str, page_id: str | None = None) -> dict:
    """A page that shows another website (some sites refuse to be shown in a frame)."""
    return _save_page(page_id, name, url=url.strip())


def create_playlist(name: str, items: list[dict], page_id: str | None = None) -> dict:
    """A playlist rotating pages: items like [{"page": "welcome", "seconds": 120}]."""
    clean = [{"page": str(i["page"]), "seconds": max(5, min(int(i.get("seconds", 20)), 3600))}
             for i in items][:50]
    return _save_page(page_id, name, playlist=clean)


def delete_content(id: str) -> dict:  # noqa: A002 - the tool argument agents see
    """Delete a page or an uploaded photo/video by id. Built-ins can't be deleted."""
    m = _m()
    if m.pages.items.get(id):
        m.pages.delete(id)
        return {"deleted": id, "type": "page"}
    if m.media.delete(id):
        return {"deleted": id, "type": "media"}
    raise ValueError("built-ins can't be deleted" if m.pages.get(id) or m.media.get(id)
                     else f"nothing called {id!r}")


# ---------- sharing ----------


def share_link_to_screen(screen: str, url: str, shared_by: str = "") -> dict:
    """Show a web link on a screen right away, over what it shows, until stop_sharing."""
    m = _m()
    if not re.match(r"^https?://\S+$", url.strip()):
        raise ValueError("give a link starting with http:// or https://")
    device_id = _screen_id(screen)
    _may_command(device_id, "route")  # like showing something: holds and display screens apply
    try:
        return m._start_share(device_id,
                              {"kind": "url", "url": url.strip(), "from": shared_by[:60]})
    except HTTPException as exc:
        raise _fail(exc) from exc


def stop_sharing(screen: str) -> dict:
    """Stop whatever is being shared on a screen."""
    m = _m()
    device_id = _screen_id(screen)
    _may_command(device_id, "route")
    current = m.hub.shares.pop(device_id, None)
    if current:
        m.hub.queue_command(device_id, "share_stop", {"session": current["session"]})
    return {"stopped": bool(current)}


def set_screen_sharing(
    screen: str, enabled: bool | None = None, badge: bool | None = None, new_link: bool = False
) -> dict:
    """Turn a screen's share link on/off, show its link + QR in the corner (badge), or replace
    the link (the old one stops working)."""
    m = _m()
    device_id = _screen_id(screen)
    if enabled is False or new_link:
        stop_sharing(device_id)
    m.hub.set_sharing(device_id, enabled, new_link, badge)
    _command(device_id, "refresh")
    return _summary(m.hub.devices[device_id])


# ---------- rooms and Zoom ----------


def list_rooms() -> list[dict]:
    """Rooms and the screens in each."""
    m = _m()
    return [{**r, "screens": [d.get("name") or d["id"] for d in m.hub.devices.values()
                              if d.get("room") == r["id"]],
             "zoom_room": m.zoom.rooms.get(r["id"], "")} for r in m.rooms.all()]


def save_room(name: str, site: str, room_id: str | None = None) -> dict:
    """Add a room (or rename / move one with room_id). Sites: see the site ids in list_rooms."""
    return _m().rooms.upsert(room_id, name.strip()[:60], site)


async def zoom_status() -> dict:
    """Whether Zoom is connected, and which Xos rooms are linked to Zoom Rooms."""
    m = _m()
    return await m.zoom_status()


async def zoom_control(
    room: str,
    action: Literal["join", "meet_now", "leave", "end", "mute", "unmute", "camera_off", "camera_on",
                    "share_start", "share_stop", "volume", "restart"],
    meeting: str = "", passcode: str = "", volume: int | None = None,
) -> dict:
    """Control the Zoom Room linked to an Xos room. join takes `meeting`: a Zoom meeting ID or link,
    or a Google Meet code or link. meet_now starts an instant meeting. volume is 0-100."""
    m = _m()
    try:
        # check_tool_permission already required screens.operate for this tool.
        return await m.room_zoom_control(
            m.ZoomControl(action=action, meeting=meeting, passcode=passcode, volume=volume), room,
            _caller())
    except HTTPException as exc:
        raise _fail(exc) from exc


READ_ONLY = (list_screens, list_content, list_rooms, zoom_status, take_screenshot)
TOOLS = (
    *READ_ONLY, show_on_screen, show_message, reload_screen, identify_screen, set_display,
    switch_tv_to_xos, step_page, update_screen, add_media_from_url, create_slideshow, create_announcement,
    create_link_page, create_playlist, delete_content, share_link_to_screen, stop_sharing,
    set_screen_sharing, save_room, zoom_control,
)


# The permission each tool needs: the same one its button in Admin needs (src/app/access.py).
TOOL_PERMS = {
    "list_screens": "admin.view", "list_content": "admin.view", "list_rooms": "admin.view",
    "zoom_status": "admin.view",
    "take_screenshot": "screens.show", "show_on_screen": "screens.show",
    "show_message": "screens.show", "reload_screen": "screens.show",
    "identify_screen": "screens.show", "step_page": "screens.show",
    "share_link_to_screen": "screens.show", "stop_sharing": "screens.show",
    "set_display": "screens.operate", "switch_tv_to_xos": "screens.operate",
    "zoom_control": "screens.operate",
    "update_screen": "setup", "set_screen_sharing": "setup", "save_room": "setup",
    "add_media_from_url": "content.edit", "create_slideshow": "content.edit",
    "create_announcement": "content.edit", "create_link_page": "content.edit",
    "create_playlist": "content.edit", "delete_content": "content.edit",
}
# Tools that change nothing, so they still work while an Admin previews another perspective.
PREVIEW_TOOLS = {"list_screens", "list_content", "list_rooms", "zoom_status"}


def authorized(headers: dict[str, str]) -> Caller | None:
    """Who may use /mcp: the app's token (XOS_MCP_TOKEN, full Admin), or a signed-in person whose
    role opens Xos Admin (src/app/access.py). Each tool then checks its own permission.

    Put /mcp behind the proxy so it only lets a request with an Authorization header through; the
    app checks the token itself either way. Kiosks never call /mcp, so this gates nothing else.
    """
    auth = headers.get("authorization", "")
    token = _m().settings.XOS_MCP_TOKEN.get_secret_value()
    if token and auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token):
        return APP_TOKEN
    try:
        user = user_from_headers(headers)
    except (InvalidIdentityToken, ConfigurationError):
        return None  # fail closed: an unverifiable token is no one
    if user is None or user.is_bot or not allowed_email(user.email.lower()):
        return None
    email = user.email.lower()
    caller = _m().people.caller(email, getattr(user, "name", "") or email)
    return caller if caller.can("admin.view") else None


def tool_refusal(caller: Caller | None, tool: str) -> str:
    """Why `caller` may not call `tool`, or "" if they may."""
    if caller is None:
        return "Xos MCP needs Authorization: Bearer <XOS_MCP_TOKEN> or a verified sign-in"
    if caller.preview and tool not in PREVIEW_TOOLS:
        return "Preview is read-only. Exit preview in Xos Admin to make changes."
    perm = TOOL_PERMS.get(tool, "people")  # a tool missing from the table is Admin-only
    if not caller.can(perm):
        return f"Your Xos role ({caller.role_name}) can't use {tool}"
    return ""


class CheckToolPermission:
    """MCP middleware: each tools/call needs the tool's permission; the tool then runs as that
    caller (current_caller), so command holds and Zoom follow the same rules as the REST API."""

    async def __call__(self, ctx, call_next):
        if ctx.method != "tools/call":
            return await call_next(ctx)
        raw = getattr(ctx.request, "headers", None) or {}
        caller = authorized({k.lower(): v for k, v in raw.items()})
        problem = tool_refusal(caller, str((ctx.params or {}).get("name", "")))
        if problem:
            raise MCPError(-32001, problem)
        token = current_caller.set(caller)
        try:
            return await call_next(ctx)
        finally:
            current_caller.reset(token)


class RequireCaller:
    """ASGI middleware: answer an MCP-shaped 401 unless `authorized`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/mcp" or path.startswith("/mcp/")):
            return await self.app(scope, receive, send)  # other paths still 404 below
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if authorized(headers):
            return await self.app(scope, receive, send)
        body = json.dumps({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32001, "message": "Xos MCP needs Authorization: Bearer <XOS_MCP_TOKEN> (README.md)"},
        }).encode()
        await send({
            "type": "http.response.start", "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="xos"'),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def build_mcp_app() -> ASGIApp:
    """A fresh Streamable HTTP app (own session manager) serving the exact path /mcp."""
    mcp = MCPServer(name="xos", instructions=INSTRUCTIONS, middleware=[CheckToolPermission()])
    for tool in TOOLS:
        mcp.tool()(tool)
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp", transport_security=build_transport_security()
    )
    app.add_middleware(RequireCaller)
    return app
