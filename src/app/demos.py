"""Quest demo studio: guided sessions, explicitly paired headsets, and ephemeral view previews."""

import hashlib
import hmac
import json
import math
import re
import secrets
import struct
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.access import Caller, need
from app.catalog import SITES

ID = r"^[a-zA-Z0-9_-]{3,64}$"
MAX_FILE = 4_000_000_000
MAX_MODEL = 200_000_000
MAX_LIBRARY = 10_000_000_000
CHUNK = 32_000_000
UPLOAD_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".glb": "model/gltf-binary",
}


def fail(message, status=400):
    raise HTTPException(status, message)


class Step(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    asset_id: str = Field(pattern=ID)
    projection: Literal["flat", "360", "180"] = "flat"
    stereo: Literal["mono", "sbs", "top-bottom"] = "mono"
    note: str = Field(default="", max_length=1000)


class DemoIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    steps: list[Step] = Field(min_length=1, max_length=60)


class Enroll(BaseModel):
    id: str = Field(pattern=ID)
    key: str = Field(pattern=r"^[a-f0-9]{48}$")


class Claim(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")
    unit_id: str = Field(pattern=ID)


class UnitIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None


class Start(BaseModel):
    demo_id: str = Field(pattern=ID)
    unit_ids: list[str] = Field(min_length=1, max_length=24)
    auto_start: bool = True


class Control(BaseModel):
    action: Literal["play", "pause", "next", "previous", "seek", "stop"]
    position: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)


class QuestSettings(BaseModel):
    volume: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False)
    muted: bool = False
    scene_scale: float = Field(default=1, ge=0.25, le=3, allow_inf_nan=False)
    view_distance: float = Field(default=3, ge=1, le=8, allow_inf_nan=False)
    render_scale: float = Field(default=1.25, ge=0.5, le=1.5, allow_inf_nan=False)
    show_grid: bool = True
    allow_teleport: bool = True
    preview_enabled: bool = True


class Targets(BaseModel):
    unit_ids: list[str] = Field(min_length=1, max_length=24)


class Recovery(Targets):
    action: Literal["recenter", "reload_scene", "reload_app", "identify"]


class Configure(Targets):
    settings: QuestSettings


class Acknowledgement(BaseModel):
    id: str = Field(pattern=ID)
    status: Literal["applied", "failed"]
    error: str = Field(default="", max_length=200)


class UploadStart(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=80)
    size: int = Field(gt=0)


class Download(BaseModel):
    id: str = Field(pattern=ID)
    received: int = Field(ge=0)
    total: int = Field(ge=0)


class Beat(BaseModel):
    app_mode: Literal["browser", "installed"] = "browser"
    settings_revision: int = Field(default=0, ge=0)
    command_acks: list[Acknowledgement] = Field(default_factory=list, max_length=20)
    ready_token: str = Field(default="", max_length=120)
    state: Literal["standby", "loading", "ready", "playing", "paused", "ended", "error"] = "standby"
    error: str = Field(default="", max_length=300)
    immersive: bool = False
    vr_supported: bool | None = None
    visibility: str = Field(default="visible", max_length=32)
    position: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)
    battery: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    orientation: list[float] = Field(default_factory=list, max_length=4)
    # Demo files saved on the headset, and the one it is downloading now.
    cached: list[Annotated[str, Field(pattern=ID)]] = Field(default_factory=list, max_length=500)
    download: Download | None = None
    storage_error: str = Field(default="", max_length=200)
    duration: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)
    # Whether the current scene plays from the headset's saved copy rather than a stream.
    local: bool | None = None


class Enabled(BaseModel):
    enabled: bool


