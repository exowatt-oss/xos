import asyncio
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.calendar_auth import CalendarAuth
from app.calendars import SCOPE, CalendarError, Calendars
from app.config import Settings


def make_auth(tmp_path, handler):
    return CalendarAuth(
        Settings(
            XOS_DATA_DIR=tmp_path,
            GOOGLE_CALENDAR_CLIENT_ID="client-id",
            GOOGLE_CALENDAR_CLIENT_SECRET="client-secret",
        ),
        httpx.MockTransport(handler),
    )


def test_consent_requests_calendar_only_and_state_is_expiring_one_use(tmp_path):
    auth = make_auth(tmp_path, lambda r: httpx.Response(500))
    state, url = auth.start()
    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == [SCOPE]
    assert query["access_type"] == ["offline"]
    assert "client-secret" not in url
    assert not auth.consume(state, "wrong-cookie")
    assert auth.consume(state, state)
    assert not auth.consume(state, state)
    expired, _ = auth.start()
    auth.pending[expired]["expires"] = 1
    assert not auth.consume(expired, expired)


def test_refresh_credential_is_private_persistent_and_used_for_calendar_reads(tmp_path):
    requests = []

    def google(req):
        requests.append(req)
        data = parse_qs(req.content.decode())
        assert data["client_secret"] == ["client-secret"]
        if data["grant_type"] == ["authorization_code"]:
            return httpx.Response(200, json={"refresh_token": "private-refresh", "scope": SCOPE})
        assert data["refresh_token"] == ["private-refresh"]
        return httpx.Response(200, json={"access_token": "private-access", "expires_in": 3600})

    auth = make_auth(tmp_path, google)
    asyncio.run(auth.finish("code"))
    assert stat.S_IMODE(auth.path.stat().st_mode) == 0o600
    assert auth.connected
    restarted = make_auth(tmp_path, google)
    calendars = Calendars(restarted.s, auth=restarted)
    assert calendars.configured
    assert asyncio.run(calendars._access_token()) == "private-access"
    assert asyncio.run(calendars._access_token()) == "private-access"
    assert len(requests) == 2


def test_denied_scope_or_failed_exchange_never_saves_credentials(tmp_path):
    auth = make_auth(
        tmp_path,
        lambda r: httpx.Response(
            200,
            json={
                "refresh_token": "secret",
                "scope": "openid",
            },
        ),
    )
    with pytest.raises(CalendarError, match="read-only Calendar"):
        asyncio.run(auth.finish("code"))
    assert not auth.path.exists()
    auth.transport = httpx.MockTransport(lambda r: httpx.Response(400, text="secret-response"))
    with pytest.raises(CalendarError) as error:
        asyncio.run(auth.finish("code"))
    assert "secret-response" not in str(error.value)


def test_oauth_routes_require_admin_start_and_matching_browser_callback(tmp_path, monkeypatch):
    auth = make_auth(
        tmp_path,
        lambda r: httpx.Response(
            200,
            json={
                "refresh_token": "private-refresh",
                "scope": SCOPE,
            },
        ),
    )
    monkeypatch.setattr(main, "calendar_auth", auth)
    monkeypatch.setattr(main, "calendars", Calendars(auth.s, auth=auth))
    client = TestClient(main.app, base_url="https://testserver")
    monkeypatch.setattr(main.settings, "XOS_DEV_USER", "")
    assert client.get("/api/calendar/oauth/start").status_code == 401
    monkeypatch.setattr(main.settings, "XOS_DEV_USER", "root@example.com")
    start = client.get("/api/calendar/oauth/start", follow_redirects=False)
    assert start.status_code == 303
    assert "HttpOnly" in start.headers["set-cookie"]
    assert "Secure" in start.headers["set-cookie"]
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    assert client.get("/api/calendar/oauth/callback?state=wrong&code=x").status_code == 400
    # The callback does not require the room calendar account to be an Xos administrator.
    monkeypatch.setattr(main.settings, "XOS_DEV_USER", "")
    finish = client.get(
        f"/api/calendar/oauth/callback?state={state}&code=x", follow_redirects=False
    )
    assert finish.status_code == 303
    assert "no-store" in finish.headers["cache-control"]
    assert client.get(f"/api/calendar/oauth/callback?state={state}&code=x").status_code == 400
    status = client.get("/api/calendar/status").text
    assert "private-refresh" not in status and "client-secret" not in status


def test_disabled_oauth_reports_setup_without_redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "calendar_auth", CalendarAuth(Settings(XOS_DATA_DIR=tmp_path)))
    assert TestClient(main.app).get("/api/calendar/oauth/start").status_code == 409


def test_the_connected_account_and_who_connected_it_are_recorded_never_the_token(tmp_path):
    def google(req):
        if req.method == "GET":
            assert req.url.path.endswith("/calendars/primary")
            assert req.headers["authorization"] == "Bearer fresh-access"
            return httpx.Response(200, json={"id": "rooms-calendar@example.com"})
        return httpx.Response(
            200, json={"refresh_token": "r1", "access_token": "fresh-access", "scope": SCOPE}
        )

    auth = make_auth(tmp_path, google)
    state, _ = auth.start(by="root@example.com")
    assert auth.consume(state, state)["by"] == "root@example.com"
    assert asyncio.run(auth.finish("code", by="root@example.com")) == "rooms-calendar@example.com"
    info = auth.info
    assert info["account"] == "rooms-calendar@example.com"
    assert info["connected_by"] == "root@example.com" and info["connected_at"] > 0
    assert "refresh_token" not in info


def test_an_older_connection_learns_its_account_on_the_next_refresh(tmp_path):
    reads = []

    def google(req):
        if req.method == "GET":
            reads.append(req)
            return httpx.Response(200, json={"id": "atrium-owner@example.com"})
        return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})

    auth = make_auth(tmp_path, google)
    auth.path.parent.mkdir(parents=True)
    auth.path.write_text('{"refresh_token": "old"}')  # saved before accounts were recorded
    asyncio.run(auth.access_token())
    assert (
        auth.info["account"] == "atrium-owner@example.com" and auth.last_ok and not auth.last_error
    )
    auth.token = None
    asyncio.run(auth.access_token())
    assert len(reads) == 1  # looked up once, then remembered


def test_a_revoked_grant_says_so_without_googles_raw_reply(tmp_path):
    def google(req):
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
            },
        )

    auth = make_auth(tmp_path, google)
    auth.path.parent.mkdir(parents=True)
    auth.path.write_text('{"refresh_token": "old", "account": "a@example.com"}')
    with pytest.raises(CalendarError, match="revoked Xos's calendar access") as err:
        asyncio.run(auth.access_token())
    assert "expired or revoked" not in str(err.value)
    assert auth.last_error == str(err.value)
