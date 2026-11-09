#!/usr/bin/env python3
"""xos-agent (prototype) — the per-node service that makes a Debian box an Xos node.

Stdlib only, so it runs on a stock Raspberry Pi OS / Debian 12 with nothing extra.
The production agent is planned in Go; this prototype proves the shape:

  * serves the role UI (the demo app) to the local kiosk and to controllers on the LAN
  * state hub: room host <-> table controller <-> door panel stay in sync across devices
    (GET/POST /api/state, Server-Sent Events on /api/events)
  * health:   GET /api/health  (role, room, uptime, temperature, CEC, version)
  * TV power: POST /api/cec/{on,standby}  (cec-ctl if present, otherwise dry-run)
  * meeting adapter: POST /api/adapter  — DRY RUN ONLY. Logs what Xos would do through the
    Zoom Rooms Controller SDK (primary, Linux ARM64 on the panel) and the equivalent Zoom REST
    room-events call (fallback); never sends either. Nothing here talks to Zoom or Google

Config: /etc/xos/node.json (or $XOS_NODE_CONFIG), e.g.
  {"role": "tv", "room": "lamar", "zoom_room_id": "lamar-zr", "port": 7000}
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.1.0-prototype"
STARTED = time.time()
CONFIG_PATH = Path(os.environ.get("XOS_NODE_CONFIG", "/etc/xos/node.json"))
APP_ROOT = Path(os.environ.get("XOS_APP_ROOT", Path(__file__).resolve().parents[2]))
LOG_LIMIT = 50

DEFAULT_CONFIG = {"role": "tv", "room": "lamar", "zoom_room_id": "lamar-zr", "adapter": "zrc-sdk", "port": 7000}

# Map Xos controller actions to the Zoom Rooms cloud "room events" API (PATCH /v2/rooms/{id}/events).
# Method names follow Zoom's Zoom Room controls docs; verify against a real account before going live.
ZOOM_METHODS = {
    "join": "zoomroom.meeting_join",
    "leave": "zoomroom.meeting_leave",
    "mute": "zoomroom.mute",
    "unmute": "zoomroom.unmute",
    "video_off": "zoomroom.video_mute",
    "video_on": "zoomroom.video_unmute",
    "volume": "zoomroom.volume_level",
    "share_start": "zoomroom.share_content_start",
    "share_stop": "zoomroom.share_content_stop",
    "restart": "zoomroom.restart",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as exc:
        print(f"xos-agent: bad {CONFIG_PATH}: {exc}; using defaults", flush=True)
    return cfg


CONFIG = load_config()


class Hub:
    """Latest shared demo state plus SSE subscribers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = None
        self.version = 0
        self.cond = threading.Condition(self.lock)
        self.adapter_log = []

    def put(self, state):
        with self.cond:
            self.state = state
            self.version += 1
            self.cond.notify_all()
            return self.version

    def wait(self, since, timeout=15):
        with self.cond:
            self.cond.wait_for(lambda: self.version != since, timeout=timeout)
            return self.version, self.state

    def log_adapter(self, entry):
        with self.lock:
            self.adapter_log.insert(0, entry)
            del self.adapter_log[LOG_LIMIT:]


HUB = Hub()


def cpu_temp():
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        return None


def cec_available():
    return shutil.which("cec-ctl") is not None


def cec(action):
    if not cec_available():
        return {"dry_run": True, "would_run": f"cec-ctl --to 0 --{'image-view-on' if action == 'on' else 'standby'}"}
    args = ["cec-ctl", "--to", "0", "--image-view-on" if action == "on" else "--standby"]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return {"dry_run": False, "returncode": proc.returncode, "stderr": proc.stderr[-400:]}


