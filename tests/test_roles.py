"""Roles and permissions, People & roles, a directory sync and Preview as."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import access
from app import people as people_module
from app.access import PERMISSIONS, ROLES
from app.config import settings
from app.main import app, hub, pages, people
from app.people import DirectoryError, People, RoleError

client = TestClient(app)
TV = "firetv-roles1"
BOOT = "root@example.com"


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "XOS_BOOTSTRAP_ADMINS", BOOT)
    monkeypatch.setattr(people, "path", tmp_path / "people.json")
    monkeypatch.setattr(people, "audit_path", tmp_path / "people_audit.jsonl")
    people.people.clear()
    people.roles.clear()
    people.defaults = {"department": {}, "team": {}}
    people.previews.clear()
    people.last_sync = {}
    hub.devices.clear()
    hub.commands.clear()
    hub.controls.clear()
    client.post("/api/devices/heartbeat", json={"id": TV, "role": "page", "arg": "welcome"})
    yield
    people.previews.clear()
    for page in list(pages.items):
        pages.delete(page)


def as_user(monkeypatch, email):
    monkeypatch.setattr(settings, "XOS_DEV_USER", email)


def give(email, role):
    people.roles[email] = role
    people.people.setdefault(
        email, {"email": email, "name": "", "active": True, "source": "manual"}
    )


def command(kind="route", **args):
    return client.post(f"/api/devices/{TV}/commands", json={"type": kind, "args": args})


# ---------- the matrix ----------


def test_permission_matrix_is_defined_once_and_complete():
    assert set(ROLES) == {"admin", "content_editor", "screen_operator", "member"}
    assert ROLES["admin"].perms == set(PERMISSIONS)
    assert ROLES["member"].perms == {"screens.take_shared"}
    assert ROLES["content_editor"].perms == access.COMMON_VIEWS | access.EDITOR_DEMOS | {
        "admin.view",
        "screens.take_shared",
        "screens.show",
        "content.edit",
    }
    assert ROLES["screen_operator"].perms == access.COMMON_VIEWS | access.OPERATOR_DEMOS | {
        "admin.view",
        "screens.take_shared",
        "screens.show",
        "screens.operate",
    }
    for role in ROLES.values():
        assert role.perms <= set(PERMISSIONS)
        assert "screens.take_shared" in role.perms  # everyone keeps today's Control a screen


def test_content_editor(monkeypatch):
    give("cora@example.com", "content_editor")
    as_user(monkeypatch, "cora@example.com")
    me = client.get("/u/api/me").json()
    assert me["role"] == "content_editor" and me["team"] is True and "content.edit" in me["perms"]
    assert (
        client.post("/api/pages", json={"name": "Hi", "url": "https://example.org"}).status_code
        == 200
    )
    assert command(route="page/welcome").status_code == 200  # show on a free screen
    assert command("display_off").status_code == 403
    assert client.post("/api/rooms", json={"name": "Lamar", "site": "hq"}).status_code == 403
    assert client.put(f"/api/devices/{TV}", json={"name": "x"}).status_code == 403
    assert client.get("/api/people").status_code == 403
    assert client.put("/api/zoom/map", json={"room": "atrium"}).status_code == 403
    # A screen someone holds is theirs.
    hub.take_control(TV, "ana@example.com", "Ana")
    assert command(route="page/welcome").status_code == 409


def test_screen_operator(monkeypatch):
    give("otto@example.com", "screen_operator")
    client.put(f"/api/devices/{TV}/share", json={"enabled": False})  # as the bootstrap admin
    hub.take_control(TV, "ana@example.com", "Ana")
    as_user(monkeypatch, "otto@example.com")
    assert command("display_off").status_code == 200  # held, sharing off: still theirs to run
    assert command("update").status_code == 403  # box updates are setup
    screens = client.get("/api/control").json()["screens"]
    assert [s["id"] for s in screens] == [TV] and screens[0]["sharing_off"]
    assert client.delete(f"/api/control/{TV}").status_code == 200  # release anyone's hold
    assert client.post(f"/api/control/{TV}").status_code == 200  # sharing off is fine
    assert client.post("/api/pages", json={"name": "x", "url": "https://x.io"}).status_code == 403
    assert client.post("/api/rooms", json={"name": "Lamar", "site": "hq"}).status_code == 403
    assert client.post("/api/provision", json={"ip": "203.0.113.2"}).status_code == 403


def test_member_keeps_todays_control_a_screen(monkeypatch):
    as_user(monkeypatch, "mia@example.com")
    me = client.get("/u/api/me").json()
    assert me["role"] == "member" and me["team"] is False
    assert command().status_code == 403
    assert client.post(f"/api/control/{TV}").status_code == 200
    assert command().status_code == 200
    assert client.post("/api/pages", json={"name": "x", "url": "https://x.io"}).status_code == 403
    # Signing in puts them on the People page.
    assert people.people["mia@example.com"]["source"] == "signin"


def test_assigned_admin_gets_everything(monkeypatch):
    give("ada@example.com", "admin")
    as_user(monkeypatch, "ada@example.com")
    assert client.post("/api/rooms", json={"name": "Roles room", "site": "hq"}).status_code == 200
    assert client.get("/api/people").status_code == 200
    roles = client.get("/api/roles").json()
    assert {r["id"]: r["people"] for r in roles["roles"]}["admin"] == 2  # ada + bootstrap
    assert set(roles["permissions"]) == set(PERMISSIONS)


# ---------- assigning roles ----------


def test_assign_defaults_and_audit(monkeypatch):
    people.people["dee@example.com"] = {
        "email": "dee@example.com",
        "active": True,
        "source": "directory",
        "department": "Marketing",
        "team": "Brand",
    }
    r = client.put(
        "/api/people/defaults",
        json={"kind": "department", "name": "Marketing", "role": "content_editor"},
    )
    assert r.status_code == 200
    assert people.role_of("dee@example.com") == ("content_editor", "department")
    client.put(
        "/api/people/defaults", json={"kind": "team", "name": "Brand", "role": "screen_operator"}
    )
    assert people.role_of("dee@example.com") == ("screen_operator", "team")
    row = client.put("/api/people/dee@example.com/role", json={"role": "admin"}).json()
    assert row["role"] == "admin" and row["role_source"] == "person"
    client.put("/api/people/dee@example.com/role", json={"role": ""})
    assert people.role_of("dee@example.com") == ("screen_operator", "team")
    assert client.put("/api/people/x@gmail.com/role", json={"role": "admin"}).status_code == 409
    assert client.put("/api/people/dee@example.com/role", json={"role": "boss"}).status_code == 422

    items = client.get("/api/people/audit").json()["items"]
    assert [(i["kind"], i["target"], i["from"], i["to"]) for i in items] == [
        ("role", "dee@example.com", "admin", ""),
        ("role", "dee@example.com", "", "admin"),
        ("default", "team:Brand", "", "screen_operator"),
        ("default", "department:Marketing", "", "content_editor"),
    ]
    assert all(i["by"] == BOOT for i in items)
    # Append-only: earlier lines are never rewritten.
    first = people.audit_path.read_text().splitlines()
    client.put("/api/people/dee@example.com/role", json={"role": "member"})
    after = people.audit_path.read_text().splitlines()
    assert after[: len(first)] == first and len(after) == len(first) + 1


def test_super_admins_are_always_admin_and_cant_be_changed(monkeypatch):
    monkeypatch.setattr(settings, "XOS_SUPER_ADMINS", BOOT)
    r = client.put(f"/api/people/{BOOT}/role", json={"role": "member"})
    assert r.status_code == 409 and "XOS_SUPER_ADMINS" in r.json()["detail"]
    people.people[BOOT] = {
        "email": BOOT,
        "active": False,
        "source": "directory",
        "department": "Eng",
    }
    people.defaults["department"]["Eng"] = "member"
    assert people.role_of(BOOT) == ("admin", "super")  # even after leaving the directory
    as_user(monkeypatch, BOOT)
    assert client.get("/u/api/me").json()["role"] == "admin"
    body = client.get("/api/people").json()
    assert body["you_super"] is True and body["super_admins"] == [BOOT]


def test_only_a_super_admin_grants_or_removes_admin_including_starting_admins(monkeypatch):
    gs = "gus@example.com"
    monkeypatch.setattr(settings, "XOS_SUPER_ADMINS", BOOT)
    monkeypatch.setattr(settings, "XOS_BOOTSTRAP_ADMINS", f"{BOOT},{gs}")
    give(gs, "")  # in People, nothing assigned: a starting Admin
    people.roles.pop(gs)
    assert people.role_of(gs) == ("admin", "bootstrap")
    give("ada@example.com", "admin")
    give("bo@example.com", "member")
    as_user(monkeypatch, "ada@example.com")  # an Admin, not a super admin
    for email, role in ((gs, "member"), ("bo@example.com", "admin"), ("ada@example.com", "member")):
        r = client.put(f"/api/people/{email}/role", json={"role": role})
        assert r.status_code == 409 and "super admin" in r.json()["detail"], email
    r = client.put("/api/people/bo@example.com/role", json={"role": "screen_operator"})
    assert r.status_code == 200
    as_user(monkeypatch, BOOT)  # the super admin
    assert client.put(f"/api/people/{gs}/role", json={"role": "member"}).status_code == 200
    assert people.role_of(gs) == ("member", "person")  # despite XOS_BOOTSTRAP_ADMINS
    with pytest.raises(RoleError, match="super admin"):
        people.set_role("ada@example.com", gs, "")  # clearing would make them Admin again
    assert client.put(f"/api/people/{gs}/role", json={"role": ""}).status_code == 200
    assert people.role_of(gs) == ("admin", "bootstrap")
    r = client.put("/api/people/ada@example.com/role", json={"role": "member"})
    assert r.status_code == 200


# ---------- directory sync (People.sync with a deployment-supplied fetch) ----------


def directory(roster, error=None):
    """A fake directory: an async fetch returning `roster`, or raising `error`."""

    async def fetch():
        if error is not None:
            raise error
        return list(roster)

    return fetch


def person(n, **kw):
    return {
        "id": f"id-{n}",
        "email": f"p{n}@example.com",
        "name": f"Person {n}",
        "status": "ACTIVE",
        "title": "Engineer",
        "department": {"id": "d1", "name": "Eng"},
        "team": {"id": "t1", "name": "Software"},
        "picture": f"https://img/{n}.png",
        **kw,
    }


def run(coro):
    return asyncio.run(coro)


def test_directory_sync_imports_and_marks_leavers(monkeypatch):
    roster = [person(n) for n in range(12)] + [
        {"id": "x", "email": "guest@gmail.com", "status": "ACTIVE"},
        person(99, picture="javascript:alert(1)"),
    ]
    stats = run(people.sync(directory(roster)))
    assert stats == {
        "people": 13,
        "added": 13,
        "updated": 0,
        "left": 0,
        "returned": 0,
        "role_changes": 0,
    }
    p = people.people["p3@example.com"]
    assert (p["name"], p["department"], p["team"], p["title"]) == (
        "Person 3",
        "Eng",
        "Software",
        "Engineer",
    )
    assert p["picture"] == "https://img/3.png" and people.people["p99@example.com"]["picture"] == ""
    assert "guest@gmail.com" not in people.people

    people.set_role(BOOT, "p1@example.com", "content_editor")
    stats = run(people.sync(directory(roster[2:12])))
    assert stats["left"] == 3  # p0, p1, p99
    assert people.people["p1@example.com"]["active"] is False
    assert people.roles["p1@example.com"] == "content_editor"  # kept...
    assert people.role_of("p1@example.com") == ("member", "inactive")  # ...but has no effect
    assert run(people.sync(directory(roster)))["returned"] == 3
    assert people.role_of("p1@example.com") == ("content_editor", "person")


def test_directory_safety_rails(monkeypatch):
    with pytest.raises(DirectoryError, match="unreachable"):
        run(people.sync(directory([], error=DirectoryError("directory unreachable"))))
    with pytest.raises(DirectoryError, match="no active people"):
        run(people.sync(directory([])))
    roster = [person(n) for n in range(20)]
    run(people.sync(directory(roster)))
    with pytest.raises(DirectoryError, match="under half"):
        run(people.sync(directory(roster[:9])))
    assert all(p["active"] for p in people.people.values())  # nothing changed
    assert people.last_sync["ok"] is False and people.last_sync["last_ok"]


def test_people_page_without_a_directory_lists_who_signed_in():
    body = client.get("/api/people").json()
    assert [p["email"] for p in body["people"]] == [BOOT]  # signed in (and bootstrap)
    assert client.post("/api/people/sync").status_code in (404, 405)  # no directory built in
    run(people.sync(directory([person(1), person(2)]), by=BOOT))
    body = client.get("/api/people").json()
    assert body["departments"] == ["Eng"] and body["teams"] == ["Software"]


# ---------- preview ----------


def test_preview_is_read_only_and_logged(monkeypatch):
    people.people["cora@example.com"] = {
        "email": "cora@example.com",
        "name": "Cora",
        "active": True,
        "source": "directory",
    }
    people.roles["cora@example.com"] = "content_editor"
    me = client.post("/api/preview", json={"email": "cora@example.com"}).json()
    assert me["preview"]["label"] == "Cora" and me["role"] == "content_editor"
    assert me["email"] == "cora@example.com" and "people" not in me["perms"]
    assert client.get("/u/api/me").json()["preview"]["by"] == BOOT
    # Reads follow the previewed perspective.
    assert client.get("/api/pages").status_code == 200
    assert client.get("/api/people").status_code == 403
    assert client.get("/api/appliances").status_code == 403
    # Every write is refused, even ones Cora (or the admin) could do.
    for method, path, body in [
        ("POST", "/api/pages", {"name": "x", "url": "https://x.io"}),
        ("PUT", "/api/demos/enabled", {"enabled": False}),
        ("POST", f"/api/devices/{TV}/commands", {"type": "reload"}),
        ("POST", f"/api/control/{TV}", None),
        ("POST", "/api/rooms", {"name": "Nope", "site": "hq"}),
        ("PUT", f"/api/people/{BOOT}/role", {"role": "member"}),
        ("POST", "/api/preview", {"role": "admin"}),
        ("POST", "/api/people/sync", None),
    ]:
        for prefix in ("", "/u"):
            r = client.request(method, prefix + path, json=body)
            assert r.status_code == 403 and "read-only" in r.json()["detail"], (method, path)
    assert client.get("/api/calendar/oauth/start").status_code == 403
    assert not pages.items and not hub.commands.get(TV)
    # Exit preview works, and the admin is back.
    me = client.delete("/u/api/preview").json()
    assert me["preview"] is None and me["role"] == "admin" and me["email"] == BOOT
    assert client.post("/api/pages", json={"name": "x", "url": "https://x.io"}).status_code == 200
    kinds = [(i["kind"], i["target"]) for i in client.get("/api/people/audit").json()["items"]]
    assert kinds == [("preview.end", "cora@example.com"), ("preview.start", "cora@example.com")]


def test_preview_a_role_and_who_may_preview(monkeypatch):
    me = client.post("/api/preview", json={"role": "member"}).json()
    assert me["role"] == "member" and me["perms"] == ["screens.take_shared"] and me["team"] is False
    assert client.get("/api/control").status_code == 200
    assert client.post(f"/api/control/{TV}").status_code == 403
    client.delete("/api/preview")
    # Previewing Admin still can't write.
    client.post("/api/preview", json={"role": "admin"})
    assert client.post("/api/rooms", json={"name": "No", "site": "hq"}).status_code == 403
    client.delete("/api/preview")
    assert client.post("/api/preview", json={"role": "boss"}).status_code == 400
    assert client.post("/api/preview", json={}).status_code == 400
    # Only people with the people permission may preview.
    give("otto@example.com", "screen_operator")
    as_user(monkeypatch, "otto@example.com")
    assert client.post("/api/preview", json={"role": "admin"}).status_code == 403


def test_preview_never_grants_more():
    operator = access.caller_as("screen_operator", "otto@example.com")
    preview = people.start_preview(operator, role="admin")
    assert set(preview["perms"]) == ROLES["screen_operator"].perms
    people.previews.clear()


def test_preview_expires(monkeypatch):
    client.post("/api/preview", json={"role": "member"})
    people.previews[BOOT]["started"] -= people_module.PREVIEW_S + 1
    assert client.get("/u/api/me").json()["preview"] is None
    assert client.get("/api/people/audit").json()["items"][0]["reason"] == "expired"


# ---------- MCP ----------


def test_mcp_tools_follow_roles(monkeypatch):
    from types import SimpleNamespace

    from app import mcp_server

    def as_mcp_user(email):
        user = SimpleNamespace(email=email, name=email, is_bot=False)
        monkeypatch.setattr(mcp_server, "user_from_headers", lambda _h: user)
        return mcp_server.authorized({"authorization": "Bearer id-token"})

    assert as_mcp_user("mia@example.com") is None  # members don't get /mcp
    give("cora@example.com", "content_editor")
    cora = as_mcp_user("cora@example.com")
    assert not mcp_server.tool_refusal(cora, "create_slideshow")
    assert not mcp_server.tool_refusal(cora, "show_on_screen")
    assert "can't use set_display" in mcp_server.tool_refusal(cora, "set_display")
    assert mcp_server.tool_refusal(cora, "save_room")
    assert mcp_server.tool_refusal(cora, "a_new_tool")  # unlisted tools are Admin-only
    assert set(mcp_server.TOOL_PERMS) == {t.__name__ for t in mcp_server.TOOLS}
    assert not mcp_server.tool_refusal(access.APP_TOKEN, "zoom_control")
    assert mcp_server.tool_refusal(None, "list_screens")

    client.post("/api/preview", json={"role": "content_editor"})
    boss = as_mcp_user(BOOT)
    assert not mcp_server.tool_refusal(boss, "list_screens")
    assert "read-only" in mcp_server.tool_refusal(boss, "create_slideshow")

    # A tool runs as its caller: a content editor can't take over a held screen.
    hub.take_control(TV, "ana@example.com", "Ana")
    token = mcp_server.current_caller.set(cora)
    try:
        with pytest.raises(ValueError, match="Ana"):
            mcp_server.show_on_screen(TV, "welcome")
    finally:
        mcp_server.current_caller.reset(token)


def test_people_state_survives_a_restart(tmp_path):
    store = People(tmp_path)
    store.seen("zed@example.com", "Zed")
    store.set_role(BOOT, "zed@example.com", "screen_operator")
    store.set_default(BOOT, "team", "Optics", "content_editor")
    again = People(tmp_path)
    assert again.role_of("zed@example.com") == ("screen_operator", "person")
    assert again.defaults["team"] == {"Optics": "content_editor"}
    assert again.people["zed@example.com"]["name"] == "Zed"
    assert len(again.audit()) == 2


def test_unchanged_roles_are_not_logged():
    people.set_role(BOOT, "nia@example.com", "content_editor")
    people.set_role(BOOT, "nia@example.com", "content_editor")
    people.set_default(BOOT, "team", "Optics", "")
    assert [i["to"] for i in people.audit()] == ["content_editor"]


# ---------- security review fixes ----------


def mcp_as(caller):
    from app import mcp_server

    return mcp_server, mcp_server.current_caller.set(caller)


def test_content_editor_cant_command_remotes_setup_or_room_computers(monkeypatch):
    targets = {
        "web-tablet-x": {"role": "tablet", "arg": "atrium", "name": "Atrium tablet"},
        "web-remote-x": {"role": "remote", "arg": "atrium", "name": "Atrium remote"},
        "web-setup-x": {"role": "setup"},
        "web-admin-x": {"role": "admin"},
        "xosroom-pc-x": {"role": "roompc", "arg": "atrium", "client": "xosroom-linux-1.0"},
    }
    for device_id, extra in targets.items():
        hub.devices[device_id] = {"id": device_id, "last_seen": 0, **extra}
    give("cora@example.com", "content_editor")
    cora = access.caller_as("content_editor", "cora@example.com")
    as_user(monkeypatch, "cora@example.com")
    for device_id in targets:
        for kind, args in [
            ("route", {"route": "page/welcome"}),
            ("screenshot", {}),
            ("reload", {}),
            ("message", {"text": "hi"}),
        ]:
            r = client.post(f"/api/devices/{device_id}/commands", json={"type": kind, "args": args})
            assert r.status_code == 403, (device_id, kind)
        mcp_server, token = mcp_as(cora)
        try:
            with pytest.raises(ValueError, match="Take control"):
                mcp_server.show_on_screen(device_id, "welcome")
            with pytest.raises(ValueError, match="Take control"):
                asyncio.run(mcp_server.take_screenshot(device_id))
        finally:
            mcp_server.current_caller.reset(token)
        assert not hub.commands.get(device_id)
    # Display screens still work, and an operator still reaches the room computer.
    assert command(route="page/welcome").status_code == 200
    give("otto@example.com", "screen_operator")
    as_user(monkeypatch, "otto@example.com")
    r = client.post("/api/devices/web-tablet-x/commands", json={"type": "reload"})
    assert r.status_code == 200


def test_mcp_sharing_tools_follow_holds_and_display_screens():
    hub.devices["web-tablet-y"] = {"id": "web-tablet-y", "role": "tablet", "last_seen": 0}
    cora = access.caller_as("content_editor", "cora@example.com")
    hub.take_control(TV, "ana@example.com", "Ana")
    mcp_server, token = mcp_as(cora)
    try:
        with pytest.raises(ValueError, match="Ana"):
            mcp_server.share_link_to_screen(TV, "https://example.org")
        with pytest.raises(ValueError, match="Ana"):
            mcp_server.stop_sharing(TV)
        with pytest.raises(ValueError, match="Take control"):
            mcp_server.share_link_to_screen("web-tablet-y", "https://example.org")
        hub.release_control(TV)
        assert mcp_server.stop_sharing(TV) == {"stopped": False}
    finally:
        mcp_server.current_caller.reset(token)


def test_mcp_tools_fail_closed_without_a_caller():
    from app import mcp_server

    assert mcp_server.current_caller.get() is None
    for call in (
        lambda: mcp_server.show_on_screen(TV, "welcome"),
        lambda: mcp_server.stop_sharing(TV),
        lambda: asyncio.run(mcp_server.zoom_control("atrium", "mute")),
    ):
        with pytest.raises(ValueError, match="no verified MCP caller"):
            call()
    assert not hub.commands.get(TV)


def test_sync_role_changes_are_logged_and_cant_remove_the_last_admin(monkeypatch):
    monkeypatch.setattr(settings, "XOS_BOOTSTRAP_ADMINS", "")
    monkeypatch.setattr(settings, "XOS_SUPER_ADMINS", "")  # so p0 is the only Admin
    roster = [person(n) for n in range(4)]
    run(people.sync(directory(roster)))
    people.roles["p0@example.com"] = "admin"
    people.set_default("boss@example.com", "team", "Brand", "content_editor")
    # p1 moves to Brand (gets its default); p2 leaves and loses their assigned role.
    people.set_role("boss@example.com", "p2@example.com", "screen_operator")
    moved = [roster[0], person(1, team={"id": "t2", "name": "Brand"}), roster[3]]
    stats = run(people.sync(directory(moved), by="admin@example.com"))
    assert stats["role_changes"] == 2
    synced = [i for i in people.audit() if i["kind"] == "role.sync"]
    assert sorted((i["target"], i["from"], i["to"], i["because"]) for i in synced) == [
        ("p1@example.com", "member", "content_editor", "team"),
        ("p2@example.com", "screen_operator", "member", "inactive"),
    ]
    assert all(i["by"] == "admin@example.com" for i in synced)
    # The only Admin leaving the directory would leave no Admin: refused, nothing applied.
    with pytest.raises(DirectoryError, match="without an active Admin"):
        run(people.sync(directory([roster[3], moved[1]])))
    assert people.people["p0@example.com"]["active"] is True
    assert people.role_of("p0@example.com") == ("admin", "person")


def test_admin_cant_be_a_default():
    r = client.put("/api/people/defaults", json={"kind": "team", "name": "Brand", "role": "admin"})
    assert r.status_code == 409 and "one by one" in r.json()["detail"]
    with pytest.raises(RoleError):
        people.set_default(BOOT, "department", "Eng", "admin")
    # A stored Admin default (hand-edited file) is ignored.
    people.people["x@example.com"] = {"email": "x@example.com", "active": True, "team": "Brand"}
    people.defaults["team"]["Brand"] = "admin"
    assert people.role_of("x@example.com") == ("member", "default")


def test_people_state_is_thread_safe_and_saved_atomically(tmp_path):
    import threading

    store = People(tmp_path)
    errors = []

    def sign_ins(n):
        try:
            for i in range(40):
                store.seen(f"t{n}-{i}@example.com", f"T {n} {i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def assign():
        try:
            for i in range(40):
                store.set_role(BOOT, f"r{i}@example.com", "content_editor")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=sign_ins, args=(n,)) for n in range(4)]
    threads.append(threading.Thread(target=assign))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    saved = json.loads((tmp_path / "people.json").read_text())
    assert len(saved["people"]) == 4 * 40 + 40 and len(saved["roles"]) == 40
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".people.*"))


def test_unreadable_people_json_uses_the_backup_or_refuses_to_start(tmp_path):
    store = People(tmp_path)
    store.set_role(BOOT, "kept@example.com", "screen_operator")
    store.set_role(BOOT, "also@example.com", "content_editor")  # .bak now holds the first save
    (tmp_path / "people.json").write_text("{not json")
    again = People(tmp_path)
    assert again.role_of("kept@example.com") == ("screen_operator", "person")
    assert (tmp_path / "people.json").read_text() == "{not json"  # not overwritten on start
    (tmp_path / "people.json.bak").write_text("[]")
    with pytest.raises(RuntimeError, match="Can't read"):
        People(tmp_path)
    assert (tmp_path / "people.json").read_text() == "{not json"


def test_starting_from_the_backup_keeps_it_good(tmp_path):
    store = People(tmp_path)
    store.set_role(BOOT, "kept@example.com", "screen_operator")
    store.set_role(BOOT, "also@example.com", "content_editor")
    (tmp_path / "people.json").write_text("{not json")
    again = People(tmp_path)  # starts from people.json.bak
    again.set_role(BOOT, "new@example.com", "content_editor")  # first save after recovery
    backup = json.loads((tmp_path / "people.json.bak").read_text())
    assert backup["roles"] == {"kept@example.com": "screen_operator"}  # not the corrupt file
    saved = json.loads((tmp_path / "people.json").read_text())
    assert saved["roles"]["new@example.com"] == "content_editor"
    # The next save rotates the now-good file into the backup as usual.
    again.set_role(BOOT, "more@example.com", "member")
    assert "new@example.com" in json.loads((tmp_path / "people.json.bak").read_text())["roles"]


def test_members_can_only_take_display_screens(monkeypatch):
    hub.devices["xosroom-pc-z"] = {
        "id": "xosroom-pc-z",
        "role": "roompc",
        "arg": "atrium",
        "client": "xosroom-linux-1.0",
        "last_seen": 0,
    }
    hub.devices["web-tablet-z"] = {"id": "web-tablet-z", "role": "tablet", "last_seen": 0}
    as_user(monkeypatch, "mia@example.com")
    assert [s["id"] for s in client.get("/api/control").json()["screens"]] == [TV]
    for device_id in ("xosroom-pc-z", "web-tablet-z"):
        assert client.post(f"/api/control/{device_id}").status_code == 403
        r = client.post(
            f"/api/devices/{device_id}/commands",
            json={"type": "route", "args": {"route": "page/welcome"}},
        )
        assert r.status_code == 403
