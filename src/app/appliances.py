"""Xos Linux appliances (os/appliance/): set up from Admin, flashed or installed with one code.

Admin → Screens → Add a Linux box makes a record: role (room computer or display), room, name,
what it shows, a hostname, a maintenance login (`xosadmin` and a generated password), the device
id the box will use, and a one-time setup code.

* Flashing: the Debian 13 installer's "Automated install" fetches /os/p/<code>, a preseed that
  installs Debian with that hostname and timezone, then runs the Xos installer with --enroll.
* An existing Debian box: install.sh --enroll <code>.

Enrolling (POST /api/appliances/enroll) redeems the code once: the box gets its config, its
maintenance password and a device key. From then on the server requires that key on the box's
heartbeat, command stream and acks (room computers; a display's page can't hold one).

A box flashed with the universal Xos USB starts as a display showing its pairing code. Pairing
it in Admin as the room's meeting computer makes a room-computer record bound to that box
(`from_box`); the box's root helper (os/appliance/xos-box) collects the setup code with a secret
only it holds (Boxes) and reinstalls itself as the room computer.

Saved to XOS_DATA_DIR/appliances.json (0600). The maintenance password is readable by
Admins in Admin; the room's Google password is never stored anywhere in Xos.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O, 1/I/L, U
CODE_TTL_S = 7 * 24 * 3600
ROLES = ("room", "display")
MAINT_USER = "xosadmin"
MAX_BOXES = 5000


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:24] or "box"


def _password() -> str:
    """Easy to read off a screen and type on a TV keyboard: four groups of four."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", code.upper())


class Appliances:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "appliances.json"
        try:
            self.items: dict[str, dict] = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.items = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def create(
        self, role: str, room: str, name: str, show: str, by: str, from_box: str = ""
    ) -> dict:
        if role not in ROLES:
            raise ValueError("role is room or display")
        slug = _slug(name or room)
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
            if not any(a["code"] == code for a in self.items.values()):
                break
        device_id = f"xos-{slug}-{secrets.token_hex(3)}"
        record = {
            "device_id": device_id, "role": role, "room": room, "name": name, "show": show,
            "hostname": f"xos-{slug}"[:30], "user": MAINT_USER, "password": _password(),
            "code": code, "created": time.time(), "by": by, "expires": time.time() + CODE_TTL_S,
            "enrolled": None, "from_box": from_box,
        }
        self.items[device_id] = record
        self._save()
        return record

    def by_code(self, code: str) -> dict | None:
        """A record whose setup code is still good (not used, not expired)."""
        code = normalize_code(code)
        if len(code) != 10:
            return None
        for a in self.items.values():
            if secrets.compare_digest(a["code"], code) and not a["enrolled"] and a["expires"] > time.time():
                return a
        return None

    def redeem(self, code: str, ip: str) -> dict | None:
        a = self.by_code(code)
        if a:
            a["enrolled"] = {"at": time.time(), "ip": ip}
            self._save()
        return a

    def handoff(self, box: str) -> dict | None:
        """The room-computer setup waiting for this paired box, if Admin made one."""
        for a in self.items.values():
            if a.get("from_box") == box and not a["enrolled"] and a["expires"] > time.time():
                return a
        return None

    def keyed(self, device_id: str) -> bool:
        """A room computer that enrolled: its heartbeats, stream and acks must carry its key."""
        a = self.items.get(device_id)
        return bool(a and a["enrolled"] and a["role"] == "room")

    def renew(self, device_id: str) -> dict:
        """A fresh setup code (and a new key at the next enroll), e.g. to reflash the same box."""
        a = self.items[device_id]
        a["code"] = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
        a["expires"], a["enrolled"] = time.time() + CODE_TTL_S, None
        self._save()
        return a

    def delete(self, device_id: str) -> bool:
        if self.items.pop(device_id, None) is None:
            return False
        self._save()
        return True


class Boxes:
    """Each Xos box's own secret, made on the box at its first boot (os/appliance/xos-box) and
    registered before its display opens, so before its device id is public. Only the box that
    holds it can collect a setup code meant for it. Two different secrets for one box id means
    someone else tried to be that box: it's marked contested and gets no setup code until it's
    reinstalled. Saved to XOS_DATA_DIR/boxes.json (0600), hashes only."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "boxes.json"
        try:
            self.items: dict[str, dict] = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.items = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def check(self, box: str, secret: str) -> bool:
        """True if this is the box: its first check-in registers it."""
        digest = hashlib.sha256(secret.encode()).hexdigest()
        rec = self.items.get(box)
        if rec is None:
            if not box.startswith("xos-") or len(self.items) >= MAX_BOXES:
                return False  # only Xos box ids, and a public endpoint can't grow this forever
            self.items[box] = {"secret": digest, "since": time.time(), "contested": False}
            self._save()
            return True
        if secrets.compare_digest(rec["secret"], digest):
            return not rec["contested"]
        if not rec["contested"]:
            rec["contested"] = True
            self._save()
        return False

    def usable(self, box: str) -> bool:
        rec = self.items.get(box)
        return bool(rec and not rec["contested"])


SSH_KEY = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|"
    r"sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]{40,}={0,3}(?: [^\n]{0,100})?$"
)


def ssh_keys_in(text: str) -> list[str]:
    """The public-key lines of an authorized_keys text (comments and blanks dropped). Raises
    ValueError on anything that isn't a plain public key: no options, no private keys."""
    keys = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not SSH_KEY.match(line):
            raise ValueError(f"not an SSH public key: {line[:40]}…")
        keys.append(line)
    return keys


