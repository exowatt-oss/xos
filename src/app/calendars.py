"""Room calendars: what's booked in each room, from its Google Workspace resource calendar.

A room's calendar is its resource address (Admin → Integrations → Google Calendar, else
catalog.ROOM_CALENDARS). The server reads it through the Calendar API as a service account with
domain-wide delegation (scope calendar.readonly) acting as XOS_CALENDAR_USER: resource calendars
are readable by everyone in your organization, so any ordinary account works. Room remotes show what's on
now and next, with one-tap Join from the booking's Meet (or Zoom) link.

Secrets: GOOGLE_SERVICE_ACCOUNT_JSON (the service account's JSON key). See README.md.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from datetime import datetime

import httpx
import jwt

from app.meeting_links import meeting_link

SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CACHE_S = 60  # every room iPad asks; Google sees one read per room a minute
JOIN_LINK = re.compile(r'https://[^\s<>"\']+')


class CalendarError(Exception):
    pass


def _ts(when: dict) -> float | None:
    """An event's start or end as a Unix time; None for an all-day date."""
    if "dateTime" not in when:
        return None
    return datetime.fromisoformat(when["dateTime"].replace("Z", "+00:00")).timestamp()


def booking(e: dict, calendar_id: str) -> dict | None:
    """A Calendar API event as a room booking, or None if the room isn't really booked by it."""
    if e.get("status") == "cancelled":
        return None
    room = next((a for a in e.get("attendees", []) if a.get("email") == calendar_id), None)
    if room and room.get("responseStatus") == "declined":
        return None
    start, end = _ts(e.get("start", {})), _ts(e.get("end", {}))
    if start is None or end is None:  # all-day: a hold, not a meeting
        return None
    links = [
        p.get("uri", "")
        for p in (e.get("conferenceData") or {}).get("entryPoints", [])
        if p.get("entryPointType") == "video"
    ]
    join_url = ""
    for candidate in JOIN_LINK.findall(
        html.unescape(
            " ".join(
                [e.get("hangoutLink", ""), *links, e.get("location", ""), e.get("description", "")]
            )
        )
    ):
        try:
            join_url = meeting_link(candidate.rstrip(".,)"))["url"].replace("/wc/join/", "/j/")
            break
        except ValueError:
            continue
    # Some invites provide a code instead of a clickable conference URL. Only accept
    # explicitly labelled Zoom IDs, or Google's distinctive code format, never arbitrary digits.
    text = html.unescape(
        re.sub(r"<[^>]*>", " ", " ".join([e.get("location", ""), e.get("description", "")]))
    )
    if not join_url:
        code = re.search(r"\b[a-z]{3}-[a-z]{4}-[a-z]{3}\b", text, re.I)
        zoom_id = re.search(r"\b(?:Zoom\s+)?Meeting\s+ID\s*:\s*(\d[\d -]{7,18}\d)\b", text, re.I)
        pwd = re.search(r"\b(?:Passcode|Password)\s*:\s*([\w!@#$%^&*.-]{1,40})", text, re.I)
        try:
            if code:
                join_url = meeting_link(code[0])["url"]
            elif zoom_id:
                join_url = meeting_link(zoom_id[1], pwd[1] if pwd else "")["url"].replace(
                    "/wc/join/", "/j/"
                )
        except ValueError:
            pass
    # Zoom sometimes puts the passcode beside a bare join URL instead of in its query.
    if join_url and "zoom." in join_url and "pwd=" not in join_url:
        pwd = re.search(r"\b(?:Passcode|Password)\s*:\s*([\w!@#$%^&*.-]{1,40})", text, re.I)
        if pwd:
            join_url = meeting_link(join_url, pwd[1])["url"].replace("/wc/join/", "/j/")
    private = e.get("visibility") in ("private", "confidential")
    org = e.get("organizer") or {}
    return {
        "id": e.get("id", ""),
        "title": "Private meeting" if private else (e.get("summary") or "Busy"),
        "start": start,
        "end": end,
        "organizer": ""
        if private
        else (org.get("displayName") or org.get("email", "").split("@")[0]),
        "join_url": join_url,
    }


