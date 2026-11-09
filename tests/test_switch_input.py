"""Switch TV to Xos: Display off, then Display on, so HDMI-CEC takes the TV back to the stick."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import access, main, mcp_server
from app.config import settings
from app.main import app, hub, people

client = TestClient(app)
TV = "firetv-switch1"
BOOT = "root@example.com"


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "XOS_BOOTSTRAP_ADMINS", BOOT)
    monkeypatch.setattr(people, "path", tmp_path / "people.json")
    monkeypatch.setattr(people, "audit_path", tmp_path / "people_audit.jsonl")
    monkeypatch.setattr(hub, "device_keys", {})
    people.roles.clear()
    people.previews.clear()
    hub.devices.clear()
    hub.commands.clear()
    hub.controls.clear()
    main._switching.clear()
    for name in ("SWITCH_OFF_WAIT_S", "SWITCH_ASLEEP_WAIT_S", "SWITCH_ON_WAIT_S"):
        monkeypatch.setattr(main, name, 1.0)
    monkeypatch.setattr(main, "SWITCH_SETTLE_S", 0.05)
    monkeypatch.setattr(main, "SWITCH_POLL_S", 0.01)
    yield
    people.previews.clear()
    main._switching.clear()
    hub._device_streams.pop(TV, None)


def stick(app_version="0.6.0", display_control=True, display_on=True, connected=True):
    native = {
        "app": app_version,
        "displayControl": display_control,
        "displayOn": display_on,
        "wakeable": True,
    }
    client.post(
        "/api/devices/heartbeat",
        json={
            "id": TV,
            "client": f"firetv-{app_version}",
            "role": "tv",
            "arg": "hq-central",
            "native": native,
        },
    )
    if connected:
        hub._device_streams[TV] = {asyncio.Queue(maxsize=32)}


def as_user(monkeypatch, email):
    monkeypatch.setattr(settings, "XOS_DEV_USER", email)


def command(kind):
    return client.post(f"/api/devices/{TV}/commands", json={"type": kind, "args": {}})


def switch(headers=None):
    return client.post(
        f"/api/devices/{TV}/commands", json={"type": "switch_input", "args": {}}, headers=headers
    )


@pytest.fixture
def no_steps(monkeypatch):
    """REST tests check who may start it; the steps themselves are tested below."""
    started = []

    async def fake(device_id, record):
        started.append(device_id)
        hub.ack(device_id, record["id"], True, "test")

    monkeypatch.setattr(main, "_switch", fake)
    return started


# ---------- who may ----------


def test_screen_operators_and_admins_may_switch(monkeypatch, no_steps):
    stick()
    r = switch()  # the bootstrap Admin
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "switch_input"
    people.roles["otto@example.com"] = "screen_operator"
    as_user(monkeypatch, "otto@example.com")
    assert switch().status_code == 200
    assert no_steps == [TV, TV]


def test_a_room_remote_with_its_device_key_may_switch(monkeypatch, no_steps):
    stick()
    as_user(monkeypatch, "")
    hub.heartbeat({"id": "room-ipad", "role": "tablet", "room": "hq-central", "name": "iPad"})
    key = hub.issue_key("room-ipad")
    assert switch({"X-Xos-Device": "room-ipad", "X-Xos-Key": key}).status_code == 200
    assert switch({"X-Xos-Device": "room-ipad", "X-Xos-Key": "wrong"}).status_code == 401
    assert switch().status_code == 401
    assert no_steps == [TV]


def test_members_and_content_editors_may_not(monkeypatch, no_steps):
    stick()
    as_user(monkeypatch, "mia@example.com")  # a Member, even holding the screen
    hub.take_control(TV, "mia@example.com", "Mia")
    r = switch()
    assert r.status_code == 403 and "Member" in r.json()["detail"]
    hub.release_control(TV)
    people.roles["cora@example.com"] = "content_editor"
    as_user(monkeypatch, "cora@example.com")
    assert switch().status_code == 403
    assert no_steps == []
    assert not hub.commands.get(TV)


def test_preview_is_read_only(no_steps):
    stick()
    assert client.post("/api/preview", json={"role": "screen_operator"}).status_code == 200
    r = switch()
    assert r.status_code == 403 and "Preview" in r.json()["detail"]
    assert no_steps == []


# ---------- which sticks ----------


@pytest.mark.parametrize(
    ("version", "control", "message"),
    [
        ("0.5.0", True, "0.6.0 or later"),
        ("0.5.9", True, "0.6.0 or later"),
        ("0.6.0", False, "display control"),
    ],
)
def test_older_sticks_and_ones_without_display_control_are_refused(
    no_steps, version, control, message
):
    stick(version, control)
    r = switch()
    assert r.status_code == 409 and message in r.json()["detail"]
    assert no_steps == [] and not hub.commands.get(TV)


@pytest.mark.parametrize("version", ["0.6.0", "0.6.1", "0.10.0", "1.0"])
def test_sticks_from_0_6_0_are_accepted(no_steps, version):
    stick(version)
    assert switch().status_code == 200


def test_browsers_and_offline_sticks_are_refused(no_steps):
    client.post("/api/devices/heartbeat", json={"id": "web-switch", "role": "tv"})
    r = client.post("/api/devices/web-switch/commands", json={"type": "switch_input"})
    assert r.status_code == 409 and "Xos TV app" in r.json()["detail"]
    stick(connected=False)
    r = switch()
    assert r.status_code == 409 and "isn't connected" in r.json()["detail"]
    assert no_steps == []


def test_eligibility_matches_the_page():
    assert main.tv_app_version({"client": "firetv-0.6.0", "native": {}}) == (0, 6, 0)
    assert main.tv_app_version({"client": "firetv-0.5.0", "native": {"app": "0.6.2"}}) == (0, 6, 2)
    assert main.tv_app_version({"client": "browser"}) is None
    d = {"client": "firetv-0.6.0", "native": {"app": "0.6.0", "displayControl": True}}
    assert main.switch_input_problem(d) == ""


# ---------- the steps ----------


def run_with_stick(answer_off=True, off_ok=True, asleep=False):
    """Start a switch on a fake stick that answers commands like Xos TV 0.6.0 does."""
    stick(display_on=not asleep)
    seen = []
    handled = set()

    async def fake_stick(record):
        while record["status"] == "sent":
            for cmd in reversed(hub.commands.get(TV, [])):
                if cmd["status"] != "sent" or cmd["type"] == "switch_input" or cmd["id"] in handled:
                    continue
                handled.add(cmd["id"])
                seen.append(cmd["type"])
                if cmd["type"] == "display_off":
                    if not answer_off:
                        continue  # delivered, never answered
                    hub.ack(TV, cmd["id"], off_ok, "ok" if off_ok else "error: no device admin")
                    # SleepWatch checks in asleep.
                    hub.heartbeat(
                        {"id": TV, "native": {**hub.devices[TV]["native"], "displayOn": False}}
                    )
                else:
                    hub.ack(TV, cmd["id"], True, "waking")
            await asyncio.sleep(0.005)

    async def go():
        record = main.switch_input(TV)
        assert TV in main._switching
        with pytest.raises(Exception, match="already switching"):
            main.switch_input(TV)
        await asyncio.wait_for(fake_stick(record), 5)
        await asyncio.sleep(0.02)
        return record

    return asyncio.run(go()), seen


def test_turns_the_tv_off_then_on():
    record, seen = run_with_stick()
    assert seen == ["display_off", "display_on"]
    assert [c["type"] for c in reversed(hub.commands[TV])] == [
        "switch_input",
        "display_off",
        "display_on",
    ]
    assert record["status"] == "done" and record["result"] == "The TV is on Xos"
    assert TV not in main._switching  # a new run may start


def test_waits_for_the_tv_to_settle_before_waking_it(monkeypatch):
    monkeypatch.setattr(main, "SWITCH_SETTLE_S", 0.2)
    record, _ = run_with_stick()
    off, on = reversed(hub.commands[TV][:2])
    assert (off["type"], on["type"]) == ("display_off", "display_on")
    assert on["created"] - off["acked"] >= 0.2
    assert record["status"] == "done"


def test_an_asleep_stick_is_only_woken():
    record, seen = run_with_stick(asleep=True)
    assert seen == ["display_on"] and record["status"] == "done"


def test_stops_when_display_off_fails():
    record, seen = run_with_stick(off_ok=False)
    assert seen == ["display_off"]
    assert record["status"] == "failed"
    assert record["result"] == "Display off didn't work: error: no device admin"


def test_stops_when_the_stick_doesnt_answer_and_turns_the_tv_back_on():
    """Display off reached the stick but wasn't confirmed: don't leave the TV off."""
    record, seen = run_with_stick(answer_off=False)
    assert seen == ["display_off"]
    assert record["status"] == "failed" and "didn't answer" in record["result"]
    assert [c["type"] for c in reversed(hub.commands[TV])] == [
        "switch_input",
        "display_off",
        "display_on",
    ]


def run_undelivered(asleep=False):
    """Start a switch, then the stick's command stream drops, so its steps are never delivered."""
    stick(display_on=not asleep)

    async def go():
        record = main.switch_input(TV)
        hub._device_streams.pop(TV, None)
        await asyncio.wait_for(main._switching[TV], 5)
        return record

    return asyncio.run(go())


