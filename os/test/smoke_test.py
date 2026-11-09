#!/usr/bin/env python3
"""Smoke test for an Xos node, run inside Debian 12 (see os/test/run.sh).

Checks the agent API, cross-device state sync through the hub, the dry-run Zoom adapter, and
renders every role view with the same Chromium a node uses. Screenshots go to /out.
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:7000"
OUT = Path("/out")
OUT.mkdir(exist_ok=True)
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def shot(name, route, size):
    url = f"{BASE}/app/?kiosk=1{route}"
    cmd = ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
           f"--window-size={size}", "--timeout=6000", f"--screenshot={OUT / name}", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    ok = proc.returncode == 0 and (OUT / name).exists() and (OUT / name).stat().st_size > 10_000
    check(f"render {name}", ok, route if ok else proc.stderr[-300:])


agent = subprocess.Popen([sys.executable, "/xos/agent/xos_agent.py"])
try:
    for _ in range(50):
        try:
            status, health = http("GET", "/api/health")
            break
        except OSError:
            time.sleep(0.2)
    check("agent health", status == 200 and health["role"] == "tv", json.dumps(health))

    status, _ = http("GET", "/api/state")
    check("hub starts empty", status == 204)

    # A browser (the 'tablet') loads the app and seeds the hub with its state.
    shot("01-tablet-idle.png", "#/tablet/lamar", "1280,800")
    status, payload = http("GET", "/api/state")
    seeded = status == 200 and "calls" in payload["state"]
    check("tablet seeded shared state", seeded)

    if seeded:
        # Simulate another device putting Lamar in a muted Zoom call, then render the TV.
        state = payload["state"]
        lamar = state["calls"]["lamar"]
        lamar.update(state="incall", meetingId="m4", provider="zoom", muted=True, ani=True,
                     startedAt=int(time.time() * 1000) - 95_000)
        http("POST", "/api/state", state)
        status, back = http("GET", "/api/state")
        check("state round-trip via hub", back["state"]["calls"]["lamar"]["muted"] is True)
        shot("02-tv-incall-muted.png", "#/tv/lamar", "1920,1080")
        shot("03-tablet-incall.png", "#/tablet/lamar", "1280,800")
        shot("04-panel.png", "#/panel/lamar", "1024,600")

    shot("05-signage.png", "#/signage/hq", "1920,1080")
    shot("06-wall.png", "#/wall", "1920,1080")

    status, dry = http("POST", "/api/adapter", {"room": "lamar", "action": "mute"})
    check("zoom adapter is dry-run", status == 200 and dry["dry_run"] and dry["sent"] is False,
          dry["request"]["json"]["method"])
    status, dry = http("POST", "/api/adapter", {"room": "lamar", "action": "join", "meeting_number": "84213377120"})
    check("zoom join request shape", dry["request"]["url"].endswith("/rooms/lamar-zr/events"), json.dumps(dry["request"]["json"]))
    status, cec = http("POST", "/api/cec/standby")
    check("cec without hardware is dry-run", cec.get("dry_run") is True, cec.get("would_run", ""))
finally:
    agent.terminate()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; screenshots in /out", flush=True)
sys.exit(1 if failed else 0)
