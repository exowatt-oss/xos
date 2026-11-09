"""Agents through a token gateway: machine tokens are verified, and a bot only reads.

The tokens here are signed with a local RSA key whose JWKS stands in for the gateway's
(forwarded-auth's JWKS client lookup is swapped for one that serves it). The primary sign-in
issuer's JWKS is swapped for an empty one, so nothing here reaches the network.
"""

import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from forwarded_auth import _jwks
from jwt import PyJWKClient
from jwt.algorithms import RSAAlgorithm

from app import access
from app.config import settings
from app.main import app, hub, people

client = TestClient(app)
ISSUER = "https://gateway.test"
SIGNIN_ISSUER = "https://signin.test"
AUDIENCE = "https://xos.example.org"
KID = "gw-test"
OWNER = "root@example.com"  # a bootstrap Admin: the bot still gets none of that
TV = "firetv-gateway1"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY, OTHER_KEY = _key(), _key()


class LocalJWKS(PyJWKClient):
    def __init__(self, keys):
        super().__init__(f"{ISSUER}/.well-known/jwks.json", cache_jwk_set=False)
        self.keys = keys

    def fetch_data(self):
        jwks = []
        for kid, key in self.keys:
            jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
            jwks.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": jwks}


@pytest.fixture(autouse=True)
def gateway(monkeypatch, tmp_path):
    for key in [k for k in __import__("os").environ if k.startswith("FORWARDED_AUTH_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("FORWARDED_AUTH_ISSUER", SIGNIN_ISSUER)
    monkeypatch.setenv("FORWARDED_AUTH_JWKS_URL", f"{SIGNIN_ISSUER}/jwks")
    monkeypatch.setenv("FORWARDED_AUTH_AUDIENCE", "signin-client-id")
    monkeypatch.setenv("FORWARDED_AUTH_ALLOWED_EMAIL_DOMAINS", "example.com")
    monkeypatch.setenv("FORWARDED_AUTH_DISABLE_USERINFO_FALLBACK", "1")
    monkeypatch.setenv("FORWARDED_AUTH_ADDITIONAL_ISSUER", ISSUER)
    monkeypatch.setenv("FORWARDED_AUTH_ADDITIONAL_ISSUER_AUDIENCE", AUDIENCE)
    monkeypatch.setenv(
        "FORWARDED_AUTH_ADDITIONAL_ISSUER_JWKS_URL", f"{ISSUER}/.well-known/jwks.json"
    )
    _jwks._reset_jwks_clients_for_tests()
    gateway_keys, signin_keys = LocalJWKS([(KID, KEY)]), LocalJWKS([])
    monkeypatch.setattr(
        _jwks,
        "_get_jwks_client",
        lambda url, ttl_s=3600.0: gateway_keys if url.startswith(ISSUER) else signin_keys,
    )
    # No dev sign-in: the only identity on a request is what it proves.
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    monkeypatch.setattr(settings, "XOS_BOOTSTRAP_ADMINS", OWNER)
    monkeypatch.setattr(people, "path", tmp_path / "people.json")
    monkeypatch.setattr(people, "audit_path", tmp_path / "people_audit.jsonl")
    people.people.clear()
    people.roles.clear()
    people.previews.clear()
    hub.devices.clear()
    hub.commands.clear()
    hub.controls.clear()
    client.post("/api/devices/heartbeat", json={"id": TV, "role": "page", "arg": "welcome"})
    yield
    people.previews.clear()
    _jwks._reset_jwks_clients_for_tests()


def token(key=KEY, kid=KID, algorithm="RS256", **claims):
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": OWNER,
        "act": "agent-weekly-rooms",
        "scope": "read",
        "connector": "app:xos",
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
    }
    body.update(claims)
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, key, algorithm=algorithm, headers={"kid": kid})


def bearer(tok=None, **headers):
    return {"Authorization": f"Bearer {tok or token()}", **headers}


# ---------- what a bot can read ----------