def adapter_request(body):
    """Build (never send) the Zoom REST request for a controller action."""
    action = body.get("action")
    if action == "layout":
        # Video layout has no Zoom REST room event; it only exists on the controller paths.
        calls = [str(c) for c in body.get("calls", [])][:6]
        api = body.get("api", CONFIG.get("adapter", "zrc-sdk"))
        entry = {"t": time.time(), "room": body.get("room", CONFIG["room"]), "action": "layout",
                 "backend": api, "zrc" if api == "zrc" else api: {"calls": calls}, "request": None, "sent": False}
        HUB.log_adapter(entry)
        print(f"xos-agent: DRY RUN layout -> {' + '.join(calls)}", flush=True)
        return HTTPStatus.OK, {"dry_run": True, **entry}
    method = ZOOM_METHODS.get(action)
    if not method:
        return HTTPStatus.BAD_REQUEST, {"error": f"unknown action {action!r}", "known": sorted(ZOOM_METHODS)}
    params = {}
    if action == "join" and body.get("meeting_number"):
        params = {"meeting_number": str(body["meeting_number"]), "passcode": body.get("passcode", "")}
    if action == "volume":
        params = {"volume_level": int(body.get("level", 50))}
    room_id = body.get("zoom_room_id") or CONFIG.get("zoom_room_id")
    request = {
        "method": "PATCH",
        "url": f"https://api.zoom.us/v2/rooms/{room_id}/events",
        "headers": {"Authorization": "Bearer <server-to-server OAuth token>"},
        "json": {"method": method, "params": params} if params else {"method": method},
    }
    # Primary path for appliance rooms: the panel is a paired Zoom Rooms controller (ZRC SDK), which
    # also gives live state back. Bind to concrete SDK calls once we have the SDK; logged as intent here.
    zrc = {"backend": "zrc-sdk", "action": action, "params": params, "state_feedback": True}
    entry = {"t": time.time(), "room": body.get("room", CONFIG["room"]), "action": action,
             "backend": CONFIG.get("adapter", "zrc-sdk"), "zrc": zrc, "request": request, "sent": False}
    HUB.log_adapter(entry)
    print(f"xos-agent: DRY RUN {action} -> {method} ({room_id})", flush=True)
    return HTTPStatus.OK, {"dry_run": True, **entry}


class Handler(SimpleHTTPRequestHandler):
    server_version = f"xos-agent/{VERSION}"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_ROOT), **kwargs)

    def log_message(self, fmt, *args):  # quieter than the default access log
        if not self.path.startswith(("/api/events", "/api/state")):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path == "/":
            role, room = CONFIG["role"], CONFIG["room"]
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"/app/?kiosk=1#/{role}/{room}")
            self.end_headers()
            return
        if self.path == "/api/health":
            return self.send_json(HTTPStatus.OK, {
                "ok": True, "version": VERSION, "hostname": socket.gethostname(),
                "role": CONFIG["role"], "room": CONFIG["room"], "uptime_s": round(time.time() - STARTED),
                "cpu_temp_c": cpu_temp(), "cec": cec_available(), "state_version": HUB.version,
            })
        if self.path == "/api/node":
            return self.send_json(HTTPStatus.OK, CONFIG)
        if self.path == "/api/state":
            if HUB.state is None:
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            return self.send_json(HTTPStatus.OK, {"version": HUB.version, "state": HUB.state})
        if self.path == "/api/adapter/log":
            return self.send_json(HTTPStatus.OK, HUB.adapter_log)
        if self.path == "/api/events":
            return self.stream_events()
        return super().do_GET()

    def do_POST(self):
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        if self.path == "/api/state":
            return self.send_json(HTTPStatus.OK, {"version": HUB.put(body)})
        if self.path == "/api/adapter":
            status, payload = adapter_request(body)
            return self.send_json(status, payload)
        if self.path in ("/api/cec/on", "/api/cec/standby"):
            return self.send_json(HTTPStatus.OK, cec(self.path.rsplit("/", 1)[1]))
        return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def stream_events(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = -1
        try:
            while True:
                version, state = HUB.wait(seen)
                if version != seen and state is not None:
                    seen = version
                    self.wfile.write(f"id: {version}\ndata: {json.dumps(state)}\n\n".encode())
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    port = int(os.environ.get("XOS_PORT", CONFIG.get("port", 7000)))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"xos-agent {VERSION}: role={CONFIG['role']} room={CONFIG['room']} serving {APP_ROOT} on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
