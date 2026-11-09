"""People & roles: everyone who uses Xos, the Xos role each has, and the record of every change.

Saved to XOS_DATA_DIR/people.json; role changes and previews are appended to
XOS_DATA_DIR/people_audit.jsonl, which is only ever appended to.

Anyone who signs in to Xos is added, and Admins can add someone by address before they sign in.
A deployment can also feed People from a company directory: call `People.sync(fetch)` with an
async `fetch()` returning roster entries ({email, name, title, department: {name}, team: {name},
picture, id, status}); see `apply_roster`. No directory is built in.

A person's role is, first match wins:

1. Admin, if they're a super admin (XOS_SUPER_ADMINS: it can't be changed here).
2. Member, if they've left the directory. Their assigned role is kept but has no effect until
   they're back.
3. The role assigned to them.
4. Admin, if they're a starting Admin (XOS_BOOTSTRAP_ADMINS) with no role assigned.
5. Their directory team's default role, then their department's.
6. Member.

Only a super admin can make someone Admin or remove Admin (a starting Admin included); other
Admins assign the other roles. A change that would leave Xos without an active Admin is refused.
Admin can't be a department or team default: it is given to people one by one.

Directory sync safety rails: a roster with no active people is refused, and so is one smaller
than half of the active directory people Xos knows (once it knows 10). Either is far more likely
an outage or a partial read than a real exodus. People who leave the directory are marked
inactive, never deleted. A sync never changes an assignment, but moving team or department, or
leaving, changes someone's role: each such change is logged as `role.sync`, and a sync that
would leave no active Admin is refused.

State is shared by request threads (sign-ins, from the threadpool) and the event loop (Admin,
a sync), so every change and save holds `_state_lock`. A save writes a unique temporary file
and keeps the previous good copy as people.json.bak. If people.json can't be read, Xos starts
from the .bak, or refuses to start; it never starts empty and overwrites it.

Preview: an Admin can see Xos as a role or a person. While previewing, requests from that Admin
carry the previewed permissions (never more than their own) and every write is refused
(app.main → refuse_writes_in_preview). A preview lasts PREVIEW_S or until Exit preview.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Any

from app.access import (
    DEFAULT_ROLE,
    PERMISSIONS,
    ROLES,
    Caller,
    allowed_email,
    bootstrap_emails,
    super_admin_emails,
)

logger = logging.getLogger(__name__)

PREVIEW_S = 3600
SEEN_EVERY_S = 600  # how often a signed-in person's last_seen is saved


class DirectoryError(Exception):
    """The directory was unreachable or returned an unusable roster."""


class RoleError(ValueError):
    """A role change that isn't allowed (bootstrap Admin, last Admin, unknown role)."""


def org_email(value: Any) -> str:
    """A normalized address that may hold an Xos role (XOS_ALLOWED_EMAIL_DOMAINS), or ""."""
    email = str(value or "").strip().lower()
    return email if allowed_email(email) else ""


def _picture(value: Any) -> str:
    url = str(value or "")
    return url if url.startswith("https://") and len(url) <= 500 else ""


def _ref_name(value: Any) -> str:
    return str((value or {}).get("name") or "").strip()[:120] if isinstance(value, dict) else ""


def _read_state(path: Path) -> dict:
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or not all(
        isinstance(state.get(k, {}), dict)
        for k in ("people", "roles", "defaults", "sync", "role_permissions", "person_permissions")
    ):
        raise ValueError(f"{path.name} isn't a People state file")
    return state


def locked(method: Callable) -> Callable:
    """Run a method that reads and changes shared state under the store's lock."""

    @wraps(method)
    def run(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)

    return run


