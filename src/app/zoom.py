"""Zoom Rooms through Zoom's cloud API (Server-to-Server OAuth).

Xos rooms are mapped to Zoom Rooms in Admin → Integrations. A mapped room's controller (tablet remote,
table controller) then sends real in-meeting controls: PATCH /rooms/{id}/events. Without
credentials everything stays a logged dry run. Admin can also create a room's Zoom Room and read
its activation code, which signs in the Zoom Rooms app on the room's PC.

Scopes: room:read:admin, room:write:admin (granular: zoom_rooms:read:list_rooms:admin,
zoom_rooms:read:room:admin, zoom_rooms:write:room:admin, zoom_rooms:update:room_control:admin).
The account needs a Zoom Rooms licence for each room.
"""

import json
import re
import time
from pathlib import Path
from urllib.parse import unquote

import httpx

# Xos controller action -> Zoom Rooms control method (documented for PATCH /rooms/{id}/events).
METHODS = {
    "join": "zoomroom.meeting_join",
    "meet_now": "zoomroom.meeting_join",  # no meeting number: Zoom starts an instant meeting
    "leave": "zoomroom.meeting_leave",
    "end": "zoomroom.meeting_end",
    "mute": "zoomroom.mute",
    "unmute": "zoomroom.unmute",
    "camera_off": "zoomroom.video_mute",
    "camera_on": "zoomroom.video_unmute",
    "share_start": "zoomroom.share_content_start",
    "share_stop": "zoomroom.share_content_stop",
    "volume": "zoomroom.volume_level",
    "restart": "zoomroom.restart",
}

ROOMS_CACHE_S = 5

MEET_CODE = re.compile(r"\b([a-z]{3}-[a-z]{4}-[a-z]{3})\b")


def parse_meeting(text: str, passcode: str = "") -> dict:
    """What someone typed or pasted -> how the room joins it.

    Takes a Zoom meeting ID, a Zoom link (zoom.us/j/<id>?pwd=…), or a Google Meet link or code
    (abc-defg-hij), so invites people already have keep working in a Zoom Room.
    """
    t = text.strip()
    meet = MEET_CODE.search(t.lower())
    if meet and ("meet.google.com" in t.lower() or not re.search(r"\d", t)):
        return {"kind": "meet", "url": f"https://meet.google.com/{meet.group(1)}"}
    link = re.search(r"/(?:j|w|s|wc/join)/(\d{9,11})(?=[/?#\s]|$)", t)
    pwd = re.search(r"[?&]pwd=([^&#\s]+)", t)
    link_passcode = unquote(pwd.group(1)) if pwd else ""
    digits = link.group(1) if link else re.sub(r"[\s-]", "", t)
    if not re.fullmatch(r"\d{9,11}", digits):
        raise ZoomError("Enter a Zoom meeting ID (9 to 11 digits), a Zoom link or a Google Meet code", 400)
    return {"kind": "zoom", "number": digits, "passcode": passcode or link_passcode}


