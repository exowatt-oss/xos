"""Short-lived immutable PNG handoffs. They grant no access to the room or source board."""

import base64
import hashlib
import json
import re
import secrets
import struct
import time
import uuid
from pathlib import Path


class WhiteboardExports:
    TTL = 600
    MAX_BYTES = 64_000_000
    MAX_ITEMS = 64

    def __init__(self, directory: Path):
        self.directory = directory / "whiteboard-exports"

    def _path(self, token: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise FileNotFoundError
        return self.directory / (hashlib.sha256(token.encode()).hexdigest() + ".json")

    def _save(self, path: Path, value: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, separators=(",", ":")))
        tmp.chmod(0o600)
        tmp.replace(path)

    def create(self, *, png: str, width: int, height: int, context: dict) -> tuple[str, dict]:
        try:
            if not png.startswith("data:image/png;base64,"):
                raise ValueError
            raw = base64.b64decode(png[22:], validate=True)
            if (
                not (33 <= len(raw) <= 8_000_000)
                or raw[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            ):
                raise ValueError
            if struct.unpack(">II", raw[16:24]) != (width, height):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ValueError("Export a valid PNG with matching dimensions") from exc
        now = time.time()
        files = []
        for path in self.directory.glob("*.json"):
            try:
                if path.stat().st_mtime + self.TTL < now:
                    path.unlink()
                else:
                    files.append(path)
            except FileNotFoundError:
                pass
        size = sum(p.stat().st_size for p in files)
        if len(files) >= self.MAX_ITEMS or size + len(png) > self.MAX_BYTES:
            raise OverflowError("Too many pending drawings. Try again in a few minutes.")
        token = secrets.token_urlsafe(32)
        payload = {
            "version": 1,
            "id": str(uuid.uuid4()),
            "width": width,
            "height": height,
            "png": png,
            **context,
        }
        value = {"expires": now + self.TTL, "payload": payload, "receipt": None}
        self._save(self._path(token), value)
        return token, value

    def get(self, token: str) -> dict:
        path = self._path(token)
        value = json.loads(path.read_text())
        if value["expires"] <= time.time():
            path.unlink(missing_ok=True)
            raise FileNotFoundError
        return value

    def receipt(self, token: str, receipt: dict) -> dict:
        value = self.get(token)
        if receipt["shape_id"] != "xos-" + value["payload"]["id"]:
            raise ValueError("The inserted image does not match this snapshot")
        value["receipt"] = {**receipt, "at": time.time()}
        self._save(self._path(token), value)
        return value["receipt"]
