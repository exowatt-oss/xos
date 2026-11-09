"""Xos server.

Serves the admin and kiosk web app (app/) and is the hub every Xos screen talks to: Fire TV room
TVs, iPad remotes / door panels / signage, browsers, and Linux nodes. Screens register with a
heartbeat and receive commands over Server-Sent Events.
"""

import asyncio
import base64
import dataclasses
import gzip
import hashlib
import io
import json
import logging
import re
import secrets
import sys
import tarfile
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

import httpx
import segno
from forwarded_auth import InvalidIdentityToken
from fastapi import Depends, FastAPI, HTTPException, Path, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app import access, catalog
from app import hub as hub_module
from app.access import Caller
from app.appliances import Appliances, Boxes, SshKeys, WifiNetworks, preseed, ssh_keys_in
from app.calendar_auth import CalendarAuth
from app.calendars import CalendarError, Calendars
from app.config import REPO_ROOT, settings
from app.demos import Demos, build_router as build_demo_router
from app.hub import Hub
from app.media import Media, MediaError, type_for
from app.meeting_links import PROVIDERS, meeting_link
from app.pages import Pages
from app.people import People, RoleError
from app.provision import Provisioner, ProvisionError
from app.rooms import Rooms
from app.whiteboard import Whiteboards
from app.whiteboard_exports import WhiteboardExports
from app.zoom import Zoom, ZoomError

VERSION = "0.2.0"
STARTED = time.time()
KEEPALIVE_S = 15
MAX_SCREENSHOT_BYTES = 3_000_000
DEVICE_ID = Path(pattern=r"^[A-Za-z0-9._-]{4,64}$")

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(
    logging.Formatter(
        '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
    )
)
logging.basicConfig(level=settings.LOG_LEVEL.upper(), handlers=[handler])
# httpx logs every request URL at INFO, query string included (calendar and directory queries
# can hold names and emails). Keep only its warnings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

hub = Hub(settings.XOS_DATA_DIR)
provisioner = Provisioner(
    apk=REPO_ROOT / "firetv" / "xos-tv.apk",
    key_path=settings.XOS_ADB_KEY or settings.XOS_DATA_DIR / "adb" / "adbkey",
    server=settings.XOS_PUBLIC_URL,
)
media = Media(settings.XOS_DATA_DIR, settings.XOS_MEDIA_MAX_MB * 1_000_000)
demos = Demos(settings.XOS_DATA_DIR)
pages = Pages(settings.XOS_DATA_DIR, media_exists=lambda media_id: media.get(media_id) is not None)
zoom = Zoom(settings, settings.XOS_DATA_DIR)
calendar_auth = CalendarAuth(settings)
calendars = Calendars(settings, auth=calendar_auth)
# A fresh rooms list keeps every room already in use by a screen or a Zoom link.
appliances = Appliances(settings.XOS_DATA_DIR)
boxes = Boxes(settings.XOS_DATA_DIR)
ssh_keys = SshKeys(settings.XOS_DATA_DIR)
wifi = WifiNetworks(settings.XOS_DATA_DIR)
whiteboards = Whiteboards(settings.XOS_DATA_DIR)
whiteboard_exports = WhiteboardExports(settings.XOS_DATA_DIR)
people = People(settings.XOS_DATA_DIR)
rooms = Rooms(
    settings.XOS_DATA_DIR,
    {d["room"] for d in hub.devices.values() if d.get("room")} | set(zoom.rooms),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Xos server %s starting (environment=%s)", VERSION, settings.ENVIRONMENT)
    # The mounted MCP app doesn't get this lifespan by itself; its session manager needs it.
    async with mcp_app.router.lifespan_context(mcp_app):
        yield


app = FastAPI(
    title="Xos",
    description="Screens, rooms and signage for offices.",
    version=VERSION,
    lifespan=lifespan,
    # Under /api so it's reachable like the rest of the API (screens never sign in).
    openapi_url="/api/openapi.json",
)
app.add_middleware(access.UnderUser)


def who(request: Request) -> Caller:
    caller = access.resolve(request, hub.check_key, people)
    # A device key makes a caller a trusted *remote* only for tablets and remotes. An appliance's
    # key proves it is that box (appliance_guard) and grants nothing else.
    if caller.device and hub.devices.get(caller.device, {}).get("role") not in CONTROLLER_ROLES:
        caller = dataclasses.replace(caller, device="")
    return caller


def requires(perm: str):
    """A route dependency: the caller's role must include `perm` (src/app/access.py)."""

    def check(caller: Caller = Depends(who)) -> Caller:
        access.need(caller, perm)
        return caller

    return Depends(check)


SAFE_METHODS = access.SAFE_METHODS


@app.middleware("http")
async def refuse_writes_in_preview(request: Request, call_next):
    """While an Admin previews Xos as a role or a person, every write they send is refused here,
    whatever the page asks for. Only Exit preview (DELETE /api/preview) gets through."""
    path = request.url.path.removeprefix(access.SSO_PREFIX)
    if (people.previews and request.method not in SAFE_METHODS and path.startswith("/api/")
            and not (path == "/api/preview" and request.method == "DELETE")):
        email, _ = access.identify(request)
        if email and people.preview_of(email):
            return JSONResponse(
                {"detail": "Preview is read-only. Exit preview to make changes."}, status_code=403
            )
    return await call_next(request)


@app.middleware("http")
async def check_bearer_and_bots(request: Request, call_next):
    """A Bearer on /api must verify (a person's sign-in token or an agent's gateway token), or the
    request gets 401: it never falls back to identity headers. A verified agent may only read
    the routes in access.BOT_READS; every other request it sends, including any write, is 403.
    /mcp checks its own token (src/app/mcp_server.py) and turns bots away there."""
    path = request.url.path.removeprefix(access.SSO_PREFIX)
    if request.headers.get("authorization") and not (path == "/mcp" or path.startswith("/mcp/")):
        try:
            user = access.platform_user(request)
        except InvalidIdentityToken as exc:
            if not path.startswith("/api/"):
                return await call_next(request)  # elsewhere, an unverified Bearer is no one
            logger.info("Rejected a Bearer on %s %s: %s", request.method, path, exc.reason)
            return JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="xos"'},
            )
        if user is not None and user.is_bot:
            bot = access.bot_caller(user)
            allowed = access.bot_may(bot, request.method, path)
            logger.info(
                "Agent %s (for %s, %s) %s %s: %s (gateway request %s)",
                bot.bot,
                bot.bot_owner,
                bot.bot_scope or "no scope",
                request.method,
                path,
                "allowed" if allowed else "refused",
                request.headers.get("x-request-id", "-"),
            )
            if not allowed:
                return JSONResponse({"detail": access.BOT_REFUSAL}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    # Kiosks run for weeks: make them revalidate files so a deploy reaches every screen on reload.
    response = await call_next(request)
    if request.url.path.startswith("/media/") and response.status_code in (200, 206):
        # Uploads get a new name for every file, so a screen can keep each one for good instead
        # of re-checking it on every play and seek.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


# ---------- health and catalog ----------


@app.get("/api/time")
async def server_time():
    """The server's clock in ms, so screens showing the same page can play it in step."""
    return {"now": time.time() * 1000}


@app.get("/api/health")
async def health():
    """Liveness probe used by the platform deploy workflow; also how clients detect a hub."""
    return {
        "status": "ok",
        "ok": True,
        "version": VERSION,
        "role": "server",
        "uptime_s": round(time.time() - STARTED),
    }


@app.get("/api/config")
async def config():
    """Rooms, sites and roles for device setup screens (iOS app, Fire TV menu)."""
    return {
        "version": VERSION,
        "rooms": rooms.all(),
        "sites": catalog.SITES,
        "roles": catalog.ROLES,
    }


# ---------- rooms (Admin → Rooms) ----------


class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    site: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    # The room's Google resource calendar (c_…@resource.calendar.google.com); "" = the default.
    calendar: str | None = Field(default=None, max_length=120)


@app.get("/api/rooms")
async def list_rooms():
    return rooms.all()


@app.post("/api/rooms", dependencies=[requires("setup")])
async def create_room(room: RoomIn):
    try:
        return rooms.upsert(None, room.name, room.site, room.calendar)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/rooms/{room_id}", dependencies=[requires("setup")])
async def update_room(room: RoomIn, room_id: str):
    if room_id not in rooms.items:
        raise HTTPException(404, "unknown room")
    try:
        return rooms.upsert(room_id, room.name, room.site, room.calendar)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/rooms/{room_id}", dependencies=[requires("setup")])
async def delete_room(room_id: str):
    """Remove a room. Its screens keep running and simply have no room until one is set."""
    if not rooms.delete(room_id):
        raise HTTPException(404, "unknown room")
    zoom.map_room(room_id, None)
    for device_id, record in list(hub.devices.items()):
        if record.get("room") == room_id:
            hub.set_room(device_id, "")
    return {"ok": True}


@app.get("/api/rooms/{room}/meeting")
async def current_room_meeting(room: str, device: str = "", caller: Caller = Depends(who)):
    if not (caller.can("screens.operate") or caller.device):
        raise HTTPException(403, "Use the room controller or sign in to view its invitation")
    candidates = [
        d for d in hub.devices.values()
        if d.get("role") == "roompc" and (d.get("room") or d.get("arg")) == room
    ]
    if device:
        candidates = [d for d in candidates if d["id"] == device]
    candidates = [
        d for d in candidates
        if time.time() - d.get("last_seen", 0) <= settings.DEVICE_ONLINE_S
    ]
    candidates.sort(key=lambda d: (
        bool(((d.get("native") or {}).get("meeting") or {}).get("error")),
        not d.get("client", "").startswith("xosroom-linux"),
        d.get("name") or d["id"],
    ))
    pc = next(iter(candidates), None)
    state = ((pc or {}).get("native") or {}).get("meeting") or {}
    if not isinstance(state, dict):
        return {"url": "", "qr": ""}
    if not state.get("in_call") or not state.get("join_url"):
        return {"url": "", "qr": ""}
    try:
        link = meeting_link(state["join_url"])
    except ValueError:
        return {"url": "", "qr": ""}
    svg = io.BytesIO()
    segno.make(link["url"], error="m").save(
        svg, kind="svg", scale=8, border=4, light="#fff", xmldecl=False
    )
    return {"url": link["url"], "provider": link["provider"],
            "qr": "data:image/svg+xml;base64," + base64.b64encode(svg.getvalue()).decode()}


# ---------- room calendars (Google resource calendars; src/app/calendars.py) ----------


@app.get("/api/calendar/status")
async def calendar_status():
    return {
        "configured": calendars.configured,
        "user": "Connected Google account" if calendar_auth.connected else settings.XOS_CALENDAR_USER,
        "oauth_available": calendar_auth.available,
        "oauth_connected": calendar_auth.connected,
        "rooms": {r["id"]: rooms.calendar(r["id"]) for r in rooms.all()},
    }


@app.get("/api/calendar/oauth/start")
async def calendar_oauth_start(caller: Caller = requires("integrations")):
    if caller.preview:  # a GET, but it connects an account: no changes while previewing
        raise HTTPException(403, "Preview is read-only. Exit preview to make changes.")
    try:
        state, url = calendar_auth.start(by=caller.email)
    except CalendarError as exc:
        raise HTTPException(409, str(exc)) from exc
    response = RedirectResponse(url, status_code=303)
    response.set_cookie("xos_calendar_oauth", state, max_age=600, httponly=True,
                        secure=True, samesite="lax", path="/api/calendar/oauth/callback")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/calendar/oauth/callback")
async def calendar_oauth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    pending = calendar_auth.consume(state, request.cookies.get("xos_calendar_oauth", ""))
    if not pending:
        raise HTTPException(400, "Google connection expired. Start again from Integrations.")
    if error or not code:
        raise HTTPException(400, "Google Calendar access was not granted. Try again from Integrations.")
    try:
        account = await calendar_auth.finish(code, by=pending["by"])
    except (CalendarError, httpx.HTTPError) as exc:
        raise HTTPException(400, "Google Calendar connection failed. Reconnect from Integrations.") from exc
    people.record(pending["by"] or "unknown", "calendar.connect", account or "unknown account")
    calendars._cache.clear()
    response = RedirectResponse("/app/#/admin/integrations", status_code=303)
    response.delete_cookie("xos_calendar_oauth", path="/api/calendar/oauth/callback")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/rooms/{room}/calendar")
async def room_calendar(room: str, response: Response, caller: Caller = Depends(who)):
    """What's booked in the room today: now and next. The join links go only to people who can
    operate screens and the room's trusted remotes (its iPad); a door panel or TV gets titles and
    times."""
    response.headers["Cache-Control"] = "no-store"
    calendar_id = rooms.calendar(room)
    out = {
        "room": room,
        "configured": calendars.configured,
        "calendar": calendar_id,
        "bookings": [],
        "error": "",
        "now": time.time(),
        "refreshed_at": calendars.refreshed_at.get(calendar_id),
        "display": rooms.display(room),
    }
    if not (calendar_id and calendars.configured):
        return out
    try:
        bookings = await calendars.bookings(calendar_id)
    except CalendarError as exc:
        out["error"] = str(exc)
        return out
    out["refreshed_at"] = calendars.refreshed_at.get(calendar_id)
    trusted = caller.can("screens.operate") or caller.device
    out["bookings"] = [b if trusted else {**b, "join_url": ""} for b in bookings[:8]]
    return out


# ---------- Zoom (Admin → Integrations, room remotes) ----------


@app.get("/api/zoom/status")
async def zoom_status():
    status = {
        "configured": zoom.configured,
        "connected": False,
        "rooms": zoom.rooms,
        "error": "",
        "zoom_rooms": None,
    }
    if zoom.configured:
        try:
            status["zoom_rooms"] = len(await zoom.list_rooms())
            status["connected"] = True
        except ZoomError as exc:
            status["error"] = str(exc)
    return status


# ---------- credentials: what each integration needs, set or not (never a value) ----------


def _is_set(key: str) -> bool:
    value = getattr(settings, key)
    return bool(value.get_secret_value() if hasattr(value, "get_secret_value") else value)


def _credential(key, name, secrets, status, detail, **extra) -> dict:
    return {"key": key, "name": name, "secrets": {k: _is_set(k) for k in secrets},
            "status": status, "detail": detail, **extra}


@app.get("/api/integrations/credentials", dependencies=[requires("integrations")])
async def integration_credentials():
    """Every app secret Xos reads, whether it's set and whether it works. `status` is ok,
    action (someone needs to do something), optional (Xos works without it) or unused (set, but
    another credential is read instead). Values never leave the server."""
    items = []
    calendar = calendar_auth.info
    service_account = _is_set("GOOGLE_SERVICE_ACCOUNT_JSON") and _is_set("XOS_CALENDAR_USER")
    if calendar_auth.connected:
        items.append(_credential(
            "google_calendar", "Google Calendar (Google Cloud OAuth client)",
            ["GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET"],
            "action" if calendar_auth.last_error else "ok",
            calendar_auth.last_error or "Reads room calendars as "
            + (calendar.get("account") or "the connected account (named after its next refresh)"),
            account=calendar.get("account", ""), connected_at=calendar.get("connected_at"),
            connected_by=calendar.get("connected_by", ""), last_ok=calendar_auth.last_ok or None))
    else:
        items.append(_credential(
            "google_calendar", "Google Calendar (Google Cloud OAuth client)",
            ["GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET"],
            "action" if calendar_auth.available or not service_account else "unused",
            "The OAuth client is set: connect the room calendar account in Google Calendar below."
            if calendar_auth.available else "Not set. Room calendars need it or a service account."))
    items.append(_credential(
        "google_service_account", "Google service account (domain-wide delegation)",
        ["GOOGLE_SERVICE_ACCOUNT_JSON", "XOS_CALENDAR_USER"],
        ("unused" if calendar_auth.connected else "ok") if service_account else "optional",
        ("Set, but the OAuth connection above is read instead. Safe to remove."
         if calendar_auth.connected else f"Reads room calendars as {settings.XOS_CALENDAR_USER}.")
        if service_account else "Not needed: the OAuth connection reads the calendars."))
    zoom_item = ["zoom", "Zoom (Server-to-Server OAuth)",
                 ["ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"]]
    if zoom.configured:
        try:
            count = len(await zoom.list_rooms())
            items.append(_credential(*zoom_item, "ok", f"Connected: {count} Zoom Rooms in the "
                                     f"account, {len(zoom.rooms)} linked to an Xos room."))
        except ZoomError as exc:
            items.append(_credential(*zoom_item, "action", str(exc)))
    else:
        items.append(_credential(*zoom_item, "optional", "Only for Zoom Rooms hardware. Room "
                                 "computers join Zoom as web-client guests without it."))
    items.append(_credential(
        "mcp", "AI agents (MCP) token", ["XOS_MCP_TOKEN"],
        "ok" if _is_set("XOS_MCP_TOKEN") else "optional",
        "Agents can call /mcp with the Xos token." if _is_set("XOS_MCP_TOKEN")
        else "Only signed-in people can use /mcp."))
    relay = any(u.strip().startswith(("turn:", "turns:")) for u in settings.XOS_ICE_SERVERS.split(","))
    relay_ok = relay and _is_set("XOS_TURN_USERNAME") and _is_set("XOS_TURN_PASSWORD")
    items.append(_credential(
        "turn", "Screen-sharing relay (TURN)",
        ["XOS_ICE_SERVERS", "XOS_TURN_USERNAME", "XOS_TURN_PASSWORD"],
        "ok" if relay_ok else "action",
        "Shares relay through TURN when a laptop and a screen are on different networks."
        if relay_ok else "A TURN URL is set without its username and password." if relay
        else "Only public STUN: a laptop and a screen on different office networks may not connect."))
    return {"items": items}


@app.get("/api/zoom/rooms", dependencies=[requires("integrations")])
async def zoom_rooms():
    try:
        return await zoom.list_rooms()
    except ZoomError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


class ZoomMap(BaseModel):
    room: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    zoom_room_id: str | None = Field(default=None, max_length=80)


@app.put("/api/zoom/map", dependencies=[requires("integrations")])
async def zoom_map(m: ZoomMap):
    return {"rooms": zoom.map_room(m.room, (m.zoom_room_id or "").strip() or None)}


class ZoomSetup(BaseModel):
    room: str = Field(pattern=r"^[a-z0-9-]{1,40}$")


@app.post("/api/zoom/setup", dependencies=[requires("integrations")])
async def zoom_setup(z: ZoomSetup):
    """Give an Xos room its own Zoom Room (or reuse its linked one) and return the activation
    code to type into the Zoom Rooms app on the room's PC."""
    try:
        zoom_room = zoom.rooms.get(z.room)
        created = False
        if not zoom_room:
            name = rooms.items.get(z.room, {}).get("name") or z.room
            zoom_room = (await zoom.create_room(name))["id"]
            if not zoom_room:
                raise ZoomError("Zoom didn't return the new room's ID", 502)
            zoom.map_room(z.room, zoom_room)
            created = True
        # A room whose PC isn't signed in (or is offline) may hold an expired code: get a new one.
        # A room that's up keeps its code; it doesn't need one.
        status = None if created else (await zoom.room_status(zoom_room) or {}).get("status")
        code = await zoom.activation_code(zoom_room, fresh=status not in ("Available", "InMeeting"))
    except ZoomError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"room": z.room, "zoom_room_id": zoom_room, "created": created, "activation_code": code}