class ZoomError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class Zoom:
    def __init__(self, settings, data_dir: Path, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self.transport = transport  # tests inject a mock
        self.map_path = data_dir / "zoom.json"
        try:
            self.rooms: dict[str, str] = json.loads(self.map_path.read_text()).get("rooms", {})
        except (FileNotFoundError, ValueError):
            self.rooms = {}
        self._token: tuple[str, float] | None = None
        self._rooms_cache: tuple[list[dict], float] | None = None
        self.last_error = ""

    @property
    def configured(self) -> bool:
        s = self.s
        return bool(s.ZOOM_ACCOUNT_ID and s.ZOOM_CLIENT_ID and s.ZOOM_CLIENT_SECRET.get_secret_value())

    def map_room(self, room: str, zoom_room_id: str | None) -> dict[str, str]:
        if zoom_room_id:
            self.rooms[room] = zoom_room_id
        else:
            self.rooms.pop(room, None)
        self.map_path.parent.mkdir(parents=True, exist_ok=True)
        self.map_path.write_text(json.dumps({"rooms": self.rooms}))
        return self.rooms

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self.transport)

    async def _access_token(self) -> str:
        if self._token and self._token[1] > time.time():
            return self._token[0]
        if not self.configured:
            raise ZoomError("Zoom isn't connected yet: add the Zoom app credentials", 409)
        async with self._client() as c:
            r = await c.post(
                self.s.ZOOM_OAUTH_URL,
                params={"grant_type": "account_credentials", "account_id": self.s.ZOOM_ACCOUNT_ID},
                auth=(self.s.ZOOM_CLIENT_ID, self.s.ZOOM_CLIENT_SECRET.get_secret_value()),
            )
        if r.status_code != 200:
            self.last_error = f"Zoom sign-in failed ({r.status_code}): {r.text[:200]}"
            raise ZoomError(self.last_error, 502)
        body = r.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)) - 60)
        return self._token[0]

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        token = await self._access_token()
        async with self._client() as c:
            r = await c.request(method, self.s.ZOOM_API_BASE + path,
                                headers={"Authorization": f"Bearer {token}"}, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("message", r.text)
            except ValueError:
                detail = r.text
            self.last_error = f"Zoom {r.status_code}: {detail[:200]}"
            raise ZoomError(self.last_error, 502 if r.status_code >= 500 else 400)
        self.last_error = ""
        return r

    async def list_rooms(self) -> list[dict]:
        # Every room iPad asks for its room's status every few seconds, and Zoom only lists them
        # all (rate limit MEDIUM): answer from a few seconds' cache.
        if self._rooms_cache and self._rooms_cache[1] > time.monotonic():
            return self._rooms_cache[0]
        rooms = await self._fetch_rooms()
        self._rooms_cache = (rooms, time.monotonic() + ROOMS_CACHE_S)
        return rooms

    async def _fetch_rooms(self) -> list[dict]:
        rooms, token = [], ""
        while True:
            params = {"page_size": 300, **({"next_page_token": token} if token else {})}
            body = (await self._call("GET", "/rooms", params=params)).json()
            rooms += [{"id": r.get("id") or r.get("room_id"), "name": r.get("name"),
                       "status": r.get("status"), "location_id": r.get("location_id")}
                      for r in body.get("rooms", [])]
            token = body.get("next_page_token") or ""
            if not token:
                return rooms

    async def room_status(self, zoom_room_id: str) -> dict | None:
        return next((r for r in await self.list_rooms() if r["id"] == zoom_room_id), None)

    async def create_room(self, name: str) -> dict:
        """A new Zoom Room. It takes one of the account's Zoom Rooms licences."""
        try:
            body = (await self._call("POST", "/rooms", json={"name": name, "type": "ZoomRoom"})).json()
        except ZoomError as exc:
            if "subscription" in str(exc).lower():
                raise ZoomError("your Zoom account has no Zoom Rooms licence free. Add one in "
                                "the Zoom web portal (Billing), then press Set up Zoom Room again.", 409) from exc
            raise
        self._rooms_cache = None
        return {"id": body.get("id") or body.get("room_id"), "name": body.get("name", name)}

    async def activation_code(self, zoom_room_id: str, fresh: bool = False) -> str:
        """The code that signs in the Zoom Rooms app on the room's PC. Codes expire (about 10
        days); `fresh` asks Zoom for a new one."""
        params = {"regenerate_activation_code": "true"} if fresh else None
        body = (await self._call("GET", f"/rooms/{zoom_room_id}", params=params)).json()
        return (body.get("basic") or {}).get("activation_code") or body.get("activation_code") or ""

    async def control(self, zoom_room_id: str, action: str, meeting: str = "",
                      passcode: str = "", volume: int | None = None) -> dict:
        method = METHODS.get(action)
        if not method:
            raise ZoomError(f"{action} isn't available through Zoom's cloud API", 400)
        body: dict = {"method": method}
        if action == "join":
            if not meeting.strip():
                raise ZoomError("Enter a meeting ID or link to join", 400)
            m = parse_meeting(meeting, passcode)
            if m["kind"] == "meet":
                body = {"method": "zoomroom.thirdparty_meeting_join",
                        "params": {"meeting_source_type": "GOOGLE_MEET", "join_type": "url", "join_url": m["url"]}}
            else:
                body["params"] = {"meeting_number": m["number"],
                                  **({"passcode": m["passcode"]} if m["passcode"] else {}), "force_accept": True}
        elif action == "meet_now":
            body["params"] = {"force_accept": True}
        elif action == "volume":
            if volume is None:
                raise ZoomError("Choose a volume from 0 to 100", 400)
            body["params"] = {"volume_level": max(0, min(100, int(volume)))}
        await self._call("PATCH", f"/rooms/{zoom_room_id}/events", json=body)
        return {"sent": True, "method": body["method"]}