def test_an_undelivered_display_off_expires_and_nothing_else_is_sent():
    record = run_undelivered()
    assert record["status"] == "failed" and "Display off" in record["result"]
    assert [(c["type"], c["status"]) for c in reversed(hub.commands[TV])] == [
        ("switch_input", "failed"),
        ("display_off", "expired"),  # a late check-in can't turn the TV off now
    ]
    assert TV not in main._switching


def test_an_undelivered_display_on_expires():
    record = run_undelivered(asleep=True)
    assert record["status"] == "failed" and "Display on" in record["result"]
    assert [(c["type"], c["status"]) for c in reversed(hub.commands[TV])] == [
        ("switch_input", "failed"),
        ("display_on", "expired"),
    ]


def test_manual_display_on_and_off_wait_for_a_running_switch():
    stick()
    main._switching[TV] = object()  # a switch is running
    for kind in ("display_on", "display_off"):
        r = command(kind)
        assert r.status_code == 409 and "switching this TV" in r.json()["detail"]
    otto = access.caller_as("screen_operator", "otto@example.com")
    token = mcp_server.current_caller.set(otto)
    try:
        with pytest.raises(ValueError, match="switching this TV"):
            mcp_server.set_display(TV, True)
    finally:
        mcp_server.current_caller.reset(token)
    assert not hub.commands.get(TV)
    main._switching.clear()
    assert command("display_on").status_code == 200