class SshKeys:
    """Public keys that may log in to every Xos box as xosadmin, added in Admin (team only) on top
    of os/appliance/ssh/authorized_keys in Git. Boxes sync both every 10 minutes (xos-keys).
    Saved to XOS_DATA_DIR/ssh_keys.json."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "ssh_keys.json"
        try:
            self.keys: list[str] = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.keys = []

    def set(self, text: str) -> list[str]:
        keys = ssh_keys_in(text)
        if len(keys) > 50:
            raise ValueError("at most 50 keys")
        self.keys = list(dict.fromkeys(keys))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.keys))
        tmp.replace(self.path)
        return self.keys


class WifiNetworks:
    """The Wi-Fi networks Xos boxes join, in order of preference (Admin → Screens → Linux boxes).

    Saved to XOS_DATA_DIR/wifi.json (0600), never in Git or the image. A box gets them only when
    it redeems its one-time setup code, and keeps them root-only in NetworkManager. Wired Ethernet
    still wins when it's plugged in.
    """

    def __init__(self, data_dir: Path):
        self.path = data_dir / "wifi.json"
        try:
            self.networks: list[dict] = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.networks = []

    def set(self, networks: list[dict]) -> list[dict]:
        clean = []
        for n in networks:
            ssid, psk = str(n.get("ssid", "")).strip(), str(n.get("psk", ""))
            if not ssid:
                continue
            if not 1 <= len(ssid.encode()) <= 32:
                raise ValueError(f"{ssid!r}: a Wi-Fi name is 1 to 32 bytes")
            if psk and not 8 <= len(psk) <= 63:
                raise ValueError(f"{ssid}: a WPA password is 8 to 63 characters")
            clean.append({"ssid": ssid, "psk": psk})
        self.networks = clean
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean))
        tmp.chmod(0o600)
        tmp.replace(self.path)
        return clean


def site_timezone(site: str) -> str:
    """XOS_SITE_TIMEZONES' entry for the site, else XOS_TIMEZONE."""
    from app.config import settings

    try:
        zones = json.loads(settings.XOS_SITE_TIMEZONES or "{}")
    except ValueError:
        zones = {}
    zone = zones.get(site) if isinstance(zones, dict) else None
    return zone if isinstance(zone, str) and zone else settings.XOS_TIMEZONE


def preseed(a: dict, server: str, site: str) -> str:
    """Debian 13 automated install for this box. Wipes the first disk. No secrets: the box gets
    its password and key when the Xos installer redeems the code at the end."""
    tz = site_timezone(site)
    return f"""# Xos appliance: Debian 13 automated install for {a['name'] or a['device_id']} ({a['role']}, room {a['room'] or '-'}).
# Generated by the Xos server for one setup code. ERASES THE FIRST DISK.
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string {a['hostname']}
d-i netcfg/get_domain string
d-i netcfg/hostname string {a['hostname']}
d-i hw-detect/load_firmware boolean true
d-i mirror/country string manual
d-i mirror/http/hostname string deb.debian.org
d-i mirror/http/directory string /debian
d-i mirror/http/proxy string
d-i clock-setup/utc boolean true
d-i time/zone string {tz}
d-i clock-setup/ntp boolean true
d-i passwd/root-login boolean false
d-i passwd/user-fullname string Xos maintenance
d-i passwd/username string {MAINT_USER}
# Locked until Xos sets the real password from Admin at the end of the install.
d-i passwd/user-password-crypted password !
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i partman-efi/non_efi_system boolean true
d-i apt-setup/non-free-firmware boolean true
d-i apt-setup/services-select multiselect security, updates
tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string curl ca-certificates python3 sudo unattended-upgrades
d-i pkgsel/upgrade select full-upgrade
popularity-contest popularity-contest/participate boolean false
d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string default
d-i preseed/late_command string in-target sh -c 'curl -fsSL {server}/os/appliance/install.sh | bash -s -- --enroll {a['code']} --server {server} --no-start'
d-i finish-install/reboot_in_progress note
"""