class Demos:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "demos.json"
        self.asset_dir = data_dir / "media" / "quest"
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {
                "enabled": True,
                "demos": {},
                "assets": {},
                "devices": {},
                "sessions": {},
                "units": {
                    f"{site}-{i:02}": {
                        "id": f"{site}-{i:02}",
                        "site": site,
                        "name": f"{site.title()} Quest {i:02}",
                        "enabled": True,
                        "device_id": "",
                        "session_id": "",
                    }
                    for site in (s["id"] for s in SITES)  # six headset slots per site
                    for i in range(1, 7)
                },
            }
        # Live telemetry and previews expire on a server restart. Never record a headset feed.
        self.live = {}
        self.previews = {}
        # When someone last looked at each headset's view: watched headsets send it every second.
        self.watched = {}
        self.commands = {}  # Deliberately transient: never replay recovery after a server restart.
        self.data.setdefault("unit_settings", {})

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)

    def enroll(self, body: Enroll):
        devices = self.data["devices"]
        digest = hashlib.sha256(body.key.encode()).hexdigest()
        d = devices.get(body.id)
        if d and not hmac.compare_digest(d["key_hash"], digest):
            fail("This headset identity already has a different key", 401)
        if not d:
            expired = [
                k
                for k, v in devices.items()
                if not v.get("unit_id") and v["created"] < time.time() - 900
            ]
            for k in expired:
                del devices[k]
            if sum(not v.get("unit_id") for v in devices.values()) >= 64:
                fail("Too many pending headsets. Try again later.", 429)
            used = {v.get("code") for v in devices.values()}
            code = f"{secrets.randbelow(1_000_000):06}"
            while code in used:
                code = f"{secrets.randbelow(1_000_000):06}"
            d = devices[body.id] = {
                "key_hash": digest,
                "code": code,
                "created": time.time(),
                "unit_id": "",
            }
            self.save()
        if not d["unit_id"] and d["created"] < time.time() - 900:
            used = {v.get("code") for v in devices.values()}
            code = f"{secrets.randbelow(1_000_000):06}"
            while code in used:
                code = f"{secrets.randbelow(1_000_000):06}"
            d["code"] = code
            d["created"] = time.time()
            self.save()
        return {"paired": bool(d["unit_id"]), "code": "" if d["unit_id"] else d["code"]}

    def authenticate(self, request):
        did = request.headers.get("x-xos-quest", "")
        key = request.headers.get("x-xos-key", "")
        d = self.data["devices"].get(did)
        if not d or not hmac.compare_digest(
            d["key_hash"], hashlib.sha256(key.encode()).hexdigest()
        ):
            fail("Headset access is unavailable. Reopen setup.", 401)
        return did, d

    def claim(self, body: Claim):
        unit = self.data["units"].get(body.unit_id)
        if not unit:
            fail("Unknown headset slot", 404)
        d = next(
            (
                (did, d)
                for did, d in self.data["devices"].items()
                if d["code"] == body.code and not d["unit_id"] and d["created"] > time.time() - 900
            ),
            None,
        )
        if not d:
            fail("That pairing code has expired or is already used", 404)
        if unit["device_id"]:
            fail("Unpair the current headset from this slot first", 409)
        did, record = d
        record["unit_id"] = unit["id"]
        record["code"] = ""
        unit["device_id"] = did
        self.save()
        return unit

    @staticmethod
    def position(s, now=None):
        now = time.time() if now is None else now
        return s["position"] + (max(0, now - s["anchor_at"]) if s["playing"] else 0)

    def token(self, s):
        demo = self.data["demos"][s["demo_id"]]
        return f"{s['id']}:{s['step_index']}:{demo['updated']}"

    def session(self, s, lookup):
        demo = self.data["demos"].get(s["demo_id"])
        if not demo:
            return None
        step = demo["steps"][s["step_index"]]
        return {
            **s,
            "demo_name": demo["name"],
            "step": step,
            "steps": len(demo["steps"]),
            "asset": lookup(step["asset_id"]),
            "step_token": self.token(s),
        }

    def settings(self, uid):
        return self.data["unit_settings"].get(
            uid, {"revision": 1, "values": QuestSettings().model_dump()}
        )

    def command_history(self, did):
        history = self.commands.get(did, [])
        for command in history:
            if command["status"] == "queued" and command["expires"] <= time.time():
                command["status"] = "expired"
        return history

    def targets(self, ids):
        units = [self.data["units"].get(uid) for uid in dict.fromkeys(ids)]
        if any(not u or not u["device_id"] for u in units):
            fail("Choose paired headsets", 409)
        return units

    def recover(self, body: Recovery):
        units = self.targets(body.unit_ids)
        now = time.time()
        for u in units:
            live = self.live.get(u["device_id"], {})
            if now - live.get("last_seen", 0) >= 10 or live.get("visibility") != "visible":
                fail("Wake every selected headset and open Xos before sending recovery", 409)
        issued = []
        for u in units:
            did = u["device_id"]
            history = self.command_history(did)
            if sum(c["status"] == "queued" for c in history) >= 4:
                fail("Wait for pending recovery actions to finish", 409)
        for u in units:
            did = u["device_id"]
            command = {
                "id": secrets.token_hex(12),
                "action": body.action,
                "status": "queued",
                "created": now,
                "expires": now + 20,
            }
            self.commands[did] = [*self.command_history(did)[-19:], command]
            issued.append({"unit_id": u["id"], **command})
        return {"commands": issued}

    def state(self, did, lookup):
        d = self.data["devices"][did]
        unit = self.data["units"].get(d["unit_id"])
        s = self.data["sessions"].get(unit["session_id"]) if unit else None
        active = self.data["enabled"] and unit and unit["enabled"]
        return {
            "now": time.time(),
            "settings": self.settings(unit["id"]) if unit else None,
            "commands": [c for c in self.command_history(did) if c["status"] == "queued"],
            "enabled": bool(active),
            "unit": unit,
            "session": self.session(s, lookup) if active and s else None,
            "library": self.library(lookup) if unit else [],
            "preview_every": 1
            if unit and time.time() - self.watched.get(unit["id"], 0) < 15
            else 5,
        }

    def library(self, lookup):
        """Every file a demo uses: paired headsets keep a copy and play from it."""
        files = {}
        for demo in self.data["demos"].values():
            for step in demo["steps"]:
                a = lookup(step["asset_id"])
                if a and a.get("url"):
                    files[a["id"]] = {
                        "id": a["id"],
                        "url": a["url"],
                        "bytes": a.get("bytes", 0),
                        "kind": a.get("kind", ""),
                    }
        return list(files.values())

    def heartbeat(self, did, body: Beat, lookup):
        for ack in body.command_acks:
            for command in self.command_history(did):
                if command["id"] == ack.id and command["status"] in ("queued", "expired"):
                    command.update(status=ack.status, error=ack.error)
        self.live[did] = {**body.model_dump(exclude={"command_acks"}), "last_seen": time.time()}
        unit_id = self.data["devices"][did]["unit_id"]
        unit = self.data["units"].get(unit_id)
        s = self.data["sessions"].get(unit["session_id"]) if unit else None
        if s and s["preparing"] and self.data["enabled"]:
            token = self.token(s)
            active = [
                uid for uid in s["unit_ids"] if self.data["units"][uid]["session_id"] == s["id"]
            ]
            if active and all(
                self.live.get(self.data["units"][uid]["device_id"], {}).get("ready_token") == token
                and self.live[self.data["units"][uid]["device_id"]]["last_seen"] > time.time() - 8
                and self.live[self.data["units"][uid]["device_id"]]["state"] != "error"
                and self.live[self.data["units"][uid]["device_id"]]["visibility"] != "hidden"
                for uid in active
            ):
                s.update(preparing=False, playing=True, anchor_at=time.time() + 2)
                s["revision"] += 1
                self.save()
        return self.state(did, lookup)

    def overview(self, lookup):
        units = []
        now = time.time()
        for unit in self.data["units"].values():
            live = self.live.get(unit["device_id"], {})
            preview = self.previews.get(unit["id"])
            units.append(
                {
                    **unit,
                    "paired": bool(unit["device_id"]),
                    "online": now - live.get("last_seen", 0) < 10,
                    "telemetry": live,
                    "settings": self.settings(unit["id"]),
                    "commands": self.command_history(unit["device_id"]),
                    "preview_at": preview[0] if preview else 0,
                }
            )
        return {
            "enabled": self.data["enabled"],
            "now": now,
            "units": units,
            "demos": list(self.data["demos"].values()),
            "sessions": [self.session(s, lookup) for s in self.data["sessions"].values()],
            "pending": [
                {"id": did, "code": d["code"], "created": d["created"]}
                for did, d in self.data["devices"].items()
                if not d["unit_id"] and d["created"] > now - 900
            ],
        }

    def create_session(self, body: Start):
        if not self.data["enabled"]:
            fail("Enable demo mode first", 409)
        if body.demo_id not in self.data["demos"]:
            fail("Choose a saved demo", 404)
        ids = list(dict.fromkeys(body.unit_ids))
        for uid in ids:
            u = self.data["units"].get(uid)
            if not u or not u["enabled"] or not u["device_id"]:
                fail("Choose enabled, paired headsets")
        for s in self.data["sessions"].values():
            if set(s["unit_ids"]) & set(ids):
                self.control(s["id"], Control(action="stop"), save=False)
        sid = secrets.token_hex(6)
        s = self.data["sessions"][sid] = {
            "id": sid,
            "demo_id": body.demo_id,
            "unit_ids": ids,
            "step_index": 0,
            "playing": False,
            "preparing": body.auto_start,
            "position": 0,
            "anchor_at": time.time(),
            "revision": 1,
            "created": time.time(),
        }
        for uid in ids:
            self.data["units"][uid]["session_id"] = sid
        # Keep a small session history; never remove one still assigned to a headset.
        assigned = {u["session_id"] for u in self.data["units"].values()}
        for old in list(self.data["sessions"]):
            if len(self.data["sessions"]) <= 30:
                break
            if old not in assigned:
                del self.data["sessions"][old]
        self.save()
        return s

    def control(self, sid, body: Control, save=True):
        s = self.data["sessions"].get(sid)
        if not s:
            fail("Unknown demo session", 404)
        if body.action == "play" and not self.data["enabled"]:
            fail("Enable demo mode first", 409)
        s["position"] = self.position(s)
        s["anchor_at"] = time.time()
        s["preparing"] = False
        if body.action == "play":
            s.update(playing=False, preparing=True)  # arm when every assigned headset is ready
        elif body.action in ("pause", "stop"):
            s["playing"] = False
        elif body.action in ("next", "previous"):
            n = len(self.data["demos"][s["demo_id"]]["steps"])
            direction = 1 if body.action == "next" else -1
            s.update(
                step_index=max(0, min(n - 1, s["step_index"] + direction)),
                position=0,
                playing=False,
                preparing=True,
            )
        elif body.action == "seek":
            s["position"] = body.position
            if s["playing"]:
                s["anchor_at"] = time.time() + 2
        if body.action == "stop":
            s["position"] = 0
            for uid in s["unit_ids"]:
                if self.data["units"][uid]["session_id"] == sid:
                    self.data["units"][uid]["session_id"] = ""
        s["revision"] += 1
        if save:
            self.save()
        return s


