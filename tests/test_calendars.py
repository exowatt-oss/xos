"""Room calendars: the room's Google resource calendar → now / next with one-tap Join."""

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main
from app.calendars import SCOPE, CalendarError, Calendars, booking

ATRIUM = "c_atrium0example@resource.calendar.google.com"
client = TestClient(main.app)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def event(title, start, end, **extra):
    return {
        "id": title,
        "status": "confirmed",
        "summary": title,
        "start": {"dateTime": iso(start)},
        "end": {"dateTime": iso(end)},
        "organizer": {"email": "root@example.com"},
        **extra,
    }


NOW = time.time()
EVENTS = [
    event(
        "Test",
        NOW - 300,
        NOW + 1500,
        conferenceData={
            "entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1"},
                {"entryPointType": "video", "uri": "https://meet.google.com/new-ojef-jha"},
            ]
        },
    ),
    event("Drone Sync", NOW + 3600, NOW + 5400, hangoutLink="https://meet.google.com/ods-feaa-fxd"),
    event(
        "1:1",
        NOW + 6000,
        NOW + 7000,
        visibility="private",
        hangoutLink="https://meet.google.com/abc-defg-hij",
    ),
    event(
        "Declined",
        NOW + 100,
        NOW + 200,
        attendees=[{"email": ATRIUM, "responseStatus": "declined"}],
    ),
    event(
        "Zoom call", NOW + 8000, NOW + 9000, location="https://example.zoom.us/j/98765432101?pwd=x1"
    ),
    {
        "id": "hold",
        "status": "confirmed",
        "summary": "Offsite",
        "start": {"date": "2026-10-19"},
        "end": {"date": "2026-10-20"},
    },
    event("Earlier", NOW - 7200, NOW - 3600),
]
seen: list[httpx.Request] = []