@app.get("/api/rooms/{room}/zoom")
async def room_zoom_status(room: str):
    zoom_room = zoom.rooms.get(room)
    out = {
        "room": room,
        "mapped": bool(zoom_room),
        "configured": zoom.configured,
        "zoom_room_id": zoom_room,
        "instant_meetings": settings.XOS_INSTANT_MEETINGS,
        "status": None,
        "name": None,
        "error": "",
    }
    if zoom_room and zoom.configured:
        try:
            found = await zoom.room_status(zoom_room)
            if found:
                out.update(status=found["status"], name=found["name"])
            else:
                out["error"] = "That Zoom Room ID isn't in this Zoom account"
        except ZoomError as exc:
            out["error"] = str(exc)
    return out


class ZoomControl(BaseModel):
    action: Literal[
        "join",
        "meet_now",
        "leave",
        "end",
        "mute",
        "unmute",
        "camera_off",
        "camera_on",
        "share_start",
        "share_stop",
        "volume",
        "restart",
    ]
    # A Zoom meeting ID, a Zoom link or a Google Meet link/code. meeting_number is the older name.
    meeting: str = Field(default="", max_length=400)
    meeting_number: str = Field(default="", max_length=20)
    passcode: str = Field(default="", max_length=40)
    volume: int | None = Field(default=None, ge=0, le=100)


@app.post("/api/rooms/{room}/zoom")
async def room_zoom_control(c: ZoomControl, room: str, caller: Caller = Depends(who)):
    """Real in-meeting control for the Zoom Room mapped to this Xos room."""
    if not caller.device:
        access.need(caller, "screens.operate")
    if c.action in {"join", "meet_now"} and room in rooms.items and whiteboards.get(room)["active"]:
        raise HTTPException(409, "Exit the whiteboard before joining a meeting")
    zoom_room = zoom.rooms.get(room)
    entry = {
        "t": time.time(),
        "room": room,
        "action": c.action,
        "backend": "zoom-rest",
        "request": {"room": zoom_room, "action": c.action},
        "sent": False,
    }
    try:
        if not zoom_room:
            raise ZoomError(f"Room {room} isn't mapped to a Zoom Room (Admin → Integrations)", 409)
        result = await zoom.control(
            zoom_room, c.action, c.meeting or c.meeting_number, c.passcode, c.volume
        )
        entry.update(sent=True, live=result)
        return {"ok": True, **result}
    except ZoomError as exc:
        entry["live"] = {"error": str(exc)}
        raise HTTPException(exc.status, str(exc)) from exc
    finally:
        hub.log_zoom(entry)


@app.get("/api/zoom/log", dependencies=[requires("integrations")])
async def zoom_log():
    return hub.zoom_log


# ---------- room whiteboards ----------


BoardCoordinate = Annotated[float, Field(ge=-100_000, le=100_000, allow_inf_nan=False)]
# Any ink colour, with alpha for highlighter ink (the iPad app's PencilKit marker).
InkColor = Annotated[str, Field(pattern=r"^#[0-9a-f]{6}(?:[0-9a-f]{2})?$")]
InkWidth = Annotated[float, Field(ge=1, le=64, allow_inf_nan=False)]


