"""Rooms: the meeting rooms and spaces Xos screens belong to, managed in Admin → Rooms.

Saved to XOS_DATA_DIR/rooms.json. A screen is put in a room in Admin → Screens; the room's
screens then show its name, its table remote controls them, and Zoom links per room.
"""

import json
import re
import time
from pathlib import Path

from app.catalog import ROOM_CALENDARS, SEED_ROOMS, SITES

CALENDAR_ID = re.compile(r"^[\w.+-]+@[\w.-]+\.[a-z]{2,}$")
ROOM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SITE_IDS = {s["id"] for s in SITES}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "room"


class Rooms:
    def __init__(self, data_dir: Path, extra_ids: set[str] = frozenset()):
        self.path = data_dir / "rooms.json"
        try:
            self.items: dict[str, dict] = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.items = {}
            # First start: the rooms already in use (screens or Zoom links) plus the seed list.
            for room in SEED_ROOMS:
                self.items[room["id"]] = {**room, "created": time.time()}
            for rid in sorted(extra_ids):
                if ROOM_ID.match(rid) and rid not in self.items:
                    name = rid.replace("-", " ").title()
                    self.items[rid] = {"id": rid, "name": name, "site": SITES[0]["id"],
                                       "created": time.time()}
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items))
        tmp.replace(self.path)

    def all(self) -> list[dict]:
        return sorted(
            ({**room, "display": self.display(room["id"])} for room in self.items.values()),
            key=lambda room: (room["site"], room["name"].lower()),
        )

    def upsert(self, room_id: str | None, name: str, site: str, calendar: str | None = None) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("A room needs a name")
        if site not in SITE_IDS:
            raise ValueError(f"Unknown site {site}")
        if room_id is None:
            base = slugify(name)
            room_id, n = base, 2
            while room_id in self.items:
                room_id, n = f"{base[:36]}-{n}", n + 1
        known = self.items.get(room_id, {"created": time.time()})
        self.items[room_id] = {**known, "id": room_id, "name": name, "site": site}
        if calendar is not None:  # "" goes back to the default (catalog.ROOM_CALENDARS)
            if calendar and not CALENDAR_ID.match(calendar):
                raise ValueError("A room calendar is its resource address, like c_…@resource.calendar.google.com")
            self.items[room_id]["calendar"] = calendar.strip()
        self._save()
        return self.items[room_id]

    def display(self, room_id: str) -> dict:
        return {
            "idle_page": "",
            "meeting_lead_minutes": 15,
            **self.items.get(room_id, {}).get("display", {}),
            "last_meeting_end": self.items.get(room_id, {}).get("last_meeting_end", 0),
        }

    def set_display(self, room_id: str, idle_page: str, meeting_lead_minutes: int) -> dict:
        self.items[room_id]["display"] = {
            "idle_page": idle_page,
            "meeting_lead_minutes": meeting_lead_minutes,
        }
        self._save()
        return self.display(room_id)

    def meeting_ended(self, room_id: str) -> None:
        if room_id in self.items:
            self.items[room_id]["last_meeting_end"] = time.time()
            self._save()

    def calendar(self, room_id: str) -> str:
        """The room's Google resource calendar: set in Admin, else the catalog's."""
        return (self.items.get(room_id, {}).get("calendar") or ROOM_CALENDARS.get(room_id, ""))

    def delete(self, room_id: str) -> bool:
        if self.items.pop(room_id, None) is None:
            return False
        self._save()
        return True
