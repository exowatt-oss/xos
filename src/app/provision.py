"""Set up an Android screen (Fire TV Stick, Android TV, Android smart board) from Admin by its IP.

The server does what firetv/provision.sh does from a laptop, over network ADB (port 5555):
install Xos TV, let it open itself at boot, turn on display control, and open it. The screen then
checks in, and Admin names it and picks what it shows, with no pairing code to read off the TV.

The first time, the screen asks "Allow USB debugging?" for this server's key (kept in
XOS_DATA_DIR/adb). Someone at the screen accepts it once, ticking "Always allow", and later
set-ups of that screen need no one there. Only the fixed steps below ever run on a screen.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import socket
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

PKG = "com.example.xos.tv"
ADB_PORT = 5555
AUTH_WAIT_S = 120  # time someone has to accept the prompt on the screen
CHECK_IN_WAIT_S = 45
MAX_JOBS = 30


class ProvisionError(Exception):
    pass


def check_ip(ip: str) -> str:
    """A private-network IPv4 address, or ProvisionError."""
    try:
        addr = ipaddress.IPv4Address(ip.strip())
    except ValueError as e:
        raise ProvisionError("Enter the screen's IP address, like 192.0.2.102.") from e
    if not addr.is_private or addr.is_loopback or addr.is_link_local:
        raise ProvisionError("Only screens on the office network (private IP addresses).")
    return str(addr)


def _signer(key_path: Path):
    from adb_shell.auth.keygen import keygen
    from adb_shell.auth.sign_pythonrsa import PythonRSASigner

    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        keygen(str(key_path))  # writes key_path and key_path.pub
        key_path.chmod(0o600)
    return PythonRSASigner(key_path.with_suffix(".pub").read_text(), key_path.read_text())


def run_adb(ip: str, apk: Path, key_path: Path, server: str, step: Callable[[str], None]) -> dict:
    """The device-side steps, blocking (run in a thread). Returns what it learned."""
    from adb_shell.adb_device import AdbDeviceTcp
    from adb_shell.exceptions import DeviceAuthError

    step("Connecting")
    try:
        socket.create_connection((ip, ADB_PORT), timeout=4).close()
    except ConnectionRefusedError as e:
        raise ProvisionError(
            f"{ip} answered but ADB is off. Turn on ADB debugging on the screen "
            "(Fire TV: Settings → My Fire TV → Developer options; tap About → device name 7 "
            "times to show them)."
        ) from e
    except OSError as e:  # timed out or no route: usually a firewall between the server and it
        raise ProvisionError(
            f"The Xos server can't reach {ip}. Check the IP; if it's right, the network between "
            "the server and that screen blocks ADB (TCP 5555). Set it up from a laptop on the "
            f"same network instead: firetv/provision.sh {ip}"
        ) from e

    dev = AdbDeviceTcp(ip, ADB_PORT, default_transport_timeout_s=10)
    asked = []

    def on_auth(_dev):
        asked.append(True)
        step('Accept "Allow USB debugging?" on the screen (tick Always allow)')

    try:
        dev.connect(
            rsa_keys=[_signer(key_path)],
            auth_timeout_s=AUTH_WAIT_S,
            read_timeout_s=AUTH_WAIT_S,
            transport_timeout_s=10,
            auth_callback=on_auth,
        )
    except DeviceAuthError as e:
        raise ProvisionError("The screen didn't allow it. Run it again and accept the prompt.") from e
    except Exception as e:  # noqa: BLE001 - adb-shell raises many transport types
        if asked:
            raise ProvisionError(
                "Nobody accepted the prompt on the screen in time. Run it again."
            ) from e
        raise ProvisionError(f"ADB connection failed: {e}") from e

    def sh(cmd: str, timeout: float = 30) -> str:
        return (dev.shell(cmd, timeout_s=timeout, read_timeout_s=timeout) or "").strip()

    try:
        sdk = int(sh("getprop ro.build.version.sdk") or 0)
        info = {
            "model": " ".join(
                x for x in (sh("getprop ro.product.manufacturer"), sh("getprop ro.product.model"))
                if x
            ),
            "android": sh("getprop ro.build.version.release"),
            "sdk": sdk,
        }
        step(f"Connected to {info['model'] or 'the screen'} · Android {info['android']}")
        had_xos = PKG in sh(f"pm path {PKG}")

        step("Installing Xos TV")
        tmp = "/data/local/tmp/xos-tv.apk"
        dev.push(str(apk), tmp, transport_timeout_s=120, read_timeout_s=120)
        out = sh(f"pm install -r {tmp}", timeout=180)
        sh(f"rm -f {tmp}")
        if "Success" not in out:
            raise ProvisionError(f"Install failed: {out[-200:] or 'no answer'}")

        if sdk >= 29:  # Android 10+: opening itself at boot needs "display over other apps"
            sh(f"appops set {PKG} SYSTEM_ALERT_WINDOW allow")
            sh("appops write-settings")
        sh(f"dumpsys deviceidle whitelist +{PKG}")  # keep its connection while the TV is off
        admin = sh(f"dpm set-active-admin {PKG}/.AdminReceiver")
        info["display_control"] = "Success" in admin or "already" in admin.lower()

        step("Opening Xos")
        # A screen that already ran Xos keeps what it was showing; a new one starts on setup.
        route = "" if had_xos else "--es route '#/setup'"
        sh(
            f"am start -n {PKG}/.MainActivity --es server '{server}' {route} "
            "--ez boot true --ei return_min 2"
        )
        info["had_xos"] = had_xos
        return info
    finally:
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass


WAKE_AUTH_S = 8  # waking needs this server's key already allowed: no one is at a TV that's off


def wake_adb(ip: str, key_path: Path) -> None:
    """Wake a sleeping screen and bring Xos back to the front, over network ADB (blocking).

    For TV on when the screen can't hear it: Xos TV before 0.6.0 drops off Xos while asleep. Only
    works on a screen this server set up (its key is allowed) whose network stays up asleep.
    """
    from adb_shell.adb_device import AdbDeviceTcp

    try:
        socket.create_connection((ip, ADB_PORT), timeout=4).close()
    except ConnectionRefusedError as e:
        raise ProvisionError(f"{ip} answered but ADB is off.") from e
    except OSError as e:
        raise ProvisionError(f"The Xos server can't reach {ip}.") from e
    dev = AdbDeviceTcp(ip, ADB_PORT, default_transport_timeout_s=10)
    try:
        dev.connect(
            rsa_keys=[_signer(key_path)],
            auth_timeout_s=WAKE_AUTH_S,
            read_timeout_s=WAKE_AUTH_S,
            transport_timeout_s=10,
        )
    except Exception as e:  # noqa: BLE001 - adb-shell raises many transport types
        raise ProvisionError(f"{ip} didn't let this server in over ADB.") from e
    try:
        dev.shell("input keyevent KEYCODE_WAKEUP", timeout_s=15)
        dev.shell(f"am start -n {PKG}/.MainActivity", timeout_s=15)
    finally:
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass


class Provisioner:
    """Set-up jobs started from Admin → Screens → Add a screen, kept in memory."""

    def __init__(self, apk: Path, key_path: Path, server: str):
        self.apk = apk
        self.key_path = key_path
        self.server = server
        self.jobs: dict[str, dict] = {}

    async def wake(self, ip: str) -> None:
        await asyncio.to_thread(wake_adb, check_ip(ip), self.key_path)

    def list(self) -> list[dict]:
        return sorted(self.jobs.values(), key=lambda j: -j["started"])

    def start(self, ip: str, name: str, route: str, on_check_in) -> dict:
        ip = check_ip(ip)
        if any(j["ip"] == ip and j["state"] == "running" for j in self.jobs.values()):
            raise ProvisionError(f"{ip} is already being set up.")
        if not self.apk.exists():
            raise ProvisionError("The Xos TV app isn't on this server (firetv/xos-tv.apk).")
        for old in sorted(self.jobs.values(), key=lambda j: j["started"])[: -MAX_JOBS + 1]:
            if old["state"] != "running":
                del self.jobs[old["id"]]
        job = {
            "id": secrets.token_hex(4),
            "ip": ip,
            "name": name,
            "state": "running",
            "step": "Starting",
            "log": [],
            "started": time.time(),
            "device": None,
            "error": None,
        }
        self.jobs[job["id"]] = job
        asyncio.get_running_loop().create_task(self._run(job, route, on_check_in))
        return job

    def _step(self, job: dict, text: str) -> None:
        job["step"] = text
        job["log"].append({"at": time.time(), "text": text})

    async def _run(self, job: dict, route: str, on_check_in) -> None:
        loop = asyncio.get_running_loop()

        def step(text: str) -> None:
            loop.call_soon_threadsafe(self._step, job, text)

        try:
            info = await asyncio.to_thread(
                run_adb, job["ip"], self.apk, self.key_path, self.server, step
            )
            job["info"] = info
            self._step(job, "Waiting for it to check in")
            device_id = await on_check_in(job, info, route, CHECK_IN_WAIT_S)
            if not device_id:
                raise ProvisionError(
                    "Xos opened, but the screen didn't check in. Is it online, and can it reach "
                    f"{self.server}?"
                )
            job["device"] = device_id
            job["state"] = "done"
            self._step(job, "Done")
        except ProvisionError as e:
            job["state"], job["error"] = "failed", str(e)
            self._step(job, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("provisioning %s failed", job["ip"])
            job["state"], job["error"] = "failed", f"Something went wrong: {e}"
            self._step(job, job["error"])
