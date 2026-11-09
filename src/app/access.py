"""Who is asking, and what their Xos role lets them do.

Screens can't sign in, so a sign-in gate (an auth proxy such as oauth2-proxy in front of Xos)
should cover only a narrow part of the site: everything under /u/. The browser calls /u/api/…
and the server handles it as /api/… (UnderUser below). Identity comes from the token the proxy
forwards, which forwarded-auth verifies (FORWARDED_AUTH_* settings), so a header someone typed
in gets them nothing. A Bearer that is present but doesn't verify is refused, never treated as
anonymous; a misconfigured verifier refuses every signed-in request (fail closed).

Every signed-in person has a role. This file defines the permission catalog and factory
role defaults; People stores edited role policies and per-person overrides. Routes ask
`caller.can(...)` or `need(...)` for the effective permissions.

- Admin: everything. XOS_SUPER_ADMINS are always Admin (the break-glass list: no one can demote
  them) and are the only people who can make someone Admin or remove Admin. XOS_BOOTSTRAP_ADMINS
  are the starting Admins, until a super admin gives them another role. Others get a role in
  Admin → People & roles (src/app/people.py).
- Content editor, Screen operator: parts of Admin.
- Member: anyone else who signs in. #/control, i.e. take one screen that has sharing on, while
  no one else holds it, and drive it like a remote.
- A trusted remote (the room iPad, a remote kiosk Admin named): sends its device key with each
  command, the key it was given once in a heartbeat reply. It isn't a person and has no role.
- An agent (a bot) calling through a token gateway: it sends a short-lived machine token that
  forwarded-auth verifies against its additional trusted issuer (FORWARDED_AUTH_ADDITIONAL_ISSUER;
  the token names the agent in its actor claim). It acts for its owner but never with the
  owner's role. It gets BOT_PERMS, may only GET the routes in BOT_READS, and may not write
  (BOT_WRITES is empty). It's never stored or shown in People.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from forwarded_auth import ConfigurationError, InvalidIdentityToken, User, user_from_headers
from fastapi import HTTPException, Request

from .config import settings

logger = logging.getLogger(__name__)

SSO_PREFIX = "/u"

# What a permission allows, in the words Admin → People & roles → Roles shows.
PERMISSIONS: dict[str, str] = {
    "admin.view": "Open the Xos Admin shell and MCP read tools; tab visibility is separate",
    "screens.take_shared": "Take a free screen that has sharing on (Xos → Control a screen)",
    "screens.show": "Put content on any free display (not a remote, setup screen or room "
    "computer): show, next/previous, message, reload, identify, screenshot",
    "screens.operate": "Command any screen, including held ones and ones with sharing off; "
    "release anyone's hold; display on/off and Switch TV to Xos; room Zoom, meeting and "
    "whiteboard controls",
    "content.edit": "Pages, photos and videos, slideshows, announcements and playlists",
    "setup": "Set up screens and rooms: add, rename, forget, provisioning, Linux boxes, Wi-Fi, "
    "SSH keys, sharing settings, remote access, box updates",
    "integrations": "Zoom mapping and Zoom Rooms, Google Calendar, Copper whiteboards",
    "people": "Administer access: edit role and person permissions, assign roles, "
    "and preview Xos as a role or person",
}


# Navigation visibility is independent of the action permissions checked by API routes.
VIEWS = {
    "overview": "Overview",
    "dashboard": "Dashboard",
    "content": "Content",
    "screens": "Screens",
    "rooms": "Rooms",
    "integrations": "Integrations",
    "people": "People & roles",
    "howto": "How to",
    "previews": "Views & previews",
}
PERMISSIONS.update({f"views.{key}": f"Show the {label} tab in Xos" for key, label in VIEWS.items()})
PERMISSIONS.update(
    {
        "demos.view": "Open Demo Studio and see headset status, library, and sessions",
        "demos.previews": "View headset app-view images",
        "demos.edit": "Create and edit demo walkthroughs and their steps",
        "demos.upload": "Upload demo videos, images, and GLB scenes",
        "demos.delete": "Delete demo walkthroughs and uploaded assets",
        "demos.present": "Start a demo on selected headsets",
        "demos.control": "Pause, resume, seek, advance, and stop demo sessions",
        "demos.enable": "Enable or disable demo mode and individual headsets",
        "demos.pair": "Pair or unpair Quest headsets",
        "demos.recover": "Recenter app position, retry scenes, reload and identify headsets",
        "demos.settings": "Change headset app audio, scene, rendering and preview settings",
        "demos.rename": "Rename headset slots",
    }
)
COMMON_VIEWS = {f"views.{v}" for v in ("overview", "dashboard", "content", "screens", "howto")}
EDITOR_DEMOS = {"demos.view", "demos.previews", "demos.edit", "demos.upload", "demos.delete"}
OPERATOR_DEMOS = {
    "demos.view",
    "demos.previews",
    "demos.present",
    "demos.control",
    "demos.enable",
    "demos.recover",
    "demos.settings",
}


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    summary: str
    perms: frozenset[str]


ROLES: dict[str, Role] = {
    r.id: r
    for r in (
        Role("admin", "Admin", "Everything, including people and roles.", frozenset(PERMISSIONS)),
        Role(
            "content_editor",
            "Content editor",
            "Makes content and puts it on screens.",
            frozenset(
                {"admin.view", "screens.take_shared", "screens.show", "content.edit"}
                | COMMON_VIEWS
                | EDITOR_DEMOS
            ),
        ),
        Role(
            "screen_operator",
            "Screen operator",
            "Runs any screen and room, but doesn't set them up.",
            frozenset(
                {"admin.view", "screens.take_shared", "screens.show", "screens.operate"}
                | COMMON_VIEWS
                | OPERATOR_DEMOS
            ),
        ),
        Role(
            "member",
            "Member",
            "Everyone in your organization: takes a free screen that has sharing on.",
            frozenset({"screens.take_shared"}),
        ),
    )
}
DEFAULT_ROLE = "member"


@dataclass(frozen=True)
class Caller:
    email: str = ""
    name: str = ""
    role: str = ""  # effective role id; "" when no one is signed in
    perms: frozenset[str] = frozenset()
    device: str = ""  # id of the trusted remote that sent the request, if any
    # An agent through a token gateway: its slug (the signed actor claim), the
    # owner it acts for (`sub`, for logs only, never for permissions) and its grant scope.
    bot: str = ""
    bot_owner: str = ""
    bot_scope: str = ""
    # Set while an Admin previews Xos as a role or a person: {kind, value, label, by}. The
    # caller then has that perspective's permissions and every write is refused.
    preview: dict | None = field(default=None, compare=False)

    @property
    def person(self) -> bool:
        return bool(self.email) and not self.bot

    def can(self, perm: str) -> bool:
        return perm in self.perms

    @property
    def role_name(self) -> str:
        return ROLES[self.role].name if self.role in ROLES else ""


def caller_as(role: str, email: str = "", name: str = "") -> Caller:
    return Caller(email=email, name=name or email, role=role, perms=ROLES[role].perms)


# The XOS_MCP_TOKEN holder: full Admin.
APP_TOKEN = caller_as("admin", "mcp", "MCP")

# ---------- Agents (machine tokens from a gateway) ----------
# A bot reads screens, rooms and content, and nothing else. It never gets its owner's role,
# never Admin, never people, setup or integrations, and Preview as never applies to it.
BOT_PERMS: frozenset[str] = frozenset({"admin.view"})
# The only routes a bot may GET (route templates, as in main.py). Screenshots, live feeds,
# event streams, share links, calendars, people and setup are left out on purpose.
BOT_READS: tuple[str, ...] = (
    "/api/health",
    "/api/time",
    "/api/config",
    "/api/me",
    "/api/openapi.json",
    "/api/devices",
    "/api/rooms",
    "/api/pages",
    "/api/pages/{page_id}",
    "/api/media",
)
# Writes a bot may make, as (method, route template), and only with a read_write grant. None
# yet: add one here (and a test) to allow it. Everything else a bot sends is refused.
BOT_WRITES: frozenset[tuple[str, str]] = frozenset()
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
BOT_REFUSAL = "Agents can only read Xos screens, rooms and content"


def _template(path: str) -> re.Pattern[str]:
    """/api/pages/{page_id} → a pattern matching one path segment for each {param}."""
    parts = re.split(r"\{[^/]+?\}", path)
    return re.compile("[^/]+".join(re.escape(part) for part in parts))


_BOT_READS = tuple(_template(p) for p in BOT_READS)
_BOT_WRITES = tuple((m, _template(p)) for m, p in BOT_WRITES)


def bot_may(caller: Caller, method: str, path: str) -> bool:
    """Whether a bot may send `method` to `path` (an /api/… path with /u removed)."""
    if method in SAFE_METHODS:
        return any(t.fullmatch(path) for t in _BOT_READS)
    return caller.bot_scope == "read_write" and any(
        m == method and t.fullmatch(path) for m, t in _BOT_WRITES
    )


def bot_caller(user: User) -> Caller:
    """A verified gateway identity as a caller: BOT_PERMS, never its owner's role."""
    return Caller(
        name=f"Agent {user.actor}",
        perms=BOT_PERMS,
        bot=user.actor or "",
        bot_owner=user.email.lower(),
        bot_scope=user.scope or "",
    )