def test_a_verified_bot_reads_screens_rooms_and_content():
    for path in ("/api/rooms", "/api/pages", "/api/media", "/api/config", "/api/health"):
        assert client.get(path, headers=bearer()).status_code == 200, path
    schema = client.get("/api/openapi.json", headers=bearer())
    assert schema.status_code == 200 and "/api/devices" in schema.json()["paths"]

    screens = client.get("/api/devices", headers=bearer()).json()
    assert [s["id"] for s in screens] == [TV]
    # The bounded inventory: no share code, address, commands or meeting details.
    assert set(screens[0]) == {
        "id",
        "name",
        "client",
        "role",
        "room",
        "online",
        "connected",
        "last_seen",
    }
    assert "share_code" in client.get("/api/devices").json()[0]  # screens still get it all


def test_a_bot_is_never_its_owner_and_has_no_role():
    me = client.get("/api/me", headers=bearer()).json()
    assert me["bot"] == "agent-weekly-rooms"
    assert me["email"] == "" and me["role"] == ""
    assert me["perms"] == sorted(access.BOT_PERMS) == ["admin.view"]
    assert me["preview"] is None


def test_the_bot_permission_set_is_small_and_has_no_writes():
    assert access.BOT_PERMS == {"admin.view"}
    assert not access.BOT_PERMS & {"people", "setup", "integrations", "content.edit"}
    assert not any(p.startswith("screens.") for p in access.BOT_PERMS)
    assert access.BOT_WRITES == frozenset()
    bot = access.Caller(perms=access.BOT_PERMS, bot="agent-x", bot_scope="read_write")
    assert access.bot_may(bot, "GET", "/api/pages/abc")
    assert not access.bot_may(bot, "GET", "/api/pages/abc/extra")
    assert not access.bot_may(bot, "GET", "/api/devices/x/screenshot")
    assert not access.bot_may(bot, "POST", "/api/pages")


# ---------- what a bot can't do ----------


def test_a_bot_is_refused_every_write_even_with_a_read_write_grant():
    for scope in ("read", "read_write"):
        h = bearer(token(scope=scope))
        writes = [
            ("post", "/api/rooms", {"name": "Bot room", "site": "hq"}),
            ("post", f"/api/devices/{TV}/commands", {"type": "reload", "args": {}}),
            ("post", "/api/devices/heartbeat", {"id": "firetv-botmade", "role": "page"}),
            ("post", f"/api/control/{TV}", None),
            ("post", "/api/pages", {"kind": "message", "title": "Hi"}),
            ("put", f"/api/devices/{TV}", {"name": "Renamed"}),
            ("put", f"/api/people/{OWNER}/role", {"role": "member"}),
            ("post", "/api/preview", {"role": "member"}),
            ("delete", "/api/preview", None),
            ("delete", f"/api/devices/{TV}", None),
        ]
        for method, path, body in writes:
            kwargs = {"headers": h} | ({"json": body} if body is not None else {})
            r = client.request(method.upper(), path, **kwargs)
            assert r.status_code == 403, (scope, method, path, r.status_code)
            assert r.json()["detail"] == access.BOT_REFUSAL
    assert "firetv-botmade" not in hub.devices
    assert hub.devices[TV].get("name", "") != "Renamed"
    assert not hub.commands.get(TV)


def test_a_bot_cant_see_screenshots_people_setup_or_integrations():
    for path in (
        f"/api/devices/{TV}/screenshot",
        "/api/devices/events",
        f"/api/devices/{TV}/stream",
        "/api/people",
        "/api/roles",
        "/api/people/audit",
        "/api/provision",
        "/api/devices/pairing",
        "/api/appliances",
        "/api/appliances/ssh-keys",
        "/api/appliances/wifi",
        "/api/zoom/rooms",
        "/api/zoom/log",
        "/api/calendar/status",
        "/api/integrations/exo/devices",
        "/api/control",
        "/u/api/people",
    ):
        r = client.get(path, headers=bearer())
        assert r.status_code == 403, (path, r.status_code)


def test_a_bot_cant_reach_pages_outside_the_api_either():
    for path in ("/u/whiteboards/atrium", "/os/install.sh", "/firetv/xos-tv.apk", "/app/"):
        assert client.get(path, headers=bearer()).status_code == 403, path