class WhiteboardStroke(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    color: InkColor
    width: InkWidth
    points: list[tuple[BoardCoordinate, BoardCoordinate]] = Field(min_length=1, max_length=8192)


class WhiteboardOp(BaseModel):
    action: Literal["start", "stroke", "undo", "clear", "replace", "exit"]
    epoch: int = Field(ge=0)
    id: str = Field(default="", max_length=36, pattern=r"^(?:[a-f0-9-]{36})?$")
    color: InkColor = "#17212b"
    width: InkWidth = 7
    offset: int = Field(default=0, ge=0, le=8192)
    points: list[tuple[BoardCoordinate, BoardCoordinate]] = Field(default_factory=list, max_length=256)
    done: bool = False
    # replace: the whole drawing after an edit that isn't an added stroke (PencilKit's eraser,
    # lasso moves, or undoing an older stroke). Bounded like the board itself.
    strokes: list[WhiteboardStroke] = Field(default_factory=list, max_length=1000)


def whiteboard_room(room: str) -> None:
    if room not in rooms.items:
        raise HTTPException(404, "unknown room")


def whiteboard_controller(room: str, caller: Caller) -> None:
    if caller.can("screens.operate"):
        return
    remote = hub.devices.get(caller.device, {})
    if caller.device and (remote.get("room") or remote.get("arg")) == room:
        return
    raise HTTPException(403 if caller.person or caller.device else 401,
                        "Use this room's iPad or sign in as a room administrator")


class RoomDisplaySettings(BaseModel):
    idle_page: str = Field(max_length=64, pattern=r"^(?:[a-z0-9][a-z0-9-]*)?$")
    meeting_lead_minutes: int = Field(ge=0, le=120)


@app.put("/api/rooms/{room}/display")
async def room_display_settings(
    update: RoomDisplaySettings, room: str, caller: Caller = Depends(who)
):
    whiteboard_room(room)
    whiteboard_controller(room, caller)
    if update.idle_page and pages.get(update.idle_page) is None:
        raise HTTPException(400, "Choose an existing content page")
    return rooms.set_display(room, update.idle_page, update.meeting_lead_minutes)


def ensure_room_idle(room: str) -> None:
    for device in hub.devices.values():
        if device.get("role") == "roompc" and (device.get("room") or device.get("arg")) == room:
            meeting = (device.get("native") or {}).get("meeting") or {}
            if meeting.get("in_call") or meeting.get("waiting"):
                raise HTTPException(409, "Leave the current meeting before changing the room screen")


@app.post("/api/rooms/{room}/display/home")
async def room_display_home(room: str, caller: Caller = Depends(who)):
    whiteboard_room(room)
    whiteboard_controller(room, caller)
    ensure_room_idle(room)
    if not rooms.display(room)["idle_page"]:
        raise HTTPException(400, "Choose a default TV screen in Room settings first")
    rooms.meeting_ended(room)
    commands = [command_device(d["id"], "refresh", {}) for d in list(hub.devices.values())
                if d.get("role") == "roompc" and (d.get("room") or d.get("arg")) == room
                and str(d.get("client", "")).startswith("xosroom-linux")]
    return {"display": rooms.display(room), "commands": commands}


class CopperBoardConnection(BaseModel):
    canvas_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    web_url: str = Field(default="", max_length=1000)
    invite_url: str = Field(default="", max_length=1500)


def board_drawing_access(room: str, caller: Caller, key: str) -> bool:
    return caller.person or whiteboards.drawing_key(room, key)


@app.get("/u/whiteboards/{room}")
async def whiteboard_org_link(room: str, caller: Caller = Depends(who)):
    whiteboard_room(room)
    if not caller.person:
        raise HTTPException(401, "Sign in with your organization account to draw")
    return RedirectResponse(f"/app/#/whiteboard/{room}", status_code=303)


@app.get("/api/rooms/{room}/whiteboard/access")
async def whiteboard_access(room: str, request: Request, caller: Caller = Depends(who)):
    whiteboard_room(room)
    return {"editable": board_drawing_access(room, caller, request.headers.get("x-xos-board", ""))}


@app.get("/api/rooms/{room}/whiteboard/share")
async def whiteboard_share(room: str, request: Request, caller: Caller = Depends(who)):
    whiteboard_room(room)
    if not board_drawing_access(room, caller, request.headers.get("x-xos-board", "")):
        whiteboard_controller(room, caller)
    return {"org_url": f"{settings.XOS_PUBLIC_URL.rstrip('/')}/u/whiteboards/{room}",
            **whiteboards.copper_links(room)}


@app.put("/api/rooms/{room}/whiteboard/copper")
async def connect_whiteboard_copper(connection: CopperBoardConnection, room: str,
                                   caller: Caller = Depends(who)):
    from urllib.parse import urlsplit

    whiteboard_room(room)
    access.need(caller, "integrations")
    url = urlsplit(connection.web_url)
    if connection.web_url and (url.scheme != "https" or not url.hostname
                              or url.username or url.password or url.fragment):
        raise HTTPException(400, "Use a Copper Cloud HTTPS invitation link")
    invitation = urlsplit(connection.invite_url)
    if connection.invite_url and (invitation.scheme != "copper" or invitation.netloc != "canvas"
                                  or not re.fullmatch(r"/join/[A-Za-z0-9_-]{20,100}", invitation.path)
                                  or invitation.fragment):
        raise HTTPException(400, "Use the Copper Canvas invitation returned by Copper")
    key = whiteboards.connect_copper(room, connection.canvas_id, connection.web_url,
                                     connection.invite_url)
    return {**whiteboards.copper_links(room),
            "embed_url": f"{settings.XOS_PUBLIC_URL.rstrip('/')}/app/#/whiteboard/{room}?board_key={key}"}


@app.post("/api/rooms/{room}/whiteboard/drawing")
async def whiteboard_drawing(op: WhiteboardOp, room: str, request: Request,
                             caller: Caller = Depends(who)):
    whiteboard_room(room)
    if not board_drawing_access(room, caller, request.headers.get("x-xos-board", "")):
        raise HTTPException(403 if caller.person else 401, "Sign in with your organization account to draw")
    if op.action not in {"stroke", "undo", "clear", "replace"}:
        raise HTTPException(403, "Canvas access can draw but cannot control the room TV")
    if op.action == "stroke" and not op.id:
        raise HTTPException(400, "A stroke needs an id")
    try:
        return whiteboards.apply(room, op.model_dump(mode="json"), detached=True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


async def drawing_context(room: str, caller: Caller) -> dict:
    meetings, warning = [], ""
    if calendars.configured and rooms.calendar(room):
        try:
            meetings = [{k: b[k] for k in ("id", "title", "start", "end")} for b in
                        await calendars.bookings(rooms.calendar(room)) if b["end"] > time.time()]
        except CalendarError:
            warning = "The meeting calendar is unavailable. Choose Personal or another canvas."
    return {"room": room, "room_name": rooms.items[room]["name"],
            "person": {"email": caller.email, "name": caller.name}, "meetings": meetings[:8],
            "warning": warning, **whiteboards.copper_links(room)}


def require_drawing_or_remote(room: str, caller: Caller, request: Request) -> None:
    if not board_drawing_access(room, caller, request.headers.get("x-xos-board", "")):
        whiteboard_controller(room, caller)


@app.get("/api/rooms/{room}/whiteboard/context")
async def whiteboard_context(room: str, request: Request, caller: Caller = Depends(who)):
    whiteboard_room(room)
    require_drawing_or_remote(room, caller, request)
    return await drawing_context(room, caller)


class DrawingSnapshot(BaseModel):
    png: str = Field(max_length=10_700_000)
    width: int = Field(ge=1, le=4096)
    height: int = Field(ge=1, le=4096)
    epoch: int = Field(ge=0)
    revision: int = Field(ge=0)
    meeting_id: str = Field(default="", max_length=200)


class DrawingReceipt(BaseModel):
    canvas_id: str = Field(pattern=r"^(?:personal|[a-f0-9-]{36})$")
    canvas_name: str = Field(min_length=1, max_length=200)
    shape_id: str = Field(pattern=r"^xos-[a-f0-9-]{36}$")


@app.post("/api/rooms/{room}/whiteboard/exports")
async def whiteboard_export(snapshot: DrawingSnapshot, room: str, request: Request,
                             caller: Caller = Depends(who)):
    whiteboard_room(room)
    require_drawing_or_remote(room, caller, request)
    board = whiteboards.get(room)
    if snapshot.epoch != board["epoch"] or snapshot.revision > board["revision"]:
        raise HTTPException(409, "The drawing changed. Try inserting again.")
    context = await drawing_context(room, caller)
    meeting = next((m for m in context["meetings"] if m["id"] == snapshot.meeting_id), None)
    if snapshot.meeting_id and meeting is None:
        raise HTTPException(409, "That meeting is no longer available. Choose another destination.")
    title = ((meeting["title"] if meeting else context["room_name"]) + " · Whiteboard")[:200]
    try:
        token, value = whiteboard_exports.create(png=snapshot.png, width=snapshot.width,
            height=snapshot.height, context={"title": title, "room": room,
                "meeting_id": meeting["id"] if meeting else "",
                "meeting_title": meeting["title"][:200] if meeting else "",
                "copper_canvas_id": whiteboards.copper.get(room, {}).get("id", ""),
                "created_by": caller.email, "revision": snapshot.revision})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(429, str(exc)) from exc
    url = f"{settings.XOS_PUBLIC_URL.rstrip('/')}/api/whiteboard-exports/{token}"
    return {"source": url, "status_url": url + "/status", "expires": value["expires"]}


def export_value(token: str) -> dict:
    try:
        return whiteboard_exports.get(token)
    except (OSError, ValueError):
        raise HTTPException(410, "This drawing snapshot expired. Insert again from Xos.")


@app.get("/api/whiteboard-exports/{token}")
async def whiteboard_export_payload(token: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return export_value(token)["payload"]


@app.get("/api/whiteboard-exports/{token}/status")
async def whiteboard_export_status(token: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    value = export_value(token)
    return {"status": "inserted" if value["receipt"] else "pending", "receipt": value["receipt"],
            "expires": value["expires"]}


@app.post("/api/whiteboard-exports/{token}/receipt")
async def whiteboard_export_receipt(token: str, receipt: DrawingReceipt):
    export_value(token)
    try:
        return whiteboard_exports.receipt(token, receipt.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/rooms/{room}/whiteboard")
async def whiteboard_state(room: str, response: Response):
    whiteboard_room(room)
    response.headers["Cache-Control"] = "no-store"
    return whiteboards.snapshot(room)


@app.get("/api/rooms/{room}/whiteboard/stream")
async def whiteboard_stream(room: str, request: Request):
    whiteboard_room(room)
    queue = whiteboards.subscribe(room)

    async def stream():
        try:
            yield "retry: 1500\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                    yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            whiteboards.unsubscribe(room, queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/rooms/{room}/whiteboard")
async def whiteboard_write(op: WhiteboardOp, room: str, caller: Caller = Depends(who)):
    whiteboard_room(room)
    whiteboard_controller(room, caller)
    if op.action == "stroke" and not op.id:
        raise HTTPException(400, "A stroke needs an id")
    if op.action == "start":
        for device in hub.devices.values():
            if device.get("role") == "roompc" and (device.get("room") or device.get("arg")) == room:
                meeting = (device.get("native") or {}).get("meeting") or {}
                if meeting.get("in_call") or meeting.get("waiting"):
                    raise HTTPException(409, "Leave the meeting before opening the whiteboard")
                if any(c.get("type") == "meeting" and c.get("args", {}).get("action") in {"join", "meet_now"}
                       and c.get("status") in {"pending", "sent"}
                       and time.time() - c.get("created", 0) < hub_module.COMMAND_TTL_S
                       for c in hub.commands.get(device["id"], [])):
                    raise HTTPException(409, "Wait for the meeting launch to finish before opening the whiteboard")
        if zoom.configured and room in zoom.rooms:
            try:
                status = await zoom.room_status(zoom.rooms[room])
            except ZoomError as exc:
                raise HTTPException(409, "Cannot confirm the Zoom Room is idle. Try again shortly.") from exc
            if (status or {}).get("status") == "InMeeting":
                raise HTTPException(409, "Leave the meeting before opening the whiteboard")
    was_active = whiteboards.get(room)["active"]
    try:
        event = whiteboards.apply(room, op.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if op.action == "start" and not was_active:
        # Existing TV pages may predate whiteboard support. Refresh just this room's displays.
        # The Linux room's refresh checks the actual browser and refuses to leave a call.
        for device in hub.devices.values():
            if (device.get("room") or device.get("arg")) != room:
                continue
            role = device.get("role")
            if role == "roompc":
                command_device(device["id"], "refresh", {})
            elif role in {"tv", "page", "media", "signage", "wall"}:
                command_device(device["id"], "reload", {})
    return event


# ---------- devices ----------


class Heartbeat(BaseModel):
    """A field left out keeps its last value: a sleeping Fire TV (SleepWatch) sends only id,
    client and native, and what its page last reported stays."""

    id: str = Field(min_length=4, max_length=64)
    client: str = Field(default="browser", max_length=64)  # e.g. firetv-0.2.0, ios-0.1.0
    role: str | None = Field(default=None, max_length=16)
    arg: str | None = Field(default=None, max_length=32)
    label: str | None = Field(default=None, max_length=80)
    screen: str | None = Field(default=None, max_length=32)  # e.g. 1920x1080
    user_agent: str | None = Field(default=None, max_length=300)
    # Filled in by the Fire TV / iOS app bridge: model, OS, LAN address, display on/off, etc.
    native: dict | None = None
    # AirPlay receiver name on Linux nodes installed with --airplay (os/install.sh); else "".
    airplay: str | None = Field(default=None, max_length=60)
    wake_lock: (
        Literal["inactive", "background", "unsupported", "requesting", "held", "released", "denied"]
        | None
    ) = None
    # Modern remotes persist their random key before enrolling, so losing a reply is harmless.
    # None preserves legacy clients; empty means storage failed and must not consume a key.
    enrollment_key: str | None = Field(
        default=None, max_length=128, pattern=r"^(?:[A-Za-z0-9_-]{32,128})?$"
    )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "")


# Remotes that send commands without anyone signing in: the room iPad and remote kiosks. A
# remote gets its device key once, in a heartbeat reply, if Admin named it or it was already
# running before sign-in arrived (2026-10-15); a browser that just opens #/remote gets none.
CONTROLLER_ROLES = {"remote", "tablet"}
TRUSTED_BEFORE = 1792022400  # 2026-10-15 00:00 UTC


def appliance_guard(request: Request, device_id: str) -> None:
    """An enrolled room computer must prove it is that box: heartbeats, its command stream and
    its acks carry its device key. Everything else checks in as before."""
    if appliances.keyed(device_id) and not hub.check_key(device_id, request.headers.get("x-xos-key", "")):
        raise HTTPException(401, "this device id belongs to an enrolled Xos box; send its device key")


@app.post("/api/devices/heartbeat")
async def heartbeat(beat: Heartbeat, request: Request):
    appliance_guard(request, beat.id)
    known = hub.devices.get(beat.id, {})
    trusted = beat.role in CONTROLLER_ROLES and (
        known.get("name") or known.get("first_seen", time.time()) < TRUSTED_BEFORE
    )
    key = None
    if trusted and beat.enrollment_key != "":
        key = hub.issue_key(beat.id, beat.enrollment_key or "")
    data = {
        **beat.model_dump(exclude_none=True, exclude={"enrollment_key"}),
        "ip": _client_ip(request),
    }
    if beat.role in CONTROLLER_ROLES and beat.enrollment_key is not None:
        data["remote_access"] = (
            "ready"
            if hub.check_key(beat.id, beat.enrollment_key)
            else "needs_reset"
            if trusted
            else "needs_setup"
        )
    # Only the enrolled appliance can report an actual call ending for its assigned room.
    if (
        appliances.keyed(beat.id)
        and known.get("role") == "roompc"
        and data.get("native", {}).get("meeting", {}).get("ended")
        and not known.get("native", {}).get("meeting", {}).get("ended")
    ):
        rooms.meeting_ended(known.get("room") or known.get("arg", ""))
    record = hub.heartbeat(data)
    # Whether this server holds the device's command stream. A kiosk whose stream died without
    # an error (a NAT or proxy dropped it) still sees it open, and reopens it when this is false.
    reply = {**record, "stream": hub.connected(beat.id)}
    return {**reply, "key": key} if key else reply


@app.get("/api/devices/events")
async def device_events(request: Request):
    """Server-Sent Events: a line whenever a screen changes or a command is queued or answered.

    Remotes and Admin re-read /api/devices when one arrives instead of polling, so a tap shows
    its result as soon as the TV confirms it.
    """
    queue = hub.watch()

    async def stream():
        try:
            yield "retry: 2000\n: connected\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.unwatch(queue)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@app.get("/api/devices/{device_id}/meeting-controls")
async def meeting_controls(device_id: str, response: Response, caller: Caller = Depends(who)):
    response.headers["Cache-Control"] = "no-store"
    may_command(caller, device_id, "meeting")
    d = hub.devices.get(device_id, {})
    if time.time() - d.get("last_seen", 0) > settings.DEVICE_ONLINE_S:
        return {"controls": []}
    meeting = (d.get("native") or {}).get("meeting") or {}
    return {"back": bool(meeting.get("in_call") and meeting.get("control_back")),
            "controls": meeting.get("controls", []) if meeting.get("in_call") else [],
            "labels": meeting.get("control_labels", []) if meeting.get("in_call") else [],
            "diagnostics": meeting.get("control_diagnostics", []) if meeting.get("in_call") else []}


def inventory_row(device: dict) -> dict:
    """A screen without its share code, address, commands or meeting details."""
    return {
        "id": device["id"],
        "name": device.get("name") or device.get("label") or device["id"],
        "client": device.get("client", ""),
        "role": device.get("role", ""),
        "room": device.get("room", ""),
        "online": bool(device.get("online")),
        "connected": bool(device.get("connected")),
        "last_seen": device.get("last_seen"),
    }


@app.get("/api/devices")
async def devices(request: Request):
    rows = hub.device_list(settings.DEVICE_ONLINE_S)
    if access.calling_bot(request):  # agents get the bounded inventory
        return [inventory_row(d) for d in rows]
    return rows


# ---------- who is signed in, and taking control of a screen (#/control) ----------


def me_view(caller: Caller) -> dict:
    return {
        "email": caller.email,
        "name": caller.name,
        "role": caller.role,
        "role_name": caller.role_name,
        "perms": sorted(caller.perms),
        # Older pages read `team` as "may open Admin".
        "team": caller.can("admin.view"),
        "preview": caller.preview,
        # An agent through a token gateway: its slug. It has no role.
        "bot": caller.bot or None,
    }


@app.get("/api/me")
async def me(caller: Caller = Depends(who)):
    """Reach it as /u/api/me: the sign-in gate adds who you are. Signed out: email is "".
    While an Admin previews, this is the previewed perspective, with `preview` set."""
    return me_view(caller)


@app.get("/u/signin", include_in_schema=False)
async def signin(next: str = "/app/#/control"):
    """Behind the sign-in gate: sign-in happens on the way here, then back to `next`."""
    return RedirectResponse(next if re.fullmatch(r"/(?!/)[^\s\\]*", next) else "/app/", 303)


# ---------- people and roles (Admin → People & roles; src/app/people.py) ----------


def roles_view() -> list[dict]:
    counts = people.counts()
    return [
        {"id": r.id, "name": r.name, "summary": r.summary, "perms": sorted(people.permissions_for_role(r.id)),
         "default_perms": sorted(r.perms), "customized": r.id in people.role_permissions,
         "people": counts[r.id]}
        for r in access.ROLES.values()
    ]


@app.get("/api/roles")
async def list_roles(caller: Caller = requires("people")):
    """Each role, the permissions it has (the matrix lives in src/app/access.py) and how many
    active people have it."""
    return {"roles": roles_view(), "permissions": access.PERMISSIONS}


class RolePermissionsIn(BaseModel):
    permissions: list[str] | None = Field(default=None, max_length=100)


class PersonPermissionsIn(BaseModel):
    permissions: dict[str, bool] = Field(max_length=100)


@app.put("/api/roles/{role}/permissions")
async def set_role_permissions(role: str, body: RolePermissionsIn, caller: Caller = requires("people")):
    try:
        return people.set_permissions(caller.email, "role", role, body.permissions)
    except RoleError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.put("/api/people/{email}/permissions")
async def set_person_permissions(email: str, body: PersonPermissionsIn, caller: Caller = requires("people")):
    try:
        return people.set_permissions(caller.email, "person", email, body.permissions)
    except RoleError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/people")
async def list_people(caller: Caller = requires("people")):
    rows = people.all()
    return {
        "people": rows,
        "departments": sorted({p["department"] for p in rows if p.get("department")}),
        "teams": sorted({p["team"] for p in rows if p.get("team")}),
        "defaults": people.defaults,
        "bootstrap": sorted(access.bootstrap_emails()),
        "super_admins": sorted(access.super_admin_emails()),
        "you_super": caller.email in access.super_admin_emails(),
    }


class RoleIn(BaseModel):
    role: Literal["", "admin", "content_editor", "screen_operator", "member"]


@app.put("/api/people/{email}/role")
async def set_person_role(body: RoleIn, email: str, caller: Caller = requires("people")):
    """Give one person a role ("" goes back to their team, department or Member default)."""
    try:
        return people.set_role(caller.email, email, body.role)
    except RoleError as exc:
        raise HTTPException(409, str(exc)) from exc


class DefaultIn(BaseModel):
    kind: Literal["department", "team"]
    name: str = Field(min_length=1, max_length=120)
    role: Literal["", "admin", "content_editor", "screen_operator", "member"]


@app.put("/api/people/defaults")
async def set_role_default(body: DefaultIn, caller: Caller = requires("people")):
    """The role everyone in a directory department or team gets unless they're given one."""
    try:
        return {"defaults": people.set_default(caller.email, body.kind, body.name, body.role)}
    except RoleError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/people/audit")
async def people_audit(limit: int = 200, caller: Caller = requires("people")):
    """Role changes and previews, newest first (XOS_DATA_DIR/people_audit.jsonl)."""
    return {"items": people.audit(max(1, min(limit, 1000)))}


class PreviewIn(BaseModel):
    role: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=254)


@app.post("/api/preview")
async def start_preview(body: PreviewIn, caller: Caller = requires("people")):
    """See Xos as a role or a person. Until Exit preview, this Admin's requests get that
    perspective's permissions and every write is refused (refuse_writes_in_preview)."""
    if bool(body.role) == bool(body.email):
        raise HTTPException(400, "Pick a role or a person to preview")
    try:
        people.start_preview(caller, role=body.role, email=body.email)
    except RoleError as exc:
        raise HTTPException(400, str(exc)) from exc
    return me_view(people.caller(caller.email, caller.name))


@app.delete("/api/preview")
async def exit_preview(request: Request):
    email, name = access.identify(request)
    if not email:
        raise HTTPException(401, "Sign in to use Xos")
    people.end_preview(email)
    return me_view(people.caller(email, name))


def display_screen(d: dict) -> bool:
    """A screen that shows things, not a remote, a setup or admin view, or a room's meeting
    computer (a route to a Linux room PC changes the room's idle display: command_device)."""
    return d.get("role") not in (*CONTROLLER_ROLES, "setup", "admin", "roompc")


def controllable(d: dict, caller: Caller | None = None) -> bool:
    """A screen anyone in your organization may take: a display with Share to this screen turned on. A
    screen operator may also take one with sharing off."""
    if not display_screen(d):
        return False
    return not d.get("share_off") or bool(caller and caller.can("screens.operate"))


@app.get("/api/control")
async def control_list(caller: Caller = Depends(who)):
    access.need(caller, "screens.take_shared")
    rows = []
    for d in hub.device_list(settings.DEVICE_ONLINE_S):
        if not controllable(d, caller):
            continue
        hold = hub.controller(d["id"])
        rows.append(
            {
                "id": d["id"],
                "name": d.get("name") or d.get("label") or d["id"],
                "room": d.get("room", ""),
                "role": d.get("role", ""),
                "arg": d.get("arg", ""),
                "online": d["online"],
                "screenshot": d.get("screenshot"),
                "native": bool(d.get("native")),
                "controlled_by": {"name": hold["name"], "since": hold["since"]} if hold else None,
                "mine": bool(hold and hold["email"] == caller.email),
                "sharing_off": bool(d.get("share_off")),
            }
        )
    return {
        "me": me_view(caller),
        "screens": rows,
        "idle_s": hub_module.CONTROL_IDLE_S,
    }


@app.post("/api/control/{device_id}")
async def take_control(device_id: str = DEVICE_ID, caller: Caller = Depends(who)):
    d = hub.devices.get(device_id)
    if d is None:
        raise HTTPException(404, "unknown device")
    if not caller.person:
        raise HTTPException(401, "Sign in to control a screen")
    access.need(caller, "screens.take_shared")
    if not controllable(d, caller):
        raise HTTPException(403, "This screen doesn't have sharing turned on")
    hold = hub.take_control(device_id, caller.email, caller.name)
    if hold is None:
        raise HTTPException(409, f"{hub.controller(device_id)['name']} is controlling this screen")
    logger.info("control %s taken by %s", device_id, caller.email)
    return {"ok": True, "since": hold["since"]}


@app.delete("/api/control/{device_id}")
async def release_control(device_id: str = DEVICE_ID, caller: Caller = Depends(who)):
    hold = hub.controller(device_id)
    if hold and (hold["email"] == caller.email or caller.can("screens.operate")):
        hub.release_control(device_id)
        return {"ok": True}
    if hold is None:
        return {"ok": True}
    raise HTTPException(403 if caller.person else 401, f"{hold['name']} is controlling this screen")


# ---------- device control ----------
# Admin → Screens queues a command; the device receives it on its command stream, runs it and
# acknowledges. Devices only make outbound connections, so this works through any office NAT.

CommandType = Literal[
    "route",  # args.route e.g. "tv/lamar", "signage/hq": change what the screen shows
    "reload",  # reload the page (picks up a new deploy)
    "identify",  # show the device name full-screen for a few seconds
    "message",  # args.text, args.seconds: banner on the screen
    "screenshot",  # upload what is on screen to /api/devices/{id}/screenshot
    "display_off",  # put the stick to sleep; HDMI-CEC turns the TV off
    "display_on",  # wake the stick; HDMI-CEC turns the TV on and selects its input
    "switch_input",  # Switch TV to Xos: the server runs display_off, then display_on (below)
    "return_min",  # args.minutes: come back to Xos this long after someone leaves it
    "page_scroll",  # args.dir up|down|top|bottom: scroll the page view (uploaded pages)
    "page_key",  # args.key e.g. ArrowRight: next slide in an uploaded deck
    "share",  # args: a share session (kind screen|url|image); sent by POST /api/share/{code}
    "share_stop",  # args.session: stop showing that share
    "refresh",  # check in now, e.g. to pick up new share-badge settings
    "meeting",  # args.action (join, meet_now, leave, mute, ...): a room computer (os/mac/xos-room.py)
    "update",  # an Xos Linux box: check for a new release now (setup permission)
]


class Command(BaseModel):
    type: CommandType
    args: dict = Field(default_factory=dict)


class Ack(BaseModel):
    ok: bool = True
    result: object = None


# What someone who took a screen from #/control may do with it (not timers or share plumbing).
HOLDER_COMMANDS = {
    "route",
    "reload",
    "identify",
    "message",
    "screenshot",
    "display_off",
    "display_on",
    "page_scroll",
    "page_key",
}


# What a content editor may send to any screen no one else is holding (screens.show).
SHOW_COMMANDS = {
    "route", "reload", "identify", "message", "screenshot", "page_scroll", "page_key",
}


def may_command(caller: Caller, device_id: str, kind: str) -> None:
    """Screen operators and trusted remotes may; content editors may show things on a display
    screen no one else holds (never a remote, a setup view or a room computer); anyone else only
    on a screen they hold."""
    if kind in ("display_on", "display_off") and device_id in _switching:
        # Switch TV to Xos is turning this TV off and on: a manual tap would undo a step.
        raise HTTPException(409, "Xos is switching this TV to Xos. Try again when it's done.")
    if caller.can("screens.operate") or caller.device:
        return
    if not caller.person:
        raise HTTPException(401, "Sign in to control this screen")
    if kind in HOLDER_COMMANDS and hub.touch_control(device_id, caller.email):
        return
    hold = hub.controller(device_id)
    if hold and hold["email"] != caller.email:
        raise HTTPException(409, f"{hold['name']} is controlling this screen")
    if (kind in SHOW_COMMANDS and caller.can("screens.show")
            and display_screen(hub.devices.get(device_id, {}))):
        return
    raise HTTPException(403, "Take control of this screen first (Xos → Control)")


@app.post("/api/devices/{device_id}/commands")
async def send_command(cmd: Command, device_id: str = DEVICE_ID, caller: Caller = Depends(who)):
    if device_id not in hub.devices:
        raise HTTPException(404, "unknown device")
    if cmd.type == "switch_input":
        if not caller.device:  # a room remote, or someone who may turn displays off and on
            access.need(caller, "screens.operate")
        started = switch_input(device_id)
        logger.info("switch_input → %s by %s", device_id, caller.email or caller.device)
        return started
    may_command(caller, device_id, cmd.type)
    if cmd.type == "update" and not caller.can("setup"):
        raise HTTPException(403, "Updating a box needs the Admin role")
    args = _meeting_args(cmd.args) if cmd.type == "meeting" else cmd.args
    queued = command_device(device_id, cmd.type, args)
    logger.info("command %s → %s (%s)", cmd.type, device_id, queued["status"])
    return queued


MEETING_ACTIONS = {
    "control",
    "control_back",
    "join",
    "meet_now",
    "leave",
    "end",
    "mute",
    "unmute",
    "camera_off",
    "camera_on",
    "share_start",
    "share_stop",
    "volume",
}


def _meeting_args(args: dict) -> dict:
    """What the room iPad asked a room computer, checked: a join carries a Meet or Zoom URL the
    computer can open (from a code, ID or link someone typed), never an arbitrary one."""
    action = args.get("action")
    if action not in MEETING_ACTIONS:
        raise HTTPException(400, f"unknown meeting action {action!r}")
    provider = args.get("provider", "meet")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise HTTPException(400, "Choose Meet, Teams, Webex or Zoom")
    if action == "meet_now":
        return {"action": action, **({"provider": provider} if "provider" in args else {})}
    if action == "join":
        try:
            link = meeting_link(str(args.get("meeting", "")), str(args.get("passcode", "")))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        # Preserve old clients' normalized Zoom URL; the browser agent selects the web client.
        url = link["url"].replace("/wc/join/", "/j/")
        return {"action": "join", "url": url}
    if action == "control":
        token = args.get("control", "")
        if not isinstance(token, str) or not re.fullmatch(
            r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", token
        ):
            raise HTTPException(400, "Choose a current meeting control")
        return {"action": "control", "control": token}
    if action == "volume":
        try:
            return {"action": "volume", "volume": max(0, min(100, int(args.get("volume", 50))))}
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "volume is 0 to 100") from e
    return {"action": action}


_waking: set[str] = set()
_wake_tasks: set[asyncio.Task] = set()


def command_device(device_id: str, kind: str, args: dict) -> dict:
    """Queue a command. TV on for a Fire TV that isn't listening (asleep on Xos TV before 0.6.0,
    which drops off Xos) also wakes it over network ADB, the way Add a screen reaches it.

    Only a screen this server set up (Add a screen → by IP) and still at that address: any other
    screen hasn't allowed the server's key, and trying would put "Allow USB debugging?" on the TV.
    """
    if kind == "meeting" and args.get("action") == "meet_now" and not settings.XOS_INSTANT_MEETINGS:
        raise HTTPException(409, "This room joins invitations as a guest. "
                            "Choose a calendar meeting or enter its code.")
    device = hub.devices.get(device_id, {})
    room = device.get("room") or device.get("arg")
    if kind == "meeting" and args.get("action") in {"join", "meet_now"}:
        if room in rooms.items and whiteboards.get(room)["active"]:
            raise HTTPException(409, "Exit the whiteboard before joining a meeting")
    if (kind == "route" and device.get("role") == "roompc"
            and str(device.get("client", "")).startswith("xosroom-linux")):
        ensure_room_idle(room)
        route = args.get("route", "")
        page = route[5:] if isinstance(route, str) and route.startswith("page/") else ""
        if room not in rooms.items or not page or pages.get(page) is None:
            raise HTTPException(400, "Choose an existing content page for this room TV")
        rooms.set_display(room, page, rooms.display(room)["meeting_lead_minutes"])
        rooms.meeting_ended(room)
        kind, args = "refresh", {}
    queued = hub.queue_command(device_id, kind, args)
    ip = (device.get("native") or {}).get("ip", "")
    if (
        kind == "display_on"
        and queued["status"] == "pending"
        and ip
        and device.get("adb_ip") == ip
        and device_id not in _waking
    ):
        try:
            task = asyncio.get_running_loop().create_task(_wake(device_id, ip, queued))
        except RuntimeError:  # no event loop here (a sync caller): the command waits for the TV
            return queued
        _waking.add(device_id)
        _wake_tasks.add(task)
        task.add_done_callback(_wake_tasks.discard)
    return queued


async def _wake(device_id: str, ip: str, cmd: dict) -> None:
    try:
        await provisioner.wake(ip)
        logger.info("woke %s over ADB at %s", device_id, ip)
    except ProvisionError as e:
        logger.info("couldn't wake %s over ADB: %s", device_id, e)
        if cmd["status"] == "pending":  # still unheard: say why, instead of waiting forever
            hub.ack(
                device_id,
                cmd["id"],
                False,
                f"The TV is asleep and Xos can't reach it to wake it ({str(e).rstrip('.')}). "
                "Press a button on its remote.",
            )
    except Exception:  # noqa: BLE001
        logger.exception("waking %s failed", device_id)
    finally:
        _waking.discard(device_id)


# ---------- Switch TV to Xos ----------
# Someone switched the room TV to another HDMI input. Display off and then Display on wakes the
# stick, and HDMI-CEC one-touch play switches the TV back to its input. Xos TV 0.6.0 stays
# reachable while asleep (SleepWatch), so the server runs the two steps with the commands every
# stick already has: no new APK. It runs only when someone asks. Xos never takes the input back
# by itself.

SWITCH_MIN_VERSION = (0, 6, 0)
SWITCH_OFF_WAIT_S = 15.0  # for the stick to answer Display off
SWITCH_ASLEEP_WAIT_S = 20.0  # then for it to check in asleep (SleepWatch does that at once)
SWITCH_SETTLE_S = 5.0  # then for the TV to finish going to standby; many ignore CEC until then
SWITCH_ON_WAIT_S = 30.0  # for the sleeping stick to answer Display on
SWITCH_POLL_S = 0.25
_switching: dict[str, asyncio.Task] = {}


def tv_app_version(d: dict) -> tuple[int, int, int] | None:
    """The Xos TV version a stick reports (native.app, else its client "firetv-0.6.0")."""
    raw = str((d.get("native") or {}).get("app") or "")
    client = str(d.get("client", ""))
    if not raw and client.startswith("firetv-"):
        raw = client[len("firetv-") :]
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", raw)
    return (int(m[1]), int(m[2]), int(m[3] or 0)) if m else None


def switch_input_problem(d: dict) -> str:
    """Why Switch TV to Xos can't run on this screen, or "" when it can. app/js/tv-input.js
    says the same in Admin and the room remote."""
    if not str(d.get("client", "")).startswith("firetv") or not isinstance(d.get("native"), dict):
        return "Switch TV to Xos needs the Xos TV app on a Fire TV stick."
    version = tv_app_version(d)
    if version is None or version < SWITCH_MIN_VERSION:
        return (
            "Switch TV to Xos needs Xos TV 0.6.0 or later. "
            "Update the stick with Admin → Add a screen → by IP."
        )
    if not d["native"].get("displayControl"):
        return (
            "Switch TV to Xos needs display control, which is off on this stick. "
            "Run firetv/provision.sh to turn it on."
        )
    return ""


def switch_input(device_id: str) -> dict:
    """Start Switch TV to Xos on a stick and return its command record. The steps run in the
    background; the record finishes as done or failed, like any command."""
    d = hub.devices.get(device_id)
    if d is None:
        raise HTTPException(404, "unknown device")
    problem = switch_input_problem(d)
    if problem:
        raise HTTPException(409, problem)
    if device_id in _switching:
        raise HTTPException(409, "Xos is already switching this TV. Wait for it to finish.")
    if not hub.connected(device_id):
        raise HTTPException(409, "The stick isn't connected to Xos right now. Try again soon.")
    record = hub.queue_command(device_id, "switch_input", {}, status="sent")
    task = asyncio.get_running_loop().create_task(_switch(device_id, record))
    _switching[device_id] = task
    task.add_done_callback(lambda _t: _switching.pop(device_id, None))
    return record


class _Forgotten(Exception):
    """The screen was forgotten in Admin mid-switch: stop and leave no trace of it."""


def _queue_step(device_id: str, kind: str) -> dict:
    if device_id not in hub.devices:  # queueing would bring back hub.commands[device_id]
        raise _Forgotten
    return command_device(device_id, kind, {})


async def _answer(device_id: str, cmd: dict, wait_s: float) -> dict:
    """The command once the stick answers it, or as it is after `wait_s`. A step still waiting to
    be delivered then expires, so a late delivery can't turn the TV off or on after Xos said the
    switch failed."""
    deadline = time.monotonic() + wait_s
    while cmd["status"] in ("pending", "sent") and time.monotonic() < deadline:
        if device_id not in hub.devices:
            raise _Forgotten
        await asyncio.sleep(SWITCH_POLL_S)
    if cmd["status"] == "pending":
        cmd["status"] = "expired"
        hub.notify(device_id, "command", command=cmd)
    return cmd


async def _switch(device_id: str, record: dict) -> None:
    def failed(step: str, cmd: dict) -> None:
        why = cmd.get("result") if cmd["status"] == "failed" else "the stick didn't answer"
        hub.ack(device_id, record["id"], False, f"{step} didn't work: {why}")

    def asleep() -> bool:
        return (hub.devices.get(device_id, {}).get("native") or {}).get("displayOn") is False

    try:
        if not asleep():  # an already sleeping stick only needs waking
            hub.progress(record, device_id, "Turning the TV off")
            off = await _answer(device_id, _queue_step(device_id, "display_off"), SWITCH_OFF_WAIT_S)
            if off["status"] != "done":
                failed("Display off", off)
                if off["status"] == "sent":  # delivered, not confirmed: don't leave the TV off
                    _queue_step(device_id, "display_on")
                return
            deadline = time.monotonic() + SWITCH_ASLEEP_WAIT_S
            while not asleep() and time.monotonic() < deadline:
                if device_id not in hub.devices:
                    raise _Forgotten
                await asyncio.sleep(SWITCH_POLL_S)
            await asyncio.sleep(SWITCH_SETTLE_S)
        hub.progress(record, device_id, "Turning the TV on")
        on = await _answer(device_id, _queue_step(device_id, "display_on"), SWITCH_ON_WAIT_S)
        if on["status"] != "done":
            return failed("Display on", on)
        hub.ack(device_id, record["id"], True, "The TV is on Xos")
    except _Forgotten:
        logger.info("%s was forgotten while switching it to Xos", device_id)
    except Exception:  # noqa: BLE001
        logger.exception("switching %s to Xos failed", device_id)
        hub.ack(device_id, record["id"], False, "Xos couldn't finish switching the TV")


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=60)  # e.g. "Atrium room"; "" clears it
    room: str | None = Field(default=None, max_length=40)  # Xos room this screen is in; "" clears


@app.put("/api/devices/{device_id}", dependencies=[requires("setup")])
async def update_device(update: DeviceUpdate, device_id: str = DEVICE_ID):
    if device_id not in hub.devices:
        raise HTTPException(404, "unknown device")
    record = hub.devices[device_id]
    if update.name is not None:
        record = hub.rename(device_id, update.name.strip())
    if update.room is not None:
        record = hub.set_room(device_id, update.room.strip())
    return record


@app.post("/api/devices/{device_id}/remote-access/reset", dependencies=[requires("setup")])
async def reset_remote_access(device_id: str = DEVICE_ID):
    """Re-enroll a named room remote without forgetting its identity, room or settings."""
    record = hub.devices.get(device_id)
    if record is None:
        raise HTTPException(404, "unknown device")
    if record.get("role") not in CONTROLLER_ROLES or not record.get("name"):
        raise HTTPException(400, "Name this room remote in Admin before resetting its access")
    hub.revoke_key(device_id)
    queued = command_device(device_id, "reload", {})
    return {"ok": True, "command": queued}


@app.delete("/api/devices/{device_id}", dependencies=[requires("setup")])
async def forget_device(device_id: str = DEVICE_ID):
    """Remove a screen from Admin. If it is still running it re-registers on its next heartbeat."""
    if not hub.forget(device_id):
        raise HTTPException(404, "unknown device")
    return {"ok": True}


class PairRequest(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]{4,64}$")


@app.post("/api/devices/pair")
async def pair(req: PairRequest):
    """A new screen's setup page asks for the code it should display."""
    return {"code": hub.pair_code(req.id)}


DEFAULT_ROUTE = "page/welcome"


@app.get("/api/devices/pairing", dependencies=[requires("setup")])
async def pairing_screens():
    """Screens showing a pairing code right now (Admin → Add a screen → Waiting to be added)."""
    now = time.time()
    out = []
    for code, p in hub.pair_codes.items():
        d = hub.devices.get(p["device"], {})
        if now - p["created"] > hub_module.PAIR_TTL_S or now - d.get("last_seen", 0) > 120:
            continue  # expired, or the screen has gone away
        n = d.get("native") or {}
        out.append({"code": code, "device": p["device"], "since": p["created"], "client": d.get("client", ""),
                    "ip": n.get("ip") or d.get("ip", ""), "model": n.get("model", ""),
                    "screen": d.get("screen", "")})
    return sorted(out, key=lambda x: -x["since"])


@app.get("/api/pair/qr.svg", include_in_schema=False)
async def pair_qr(code: str = ""):
    """The QR on a setup screen: opens Admin → Add a screen with this code filled in."""
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(400, "a 6-digit code")
    buf = io.BytesIO()
    link = f"{settings.XOS_PUBLIC_URL.rstrip('/')}/app/#/admin/screens/pair-{code}"
    segno.make(link, error="m").save(buf, kind="svg", scale=10, border=2, dark="#000", light="#fff",
                                     xmldecl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


class Claim(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(default="", max_length=60)
    route: str = Field(default="", pattern=r"^$|^[a-z]+(/[a-z0-9-]+){0,2}$")
    room: str = Field(default="", pattern=r"^$|^[a-z0-9-]{1,40}$")
    # "room": an Xos box becomes the room's meeting computer (Meet, Zoom, Teams) instead of a screen
    use: Literal["screen", "room"] = "screen"


@app.post("/api/devices/claim")
async def claim(req: Claim, caller: Caller = requires("setup")):
    """Admin enters the code from a setup screen: name it, put it in its room, tell it what to show."""
    if req.room and req.room not in rooms.items:
        raise HTTPException(400, "Pick one of the rooms in Admin → Rooms")
    if req.use == "room":
        return _claim_room_computer(req, caller)
    device_id = hub.claim(req.code)
    if device_id is None:
        raise HTTPException(404, "No screen is showing that code. Check the code on the TV.")
    if device_id not in hub.devices:
        hub.heartbeat({"id": device_id, "client": "browser", "role": "setup"})
    if req.name.strip():
        hub.rename(device_id, req.name.strip())
    if req.room:
        hub.set_room(device_id, req.room)
    # A newly added screen plays the welcome page until Admin gives it something else.
    hub.queue_command(device_id, "route", {"route": req.route or DEFAULT_ROUTE})
    return hub.devices[device_id]


def _claim_room_computer(req: Claim, caller: Caller) -> dict:
    """A paired Xos box becomes the room's meeting computer: a room-computer setup bound to this
    box, which its helper (os/appliance/xos-box) collects and installs within a minute."""
    if not req.room:
        raise HTTPException(400, "Pick the room this computer runs meetings for")
    box = next((p["device"] for c, p in hub.pair_codes.items() if c == req.code), None)
    if box is None:
        raise HTTPException(404, "No screen is showing that code. Check the code on the TV.")
    if not hub.devices.get(box, {}).get("client", "").startswith("xos-display"):
        raise HTTPException(400, "Only an Xos box (a mini PC from the Xos USB) can be a meeting room computer")
    if not boxes.usable(box):
        raise HTTPException(409, "This box hasn't checked in with its setup helper (or another device "
                                 "claimed its id). Update it, or reinstall it from the Xos USB.")
    hub.claim(req.code)
    name = req.name.strip() or f"{rooms.items[req.room].get('name', req.room)} room computer"
    for old in [i for i, a in appliances.items.items() if a.get("from_box") == box and not a["enrolled"]]:
        appliances.delete(old)  # paired again: only the latest choice counts
        hub.forget(old)
    a = appliances.create("room", req.room, name, "", caller.email, from_box=box)
    _pending_box(a)
    # Until it restarts as the room computer, the box shows the room's screen.
    hub.queue_command(box, "route", {"route": f"tv/{req.room}"})
    return {**hub.devices[a["device_id"]], "becoming_room_computer": True, "box": box}


class ProvisionRequest(BaseModel):
    ip: str = Field(max_length=40)
    name: str = Field(default="", max_length=60)
    route: str = Field(default="", pattern=r"^$|^[a-z]+(/[a-z0-9-]+){0,2}$")


async def _provisioned(job: dict, info: dict, route: str, wait_s: float) -> str | None:
    """After set-up: find the screen by the IP it reports, then name it and set what it shows."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        for device_id, d in hub.devices.items():
            native = d.get("native") or {}
            if native.get("ip") == job["ip"] and d.get("last_seen", 0) >= job["started"]:
                hub.mark_adb(device_id, job["ip"])
                if job["name"]:
                    hub.rename(device_id, job["name"])
                if route or not info.get("had_xos"):
                    # Like a claim: a new screen shows the welcome page until Admin picks something else.
                    hub.queue_command(device_id, "route", {"route": route or DEFAULT_ROUTE})
                    for code in [c for c, p in hub.pair_codes.items() if p["device"] == device_id]:
                        del hub.pair_codes[code]
                return device_id
        await asyncio.sleep(1)
    return None


@app.post("/api/provision", status_code=202, dependencies=[requires("setup")])
async def provision(req: ProvisionRequest):
    """Admin → Add a screen → by IP: install and open Xos on an Android screen over network ADB."""
    try:
        return provisioner.start(req.ip, req.name.strip(), req.route, _provisioned)
    except ProvisionError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/provision", dependencies=[requires("setup")])
async def provision_jobs():
    return provisioner.list()


@app.get("/api/devices/{device_id}/commands")
async def command_history(device_id: str = DEVICE_ID):
    return hub.commands.get(device_id, [])


@app.post("/api/devices/{device_id}/commands/{cmd_id}/ack")
async def ack_command(ack: Ack, cmd_id: str, request: Request, device_id: str = DEVICE_ID):
    appliance_guard(request, device_id)
    cmd = hub.ack(device_id, cmd_id, ack.ok, ack.result)
    if cmd is None:
        raise HTTPException(404, "unknown command")
    return cmd


@app.get("/api/devices/{device_id}/stream")
async def device_stream(request: Request, device_id: str = DEVICE_ID):
    """Server-Sent Events: commands for this device, as they are queued."""
    appliance_guard(request, device_id)
    queue = hub.subscribe_device(device_id)

    async def stream():
        try:
            yield "retry: 3000\n: connected\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.unsubscribe_device(device_id, queue)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@app.post("/api/devices/{device_id}/screenshot")
async def upload_screenshot(request: Request, device_id: str = DEVICE_ID, capture_id: str = ""):
    media_type = request.headers.get("content-type", "").split(";")[0]
    if media_type not in ("image/jpeg", "image/png"):
        raise HTTPException(415, "send image/jpeg or image/png")
    data = await request.body()
    if not data or len(data) > MAX_SCREENSHOT_BYTES:
        raise HTTPException(413, "screenshot missing or too large")
    if not hub.save_screenshot(device_id, data, media_type, capture_id):
        raise HTTPException(409, "screenshot capture was superseded")
    return {"ok": True, "bytes": len(data)}


@app.get("/api/devices/{device_id}/screenshot")
async def get_screenshot(device_id: str = DEVICE_ID):
    path = hub.screenshot_path(device_id)
    if not path.exists():
        raise HTTPException(404, "no screenshot yet")
    media_type = hub.devices.get(device_id, {}).get("screenshot", {}).get("type", "image/jpeg")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


# ---------- share to a screen (/s/<code>) ----------
# Every screen has a short share link, shown on its idle screen, in Admin and on the room remote.
# Opening it on a laptop or phone shares a screen (WebRTC, like the camera feed), a link or a
# photo to that screen. The screen shows it over whatever it was showing, until it's stopped.

SHARE_CODE = Path(pattern=r"^[A-Za-z0-9]{3,12}$")
MAX_SHARE_IMAGE_BYTES = 10_000_000
SHARE_IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")


def _share_target(code: str) -> str:
    device_id = hub.share_device(code)
    if not device_id:
        raise HTTPException(404, "no screen has this share link (it may have been replaced)")
    return device_id


def _share_link(request: Request, code: str) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}/s/{code}"


def _share_status(device_id: str) -> dict:
    d = hub.devices[device_id]
    return {
        "code": d["share_code"],
        "device": device_id,
        "name": d.get("name") or d.get("label") or device_id,
        "room": d.get("room", ""),
        "online": hub.connected(device_id),
        "enabled": not d.get("share_off"),
        "airplay": d.get("airplay", ""),
        "badge": bool(d.get("share_badge")),
        "sharing": hub.shares.get(device_id),
    }


def _start_share(device_id: str, session: dict) -> dict:
    status = _share_status(device_id)
    if not status["enabled"]:
        raise HTTPException(409, "sharing to this screen is turned off in Admin")
    if not status["online"]:
        raise HTTPException(409, "this screen is offline")
    session = {
        "session": secrets.token_hex(8),
        "since": time.time(),
        "code": status["code"],
        **session,
    }
    hub.shares[device_id] = session
    hub.queue_command(device_id, "share", session)
    logger.info("share %s → %s", session["kind"], device_id)
    return session


class ShareStart(BaseModel):
    kind: Literal["screen", "url"]
    url: str = Field(default="", max_length=2000)  # kind url: the page to show
    name: str = Field(default="", max_length=60)  # who is sharing, shown on the screen


class ShareStop(BaseModel):
    session: str = Field(default="", max_length=32)  # "" stops whatever is showing


class ShareSettings(BaseModel):
    enabled: bool | None = None
    new_code: bool = False  # replace the link; the old one stops working
    badge: bool | None = None  # show the link and QR code in the screen's corner


@app.get("/s", include_in_schema=False)
async def share_picker():
    """Short link with no code: pick the screen from a list (or type its code)."""
    return RedirectResponse("/app/#/share")


SHAREABLE_ROLES = {"tv", "signage", "page", "media", "wall", "panel", "feed", "camera"}


@app.get("/api/share")
async def share_screens():
    """Screens someone can share to right now, for the picker at /s: named, on, sharing on."""
    found = []
    for d in hub.devices.values():
        if d.get("share_off") or not d.get("name") or d.get("role") not in SHAREABLE_ROLES:
            continue
        status = _share_status(d["id"])
        if status["online"]:
            found.append(
                {k: status[k] for k in ("code", "name", "room", "airplay")}
                | {"busy": bool(status["sharing"])}
            )
    return sorted(found, key=lambda s: s["name"].lower())


@app.get("/s/{code}", include_in_schema=False)
async def share_page(code: str = SHARE_CODE):
    return RedirectResponse(f"/app/#/share/{code.lower()}")


@app.get("/api/share/{code}")
async def share_status(request: Request, code: str = SHARE_CODE):
    return {**_share_status(_share_target(code)), "link": _share_link(request, code.lower())}


@app.get("/api/share/{code}/qr.svg", include_in_schema=False)
async def share_qr(request: Request, code: str = SHARE_CODE):
    _share_target(code)
    buf = io.BytesIO()
    segno.make(_share_link(request, code.lower()), error="m").save(
        buf, kind="svg", scale=10, border=2, dark="#000", light="#fff", xmldecl=False
    )
    return Response(
        buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-cache"}
    )


@app.post("/api/share/{code}")
async def share_start(start: ShareStart, code: str = SHARE_CODE):
    device_id = _share_target(code)
    session = {"kind": start.kind, "from": start.name.strip()}
    if start.kind == "url":
        if not re.match(r"^https?://[^\s]+$", start.url.strip()):
            raise HTTPException(400, "enter a link starting with http:// or https://")
        session["url"] = start.url.strip()
    return _start_share(device_id, session)


@app.post("/api/share/{code}/image")
async def share_image(request: Request, code: str = SHARE_CODE, name: str = ""):
    device_id = _share_target(code)
    media_type = request.headers.get("content-type", "").split(";")[0]
    if media_type not in SHARE_IMAGE_TYPES:
        raise HTTPException(415, "send a JPEG, PNG, GIF or WebP image")
    data = await request.body()
    if not data or len(data) > MAX_SHARE_IMAGE_BYTES:
        raise HTTPException(413, "image missing or larger than 10 MB")
    status = _share_status(device_id)
    if not status["enabled"] or not status["online"]:
        return _start_share(device_id, {"kind": "image"})  # raises the matching 409
    hub.save_share_media(device_id, data, media_type)
    url = f"/api/share/{status['code']}/media?t={int(time.time() * 1000)}"
    return _start_share(device_id, {"kind": "image", "from": name.strip()[:60], "url": url})


@app.get("/api/share/{code}/media", include_in_schema=False)
async def share_media(code: str = SHARE_CODE):
    device_id = _share_target(code)
    path = hub.share_media_path(device_id)
    if not path.exists():
        raise HTTPException(404, "nothing shared")
    media_type = hub.devices[device_id].get("share_media", "image/jpeg")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@app.post("/api/share/{code}/stop")
async def share_stop(stop: ShareStop, code: str = SHARE_CODE):
    device_id = _share_target(code)
    current = hub.shares.get(device_id)
    if current and stop.session in ("", current["session"]):
        hub.shares.pop(device_id, None)
        hub.queue_command(device_id, "share_stop", {"session": current["session"]})
        return {"stopped": True}
    return {"stopped": False}


@app.put("/api/devices/{device_id}/share", dependencies=[requires("setup")])
async def device_share_settings(s: ShareSettings, request: Request, device_id: str = DEVICE_ID):
    if device_id not in hub.devices:
        raise HTTPException(404, "unknown device")
    current = hub.shares.pop(device_id, None) if s.enabled is False or s.new_code else None
    if current:
        hub.queue_command(device_id, "share_stop", {"session": current["session"]})
    record = hub.set_sharing(device_id, s.enabled, s.new_code, s.badge)
    hub.queue_command(device_id, "refresh", {})  # the screen re-reads its badge and link now
    return {**_share_status(device_id), "link": _share_link(request, record["share_code"])}


# ---------- live camera feeds (#/camera on the host, #/feed/<camera> on any screen) ----------
# Video goes peer to peer over the office network (WebRTC); the server only relays the setup
# messages. The camera page is peer "camera"; each viewer picks a random peer id.

PEER = r"^[A-Za-z0-9._-]{2,64}$"


class Signal(BaseModel):
    from_: str = Field(alias="from", pattern=PEER)
    to: str = Field(pattern=r"^\*$|^[A-Za-z0-9._-]{2,64}$")
    type: Literal["join", "offer", "answer", "ice", "bye"]
    data: dict | str | None = None


@app.get("/api/rtc/config")
async def rtc_config():
    """ICE servers for the browser's RTCPeerConnection (XOS_ICE_SERVERS)."""
    servers = []
    for url in filter(None, (u.strip() for u in settings.XOS_ICE_SERVERS.split(","))):
        server: dict = {"urls": url}
        if url.startswith(("turn:", "turns:")):
            server |= {
                "username": settings.XOS_TURN_USERNAME,
                "credential": settings.XOS_TURN_PASSWORD.get_secret_value(),
            }
        servers.append(server)
    return {"iceServers": servers}


@app.get("/api/rtc/{channel}/peers")
async def rtc_peers(channel: str = Path(pattern=PEER)):
    return {"peers": hub.rtc_peers(channel), "camera": "camera" in hub.rtc_peers(channel)}


@app.post("/api/rtc/{channel}/signal")
async def rtc_signal(sig: Signal, channel: str = Path(pattern=PEER)):
    msg = {"from": sig.from_, "to": sig.to, "type": sig.type, "data": sig.data}
    delivered = hub.rtc_send(channel, msg)
    if not delivered and sig.to != "*":
        raise HTTPException(409, f"{sig.to} isn't connected")
    return {"delivered": delivered}


@app.get("/api/rtc/{channel}/peer/{peer}")
async def rtc_stream(
    request: Request, channel: str = Path(pattern=PEER), peer: str = Path(pattern=PEER)
):
    """Server-Sent Events: signalling messages for one peer of a feed."""
    queue = hub.rtc_join(channel, peer)

    async def stream():
        try:
            yield "retry: 3000\n: connected\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.rtc_leave(channel, peer, queue)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


# ---------- media library (Admin → Content → Media; served at /media/<file>) ----------


def _with_use(item: dict) -> dict:
    used = [
        p["name"]
        for p in pages.all()
        if p.get("kind") == "slides" and any(i["media"] == item["id"] for i in p["items"])
    ]
    return {**item, "used_by": used}


@app.get("/api/media")
async def list_media():
    return {"items": [_with_use(m) for m in media.all()], **media.usage()}


@app.post("/api/media", dependencies=[requires("content.edit")])
async def upload_media(request: Request, name: str = "", filename: str = ""):
    """Upload one photo or video: the raw file as the request body, with its Content-Type."""
    media_type = type_for(filename or name, request.headers.get("content-type", ""))
    try:
        return await media.save(
            name or re.sub(r"\.[A-Za-z0-9]+$", "", filename), media_type, request.stream()
        )
    except MediaError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


class MediaImport(BaseModel):
    url: str = Field(max_length=2000)
    name: str = Field(default="", max_length=80)


@app.post("/api/media/import", dependencies=[requires("content.edit")])
async def import_media(m: MediaImport):
    """Download a photo or video from a link into the library."""
    try:
        return await media.save_from_url(m.url.strip(), m.name.strip())
    except MediaError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


class MediaRename(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@app.put("/api/media/{media_id}", dependencies=[requires("content.edit")])
async def rename_media(m: MediaRename, media_id: str):
    try:
        return media.rename(media_id, m.name)
    except MediaError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@app.delete("/api/media/{media_id}", dependencies=[requires("content.edit")])
async def delete_media(media_id: str):
    """Delete an upload. Slideshows that used it skip it from then on."""
    if not media.delete(media_id):
        raise HTTPException(
            409 if media.get(media_id) else 404,
            "built-in media can't be deleted" if media.get(media_id) else "unknown media",
        )
    return {"ok": True}


# ---------- pages (Admin → Content, kiosk route #/page/<id>) ----------


class PlaylistItem(BaseModel):
    page: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")
    seconds: int = Field(default=20, ge=5, le=3600)


class Slide(BaseModel):
    media: str = Field(pattern=r"^[a-z0-9-]{3,48}$")
    seconds: int = Field(default=10, ge=2, le=3600)  # photos; videos play to the end


class Announcement(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=600)
    tag: str = Field(default="", max_length=30)
    style: Literal["dark", "gradient", "light"] = "dark"


class PageIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    html: str | None = None
    url: str | None = Field(default=None, max_length=2000)
    playlist: list[PlaylistItem] | None = Field(default=None, max_length=50)
    slides: list[Slide] | None = Field(default=None, max_length=200)
    fit: Literal["cover", "contain"] = "cover"
    announcement: Announcement | None = None


def _save_page(page_id: str | None, page: PageIn) -> dict:
    try:
        return pages.upsert(
            page_id,
            page.name,
            page.html,
            page.url,
            [i.model_dump() for i in page.playlist] if page.playlist is not None else None,
            [i.model_dump() for i in page.slides] if page.slides is not None else None,
            page.announcement.model_dump() if page.announcement else None,
            page.fit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/pages")
async def list_pages():
    return pages.all()


@app.get("/api/pages/{page_id}")
async def get_page(page_id: str):
    page = pages.get(page_id)
    if not page:
        raise HTTPException(404, "unknown page")
    if page.get("kind") == "slides":
        page = {**page, "media": {i["media"]: media.get(i["media"]) for i in page["items"]}}
    return page


@app.post("/api/pages", dependencies=[requires("content.edit")])
async def create_page(page: PageIn):
    return _save_page(None, page)


@app.put("/api/pages/{page_id}", dependencies=[requires("content.edit")])
async def update_page(page: PageIn, page_id: str):
    if pages.get(page_id) is None:
        raise HTTPException(404, "unknown page")
    if page_id not in pages.items:
        raise HTTPException(409, "built-in pages can't be changed")
    return _save_page(page_id, page)


@app.delete("/api/pages/{page_id}", dependencies=[requires("content.edit")])
async def delete_page(page_id: str):
    if page_id not in pages.items:
        raise HTTPException(
            409 if pages.get(page_id) else 404,
            "built-in pages can't be deleted" if pages.get(page_id) else "unknown page",
        )
    pages.delete(page_id)
    return {"ok": True}


@app.get("/pages/{page_id}/", include_in_schema=False)
async def serve_page(page_id: str):
    html = pages.html(page_id)
    if html is None:
        raise HTTPException(404, "no uploaded HTML for this page")
    # Shown inside a sandboxed iframe (no same-origin), so the page can't call the Xos API.
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})


# ---------- site and kiosk views ----------
# Only these paths are served; the rest of the repo (source, config) is not.

app.mount("/app", StaticFiles(directory=REPO_ROOT / "app", html=True), name="kiosk")
app.mount("/static", StaticFiles(directory=REPO_ROOT / "static"), name="static")
app.mount("/media", StaticFiles(directory=media.dir), name="media")


@app.get("/", include_in_schema=False)
async def home():
    return RedirectResponse("/app/")


@app.get("/app", include_in_schema=False)
async def app_slash():
    return RedirectResponse("/app/")


@app.get("/quest", include_in_schema=False)
async def quest_setup_link():
    return RedirectResponse("/app/quest/", 307)


@app.get("/quest/install", include_in_schema=False)
async def quest_install_guide():
    return FileResponse(REPO_ROOT / "app" / "quest" / "install.html", media_type="text/html")


@app.get("/quest/xos-quest.apk", include_in_schema=False)
async def quest_apk():
    """An Xos Quest APK, if the operator packaged one (Meta Bubblewrap around /app/quest/) and put
    it at quest/xos-quest.apk. None is checked in."""
    apk = REPO_ROOT / "quest" / "xos-quest.apk"
    if not apk.is_file():
        raise HTTPException(404, "No Xos Quest APK on this server: package /app/quest/ first")
    return FileResponse(
        apk, media_type="application/vnd.android.package-archive", filename="xos-quest.apk"
    )


@app.get("/setup", include_in_schema=False)
async def setup_screen():
    """Short link for a new screen or iPad: shows its pairing code (Admin → Screens → Add a screen)."""
    return RedirectResponse("/app/?kiosk=1#/setup")


@app.get("/styles.css", include_in_schema=False)
async def styles():
    return FileResponse(REPO_ROOT / "styles.css", media_type="text/css")


@app.get("/main.js", include_in_schema=False)
async def site_js():
    return FileResponse(REPO_ROOT / "main.js", media_type="text/javascript")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(REPO_ROOT / "static" / "favicon.png", media_type="image/png")


@app.get("/firetv/xos-tv.apk", include_in_schema=False)
async def firetv_apk():
    """The Xos TV APK, if this server has one (build it from firetv/; it isn't checked in)."""
    if not (REPO_ROOT / "firetv" / "xos-tv.apk").is_file():
        raise HTTPException(404, "No Xos TV APK on this server: build one from firetv/")
    return FileResponse(
        REPO_ROOT / "firetv" / "xos-tv.apk",
        media_type="application/vnd.android.package-archive",
        filename="xos-tv.apk",
    )


# ---------- Xos Linux nodes (Raspberry Pi / mini PC) install from here ----------
# curl -fsSL https://xos.example.org/os/install.sh | sudo bash -s -- --role tv --room atrium …
# The installer fetches the rest (os/, app/, static/) as one tarball, so nothing is copied by hand,
# and each node re-checks nightly and updates itself when the bundle's hash changes.

_bundle: tuple[bytes, str] | None = None


def _node_bundle() -> tuple[bytes, str]:
    global _bundle
    if _bundle is None:
        buf = io.BytesIO()
        skip = {"__pycache__", "test", "node_modules", "media"}

        def clean(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
            # Same files give the same bytes (and hash) on every deploy, so nodes only update on change.
            if skip & set(ti.name.split("/")):
                return None
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, "root", "root"
            return ti

        with tarfile.open(fileobj=buf, mode="w") as tar:
            for top in ("os", "app", "static"):
                tar.add(REPO_ROOT / top, arcname=top, filter=clean)
        data = gzip.compress(buf.getvalue(), mtime=0)
        _bundle = (data, hashlib.sha256(data).hexdigest())
    return _bundle


@app.get("/os/install.sh", include_in_schema=False)
async def node_installer():
    return FileResponse(REPO_ROOT / "os" / "install.sh", media_type="text/x-shellscript")


# ---------- Xos Linux boxes: set up from Admin (src/app/appliances.py, README.md) ----------


class ApplianceIn(BaseModel):
    role: Literal["room", "display"]
    room: str = Field(default="", pattern=r"^$|^[a-z0-9-]{1,40}$")
    name: str = Field(min_length=1, max_length=60)
    show: str = Field(default="", pattern=r"^$|^[a-z]+(/[a-z0-9-]+){0,2}$")


def _appliance_view(a: dict) -> dict:
    base = settings.XOS_PUBLIC_URL.rstrip("/")
    online = any(x["id"] == a["device_id"] and x["online"] for x in hub.device_list(settings.DEVICE_ONLINE_S))
    return {**a, "online": online, "preseed_url": f"{base}/os/p/{a['code']}",
            "install": f"curl -fsSL {base}/os/appliance/install.sh | sudo bash -s -- --enroll {a['code']}"}


@app.get("/api/appliances", dependencies=[requires("setup")])
async def list_appliances():
    """Every Linux box set up from Admin, with its setup code and maintenance login (team only)."""
    return [_appliance_view(a) for a in sorted(appliances.items.values(), key=lambda a: -a["created"])]


@app.post("/api/appliances")
async def create_appliance(body: ApplianceIn, caller: Caller = requires("setup")):
    if body.role == "room" and body.room not in rooms.items:
        raise HTTPException(400, "A room computer needs one of the rooms in Admin → Rooms")
    a = appliances.create(body.role, body.room, body.name.strip(), body.show, caller.email)
    _pending_box(a)
    return _appliance_view(a)


def _pending_box(a: dict) -> None:
    """It shows up in Admin → Screens right away, named and in its room, until it checks in."""
    room = a["role"] == "room"
    hub.heartbeat({"id": a["device_id"], "client": "xos-appliance-pending",
                   "role": "roompc" if room else (a["show"].split("/")[0] or "setup"),
                   "arg": a["room"] if room else "/".join(a["show"].split("/")[1:])})
    hub.devices[a["device_id"]]["last_seen"] = 0  # not online until the box itself checks in
    hub.rename(a["device_id"], a["name"])
    if a["room"]:
        hub.set_room(a["device_id"], a["room"])


@app.post("/api/appliances/{device_id}/renew", dependencies=[requires("setup")])
async def renew_appliance(device_id: str = DEVICE_ID):
    """A new setup code, to reflash or reinstall the same box (it keeps its name and room)."""
    if device_id not in appliances.items:
        raise HTTPException(404, "unknown box")
    hub.revoke_key(device_id)
    return _appliance_view(appliances.renew(device_id))


@app.delete("/api/appliances/{device_id}", dependencies=[requires("setup")])
async def delete_appliance(device_id: str = DEVICE_ID):
    if not appliances.delete(device_id):
        raise HTTPException(404, "unknown box")
    hub.forget(device_id)
    return {"ok": True}


class WifiIn(BaseModel):
    ssid: str = Field(max_length=64)
    psk: str = Field(default="", max_length=63)


class WifiList(BaseModel):
    networks: list[WifiIn] = Field(max_length=8)


@app.get("/api/appliances/wifi", dependencies=[requires("setup")])
async def get_wifi():
    """The Wi-Fi networks new boxes join, preferred first (team only: it has the passwords)."""
    return {"networks": wifi.networks}


@app.put("/api/appliances/wifi", dependencies=[requires("setup")])
async def put_wifi(body: WifiList):
    try:
        return {"networks": wifi.set([n.model_dump() for n in body.networks])}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class EnrollIn(BaseModel):
    code: str = Field(max_length=20)


@app.post("/api/appliances/enroll")
async def enroll_appliance(body: EnrollIn, request: Request):
    """The box redeems its one-time setup code: its config, maintenance password and device key."""
    a = appliances.redeem(body.code, _client_ip(request))
    if not a:
        raise HTTPException(404, "That setup code isn't valid, was used already or has expired (Admin → Screens → Linux boxes)")
    hub.device_keys.pop(a["device_id"], None)
    key = hub.issue_key(a["device_id"])
    if a.get("from_box"):
        hub.forget(a["from_box"])  # the box's screen entry: it's the room computer now
    logger.info("Xos box %s enrolled from %s", a["device_id"], _client_ip(request))
    return {"device_id": a["device_id"], "device_key": key, "role": a["role"], "room": a["room"],
            "name": a["name"], "show": a["show"], "hostname": a["hostname"], "user": a["user"],
            "password": a["password"], "server": settings.XOS_PUBLIC_URL.rstrip("/"),
            "wifi": wifi.networks}


class HandoffIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]{4,64}$")
    secret: str = Field(min_length=32, max_length=128)


@app.post("/api/appliances/handoff")
async def appliance_handoff(body: HandoffIn):
    """An Xos box's helper checks in (os/appliance/xos-box): registers its secret the first time,
    and collects the setup code if Admin paired it as a room computer."""
    if not boxes.check(body.id, body.secret):
        raise HTTPException(403, "This box's secret doesn't match; reinstall it from the Xos USB")
    a = appliances.handoff(body.id)
    if not a:
        return {"code": ""}
    logger.info("Xos box %s collects its room computer setup (%s)", body.id, a["device_id"])
    return {"code": a["code"], "role": a["role"], "room": a["room"]}


@app.get("/os/p/{code}", include_in_schema=False)
async def appliance_preseed(code: str):
    """The Debian 13 installer's preseed for one box (no secrets; the code is redeemed later)."""
    a = appliances.by_code(code)
    if not a:
        raise HTTPException(404, "unknown or used setup code")
    site = rooms.items.get(a["room"], {}).get("site", "")
    return Response(preseed(a, settings.XOS_PUBLIC_URL.rstrip("/"), site), media_type="text/plain")


class SshKeysIn(BaseModel):
    keys: str = Field(max_length=40000)


@app.get("/api/appliances/ssh-keys", dependencies=[requires("setup")])
async def get_ssh_keys():
    """Keys for every Xos box's maintenance login: the ones in Git and the ones added here."""
    return {"repo": _repo_ssh_keys(), "keys": ssh_keys.keys}


@app.put("/api/appliances/ssh-keys", dependencies=[requires("setup")])
async def put_ssh_keys(body: SshKeysIn):
    try:
        return {"repo": _repo_ssh_keys(), "keys": ssh_keys.set(body.keys)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _repo_ssh_keys() -> list[str]:
    try:
        return ssh_keys_in((REPO_ROOT / "os" / "appliance" / "ssh" / "authorized_keys").read_text())
    except (OSError, ValueError):
        return []


@app.get("/os/appliance/authorized_keys", include_in_schema=False)
async def appliance_authorized_keys():
    """What every Xos box's xosadmin accepts (public keys only), fetched by os/appliance/xos-keys."""
    keys = list(dict.fromkeys(_repo_ssh_keys() + ssh_keys.keys))
    return Response("".join(k + "\n" for k in keys), media_type="text/plain",
                    headers={"Cache-Control": "no-store"})


@app.get("/os/appliance/install.sh", include_in_schema=False)
async def appliance_installer():
    """The Xos Linux appliance (display or room computer): README.md."""
    return FileResponse(REPO_ROOT / "os" / "appliance" / "install.sh", media_type="text/x-shellscript")


@app.get("/os/mac/xos-room.py", include_in_schema=False)
async def mac_room_agent():
    """A Mac as a meeting room's computer (Google Meet in Chrome, driven by the room iPad)."""
    return FileResponse(REPO_ROOT / "os" / "mac" / "xos-room.py", media_type="text/x-python")


@app.get("/os/xos-node.tar.gz", include_in_schema=False)
async def node_bundle():
    data, digest = _node_bundle()
    return Response(data, media_type="application/gzip", headers={"X-Xos-Bundle": digest})


@app.get("/os/xos-node.sha256", include_in_schema=False)
async def node_bundle_hash():
    return Response(_node_bundle()[1] + "\n", media_type="text/plain")


app.include_router(build_demo_router(demos, media, requires))


# ---------- MCP (/mcp): every Admin action as an agent tool (app/mcp_server.py) ----------
# Mounted at the root, last: the MCP app owns the exact /mcp path, and Mount("/") matches
# anything, so every other route must come before it.
from app.mcp_server import build_mcp_app  # noqa: E402

mcp_app = build_mcp_app()
app.mount("/", mcp_app, name="mcp")