class People:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "people.json"
        self.audit_path = data_dir / "people_audit.jsonl"
        self._state_lock = threading.RLock()
        state = self._load()
        # email -> {email, name, department, team, title, picture, active, source, ...}
        self.people: dict[str, dict] = state.get("people", {})
        # email -> role id, as assigned in Admin
        self.roles: dict[str, str] = state.get("roles", {})
        # {"department": {name: role}, "team": {name: role}}
        self.defaults: dict[str, dict[str, str]] = {
            "department": {},
            "team": {},
            **state.get("defaults", {}),
        }
        self.last_sync: dict = state.get("sync", {})
        self.role_permissions: dict[str, list[str]] = state.get("role_permissions", {})
        self.person_permissions: dict[str, dict[str, bool]] = state.get("person_permissions", {})
        # Admin email -> {kind, value, label, role, perms, by, started}; in memory only.
        self.previews: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    # ---- persistence ----

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def _load(self) -> dict:
        """people.json, else its .bak. Raises rather than start empty over a damaged file."""
        if not self.path.exists() and not self.backup_path.exists():
            return {}  # first start
        try:
            return _read_state(self.path)
        except (OSError, ValueError) as exc:
            logger.error("people.json is unreadable (%s); trying %s", exc, self.backup_path.name)
            try:
                state = _read_state(self.backup_path)
            except (OSError, ValueError) as bak_exc:
                raise RuntimeError(
                    f"Can't read {self.path} or its backup ({exc}; {bak_exc}). Restore one of "
                    "them, or move both away to start with no people and roles."
                ) from bak_exc
            logger.warning("started People & roles from %s", self.backup_path.name)
            return state

    def _current_is_good(self) -> bool:
        """Only a readable people.json may replace the backup (never a damaged one)."""
        try:
            _read_state(self.path)
        except (OSError, ValueError):
            return False
        return True

    def _save(self) -> None:
        state = {
            "people": self.people,
            "roles": self.roles,
            "role_permissions": self.role_permissions,
            "person_permissions": self.person_permissions,
            "defaults": self.defaults,
            "sync": self.last_sync,
        }
        with self._state_lock:
            data = json.dumps(state)
            tmp = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".people.", suffix=".tmp")
                with os.fdopen(fd, "w") as f:
                    f.write(data)
                if self._current_is_good():
                    shutil.copy2(self.path, self.backup_path)  # the last good copy
                os.replace(tmp, self.path)
                tmp = None
            except OSError as exc:  # state still lives in memory; log and carry on
                logger.warning("could not save people: %s", exc)
            finally:
                if tmp:
                    Path(tmp).unlink(missing_ok=True)

    def _audit(self, by: str, kind: str, target: str, **details: Any) -> dict:
        entry = {"at": time.time(), "by": by, "kind": kind, "target": target, **details}
        with self._state_lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a") as f:  # append-only: never rewritten or truncated
                f.write(json.dumps(entry) + "\n")
        logger.info("people %s %s by %s", kind, target, by)
        return entry

    def record(self, by: str, kind: str, target: str, **details: Any) -> dict:
        """Log something that isn't a role change, such as a directory event."""
        return self._audit(by, kind, target, **details)

    def audit(self, limit: int = 200) -> list[dict]:
        """The newest `limit` entries, newest first."""
        try:
            lines = self.audit_path.read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
            if len(out) >= limit:
                break
        return out

    # ---- who has which role ----

    @locked
    def seen(self, email: str, name: str) -> None:
        """Someone signed in: keep them on the People page."""
        now = time.time()
        p = self.people.get(email)
        if p is None:
            self.people[email] = {
                "email": email,
                "name": name if name != email else "",
                "active": True,
                "source": "signin",
                "first_seen": now,
                "last_seen": now,
            }
            self._save()
        elif now - p.get("last_seen", 0) > SEEN_EVERY_S:
            p["last_seen"] = now
            if not p.get("name") and name and name != email:
                p["name"] = name
            self._save()

    def _effective(
        self,
        email: str,
        roles: dict[str, str],
        defaults: dict[str, dict[str, str]],
        people: dict[str, dict] | None = None,
    ) -> tuple[str, str]:
        """(role id, why) for one person under the given assignments and people."""
        if email in super_admin_emails():
            return "admin", "super"
        p = (self.people if people is None else people).get(email, {})
        if p and not p.get("active", True):
            return DEFAULT_ROLE, "inactive"
        if roles.get(email) in ROLES:
            return roles[email], "person"
        if email in bootstrap_emails():
            return "admin", "bootstrap"
        for kind in ("team", "department"):  # a team default wins over its department's
            role = defaults[kind].get(p.get(kind) or "")
            if role in ROLES and role != "admin":  # Admin is never a default
                return role, kind
        return DEFAULT_ROLE, "default"

    def role_of(self, email: str) -> tuple[str, str]:
        return self._effective(email, self.roles, self.defaults)

    def _roles_map(self, roles: dict, defaults: dict, people: dict | None = None) -> dict[str, str]:
        people = self.people if people is None else people
        everyone = super_admin_emails() | bootstrap_emails() | set(people) | set(roles)
        return {e: self._effective(e, roles, defaults, people)[0] for e in everyone}

    def _admins(
        self, roles: dict[str, str], defaults: dict[str, dict[str, str]], people: dict | None = None
    ) -> set[str]:
        return {e for e, r in self._roles_map(roles, defaults, people).items() if r == "admin"}

    def _check_admins_left(self, roles: dict, defaults: dict) -> None:
        if self._admins(self.roles, self.defaults) and not self._admins(roles, defaults):
            raise RoleError("That would leave Xos without an Admin. Make someone else Admin first.")

    @locked
    def caller(self, email: str, name: str, device: str = "") -> Caller:
        role, _ = self.role_of(email)
        perms = self.permissions_for(email)
        preview = self.preview_of(email)
        if preview is None:
            return Caller(email=email, name=name, role=role, perms=perms, device=device)
        shown = {k: preview[k] for k in ("kind", "value", "label", "by", "started")}
        # A preview never grants anything the Admin doesn't already have.
        target = (
            self.permissions_for(preview["value"])
            if preview["kind"] == "person"
            else self.permissions_for_role(preview["role"])
        )
        perms = target & perms
        if preview["kind"] == "person":
            p = self.people.get(preview["value"], {})
            email, name = preview["value"], p.get("name") or preview["value"]
        return Caller(
            email=email,
            name=name,
            role=self.role_of(email)[0] if preview["kind"] == "person" else preview["role"],
            perms=perms,
            device="",
            preview=shown,
        )

    @locked
    def set_role(self, by: str, email: str, role: str) -> dict:
        """Assign `role` to one person ("" clears it back to their default)."""
        email = org_email(email)
        if not email:
            raise RoleError("Use an @example.com address")
        if role and role not in ROLES:
            raise RoleError(f"Unknown role {role}")
        if email in super_admin_emails():
            raise RoleError(
                f"{email} is a super admin (XOS_SUPER_ADMINS), so they're always Admin. "
                "Change that environment variable to remove them."
            )
        if self.roles.get(email, "") == role and (email in self.people or not role):
            return self.view(email)  # no change, nothing to record
        before = self.role_of(email)[0]
        # Clearing a starting Admin's role makes them Admin again, so it grants Admin too.
        grants = role == "admin" or (not role and email in bootstrap_emails())
        if (grants or before == "admin") and by not in super_admin_emails():
            raise RoleError("Only a super admin can make someone Admin or remove Admin.")
        roles = {k: v for k, v in self.roles.items() if k != email}
        if role:
            roles[email] = role
        self._check_admins_left(roles, self.defaults)
        old = self.roles.get(email, "")
        self.roles = roles
        if email not in self.people:
            self.people[email] = {
                "email": email,
                "name": "",
                "active": True,
                "source": "manual",
                "first_seen": time.time(),
            }
        self._save()
        after = self.role_of(email)[0]
        self._audit(
            by,
            "role",
            email,
            **{"from": old, "to": role, "effective_from": before, "effective_to": after},
        )
        return self.view(email)

    @locked
    def set_default(self, by: str, kind: str, name: str, role: str) -> dict:
        """The role everyone in a directory department or team gets unless assigned one."""
        if kind not in ("department", "team"):
            raise RoleError("A default is for a department or a team")
        if role and role not in ROLES:
            raise RoleError(f"Unknown role {role}")
        if role == "admin":
            raise RoleError("Admin can't be a default. Give it to people one by one.")
        name = name.strip()
        if not name:
            raise RoleError(f"Pick a {kind}")
        if self.defaults[kind].get(name, "") == role:
            return self.defaults  # no change, nothing to record
        defaults = copy.deepcopy(self.defaults)
        old = defaults[kind].pop(name, "")
        if role:
            defaults[kind][name] = role
        self._check_admins_left(self.roles, defaults)
        self.defaults = defaults
        self._save()
        self._audit(by, "default", f"{kind}:{name}", **{"from": old, "to": role})
        return self.defaults

    def permissions_for_role(self, role: str) -> frozenset[str]:
        if role == "admin":
            return ROLES["admin"].perms
        return frozenset(self.role_permissions.get(role, ROLES[role].perms)) & frozenset(
            PERMISSIONS
        )

    def permissions_for(self, email: str) -> frozenset[str]:
        role, why = self.role_of(email)
        if role == "admin":
            return ROLES["admin"].perms
        if why == "inactive":
            return ROLES[DEFAULT_ROLE].perms
        perms = set(self.permissions_for_role(role))
        for key, allowed in self.person_permissions.get(email, {}).items():
            if key in PERMISSIONS:
                perms.add(key) if allowed else perms.discard(key)
        return frozenset(perms)

    @locked
    def set_permissions(self, by: str, kind: str, target: str, values: dict | list | None) -> dict:
        if kind == "role":
            if target not in ROLES or target == "admin":
                raise RoleError("Admin always has every permission. Choose another role.")
            if values is not None and (
                not isinstance(values, list) or any(v not in PERMISSIONS for v in values)
            ):
                raise RoleError("Choose known Xos permissions")
            before = self.role_permissions.get(target)
            if values is None:
                self.role_permissions.pop(target, None)
            else:
                self.role_permissions[target] = sorted(set(values))
            after = self.role_permissions.get(target)
        else:
            target = org_email(target)
            if not target or target not in self.people:
                raise RoleError("Choose someone in People & roles first")
            if self.role_of(target)[0] == "admin":
                raise RoleError(
                    "Admins always have every permission. Assign a different role first."
                )
            if not isinstance(values, dict) or any(
                k not in PERMISSIONS or not isinstance(v, bool) for k, v in values.items()
            ):
                raise RoleError("Use Allow, Deny, or Inherit for known Xos permissions")
            before = self.person_permissions.get(target)
            if values:
                self.person_permissions[target] = dict(values)
            else:
                self.person_permissions.pop(target, None)
            after = self.person_permissions.get(target)
        self._save()
        self._audit(by, "permissions." + kind, target, **{"from": before, "to": after})
        return {"ok": True}

    # ---- what Admin shows ----

    @locked
    def view(self, email: str) -> dict:
        p = self.people.get(email) or {"email": email, "name": "", "active": True, "source": ""}
        role, why = self.role_of(email)
        return {
            **p,
            "assigned": self.roles.get(email, ""),
            "role": role,
            "role_source": why,
            "bootstrap": email in bootstrap_emails(),
            "super": email in super_admin_emails(),
            "perms": sorted(self.permissions_for(email)),
            "permission_overrides": self.person_permissions.get(email, {}),
        }

    @locked
    def all(self) -> list[dict]:
        everyone = set(self.people) | set(self.roles) | bootstrap_emails() | super_admin_emails()
        rows = [self.view(e) for e in everyone]
        return sorted(
            rows, key=lambda r: (not r.get("active", True), (r.get("name") or r["email"]).lower())
        )

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(ROLES, 0)
        for row in self.all():
            if row.get("active", True):
                out[row["role"]] += 1
        return out

    # ---- preview ----

    @locked
    def preview_of(self, email: str) -> dict | None:
        p = self.previews.get(email)
        if p and time.time() - p["started"] > PREVIEW_S:
            self.previews.pop(email, None)
            self._audit(email, "preview.end", p["value"], preview_kind=p["kind"], reason="expired")
            return None
        return p

    @locked
    def start_preview(self, admin: Caller, role: str = "", email: str = "") -> dict:
        if role:
            if role not in ROLES:
                raise RoleError(f"Unknown role {role}")
            kind, value, label, perms = (
                "role",
                role,
                ROLES[role].name,
                self.permissions_for_role(role),
            )
        else:
            email = org_email(email)
            if not email or email not in self.people and email not in bootstrap_emails():
                raise RoleError("Pick someone on the People page")
            role, _ = self.role_of(email)
            name = self.people.get(email, {}).get("name") or email
            kind, value, label, perms = "person", email, name, self.permissions_for(email)
        preview = {
            "kind": kind,
            "value": value,
            "label": label,
            "role": role,
            "perms": sorted(perms & admin.perms),
            "by": admin.email,
            "started": time.time(),
        }
        self.previews[admin.email] = preview
        self._audit(admin.email, "preview.start", value, preview_kind=kind, role=role)
        return preview

    @locked
    def end_preview(self, email: str) -> bool:
        p = self.previews.pop(email, None)
        if p:
            self._audit(email, "preview.end", p["value"], preview_kind=p["kind"], reason="exit")
        return p is not None

    # ---- Directory (optional; see the module docstring) ----

    @locked
    def apply_roster(self, roster: list[dict], by: str = "Directory sync") -> dict:
        """Bring People in step with the directory's active roster. Assignments are never touched, but
        roles that follow from team, department or leaving are diffed, checked and logged."""
        incoming: dict[str, dict] = {}
        for p in roster:
            email = org_email(p.get("email"))
            if email and (p.get("status") or "ACTIVE") == "ACTIVE":
                incoming[email] = p
        known = [
            e for e, p in self.people.items() if p.get("source") == "directory" and p.get("active")
        ]
        if not incoming:
            raise DirectoryError("The directory returned no active people, so nothing was changed")
        if len(known) >= 10 and len(incoming) < len(known) / 2:
            raise DirectoryError(
                f"The directory returned {len(incoming)} people, under half of the {len(known)} Xos "
                "knows, so nothing was changed"
            )
        now = time.time()
        stats = {"people": len(incoming), "added": 0, "updated": 0, "left": 0, "returned": 0}
        people = copy.deepcopy(self.people)  # applied only once the roles it implies are checked
        for email, p in incoming.items():
            record = people.get(email)
            fields = {
                "email": email,
                "name": str(p.get("name") or "").strip()[:200],
                "title": str(p.get("title") or "").strip()[:200],
                "department": _ref_name(p.get("department")),
                "team": _ref_name(p.get("team")),
                "picture": _picture(p.get("picture")),
                "directory_id": str(p.get("id") or "")[:64],
            }
            if record is None:
                people[email] = {
                    **fields,
                    "active": True,
                    "source": "directory",
                    "first_seen": now,
                    "synced_at": now,
                }
                stats["added"] += 1
                continue
            if not record.get("active", True):
                stats["returned"] += 1
            elif any(record.get(k) != v for k, v in fields.items()):
                stats["updated"] += 1
            record.update(fields, active=True, source="directory", synced_at=now)
            record.pop("left_at", None)
        for email, record in people.items():
            if record.get("source") == "directory" and record.get("active") and email not in incoming:
                record.update(active=False, left_at=now)
                stats["left"] += 1
        before = self._roles_map(self.roles, self.defaults)
        after = self._roles_map(self.roles, self.defaults, people)
        if any(r == "admin" for r in before.values()) and "admin" not in after.values():
            raise DirectoryError(
                "This sync would leave Xos without an active Admin, so nothing was changed. "
                "Make someone who is still in the directory Admin first."
            )
        changed = {
            e: (before.get(e, DEFAULT_ROLE), r)
            for e, r in after.items()
            if before.get(e, DEFAULT_ROLE) != r
        }
        self.people = people
        self._save()
        for email, (old, new) in sorted(changed.items()):
            why = self._effective(email, self.roles, self.defaults)[1]
            self._audit(by, "role.sync", email, **{"from": old, "to": new, "because": why})
        stats["role_changes"] = len(changed)
        return stats

    @locked
    def _record_sync(self, result: dict, ok: bool) -> None:
        if not ok:
            result["last_ok"] = self.last_sync.get("last_ok")
        self.last_sync = result
        self._save()

    async def sync(
        self, fetch: Callable[[], Awaitable[list[dict]]], by: str = "Directory sync"
    ) -> dict:
        """Fetch the roster with `fetch()` and apply it; remember the outcome. `fetch` raises
        DirectoryError when the directory can't be read. Raises DirectoryError."""
        async with self._lock:
            started = time.time()
            try:
                stats = self.apply_roster(await fetch(), by)
            except DirectoryError as exc:
                self._record_sync(
                    {"at": started, "ok": False, "error": str(exc), "by": by}, ok=False
                )
                logger.warning("directory sync failed: %s", exc)
                raise
            self._record_sync(
                {"at": started, "ok": True, "stats": stats, "by": by, "last_ok": started}, ok=True
            )
            logger.info("directory sync: %s", stats)
            return stats