def test_a_bot_is_refused_by_the_permission_model_too():
    bot = access.bot_caller(
        type("U", (), {"actor": "agent-x", "email": OWNER, "scope": "read_write"})()
    )
    assert bot.can("admin.view") and not bot.person
    for perm in ("people", "setup", "integrations", "content.edit", "screens.show"):
        with pytest.raises(Exception) as exc:
            access.need(bot, perm)
        assert exc.value.status_code == 403


def test_a_bot_cant_use_mcp():
    r = client.post(
        "/mcp",
        headers=bearer(Accept="application/json, text/event-stream"),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert r.status_code == 401


# ---------- tokens that don't verify ----------


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(lambda: token(key=OTHER_KEY), id="forged"),
        pytest.param(lambda: token(kid="unknown", key=OTHER_KEY), id="unknown-key"),
        pytest.param(lambda: token(key=None, algorithm="none"), id="unsigned"),
        pytest.param(lambda: token(aud="https://other.apps.example.org"), id="wrong-audience"),
        pytest.param(
            lambda: token(iat=int(time.time()) - 600, exp=int(time.time()) - 540), id="expired"
        ),
        pytest.param(lambda: token(iss="https://evil.test"), id="wrong-issuer"),
        pytest.param(lambda: token(act=None), id="no-actor"),
        pytest.param(lambda: token(exp=int(time.time()) + 3600), id="too-long"),
        pytest.param(lambda: "not-a-jwt", id="garbage"),
    ],
)
def test_a_bearer_that_doesnt_verify_gets_401(bad):
    tok = bad()
    for path in ("/api/devices", "/api/me", "/u/api/me"):
        r = client.get(path, headers=bearer(tok))
        assert r.status_code == 401, (path, r.status_code)
    r = client.post("/api/rooms", headers=bearer(tok), json={"name": "X", "site": "hq"})
    assert r.status_code == 401


def test_identity_headers_next_to_a_bearer_are_not_trusted():
    spoof = {
        "X-Auth-Request-Email": OWNER,
        "X-Auth-Request-User": "123",
        "X-Gateway-Agent": "agent-someone-else",
        "X-Gateway-Agent-Owner": OWNER,
        "X-Gateway-Agent-Owner-Verified": "true",
    }
    me = client.get("/api/me", headers=bearer(**spoof)).json()
    assert me["bot"] == "agent-weekly-rooms" and me["email"] == "" and me["role"] == ""
    r = client.post("/api/rooms", headers=bearer(**spoof), json={"name": "X", "site": "hq"})
    assert r.status_code == 403
    # A forged token with the same headers is just rejected.
    assert client.get("/api/me", headers=bearer(token(key=OTHER_KEY), **spoof)).status_code == 401
    # And the headers alone, with no token, sign no one in.
    assert client.get("/api/me", headers=spoof).json()["email"] == ""


# ---------- bots are transient ----------


def test_a_bot_is_never_saved_or_listed_in_people():
    for path in ("/api/me", "/api/devices", "/api/rooms"):
        client.get(path, headers=bearer(token(sub="newowner@example.com")))
    client.post("/api/rooms", headers=bearer(), json={"name": "X", "site": "hq"})
    assert "newowner@example.com" not in people.people
    assert not any("agent-" in email for email in people.people)
    saved = people.path.read_text() if people.path.exists() else ""
    assert "agent-weekly-rooms" not in saved and "newowner@example.com" not in saved


def test_an_owner_previewing_doesnt_change_their_bot():
    people.previews[OWNER] = {
        "kind": "role",
        "value": "member",
        "label": "Member",
        "by": OWNER,
        "started": time.time(),
        "perms": sorted(access.ROLES["member"].perms),
    }
    me = client.get("/api/me", headers=bearer()).json()
    assert me["preview"] is None and me["perms"] == ["admin.view"]
    assert client.get("/api/rooms", headers=bearer()).status_code == 200


def test_bots_cannot_access_quest_studio_or_enrollment():
    assert client.get("/api/demos/studio", headers=bearer()).status_code == 403
    assert client.get("/u/api/demos/units/hq-01/preview", headers=bearer()).status_code == 403
    assert (
        client.post(
            "/api/demos/enroll", headers=bearer(), json={"id": "quest-bot", "key": "a" * 48}
        ).status_code
        == 403
    )