def test_forgetting_the_screen_mid_switch_stops_it_cleanly(monkeypatch):
    monkeypatch.setattr(main, "SWITCH_SETTLE_S", 0.3)
    stick()

    async def go():
        record = main.switch_input(TV)
        while not any(c["type"] == "display_off" for c in hub.commands.get(TV, [])):
            await asyncio.sleep(0.005)
        off = next(c for c in hub.commands[TV] if c["type"] == "display_off")
        hub.ack(TV, off["id"], True, "ok")
        await asyncio.sleep(0.05)  # now settling, before Display on
        assert hub.forget(TV)
        await asyncio.wait_for(main._switching[TV], 5)
        return record

    asyncio.run(go())
    assert TV not in hub.commands and TV not in hub.devices
    assert TV not in main._switching


def test_it_never_runs_by_itself():
    """Only a person, a room remote or an agent starts it: nothing schedules it."""
    import inspect

    source = inspect.getsource(main)
    assert source.count("switch_input(device_id)") == 1  # the command route
    assert "switch_input(" in inspect.getsource(mcp_server.switch_tv_to_xos)


# ---------- MCP ----------


def test_mcp_tool_has_the_same_permission_and_gating(monkeypatch, no_steps):
    assert mcp_server.TOOL_PERMS["switch_tv_to_xos"] == "screens.operate"
    assert mcp_server.TOOL_PERMS["switch_tv_to_xos"] == mcp_server.TOOL_PERMS["set_display"]
    cora = access.caller_as("content_editor", "cora@example.com")
    assert "can't use switch_tv_to_xos" in mcp_server.tool_refusal(cora, "switch_tv_to_xos")
    member = access.caller_as("member", "mia@example.com")
    assert mcp_server.tool_refusal(member, "switch_tv_to_xos")
    otto = access.caller_as("screen_operator", "otto@example.com")
    assert not mcp_server.tool_refusal(otto, "switch_tv_to_xos")
    previewing = SimpleNamespace(**{**otto.__dict__, "preview": {"label": "Otto"}})
    assert "read-only" in mcp_server.tool_refusal(previewing, "switch_tv_to_xos")

    async def call():
        token = mcp_server.current_caller.set(otto)
        try:
            return await mcp_server.switch_tv_to_xos(TV)
        finally:
            mcp_server.current_caller.reset(token)

    stick("0.5.0")
    with pytest.raises(ValueError, match="0.6.0 or later"):
        asyncio.run(call())
    stick("0.6.0", display_control=False)
    with pytest.raises(ValueError, match="display control"):
        asyncio.run(call())
    stick("0.6.0")
    out = asyncio.run(call())
    assert out["screen"] == TV and out["command"] == "switch_input"
    assert no_steps == [TV]