def google(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    if request.url.host == "oauth2.googleapis.com":
        claims = jwt.decode(
            dict(httpx.QueryParams(request.content.decode()))["assertion"],
            KEY.public_key(),
            algorithms=["RS256"],
            audience="https://oauth2.googleapis.com/token",
        )
        assert claims["sub"] == "root@example.com" and claims["scope"] == SCOPE
        return httpx.Response(200, json={"access_token": "g-tok", "expires_in": 3600})
    assert request.headers["authorization"] == "Bearer g-tok"
    if request.url.path == f"/calendar/v3/calendars/{ATRIUM}/events":
        assert request.url.params["singleEvents"] == "true"
        return httpx.Response(200, json={"items": EVENTS})
    return httpx.Response(404, json={"error": {"message": "Not Found"}})


def settings(key=None):
    return SimpleNamespace(
        GOOGLE_SERVICE_ACCOUNT_JSON=SecretStr(
            json.dumps(
                key
                or {
                    "client_email": "xos@p.iam.gserviceaccount.com",
                    "private_key": PEM,
                    "private_key_id": "k1",
                }
            )
        ),
        XOS_CALENDAR_USER="root@example.com",
        GOOGLE_TOKEN_URL="https://oauth2.googleapis.com/token",
        GOOGLE_CALENDAR_API="https://www.googleapis.com/calendar/v3",
    )


@pytest.fixture
def connected(monkeypatch):
    seen.clear()
    monkeypatch.setattr(
        main, "calendars", Calendars(settings(), transport=httpx.MockTransport(google))
    )


def test_bookings_now_and_next(connected):
    out = client.get("/api/rooms/atrium/calendar").json()
    assert out["calendar"] == ATRIUM and out["error"] == ""
    titles = [b["title"] for b in out["bookings"]]
    # finished, declined and all-day ones drop out; private ones keep no title or organizer
    assert titles == ["Test", "Drone Sync", "Private meeting", "Zoom call"]
    now, sync, private, zoom = out["bookings"]
    assert now["join_url"] == "https://meet.google.com/new-ojef-jha" and now["organizer"] == "root"
    assert sync["join_url"] == "https://meet.google.com/ods-feaa-fxd"
    assert (
        private["organizer"] == "" and private["join_url"] == "https://meet.google.com/abc-defg-hij"
    )
    assert zoom["join_url"] == "https://example.zoom.us/j/98765432101?pwd=x1"


def test_join_links_only_for_the_team_and_trusted_remotes(connected, monkeypatch):
    from app.config import settings as s

    monkeypatch.setattr(s, "XOS_DEV_USER", "")  # a door panel or TV: nobody signed in, no key
    out = client.get("/api/rooms/atrium/calendar").json()
    assert out["bookings"][0]["title"] == "Test"
    assert all(b["join_url"] == "" for b in out["bookings"])


def test_google_is_asked_once_a_minute_per_room(connected):
    client.get("/api/rooms/atrium/calendar")
    client.get("/api/rooms/atrium/calendar")
    assert len([r for r in seen if "calendar/v3" in str(r.url)]) == 1


def test_without_delegation_it_says_what_the_admin_must_do(monkeypatch):
    def refused(request):
        return httpx.Response(
            401,
            json={"error": "unauthorized_client", "error_description": "Client is unauthorized"},
        )

    monkeypatch.setattr(
        main, "calendars", Calendars(settings(), transport=httpx.MockTransport(refused))
    )
    out = client.get("/api/rooms/atrium/calendar").json()
    assert "Domain-wide delegation" in out["error"] and out["bookings"] == []


def test_not_configured_is_quiet():
    out = client.get("/api/rooms/atrium/calendar").json()
    assert out["configured"] is False and out["bookings"] == [] and out["error"] == ""


def test_a_room_can_point_at_another_calendar():
    r = client.put("/api/rooms/atrium", json={"name": "Atrium", "site": "hq", "calendar": "nope"})
    assert r.status_code == 400
    other = "c_other@resource.calendar.google.com"
    assert (
        client.put(
            "/api/rooms/atrium", json={"name": "Atrium", "site": "hq", "calendar": other}
        ).status_code
        == 200
    )
    assert client.get("/api/calendar/status").json()["rooms"]["atrium"] == other
    client.put("/api/rooms/atrium", json={"name": "Atrium", "site": "hq", "calendar": ""})
    assert (
        client.get("/api/calendar/status").json()["rooms"]["atrium"] == ATRIUM
    )  # back to the default


def test_booking_parsing_edge_cases():
    assert booking({"status": "cancelled"}, ATRIUM) is None
    b = booking(
        event(
            "x",
            NOW,
            NOW + 60,
            description='Join: <a href="https://meet.google.com/xyz-abcd-efg">x</a>',
        ),
        ATRIUM,
    )
    assert b["join_url"] == "https://meet.google.com/xyz-abcd-efg"


def test_invites_accept_explicit_codes_without_turning_arbitrary_numbers_into_calls():
    def invite(description):
        return booking(event("Invite", NOW, NOW + 600, description=description), ATRIUM)["join_url"]

    assert invite("Google Meet code: abc-defg-hij") == "https://meet.google.com/abc-defg-hij"
    assert (
        invite("Zoom Meeting ID: 987 6543 2101<br>Passcode: Abc123")
        == "https://zoom.us/j/98765432101?pwd=Abc123"
    )
    assert (
        invite("https://zoom.us/j/98765432101 Passcode: Z9abc")
        == "https://zoom.us/j/98765432101?pwd=Z9abc"
    )
    webex = (
        'Join meeting<br><a href="https://acme.webex.com/acme/j.php?MTID=m3f1a2b&amp;t=x">Join</a>'
        "<br>Meeting number: 2551 234 5678<br>Meeting password: Qp7xY"
    )
    assert invite(webex) == "https://acme.webex.com/acme/j.php?MTID=m3f1a2b"
    assert invite("Meeting number: 2551 234 5678 Meeting password: Qp7xY") == ""  # not Zoom
    assert invite("Call us on 98765432101") == ""
    assert invite("Meeting ID: 123") == ""
    assert invite("https://evil.example/j/98765432101") == ""


def test_expired_cache_reloads_edits_and_cancellations_with_real_sync_time(monkeypatch):
    clock = [NOW]
    reads = []
    items = [EVENTS[0], EVENTS[1]]

    async def fetch_events(calendar_id, now):
        reads.append(calendar_id)
        return items

    calendars = Calendars(settings())
    monkeypatch.setattr(main, "calendars", calendars)
    monkeypatch.setattr(calendars, "_fetch", fetch_events)
    monkeypatch.setattr("app.calendars.time.time", lambda: clock[0])

    first = client.get("/api/rooms/atrium/calendar")
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["refreshed_at"] == NOW
    items = [
        {
            **EVENTS[0],
            "summary": "Updated booking",
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
            "conferenceData": {},
        },
        {**EVENTS[1], "status": "cancelled"},
    ]
    clock[0] += 30
    cached = client.get("/api/rooms/atrium/calendar").json()
    assert cached["bookings"][0]["title"] == "Test"
    assert cached["refreshed_at"] == NOW
    assert len(reads) == 1
    clock[0] += 31
    fresh = client.get("/api/rooms/atrium/calendar").json()
    assert [b["title"] for b in fresh["bookings"]] == ["Updated booking"]
    assert fresh["bookings"][0]["join_url"] == "https://meet.google.com/abc-defg-hij"
    assert fresh["refreshed_at"] == clock[0]
    assert len(reads) == 2


@pytest.mark.parametrize("middle_empty", [False, True])
def test_calendar_reads_all_pages_before_caching(monkeypatch, middle_empty):
    pages = []

    def paged(request):
        if request.url.host == "oauth2.googleapis.com":
            return google(request)
        page = request.url.params.get("pageToken", "")
        pages.append(page)
        if not page:
            return httpx.Response(200, json={"items": [EVENTS[0]], "nextPageToken": "second"})
        if page == "second":
            return httpx.Response(
                200, json={"items": [] if middle_empty else [EVENTS[1]], "nextPageToken": "last"}
            )
        assert page == "last"
        return httpx.Response(200, json={"items": [EVENTS[2]]})

    calendars = Calendars(settings(), transport=httpx.MockTransport(paged))
    monkeypatch.setattr(main, "calendars", calendars)
    out = client.get("/api/rooms/atrium/calendar").json()
    expected = (
        ["Test", "Private meeting"] if middle_empty else ["Test", "Drone Sync", "Private meeting"]
    )
    assert [b["title"] for b in out["bookings"]] == expected
    assert out["error"] == "" and out["refreshed_at"] > 0
    client.get("/api/rooms/atrium/calendar")
    assert pages == ["", "second", "last"]


@pytest.mark.parametrize("failure", ["http", "repeated_token"])
def test_incomplete_calendar_pages_are_not_a_successful_sync(monkeypatch, failure):
    pages = []

    def paged(request):
        if request.url.host == "oauth2.googleapis.com":
            return google(request)
        pages.append(request.url.params.get("pageToken", ""))
        if len(pages) == 1:
            return httpx.Response(200, json={"items": [EVENTS[0]], "nextPageToken": "second"})
        if failure == "http":
            return httpx.Response(503)
        return httpx.Response(200, json={"items": [], "nextPageToken": "second"})

    calendars = Calendars(settings(), transport=httpx.MockTransport(paged))
    monkeypatch.setattr(main, "calendars", calendars)
    out = client.get("/api/rooms/atrium/calendar").json()
    assert out["error"] and out["bookings"] == []
    assert ATRIUM not in calendars.refreshed_at
    assert pages == ["", "second"]


@pytest.mark.parametrize("failure", [False, True])
def test_concurrent_room_reads_share_one_refresh(monkeypatch, failure):
    calendars = Calendars(settings())
    reads = []

    async def fetch_events(calendar_id, now):
        reads.append(calendar_id)
        await asyncio.sleep(0)
        if failure:
            raise CalendarError("Provider unavailable")
        return [EVENTS[0]]

    monkeypatch.setattr(calendars, "_fetch", fetch_events)

    async def run():
        return await asyncio.gather(
            *(calendars.bookings(ATRIUM) for _ in range(3)), return_exceptions=True
        )

    results = asyncio.run(run())
    assert reads == [ATRIUM]
    if failure:
        assert all(isinstance(r, CalendarError) for r in results)
        assert ATRIUM not in calendars.refreshed_at
    else:
        assert all([b["title"] for b in r] == ["Test"] for r in results)


def test_one_rooms_refresh_does_not_block_other_calendars(monkeypatch):
    calendars = Calendars(settings())

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch_events(calendar_id, now):
            if calendar_id == ATRIUM:
                started.set()
                await release.wait()
            return [EVENTS[0]]

        monkeypatch.setattr(calendars, "_fetch", fetch_events)
        slow = asyncio.create_task(calendars.bookings(ATRIUM))
        await started.wait()
        try:
            other = await asyncio.wait_for(calendars.bookings("other-room"), timeout=1)
            assert other[0]["title"] == "Test"
            assert not slow.done()
        finally:
            release.set()
            await slow

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["auth", "events"])
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_outage_is_cached_and_recovers_without_false_sync(monkeypatch, stage, error_type):
    clock, broken, attempts = [NOW], [True], []

    async def token():
        if broken[0] and stage == "auth":
            attempts.append("auth")
            raise error_type("private provider details")
        return "g-tok"

    def transport(request):
        if broken[0] and stage == "events":
            attempts.append("events")
            raise error_type("private provider details", request=request)
        return google(request)

    calendars = Calendars(
        settings(),
        transport=httpx.MockTransport(transport),
        auth=SimpleNamespace(connected=True, access_token=token),
    )
    monkeypatch.setattr(main, "calendars", calendars)
    monkeypatch.setattr("app.calendars.time.time", lambda: clock[0])
    first = client.get("/api/rooms/atrium/calendar")
    assert first.status_code == 200
    out = first.json()
    assert out["configured"] and out["error"] and out["bookings"] == []
    assert "private provider details" not in out["error"]
    assert out["refreshed_at"] is None
    assert client.get("/api/rooms/atrium/calendar").json()["error"] == out["error"]
    assert attempts == [stage]
    broken[0] = False
    clock[0] += 61
    recovered = client.get("/api/rooms/atrium/calendar").json()
    assert recovered["error"] == "" and recovered["bookings"]
    assert recovered["refreshed_at"] == clock[0]
    clock[0] += 61
    broken[0] = True
    outage = client.get("/api/rooms/atrium/calendar").json()
    assert outage["error"] and outage["refreshed_at"] == recovered["refreshed_at"]
