"""Persisted access policy and live user previews must agree with API enforcement."""

import pytest
from fastapi.testclient import TestClient

from app import access
from app.main import app, people
from app.people import People, RoleError


@pytest.fixture
def policy(monkeypatch, tmp_path):
    store = People(tmp_path)
    store.people["guest@example.com"] = {"email": "guest@example.com", "active": True}
    store.roles["guest@example.com"] = "screen_operator"
    monkeypatch.setattr(access.settings, "XOS_BOOTSTRAP_ADMINS", "root@example.com")
    return store


def test_role_policy_person_deny_precedence_persistence_and_reset(policy):
    p = policy
    p.set_permissions(
        "admin", "role", "screen_operator", ["admin.view", "demos.view", "demos.present"]
    )
    p.set_permissions(
        "admin", "person", "guest@example.com", {"demos.present": False, "demos.previews": True}
    )
    caller = p.caller("guest@example.com", "Guest")
    assert caller.can("demos.view") and caller.can("demos.previews")
    assert not caller.can("demos.present") and not caller.can("screens.operate")
    restored = People(p.path.parent)
    assert restored.caller("guest@example.com", "Guest").perms == caller.perms
    p.set_permissions("admin", "person", "guest@example.com", {})
    assert p.caller("guest@example.com", "Guest").can("demos.present")
    p.set_permissions("admin", "role", "screen_operator", None)
    assert p.caller("guest@example.com", "Guest").can("screens.operate")
    assert [a["kind"] for a in p.audit(10)] == [
        "permissions.role",
        "permissions.person",
        "permissions.person",
        "permissions.role",
    ]


def test_admin_break_glass_unknown_permissions_and_inactive_account(policy):
    p = policy
    for kind, target, values in [
        ("role", "admin", []),
        ("person", "root@example.com", {}),
        ("role", "member", ["typo.permission"]),
        ("person", "guest@example.com", {"typo": True}),
    ]:
        with pytest.raises(RoleError):
            p.set_permissions("admin", kind, target, values)
    assert p.caller("root@example.com", "Admin").perms == set(access.PERMISSIONS)
    p.set_permissions("admin", "person", "guest@example.com", {"people": True, "demos.view": True})
    p.people["guest@example.com"]["active"] = False
    assert p.caller("guest@example.com", "Guest").perms == access.ROLES["member"].perms


def test_preview_uses_person_overrides_and_updates_when_policy_changes(policy):
    p = policy
    admin = p.caller("root@example.com", "Admin")
    p.set_permissions(
        "admin", "person", "guest@example.com", {"demos.previews": False, "demos.edit": True}
    )
    p.start_preview(admin, email="guest@example.com")
    preview = p.caller(admin.email, admin.name)
    assert preview.preview and preview.can("demos.edit") and not preview.can("demos.previews")
    p.set_permissions("other-admin", "person", "guest@example.com", {"demos.view": False})
    assert not p.caller(admin.email, admin.name).can("demos.view")
    p.end_preview(admin.email)
    p.set_permissions("admin", "role", "member", ["admin.view", "demos.view"])
    p.start_preview(admin, role="member")
    assert p.caller(admin.email, admin.name).perms == {"admin.view", "demos.view"}


def test_http_actions_are_independent_and_preview_writes_are_refused(policy, monkeypatch):
    # Use the real middleware and signed-in proxy; isolate all policy changes.
    for attr in (
        "people",
        "roles",
        "defaults",
        "role_permissions",
        "person_permissions",
        "previews",
        "path",
        "audit_path",
    ):
        monkeypatch.setattr(people, attr, getattr(policy, attr))
    c = TestClient(app)
    assert (
        c.put(
            "/u/api/people/guest@example.com/permissions",
            json={
                "permissions": {
                    "demos.view": True,
                    "demos.previews": False,
                    "demos.present": False,
                    "demos.control": True,
                    "demos.enable": False,
                }
            },
        ).status_code
        == 200
    )
    monkeypatch.setattr(access.settings, "XOS_DEV_USER", "guest@example.com")
    assert c.get("/u/api/demos/studio").status_code == 200
    assert c.get("/u/api/demos/units/hq-01/preview").status_code == 403
    assert (
        c.post(
            "/u/api/demos/sessions", json={"demo_id": "missing", "unit_ids": ["hq-01"]}
        ).status_code
        == 403
    )
    assert (
        c.post("/u/api/demos/sessions/missing/control", json={"action": "pause"}).status_code == 404
    )
    assert c.put("/u/api/demos/enabled", json={"enabled": False}).status_code == 403
    assert c.put("/u/api/roles/member/permissions", json={"permissions": []}).status_code == 403
    monkeypatch.setattr(access.settings, "XOS_DEV_USER", "root@example.com")
    assert c.post("/u/api/preview", json={"email": "guest@example.com"}).status_code == 200
    assert (
        c.post("/u/api/demos/sessions/missing/control", json={"action": "pause"}).status_code == 403
    )
    assert c.put("/u/api/roles/member/permissions", json={"permissions": []}).status_code == 403
    assert c.delete("/u/api/preview").status_code == 200