def validate_glb(path: Path):
    """Accept self-contained GLB 2.0, never external file/URL references or executable pages."""
    with path.open("rb") as f:
        header = f.read(20)
        if len(header) != 20:
            fail("Incomplete GLB file")
        magic, version, size, json_size, chunk_type = struct.unpack("<4sIIII", header)
        if (
            magic != b"glTF"
            or version != 2
            or size != path.stat().st_size
            or chunk_type != 0x4E4F534A
            or json_size > min(size - 20, 16_000_000)
        ):
            fail("Upload a valid GLB 2.0 scene")
        try:
            doc = json.loads(f.read(json_size))
        except (ValueError, UnicodeError):
            fail("Invalid GLB scene metadata")
        if not isinstance(doc, dict) or not isinstance(doc.get("asset"), dict):
            fail("Invalid GLB scene metadata")
        if doc["asset"].get("version") != "2.0":
            fail("Use GLB 2.0")
        for field in ("buffers", "images"):
            values = doc.get(field, [])
            if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
                fail("Invalid GLB buffers or textures")
            if any(
                v.get("uri") is not None
                and (not isinstance(v["uri"], str) or not v["uri"].startswith("data:"))
                for v in values
            ):
                fail("Export a self-contained GLB with embedded textures and buffers")
        extensions = doc.get("extensionsRequired", [])
        if not isinstance(extensions, list) or not all(isinstance(v, str) for v in extensions):
            fail("Invalid GLB extension list")
        unsupported = set(extensions) & {
            "KHR_draco_mesh_compression",
            "KHR_texture_basisu",
            "EXT_meshopt_compression",
        }
        if unsupported:
            fail("Export GLB without Draco, Meshopt or KTX2 compression for this viewer")


