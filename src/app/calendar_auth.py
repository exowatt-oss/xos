"""One-time Google consent for room calendars; refresh credentials stay on the server."""

import asyncio
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

from app.calendars import SCOPE, CalendarError


class CalendarAuth:
    def __init__(self, settings, transport=None):
        self.s = settings
        self.transport = transport
        self.pending = {}
        self.lock = asyncio.Lock()
        self.token = None
        # The last refresh, for Admin: when it worked, and why it didn't (never Google's raw reply).
        self.last_ok = 0.0
        self.last_error = ""

    @property
    def path(self):
        return self.s.XOS_DATA_DIR / "credentials" / "google-calendar.json"

    @property
    def available(self):
        return bool(
            self.s.GOOGLE_CALENDAR_CLIENT_ID
            and self.s.GOOGLE_CALENDAR_CLIENT_SECRET.get_secret_value()
        )

    @property
    def connected(self):
        return self.available and self.path.is_file()

    @property
    def redirect_uri(self):
        return self.s.XOS_PUBLIC_URL.rstrip("/") + "/api/calendar/oauth/callback"

    def start(self, by=""):
        if not self.available:
            raise CalendarError("Configure the Google Calendar OAuth client in app secrets first.")
        now = time.time()
        self.pending = {s: p for s, p in self.pending.items() if p["expires"] > now}
        if len(self.pending) >= 20:
            raise CalendarError("Too many pending Google connections. Try again in ten minutes.")
        state = secrets.token_urlsafe(32)
        self.pending[state] = {"expires": now + 600, "by": by}
        return state, "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": self.s.GOOGLE_CALENDAR_CLIENT_ID,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent select_account",
                "state": state,
            }
        )

    def consume(self, state, cookie):
        """The pending connection (who started it) for a matching, unexpired state; else None."""
        if not state or not cookie or not secrets.compare_digest(state, cookie):
            return None
        pending = self.pending.pop(state, None)
        return pending if pending and pending["expires"] > time.time() else None

    @property
    def info(self):
        """Which Google account the room calendars are read as, and who connected it when."""
        try:
            saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return {k: saved[k] for k in ("account", "connected_at", "connected_by") if k in saved}

    async def exchange(self, **data):
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as client:
            response = await client.post(
                self.s.GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.s.GOOGLE_CALENDAR_CLIENT_ID,
                    "client_secret": self.s.GOOGLE_CALENDAR_CLIENT_SECRET.get_secret_value(),
                    **data,
                },
            )
        if response.status_code != 200:
            # Google's raw responses may contain credentials or private account information: only
            # its error code is read.
            try:
                revoked = response.json().get("error") == "invalid_grant"
            except ValueError:
                revoked = False
            raise CalendarError(
                "Google revoked Xos's calendar access (the account's access was removed or its "
                "password changed, or the Google app is still in Testing). Reconnect in Integrations."
                if revoked
                else "Google Calendar authorization failed. Reconnect in Integrations."
            )
        return response.json()

    async def account(self, access_token):
        """The connected account's address: its primary calendar's id (calendar.readonly)."""
        try:
            async with httpx.AsyncClient(timeout=15, transport=self.transport) as client:
                response = await client.get(
                    self.s.GOOGLE_CALENDAR_API + "/calendars/primary",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            return response.json().get("id", "") if response.status_code == 200 else ""
        except (httpx.HTTPError, ValueError):
            return ""

    def _save(self, saved):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(saved, file)
        tmp.replace(self.path)

    async def finish(self, code, by=""):
        body = await self.exchange(
            grant_type="authorization_code", code=code, redirect_uri=self.redirect_uri
        )
        if SCOPE not in body.get("scope", "").split() or not body.get("refresh_token"):
            raise CalendarError("Allow read-only Calendar access and reconnect in Integrations.")
        account = await self.account(body["access_token"]) if body.get("access_token") else ""
        self._save({"refresh_token": body["refresh_token"], "account": account,
                    "connected_at": time.time(), "connected_by": by})
        self.token = None
        self.last_error = ""
        return account

    async def access_token(self):
        async with self.lock:
            if self.token and self.token[1] > time.time():
                return self.token[0]
            try:
                saved = json.loads(self.path.read_text())
                refresh = saved["refresh_token"]
            except (OSError, ValueError, KeyError) as exc:
                raise CalendarError("Connect the calendar account in Integrations.") from exc
            try:
                body = await self.exchange(grant_type="refresh_token", refresh_token=refresh)
                if not body.get("access_token"):
                    raise CalendarError("Google Calendar authorization returned no access token.")
            except CalendarError as exc:
                self.last_error = str(exc)
                raise
            self.token = (
                body["access_token"],
                time.time() + int(body.get("expires_in", 3600)) - 60,
            )
            self.last_ok, self.last_error = time.time(), ""
            if "account" not in saved:  # connected before Xos recorded the account: look it up once
                saved["account"] = await self.account(self.token[0])
                self._save(saved)
            return self.token[0]
