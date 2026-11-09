"""Media library: photos and videos screens can show (Admin → Content, MCP add_media_from_url).

Uploads are saved in XOS_DATA_DIR/media/<id><ext> and served at /media/<id><ext> (with HTTP
Range, so videos seek). Built-in media (BUILTIN, empty by default) is listed too, read-only, so a
slideshow can mix it with uploads. Size caps protect the server's disk.
"""

import json
import re
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}
EXT_TYPES = {".jpeg": "image/jpeg", **{ext: t for t, ext in TYPES.items()}}
MAX_IMAGE_BYTES = 25_000_000
MAX_VIDEO_BYTES = 300_000_000
MEDIA_ID = re.compile(r"^[a-z0-9-]{3,48}$")

# Read-only media shipped with a deployment under static/ (e.g. a brand or lobby pack), as
# (id, name, file under BUILTIN_DIR). None ship with Xos itself.
BUILTIN_DIR = "/static/builtin-media/"
BUILTIN: list[tuple[str, str, str]] = []


class MediaError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def kind_of(media_type: str) -> str:
    return "video" if media_type.startswith("video/") else "image"


def type_for(filename: str, declared: str = "") -> str:
    """The media type from the upload's declared type, else its file extension."""
    declared = declared.split(";")[0].strip().lower()
    if declared in TYPES:
        return declared
    return EXT_TYPES.get(Path(filename).suffix.lower(), "")


class Media:
    def __init__(self, data_dir: Path, max_total_bytes: int = 2_000_000_000):
        self.dir = data_dir / "media"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = data_dir / "media.json"
        self.max_total = max_total_bytes
        try:
            self.items: dict[str, dict] = json.loads(self.index_path.read_text())
        except (FileNotFoundError, ValueError):
            self.items = {}
        self.builtin = {
            mid: {"id": mid, "name": name, "kind": kind_of(EXT_TYPES[Path(f).suffix]),
                  "type": EXT_TYPES[Path(f).suffix], "url": BUILTIN_DIR + f, "builtin": True,
                  "created": 0}
            for mid, name, f in BUILTIN
        }
        for item in self.builtin.values():
            if item["kind"] == "video":  # a still for Admin's library grid
                item["poster"] = BUILTIN_DIR + "posters/" + Path(item["url"]).stem + ".webp"

    def _save(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items))
        tmp.replace(self.index_path)

    def all(self) -> list[dict]:
        uploads = sorted(self.items.values(), key=lambda m: m["created"], reverse=True)
        return uploads + list(self.builtin.values())

    def get(self, media_id: str) -> dict | None:
        return self.items.get(media_id) or self.builtin.get(media_id)

    @property
    def used_bytes(self) -> int:
        return sum(m.get("bytes", 0) for m in self.items.values())

    def usage(self) -> dict:
        return {"used_bytes": self.used_bytes, "max_bytes": self.max_total,
                "max_image_bytes": MAX_IMAGE_BYTES, "max_video_bytes": MAX_VIDEO_BYTES}

    def _limit(self, media_type: str) -> int:
        cap = MAX_VIDEO_BYTES if kind_of(media_type) == "video" else MAX_IMAGE_BYTES
        return min(cap, self.max_total - self.used_bytes)

    async def save(self, name: str, media_type: str, chunks: AsyncIterator[bytes]) -> dict:
        """Stream an upload to disk, enforcing the size caps as it arrives."""
        if media_type not in TYPES:
            raise MediaError("upload a JPEG, PNG, GIF or WebP image, or an MP4, WebM or MOV video",
                             415)
        limit = self._limit(media_type)
        if limit <= 0:
            raise MediaError("the media library is full; delete something first", 413)
        media_id = secrets.token_hex(6)
        path = self.dir / f"{media_id}{TYPES[media_type]}"
        size = 0
        try:
            with path.open("wb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise MediaError(self._too_big(media_type, limit), 413)
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if not size:
            path.unlink(missing_ok=True)
            raise MediaError("the file is empty")
        item = {"id": media_id, "name": (name.strip() or "Untitled")[:80],
                "kind": kind_of(media_type), "type": media_type, "bytes": size,
                "url": f"/media/{path.name}", "created": time.time()}
        self.items[media_id] = item
        self._save()
        return item

    def _too_big(self, media_type: str, limit: int) -> str:
        cap = MAX_VIDEO_BYTES if kind_of(media_type) == "video" else MAX_IMAGE_BYTES
        if limit < cap:
            return "not enough room left in the media library; delete something first"
        return f"{kind_of(media_type)}s can be up to {cap // 1_000_000} MB"

    async def save_from_url(self, url: str, name: str = "") -> dict:
        """Download a photo or video from a link into the library."""
        if not re.match(r"^https?://\S+$", url):
            raise MediaError("give a link starting with http:// or https://")
        try:
            async with (
                httpx.AsyncClient(follow_redirects=True, timeout=60) as client,
                client.stream("GET", url) as res,
            ):
                if res.status_code >= 400:
                    raise MediaError(f"the link answered HTTP {res.status_code}", 502)
                tail = url.split("?")[0].rsplit("/", 1)[-1]
                media_type = type_for(tail, res.headers.get("content-type", ""))
                default = re.sub(r"\.[A-Za-z0-9]+$", "", tail) or "Imported"
                return await self.save(name or default, media_type, res.aiter_bytes())
        except httpx.HTTPError as exc:
            raise MediaError(f"couldn't download the link: {exc}", 502) from exc

    def rename(self, media_id: str, name: str) -> dict:
        item = self.items.get(media_id)
        if not item:
            raise MediaError("built-in media can't be renamed" if media_id in self.builtin
                             else "unknown media", 404 if media_id not in self.builtin else 409)
        item["name"] = name.strip()[:80] or item["name"]
        self._save()
        return item

    def delete(self, media_id: str) -> bool:
        item = self.items.pop(media_id, None)
        if not item:
            return False
        (self.dir / item["url"].rsplit("/", 1)[-1]).unlink(missing_ok=True)
        self._save()
        return True
