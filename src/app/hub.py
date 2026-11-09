"""The device registry, device command streams, pairing codes and WebRTC signalling relay.

One process holds it all (run a single uvicorn worker). Devices are saved to XOS_DATA_DIR so a
restart picks up where it left off; command streams and signalling live in memory.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_LIMIT = 100
COMMAND_HISTORY = 20
COMMAND_TTL_S = 600  # undelivered commands expire so a TV doesn't act on stale clicks later
PAIR_TTL_S = 3600  # a setup-screen code is valid for an hour, then the screen asks for a new one
# Share links (/s/<code>): easy to read off a TV and type on a phone; no 0/o, 1/l/i.
SHARE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
SHARE_CODE_LEN = 5
# Taking control of a screen (#/control): the hold lapses after this long without a command.
CONTROL_IDLE_S = 600


class Hub:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.devices: dict[str, dict] = {}
        self.zoom_log: list[dict] = []
        # Device control: commands per device id, and each device's open command streams.
        self.commands: dict[str, list[dict]] = {}
        self._device_streams: dict[str, set[asyncio.Queue]] = {}
        # Pairing: 6-digit code shown on a new screen's setup page -> device id.
        self.pair_codes: dict[str, dict] = {}
        # Live camera feeds (WebRTC signalling): channel -> peer id -> that peer's message queues.
        self.rtc: dict[str, dict[str, set[asyncio.Queue]]] = {}
        # Share to a screen: device id -> what is being shared on it right now.
        self.shares: dict[str, dict] = {}
        # Remotes and Admin watching for changes (GET /api/devices/events): live, not polled.
        self._watchers: set[asyncio.Queue] = set()
        # Device keys: device id -> sha256 of the key a trusted remote sends with its commands.
        # Kept apart from devices.json so no device listing can ever include one.
        self.device_keys: dict[str, str] = {}
        # Who is controlling a screen right now (#/control): device id -> {email, name, since, last}.
        self.controls: dict[str, dict] = {}
        self._load()

    # ---- persistence ----

    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def _load(self) -> None:
        try:
            self.devices = json.loads(self._path("devices.json").read_text())
        except (FileNotFoundError, ValueError):
            pass
        try:
            self.device_keys = json.loads(self._path("device_keys.json").read_text())
        except (FileNotFoundError, ValueError):
            pass

    def _save(self, name: str, payload: object) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path(name + ".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._path(name))
        except OSError as exc:  # state still lives in memory; log and carry on
            logger.warning("could not save %s: %s", name, exc)

    # ---- change events, for remotes and Admin ----

    def watch(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._watchers.add(queue)
        return queue

    def unwatch(self, queue: asyncio.Queue) -> None:
        self._watchers.discard(queue)

    def notify(self, device_id: str, kind: str = "device", **extra: object) -> None:
        """Tell every watcher that a device changed (they re-read /api/devices) or a command moved."""
        line = f"data: {json.dumps({'device': device_id, 'kind': kind, **extra})}\n\n"
        for queue in list(self._watchers):
            if not queue.full():
                queue.put_nowait(line)

    # ---- devices ----

    # Heartbeat fields a remote shows; only a change to one of these is worth an event.
    _VISIBLE = ("role", "arg", "name", "room", "label", "native", "remote_access", "wake_lock")

    def heartbeat(self, device: dict) -> dict:
        now = time.time()
        known = self.devices.get(device["id"], {})
        record = {**known, **device, "last_seen": now, "first_seen": known.get("first_seen", now)}
        record.setdefault("share_code", self._new_share_code())
        self.devices[device["id"]] = record
        self._save("devices.json", self.devices)
        stale = now - known.get("last_seen", 0) > 90  # it was offline: tell remotes it's back
        if stale or any(known.get(k) != record.get(k) for k in self._VISIBLE):
            self.notify(device["id"])
        return record

    def forget(self, device_id: str) -> bool:
        if self.devices.pop(device_id, None) is None:
            return False
        self.commands.pop(device_id, None)
        self.shares.pop(device_id, None)
        self.controls.pop(device_id, None)
        if self.device_keys.pop(device_id, None):
            self._save("device_keys.json", self.device_keys)
        self.screenshot_path(device_id).unlink(missing_ok=True)
        self.share_media_path(device_id).unlink(missing_ok=True)
        for code in [c for c, p in self.pair_codes.items() if p["device"] == device_id]:
            del self.pair_codes[code]
        self._save("devices.json", self.devices)
        self.notify(device_id, "forgotten")
        return True

    # ---- pairing (Admin → Screens → Add a screen) ----

    def pair_code(self, device_id: str) -> str:
        """The code a setup screen shows; the same one until it expires or is used."""
        now = time.time()
        for code, p in list(self.pair_codes.items()):
            if now - p["created"] > PAIR_TTL_S:
                del self.pair_codes[code]
            elif p["device"] == device_id:
                return code
        code = f"{secrets.randbelow(1_000_000):06d}"
        while code in self.pair_codes:
            code = f"{secrets.randbelow(1_000_000):06d}"
        self.pair_codes[code] = {"device": device_id, "created": now}
        return code

    def claim(self, code: str) -> str | None:
        """Device id for a valid code; the code is used up."""
        p = self.pair_codes.pop(code, None)
        if not p or time.time() - p["created"] > PAIR_TTL_S:
            return None
        return p["device"]

    def set_room(self, device_id: str, room: str) -> dict:
        record = self.devices[device_id]
        if room:
            record["room"] = room
        else:
            record.pop("room", None)
        self._save("devices.json", self.devices)
        self.notify(device_id)
        return record

    def rename(self, device_id: str, name: str) -> dict:
        record = self.devices[device_id]
        if name:
            record["name"] = name
        else:
            record.pop("name", None)
        self._save("devices.json", self.devices)
        self.notify(device_id)
        return record

    def mark_adb(self, device_id: str, ip: str) -> None:
        """This server set the screen up over ADB at `ip`, so the screen allows its key."""
        self.devices[device_id]["adb_ip"] = ip
        self._save("devices.json", self.devices)

    # ---- device keys: a trusted remote proves it is that device ----

    @staticmethod
    def _digest(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def issue_key(self, device_id: str, proposed: str = "") -> str | None:
        """A new key for a device that has none yet; None once it has one (it is shown once)."""
        if device_id in self.device_keys:
            return None
        key = proposed or secrets.token_urlsafe(24)
        self.device_keys[device_id] = self._digest(key)
        self._save("device_keys.json", self.device_keys)
        return key

    def revoke_key(self, device_id: str) -> None:
        self.device_keys.pop(device_id, None)
        self._save("device_keys.json", self.device_keys)
        if device_id in self.devices:
            self.devices[device_id]["remote_access"] = "needs_reset"
            self._save("devices.json", self.devices)
            self.notify(device_id)

    def check_key(self, device_id: str, key: str) -> bool:
        want = self.device_keys.get(device_id)
        return bool(want and key) and hmac.compare_digest(want, self._digest(key))

    # ---- control: one person at a time drives a screen from #/control ----

    def controller(self, device_id: str) -> dict | None:
        hold = self.controls.get(device_id)
        if hold and time.time() - hold["last"] > CONTROL_IDLE_S:
            del self.controls[device_id]
            self.notify(device_id)
            return None
        return hold

    def take_control(self, device_id: str, email: str, name: str) -> dict | None:
        """Hold the screen for `email`; None when someone else holds it."""
        hold = self.controller(device_id)
        if hold and hold["email"] != email:
            return None
        now = time.time()
        hold = {"email": email, "name": name, "since": hold["since"] if hold else now, "last": now}
        self.controls[device_id] = hold
        self.notify(device_id)
        return hold

    def touch_control(self, device_id: str, email: str) -> bool:
        """A command from the holder keeps the hold; False if `email` doesn't hold the screen."""
        hold = self.controller(device_id)
        if not hold or hold["email"] != email:
            return False
        hold["last"] = time.time()
        return True

    def release_control(self, device_id: str) -> None:
        if self.controls.pop(device_id, None):
            self.notify(device_id)

    def device_list(self, online_s: int) -> list[dict]:
        now = time.time()
        rows = []
        for d in self.devices.values():
            hold = self.controller(d["id"])
            # Invitation passcodes are available only through the authorized room endpoint.
            d = {**d}
            if isinstance((d.get("native") or {}).get("meeting"), dict):
                meeting = {
                    k: v
                    for k, v in d["native"]["meeting"].items()
                    if k not in ("join_url", "controls", "control_labels", "control_diagnostics")
                }
                d["native"] = {**d["native"], "meeting": meeting}
            rows.append(
                {
                    **d,
                    "connected": self.connected(d["id"]),
                    "sharing": self.shares.get(d["id"]),
                    "online": self.connected(d["id"]) or now - d["last_seen"] < online_s,
                    "controlled_by": {"name": hold["name"], "since": hold["since"]}
                    if hold
                    else None,
                }
            )
        return sorted(rows, key=lambda d: d["last_seen"], reverse=True)

    def connected(self, device_id: str) -> bool:
        return bool(self._device_streams.get(device_id))

    # ---- device commands ----

    def queue_command(self, device_id: str, kind: str, args: dict, status: str = "pending") -> dict:
        """Queue a command for the device. With status "sent", the server runs it itself (Switch
        TV to Xos): it shows in the history like any command, but never goes to the device."""
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": kind,
            "args": args,
            "created": time.time(),
            "status": status,
            "result": None,
        }
        if kind == "screenshot":
            cmd["args"] = {**args, "capture_id": cmd["id"]}
        history = self.commands.setdefault(device_id, [])
        history.insert(0, cmd)
        del history[COMMAND_HISTORY:]
        if status == "pending":
            self._deliver(device_id)
        self.notify(device_id, "command", command=cmd)
        return cmd

    def progress(self, cmd: dict, device_id: str, result: str) -> None:
        """A step of a command the server runs, shown as its result until it finishes."""
        cmd["result"] = result
        self.notify(device_id, "command", command=cmd)

    def _deliver(self, device_id: str) -> None:
        streams = self._device_streams.get(device_id)
        if not streams:
            return
        now = time.time()
        for cmd in reversed(self.commands.get(device_id, [])):
            if cmd["status"] != "pending":
                continue
            if now - cmd["created"] > COMMAND_TTL_S:
                cmd["status"] = "expired"
                continue
            message = f"data: {json.dumps(cmd)}\n\n"
            for queue in list(streams):
                if not queue.full():
                    queue.put_nowait(message)
            cmd["status"] = "sent"

    def subscribe_device(self, device_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._device_streams.setdefault(device_id, set()).add(queue)
        self._deliver(device_id)  # anything queued while the device was away
        self.notify(device_id)
        return queue

    def unsubscribe_device(self, device_id: str, queue: asyncio.Queue) -> None:
        streams = self._device_streams.get(device_id, set())
        streams.discard(queue)
        if not streams:
            self._device_streams.pop(device_id, None)
            self.notify(device_id)

    def ack(self, device_id: str, cmd_id: str, ok: bool, result: object) -> dict | None:
        for cmd in self.commands.get(device_id, []):
            if cmd["id"] == cmd_id:
                cmd.update(status="done" if ok else "failed", result=result, acked=time.time())
                self.notify(device_id, "command", command=cmd)
                return cmd
        return None

    # ---- live camera feeds: relay WebRTC offers / answers / ICE between peers ----

    def rtc_join(self, channel: str, peer: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.rtc.setdefault(channel, {}).setdefault(peer, set()).add(queue)
        return queue

    def rtc_leave(self, channel: str, peer: str, queue: asyncio.Queue) -> None:
        peers = self.rtc.get(channel, {})
        queues = peers.get(peer, set())
        queues.discard(queue)
        if not queues:
            peers.pop(peer, None)
            self.rtc_send(channel, {"from": peer, "to": "*", "type": "bye"})
        if not peers:
            self.rtc.pop(channel, None)

    def rtc_peers(self, channel: str) -> list[str]:
        return sorted(self.rtc.get(channel, {}))

    def rtc_send(self, channel: str, msg: dict) -> int:
        """Deliver to one peer, or to everyone else when to == "*". Returns how many got it."""
        peers = self.rtc.get(channel, {})
        targets = [p for p in peers if p != msg["from"]] if msg["to"] == "*" else [msg["to"]]
        line = f"data: {json.dumps(msg)}\n\n"
        sent = 0
        for peer in targets:
            for queue in list(peers.get(peer, ())):
                if not queue.full():
                    queue.put_nowait(line)
                    sent += 1
        return sent

    # ---- screenshots ----

    def save_screenshot(
        self, device_id: str, data: bytes, media_type: str, capture_id: str = ""
    ) -> bool:
        if capture_id:
            latest = next(
                (c for c in self.commands.get(device_id, []) if c["type"] == "screenshot"),
                None,
            )
            if latest is None or latest["id"] != capture_id:
                return False
        folder = self.data_dir / "screens"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{device_id}.img").write_bytes(data)
        if device_id in self.devices:
            self.devices[device_id]["screenshot"] = {
                "at": time.time(),
                "type": media_type,
                "capture_id": capture_id,
            }
            self._save("devices.json", self.devices)
            self.notify(device_id, "screenshot")
        return True

    def screenshot_path(self, device_id: str) -> Path:
        return self.data_dir / "screens" / f"{device_id}.img"

    # ---- share to a screen (/s/<code>) ----

    def _new_share_code(self) -> str:
        taken = {d.get("share_code") for d in self.devices.values()}
        while True:
            code = "".join(secrets.choice(SHARE_ALPHABET) for _ in range(SHARE_CODE_LEN))
            if code not in taken:
                return code

    def share_device(self, code: str) -> str | None:
        """Device id for a share code, whether or not sharing is turned on."""
        code = code.lower()
        return next((i for i, d in self.devices.items() if d.get("share_code") == code), None)

    def set_sharing(
        self, device_id: str, enabled: bool | None, new_code: bool, badge: bool | None = None
    ) -> dict:
        record = self.devices[device_id]
        if enabled is not None:
            record["share_off"] = not enabled
        if badge is not None:
            # The share link and QR code in the screen's bottom-right corner (main.js).
            record["share_badge"] = badge
        if new_code or "share_code" not in record:
            record["share_code"] = self._new_share_code()
        self._save("devices.json", self.devices)
        self.notify(device_id)
        return record

    def save_share_media(self, device_id: str, data: bytes, media_type: str) -> None:
        path = self.share_media_path(device_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.devices[device_id]["share_media"] = media_type

    def share_media_path(self, device_id: str) -> Path:
        return self.data_dir / "share" / f"{device_id}.img"

    # ---- Zoom activity (Admin → Integrations) ----

    def log_zoom(self, entry: dict) -> None:
        self.zoom_log.insert(0, entry)
        del self.zoom_log[LOG_LIMIT:]