def emails(value: str) -> set[str]:
    return {e.strip().lower() for e in value.split(",") if e.strip()}


def super_admin_emails() -> set[str]:
    """XOS_SUPER_ADMINS: always Admin, and the only people who grant or remove Admin."""
    return emails(settings.XOS_SUPER_ADMINS)


def bootstrap_emails() -> set[str]:
    """XOS_BOOTSTRAP_ADMINS: Admin until a super admin assigns them another role."""
    return emails(settings.XOS_BOOTSTRAP_ADMINS)


def allowed_email(email: str) -> bool:
    """Whether an address may hold an Xos role: XOS_ALLOWED_EMAIL_DOMAINS when set (forwarded-auth
    has already applied its own required domain allowlist to every verified identity)."""
    local, at, domain = email.partition("@")
    if not (at and local and domain) or len(email) > 254:
        return False
    domains = {d.lstrip("@") for d in emails(settings.XOS_ALLOWED_EMAIL_DOMAINS)}
    return not domains or domain in domains


def platform_user(request: Request) -> User | None:
    """The verified identity on this request: a person's sign-in or an agent's gateway token, or
    None. A Bearer that fails verification raises InvalidIdentityToken; it
    never falls back to identity headers. Checked once per request."""
    cached = request.scope.get("xos.identity")
    if cached is None:
        try:
            cached = (user_from_headers(dict(request.headers)), None)
        except InvalidIdentityToken as exc:
            cached = (None, exc)
        except ConfigurationError as exc:
            # A token arrived but the verifier isn't configured (FORWARDED_AUTH_*): refuse it,
            # exactly like a token that doesn't verify. Never fall back to anonymous or headers.
            logger.error("Sign-in isn't configured, refusing the request's token: %s", exc)
            cached = (None, InvalidIdentityToken("identity verification is not configured"))
        request.scope["xos.identity"] = cached
    user, error = cached
    if error is not None:
        raise error
    return user