def build_router(store: Demos, media, requires):
    r = APIRouter(prefix="/api/demos", tags=["Quest demos"])

    def lookup(aid):
        return store.data["assets"].get(aid) or media.get(aid)

    @r.get("/studio", dependencies=[requires("demos.view")])
    async def overview():
        return {
            **store.overview(lookup),
            "assets": [
                *store.data["assets"].values(),
                *(a for a in media.all() if a["type"] != "video/quicktime"),
            ],
        }

    @r.post("/recovery", dependencies=[requires("demos.recover")])
    async def recovery(body: Recovery):
        return store.recover(body)

    @r.put("/settings", dependencies=[requires("demos.settings")])
    async def configure(body: Configure):
        units = store.targets(body.unit_ids)
        for unit in units:
            store.data["unit_settings"][unit["id"]] = {
                "revision": store.settings(unit["id"])["revision"] + 1,
                "values": body.settings.model_dump(),
            }
        store.save()
        return {"ok": True}

    def upload_type(filename):
        ext = Path(filename).suffix.lower()
        mime = UPLOAD_TYPES.get(ext)
        if not mime:
            fail("Use MP4, WebM, JPG, PNG, WebP, or a self-contained GLB", 415)
        used = sum(a["bytes"] for a in store.data["assets"].values())
        cap = min(MAX_MODEL if ext == ".glb" else MAX_FILE, MAX_LIBRARY - used)
        if cap <= 0:
            fail("The demo library is full", 413)
        return ext, mime, cap

    def register(aid, path, ext, mime, name, filename, size):
        """Adds a fully received file to the library, or removes it if it doesn't fit."""
        try:
            if not size:
                fail("The upload is empty")
            if sum(a["bytes"] for a in store.data["assets"].values()) + size > MAX_LIBRARY:
                fail("The demo library is full", 413)
            if ext == ".glb":
                validate_glb(path)
            item = {
                "id": aid,
                "name": (name.strip() or Path(filename).stem)[:80],
                "kind": "model"
                if ext == ".glb"
                else ("video" if mime.startswith("video") else "image"),
                "type": mime,
                "bytes": size,
                "url": f"/media/quest/{path.name}",
                "created": time.time(),
            }
            store.data["assets"][aid] = item
            store.save()
            return item
        except BaseException:
            path.unlink(missing_ok=True)
            store.data["assets"].pop(aid, None)
            raise

    @r.post("/assets", dependencies=[requires("demos.upload")])
    async def upload(request: Request, filename: str, name: str = ""):
        ext, mime, cap = upload_type(filename)
        aid = "xr-" + secrets.token_hex(6)
        path = store.asset_dir / (aid + ext)
        size = 0
        try:
            with path.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > cap:
                        fail("Upload exceeds the remaining storage or file limit", 413)
                    f.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return register(aid, path, ext, mime, name, filename, size)

    # Large files go up in pieces of at most CHUNK bytes, so no single request is big enough for
    # a proxy to refuse, and an interrupted upload resumes from what the server already has.
    uploads: dict[str, dict] = {}

    @r.post("/uploads", dependencies=[requires("demos.upload")])
    async def start_upload(body: UploadStart):
        ext, mime, cap = upload_type(body.filename)
        if body.size > cap:
            fail("Upload exceeds the remaining storage or file limit", 413)
        for uid, u in list(uploads.items()):
            if u["touched"] < time.time() - 3600:
                u["path"].unlink(missing_ok=True)
                del uploads[uid]
        # Pieces of an upload the server restarted during belong to no asset: remove them.
        for f in store.asset_dir.glob("xr-*") if store.asset_dir.exists() else []:
            if f.stem not in store.data["assets"] and f.stem not in uploads:
                if f.stat().st_mtime < time.time() - 3600:
                    f.unlink(missing_ok=True)
        aid = "xr-" + secrets.token_hex(6)
        store.asset_dir.mkdir(parents=True, exist_ok=True)
        path = store.asset_dir / (aid + ext)
        path.write_bytes(b"")
        uploads[aid] = {
            "path": path,
            "ext": ext,
            "mime": mime,
            "name": body.name,
            "filename": body.filename,
            "size": body.size,
            "touched": time.time(),
        }
        return {"upload": aid, "chunk": CHUNK, "received": 0}

    def pending(uid):
        u = uploads.get(uid)
        if not u:
            fail("This upload expired. Start it again.", 404)
        return u

    @r.put("/uploads/{uid}", dependencies=[requires("demos.upload")])
    async def upload_chunk(uid: str, request: Request, offset: int):
        u = pending(uid)
        received = u["path"].stat().st_size
        if offset != received:
            return {"received": received}
        data = bytearray()
        async for piece in request.stream():
            data.extend(piece)
            if len(data) > CHUNK or received + len(data) > u["size"]:
                fail("This piece is too large", 413)
        with u["path"].open("ab") as f:
            f.write(data)
        u["touched"] = time.time()
        return {"received": received + len(data)}

    @r.post("/uploads/{uid}/done", dependencies=[requires("demos.upload")])
    async def finish_upload(uid: str):
        u = pending(uid)
        size = u["path"].stat().st_size
        if size != u["size"]:
            fail(f"Received {size} of {u['size']} bytes. Resume the upload.", 409)
        del uploads[uid]
        return register(uid, u["path"], u["ext"], u["mime"], u["name"], u["filename"], size)

    @r.delete("/assets/{aid}", dependencies=[requires("demos.delete")])
    async def delete_asset(aid: str):
        a = store.data["assets"].get(aid)
        if not a:
            fail("Unknown uploaded demo asset", 404)
        if any(s["asset_id"] == aid for d in store.data["demos"].values() for s in d["steps"]):
            fail("Remove this asset from its demos before deleting it", 409)
        (store.asset_dir / Path(a["url"]).name).unlink(missing_ok=True)
        del store.data["assets"][aid]
        store.save()
        return {"ok": True}

    @r.put("/library/{demo_id}", dependencies=[requires("demos.edit")])
    async def save_demo(demo_id: str, body: DemoIn):
        if not re.fullmatch(ID, demo_id):
            fail("Invalid demo id")
        if any(not lookup(s.asset_id) for s in body.steps):
            fail("Every step must use an existing asset")
        if any(
            u["session_id"] and store.data["sessions"][u["session_id"]]["demo_id"] == demo_id
            for u in store.data["units"].values()
        ):
            fail("Stop this demo's active sessions before editing it", 409)
        item = {**body.model_dump(), "id": demo_id, "updated": int(time.time() * 1000)}
        store.data["demos"][demo_id] = item
        store.save()
        return item

    @r.delete("/library/{demo_id}", dependencies=[requires("demos.delete")])
    async def delete_demo(demo_id: str):
        if any(
            u["session_id"] and store.data["sessions"][u["session_id"]]["demo_id"] == demo_id
            for u in store.data["units"].values()
        ):
            fail("Stop the demo's sessions first", 409)
        if demo_id not in store.data["demos"]:
            fail("Unknown demo", 404)
        del store.data["demos"][demo_id]
        store.save()
        return {"ok": True}

    @r.post("/enroll")
    async def enroll(body: Enroll):
        return store.enroll(body)

    @r.post("/pair", dependencies=[requires("demos.pair")])
    async def pair(body: Claim):
        return store.claim(body)

    @r.patch("/units/{uid}")
    async def unit(uid: str, body: UnitIn, caller: Caller = requires("demos.view")):
        u = store.data["units"].get(uid)
        if not u:
            fail("Unknown headset slot", 404)
        changes = body.model_dump(exclude_none=True)
        if "name" in changes:
            need(caller, "demos.rename")
        if "enabled" in changes:
            need(caller, "demos.enable")
        if changes.get("enabled") is False:
            u["session_id"] = ""
        u.update(changes)
        store.save()
        return u

    @r.delete("/units/{uid}/pair", dependencies=[requires("demos.pair")])
    async def unpair(uid: str):
        u = store.data["units"].get(uid)
        if not u:
            fail("Unknown headset slot", 404)
        did = u["device_id"]
        store.data["devices"].pop(did, None)
        store.live.pop(did, None)
        store.commands.pop(did, None)
        store.previews.pop(uid, None)
        u.update(device_id="", session_id="")
        store.save()
        return {"ok": True}

    @r.put("/enabled", dependencies=[requires("demos.enable")])
    async def enabled(body: Enabled):
        store.data["enabled"] = body.enabled
        if not body.enabled:
            for sid in store.data["sessions"]:
                store.control(sid, Control(action="pause"), save=False)
        store.save()
        return {"enabled": body.enabled}

    @r.post("/sessions", dependencies=[requires("demos.present")])
    async def start(body: Start):
        return store.create_session(body)

    @r.post("/sessions/{sid}/control", dependencies=[requires("demos.control")])
    async def control(sid: str, body: Control):
        return store.control(sid, body)

    @r.post("/headset/heartbeat")
    async def heartbeat(request: Request, body: Beat):
        did, _ = store.authenticate(request)
        if body.orientation and (
            len(body.orientation) != 4
            or not all(math.isfinite(v) and abs(v) <= 1 for v in body.orientation)
        ):
            fail("Invalid orientation")
        return store.heartbeat(did, body, lookup)

    @r.get("/headset/state")
    async def headset_state(request: Request):
        did, _ = store.authenticate(request)
        return store.state(did, lookup)

    @r.post("/headset/preview")
    async def preview(request: Request):
        _, d = store.authenticate(request)
        uid = d["unit_id"]
        if not uid:
            fail("Pair this headset first", 403)
        if not store.settings(uid)["values"]["preview_enabled"]:
            fail("App previews are disabled for this headset", 403)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 600_000:
                fail("Preview is too large", 413)
        if not data.startswith(b"\xff\xd8\xff"):
            fail("Upload a JPEG preview", 415)
        store.previews[uid] = (time.time(), bytes(data))
        return {"ok": True}

    @r.get(
        "/units/{uid}/preview", dependencies=[requires("demos.view"), requires("demos.previews")]
    )
    async def get_preview(uid: str, request: Request):
        store.watched[uid] = time.time()
        entry = (
            store.previews.get(uid) if store.settings(uid)["values"]["preview_enabled"] else None
        )
        if not entry or entry[0] < time.time() - 15:
            return Response(status_code=204)
        # Each frame is tagged with when it arrived, so a viewer polling every second only
        # downloads new frames.
        tag = f'"{entry[0]:.3f}"'
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-store"})
        return Response(
            entry[1], media_type="image/jpeg", headers={"Cache-Control": "no-store", "ETag": tag}
        )

    return r
