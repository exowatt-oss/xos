#!/usr/bin/env python3
"""xos-update (appliance): move to the Xos server's current release, and back if it isn't healthy.

Releases live side by side in /opt/xos/releases/<sha256 of the bundle>; /opt/xos/current is a
symlink to the one in use, swapped with an atomic rename. The services run from
/opt/xos/current, so an update is: download and check the bundle, unpack it next to the others,
swap the link, restart, and watch health for a while. If the new release isn't healthy in time,
the link goes back, the services restart on the old release, and the bad one is remembered so it
isn't tried again. A power cut at any point leaves either the old or the new link, never half a
release: bundles unpack under a temporary name first.

Health: the room computer writes /run/xos/health.json every few seconds (xos_room_linux.py); it
must come from the new release with its browser running. A display is healthy when its unit is
active. Network trouble isn't a reason to roll back (the check doesn't need the server), and a room
computer in a meeting isn't updated (the next run tries again).

    xos-update            # check now (xos-update.timer runs it after boot and nightly)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("XOS_ROOT", "/opt/xos"))
CONFIG = Path(os.environ.get("XOS_CONFIG", "/etc/xos/appliance.json"))
STATE = Path(os.environ.get("XOS_STATE", "/var/lib/xos"))
HEALTH = Path(os.environ.get("XOS_HEALTH", "/run/xos/health.json"))
KEEP = 3  # releases kept on disk: the current one and two to fall back to
HEALTH_WAIT_S = 120
SERVICE = {"room": "xos-room.service", "display": "xos-display.service"}


def log(*a):
    print(*a, flush=True)


class UpdateError(Exception):
    pass


def current(root: Path = ROOT) -> str:
    link = root / "current"
    return os.readlink(link).rsplit("/", 1)[-1] if link.is_symlink() else ""


def switch(root: Path, name: str) -> None:
    """Point current at releases/<name>, atomically (a power cut leaves the old or the new link)."""
    tmp = root / "current.new"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(f"releases/{name}")
    os.replace(tmp, root / "current")


def unpack(root: Path, name: str, data: bytes, digest: str) -> Path:
    if hashlib.sha256(data).hexdigest() != digest:
        raise UpdateError("the downloaded bundle doesn't match the server's checksum")
    dest = root / "releases" / name
    if dest.exists():
        return dest
    part = root / "releases" / f".{name}.partial"
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(part, filter="data")  # no absolute paths, links out or device files
    os.replace(part, dest)
    return dest


def prune(root: Path, keep: int = KEEP) -> None:
    rels = root / "releases"
    cur = current(root)
    olds = sorted(
        (p for p in rels.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name != cur),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for p in olds[keep - 1 :]:
        shutil.rmtree(p, ignore_errors=True)


def bad_list(state: Path) -> set[str]:
    try:
        return set(json.loads((state / "update" / "bad.json").read_text()))
    except (OSError, ValueError):
        return set()


def mark_bad(state: Path, name: str) -> None:
    (state / "update").mkdir(parents=True, exist_ok=True)
    (state / "update" / "bad.json").write_text(json.dumps(sorted(bad_list(state) | {name})))


# ---------- the real world (replaced in tests) ----------


def fetch(url: str, timeout: float = 60) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def install_policy(
    role: str, root: Path = ROOT, policy_dir: Path = Path("/etc/chromium/policies/managed")
) -> None:
    """Forward updates and rollbacks both restore the matching browser policy."""
    policy = root / "current" / "os" / "appliance" / "policies" / f"{role}.json"
    if policy.is_file():
        policy_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(policy, policy_dir / "xos-appliance.json")


def restart(role: str, root: Path = ROOT) -> None:
    """Install the current release's unit files (they can change between releases), then restart.
    Called after every switch, forward or back, so units always match the running release."""
    install_policy(role, root)
    units = root / "current" / "os" / "appliance" / "systemd"
    for unit in units.glob("*.*"):
        shutil.copyfile(unit, Path("/etc/systemd/system") / unit.name)
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    if role == "display":  # boxes from before the helper: Admin can make them room computers too
        subprocess.run(["systemctl", "enable", "--now", "xos-box.timer"], check=False)
    # Boxes from before xos-keys: the maintenance login's SSH keys from Xos, starting now.
    subprocess.run(["systemctl", "enable", "--now", "xos-keys.timer"], check=False)
    subprocess.run(["systemctl", "restart", SERVICE[role]], check=False)


def healthy(role: str, name: str, since: float, health: Path = HEALTH) -> bool:
    if role == "display":
        return subprocess.run(["systemctl", "is-active", "--quiet", SERVICE[role]]).returncode == 0
    try:
        h = json.loads(health.read_text())
    except (OSError, ValueError):
        return False
    return (
        h.get("at", 0) > since and h.get("release") == name[:12] and h.get("browser") == "running"
    )


def in_meeting(health: Path = HEALTH) -> bool:
    """Never restart the room computer under a meeting: the next run tries again."""
    try:
        return bool(json.loads(health.read_text()).get("in_call"))
    except (OSError, ValueError):
        return False


def wait_healthy(check, wait: float, step: float = 3) -> bool:
    deadline = time.time() + wait
    while time.time() < deadline:
        if check():
            return True
        time.sleep(step)
    return check()


def update(
    server: str,
    role: str,
    root: Path = ROOT,
    state: Path = STATE,
    *,
    get=fetch,
    restart_fn=restart,
    healthy_fn=healthy,
    busy_fn=in_meeting,
    wait: float = HEALTH_WAIT_S,
) -> str:
    digest = get(f"{server}/os/xos-node.sha256", 30).decode().strip()
    if len(digest) != 64:
        raise UpdateError(f"the server sent no checksum ({digest[:40]!r})")
    if digest == current(root):
        return "up to date"
    if digest in bad_list(state):
        return f"skipped: {digest[:12]} failed its health check before"
    if busy_fn():
        return "skipped: the room is in a meeting"
    unpack(root, digest, get(f"{server}/os/xos-node.tar.gz", 300), digest)
    previous = current(root)
    since = time.time()
    switch(root, digest)
    restart_fn(role)
    if wait_healthy(lambda: healthy_fn(role, digest, since), wait):
        prune(root)
        return f"updated to {digest[:12]}"
    mark_bad(state, digest)
    if previous:
        switch(root, previous)
        restart_fn(role)
        return f"rolled back to {previous[:12]}: {digest[:12]} wasn't healthy"
    return f"{digest[:12]} isn't healthy and there's nothing to roll back to"


def remember(result: str, state: Path = STATE) -> None:
    """The last check's outcome, which the room computer reports to Admin."""
    try:
        (state / "update").mkdir(parents=True, exist_ok=True)
        last = state / "update" / "last.json"
        last.write_text(json.dumps({"at": time.time(), "result": result[:200]}))
        last.chmod(0o644)
    except OSError:
        pass


def main() -> None:
    cfg = json.loads(CONFIG.read_text())
    try:
        # Also refresh policy on an up-to-date box. Its first upgrade may have been run by an
        # older updater that only refreshed systemd units; Chromium reloads managed policy.
        install_policy(cfg["role"])
        out = update(cfg["server"].rstrip("/"), cfg["role"])
    except Exception as e:  # noqa: BLE001 - offline or a bad download: try again next time
        log("xos-update:", e)
        remember(f"failed: {e}")
        sys.exit(1)
    log("xos-update:", out)
    remember(out)
    if out.startswith("rolled back") or "isn't healthy" in out:
        sys.exit(2)


if __name__ == "__main__":
    main()
