"""Bounded room whiteboards. Completed strokes survive reconnects and server restarts."""

import asyncio
import copy
import hashlib
import hmac
import json
import secrets
from pathlib import Path


class Whiteboards:
    MAX_POINTS = 200_000
    MAX_STROKES = 1000

    def __init__(self, directory: Path):
        self.directory = directory / "whiteboards"
        self.boards: dict[str, dict] = {}
        self.watchers: dict[str, set[asyncio.Queue]] = {}
        try:
            self.copper = json.loads((self.directory / "copper.json").read_text())
        except (OSError, ValueError):
            self.copper = {}

    def connect_copper(self, room: str, canvas_id: str, web_url: str, invite_url: str = "") -> str:
        """Return a fresh board-only key once. Reconnecting revokes the previous key."""
        key = secrets.token_urlsafe(32)
        self.copper[room] = {
            "id": canvas_id,
            "web_url": web_url,
            "invite_url": invite_url,
            "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "copper.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.copper))
        tmp.chmod(0o600)
        tmp.replace(path)
        return key

    def drawing_key(self, room: str, key: str) -> bool:
        expected = self.copper.get(room, {}).get("key_hash", "")
        return bool(
            key
            and expected
            and hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), expected)
        )

    def copper_links(self, room: str) -> dict:
        connection = self.copper.get(room, {})
        return {
            "copper_url": f"copper://canvas/{connection['id']}" if connection else "",
            "copper_web_url": connection.get("web_url", ""),
            "copper_invite_url": connection.get("invite_url", "") or connection.get("web_url", ""),
        }

    def get(self, room: str) -> dict:
        if room not in self.boards:
            try:
                board = json.loads((self.directory / f"{room}.json").read_text())
            except (OSError, ValueError):
                board = {"active": False, "epoch": 0, "revision": 0, "strokes": []}
            self.boards[room] = board
        return self.boards[room]

    def snapshot(self, room: str) -> dict:
        return {"kind": "snapshot", **copy.deepcopy(self.get(room))}

    def save(self, room: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{room}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.get(room), separators=(",", ":")))
        tmp.replace(path)

    def apply(self, room: str, op: dict, *, detached: bool = False) -> dict:
        board = self.get(room)
        action = op["action"]
        if op["epoch"] != board["epoch"]:
            raise ValueError("The whiteboard changed. Refresh and try again.")
        event = {"kind": action}
        if action == "start":
            board["active"] = True
        elif not board["active"] and not detached:
            raise ValueError("The whiteboard has been closed.")
        elif action == "stroke":
            stroke = next((s for s in board["strokes"] if s["id"] == op["id"]), None)
            offset, points = op["offset"], op["points"]
            if stroke is None:
                if offset or not points:
                    raise ValueError("Start the stroke again.")
                if len(board["strokes"]) >= self.MAX_STROKES:
                    raise ValueError("This board is full. Clear it to keep drawing.")
                stroke = {
                    "id": op["id"],
                    "color": op["color"],
                    "width": op["width"],
                    "points": [],
                    "done": False,
                }
            if stroke["color"] != op["color"] or stroke["width"] != op["width"]:
                raise ValueError("The stroke's pen changed. Start a new stroke.")
            count = len(stroke["points"])
            if offset > count or (
                offset < count and stroke["points"][offset : offset + len(points)] != points
            ):
                raise ValueError("The stroke is out of sequence. Start a new stroke.")
            if offset == count and points:
                if stroke["done"]:
                    raise ValueError("This stroke is finished. Start a new stroke.")
                total = sum(len(s["points"]) for s in board["strokes"])
                if count + len(points) > 8192 or total + len(points) > self.MAX_POINTS:
                    raise ValueError("This board is full. Clear it to keep drawing.")
                if not count:
                    board["strokes"].append(stroke)
                stroke["points"].extend(points)
            stroke["done"] = stroke["done"] or op["done"]
            event.update(
                id=stroke["id"],
                color=stroke["color"],
                width=stroke["width"],
                offset=offset,
                points=points,
                done=stroke["done"],
            )
        else:
            if action == "replace":
                strokes = op["strokes"]
                if len({s["id"] for s in strokes}) != len(strokes):
                    raise ValueError("Each stroke needs its own id.")
                if (len(strokes) > self.MAX_STROKES
                        or sum(len(s["points"]) for s in strokes) > self.MAX_POINTS):
                    raise ValueError("This board is full. Clear it to keep drawing.")
            # Invalidate all queued input before changing the drawing or closing it.
            board["epoch"] += 1
            if action == "replace":
                board["strokes"] = [
                    {"id": s["id"], "color": s["color"], "width": s["width"],
                     "points": s["points"], "done": True}
                    for s in strokes
                ]
            elif action == "clear":
                board["strokes"] = []
            elif action == "undo":
                if board["strokes"]:
                    board["strokes"].pop()
            elif action == "exit":
                board["active"] = False
        board["revision"] += 1
        event.update(epoch=board["epoch"], revision=board["revision"], active=board["active"])
        if action == "replace":
            # Screens take a replaced drawing as a whole snapshot; older pages already apply those.
            event = self.snapshot(room)
        if action != "stroke" or op["done"]:
            self.save(room)
        for queue in self.watchers.get(room, set()):
            if queue.full():
                # A slow screen catches up with one complete snapshot, never a partial board.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self.snapshot(room))
            else:
                queue.put_nowait(event)
        return event

    def subscribe(self, room: str) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=128)
        self.watchers.setdefault(room, set()).add(queue)
        queue.put_nowait(self.snapshot(room))
        return queue

    def unsubscribe(self, room: str, queue: asyncio.Queue) -> None:
        self.watchers.get(room, set()).discard(queue)