class Calendars:
    def __init__(self, settings, transport: httpx.AsyncBaseTransport | None = None, auth=None):
        self.s = settings
        self.transport = transport  # tests inject a mock
        self.auth = auth
        self._token: tuple[str, float] | None = None
        self._cache: dict[str, tuple[float, list[dict] | None, str]] = {}
        self.refreshed_at: dict[str, float] = {}
        self._reads: dict[str, asyncio.Lock] = {}

    @property
    def configured(self) -> bool:
        return bool(
            (self.auth and self.auth.connected)
            or (self.s.GOOGLE_SERVICE_ACCOUNT_JSON.get_secret_value() and self.s.XOS_CALENDAR_USER)
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self.transport)

    async def _access_token(self) -> str:
        if self.auth and self.auth.connected:
            return await self.auth.access_token()
        if self._token and self._token[1] > time.time():
            return self._token[0]
        try:
            key = json.loads(self.s.GOOGLE_SERVICE_ACCOUNT_JSON.get_secret_value())
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": key["client_email"],
                    "sub": self.s.XOS_CALENDAR_USER,
                    "scope": SCOPE,
                    "aud": self.s.GOOGLE_TOKEN_URL,
                    "iat": now,
                    "exp": now + 3600,
                },
                key["private_key"],
                algorithm="RS256",
                headers={"kid": key.get("private_key_id", "")},
            )
        except (ValueError, KeyError, TypeError) as e:
            raise CalendarError("GOOGLE_SERVICE_ACCOUNT_JSON isn't a service account key") from e
        async with self._client() as c:
            r = await c.post(
                self.s.GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        if r.status_code != 200:
            if "unauthorized_client" in r.text:
                raise CalendarError(
                    "the service account isn't allowed to read calendars yet: a Google "
                    "Workspace admin adds its client ID under Domain-wide delegation "
                    f"with scope {SCOPE}"
                )
            raise CalendarError(
                f"Google Calendar sign-in failed ({r.status_code}). Reconnect the account."
            )
        body = r.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)) - 60)
        return self._token[0]

    async def bookings(self, calendar_id: str) -> list[dict]:
        """Today's bookings that haven't ended, soonest first (cached for a minute)."""
        async with self._reads.setdefault(calendar_id, asyncio.Lock()):
            now = time.time()
            hit = self._cache.get(calendar_id)
            if hit and hit[0] > now:
                if hit[1] is None:
                    raise CalendarError(hit[2])
                return [b for b in hit[1] if b["end"] > now]
            try:
                items = await self._fetch(calendar_id, now)
            except (CalendarError, httpx.HTTPError) as exc:
                message = (
                    str(exc)
                    if isinstance(exc, CalendarError)
                    else "Google Calendar is temporarily unreachable. Try again shortly."
                )
                self._cache[calendar_id] = (now + CACHE_S, None, message)
                if isinstance(exc, CalendarError):
                    raise
                raise CalendarError(message) from exc
            out = sorted(
                (b for e in items if (b := booking(e, calendar_id))), key=lambda b: b["start"]
            )
            self._cache[calendar_id] = (now + CACHE_S, out, "")
            self.refreshed_at[calendar_id] = time.time()
            return [b for b in out if b["end"] > now]

    async def _fetch(self, calendar_id: str, now: float) -> list[dict]:
        token = await self._access_token()
        params = {
            "timeMin": datetime.fromtimestamp(now - 12 * 3600).astimezone().isoformat(),
            "timeMax": datetime.fromtimestamp(now + 24 * 3600).astimezone().isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "50",
        }
        items = []
        page_tokens = set()
        async with self._client() as c:
            while True:
                r = await c.get(
                    f"{self.s.GOOGLE_CALENDAR_API}/calendars/{calendar_id}/events",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code == 404:
                    raise CalendarError(f"{self.s.XOS_CALENDAR_USER} can't see {calendar_id}")
                if r.status_code >= 400:
                    raise CalendarError(
                        f"Google Calendar request failed ({r.status_code}). Check calendar access."
                    )
                body = r.json()
                items.extend(body.get("items", []))
                next_page = body.get("nextPageToken")
                if not next_page:
                    return items
                if next_page in page_tokens:
                    raise CalendarError(
                        "Google Calendar returned an incomplete schedule. Try again."
                    )
                page_tokens.add(next_page)
                params["pageToken"] = next_page
