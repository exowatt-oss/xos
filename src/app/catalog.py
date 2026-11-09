"""Sites, the first-start room list, room calendars, and the kiosk roles screens can run.

Rooms themselves are managed in Admin → Rooms (src/app/rooms.py); SEED_ROOMS is only what a fresh
server starts with. Sites, seed rooms and default room calendars come from configuration
(XOS_SITES, XOS_SEED_ROOMS, XOS_ROOM_CALENDARS, JSON; see src/app/config.py). Nothing is
hard-coded: an unconfigured server has one site, no rooms and no calendars.
"""

from __future__ import annotations

import json
import logging

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_SITES = [{"id": "main", "name": "Main site"}]


def _json(name: str, value: str, kind: type, default):
    if not value.strip():
        return default
    try:
        parsed = json.loads(value)
    except ValueError:
        logger.error("%s isn't valid JSON; using the default", name)
        return default
    if not isinstance(parsed, kind):
        logger.error("%s must be a JSON %s; using the default", name, kind.__name__)
        return default
    return parsed


def _entries(items: list, *keys: str) -> list[dict]:
    return [
        {k: str(item[k]) for k in keys}
        for item in items
        if isinstance(item, dict) and all(isinstance(item.get(k), str) and item[k] for k in keys)
    ]


SITES: list[dict] = _entries(_json("XOS_SITES", settings.XOS_SITES, list, []), "id", "name") or [
    dict(s) for s in DEFAULT_SITES
]
_SITE_IDS = {s["id"] for s in SITES}

SEED_ROOMS: list[dict] = [
    r
    for r in _entries(_json("XOS_SEED_ROOMS", settings.XOS_SEED_ROOMS, list, []), "id", "name", "site")
    if r["site"] in _SITE_IDS
]

# Each room's Google resource calendar, unless Admin → Integrations sets another.
ROOM_CALENDARS: dict[str, str] = {
    str(k): str(v)
    for k, v in _json("XOS_ROOM_CALENDARS", settings.XOS_ROOM_CALENDARS, dict, {}).items()
    if isinstance(v, str)
}

# Kiosk views a device can run: role -> what its #/role/<arg> argument is.
ROLES = {
    "tv": {"label": "Room TV", "arg": "room"},
    "tablet": {"label": "Table remote", "arg": "room"},
    "panel": {"label": "Door panel", "arg": "room"},
    "signage": {"label": "Lobby signage", "arg": "site"},
    "wall": {"label": "Dashboard wall", "arg": None},
}