def calling_bot(request: Request) -> bool:
    try:
        user = platform_user(request)
    except InvalidIdentityToken:
        return False
    return user is not None and user.is_bot


def identify(request: Request) -> tuple[str, str]:
    """(email, name) of the signed-in person, or ("", ""). A bot is never a person."""
    email, name = "", ""
    try:
        user = platform_user(request)
    except InvalidIdentityToken:
        return "", ""
    if user is not None:
        if user.is_bot:
            return "", ""
        email, name = user.email.lower(), user.name or user.email
    elif settings.XOS_DEV_USER:  # local development only; never set on the server
        email = name = settings.XOS_DEV_USER.lower()
    if email and not allowed_email(email):
        return "", ""
    return email, name


def resolve(request: Request, check_key, people) -> Caller:
    try:
        user = platform_user(request)
    except InvalidIdentityToken:
        return Caller()
    if user is not None and user.is_bot:
        # Never the owner's role, a device key or a preview, and never saved in People.
        return bot_caller(user)
    device = ""
    dev_id = request.headers.get("x-xos-device", "")
    key = request.headers.get("x-xos-key", "")
    if dev_id and key and check_key(dev_id, key):
        device = dev_id
    email, name = identify(request)
    if not email:
        return Caller(device=device)
    people.seen(email, name)
    return people.caller(email, name, device=device)


def need(caller: Caller, perm: str) -> None:
    if caller.can(perm):
        return
    if caller.bot:
        raise HTTPException(403, BOT_REFUSAL)
    if not caller.person:
        raise HTTPException(401, "Sign in to use Xos Admin")
    if caller.preview:
        raise HTTPException(403, f"{caller.preview['label']} can't do this")
    raise HTTPException(403, f"Your Xos role ({caller.role_name}) can't do this. Ask an Xos Admin.")


class UnderUser:
    """ASGI middleware: /u/api/… is /api/… reached through the sign-in gate."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith(SSO_PREFIX + "/api/"):
                scope = {**scope, "path": path[len(SSO_PREFIX) :]}
                if scope.get("raw_path"):
                    scope["raw_path"] = scope["raw_path"][len(SSO_PREFIX) :]
        await self.app(scope, receive, send)
