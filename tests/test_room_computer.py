"""A Mac as a room's computer (os/mac/xos-room.py): the iPad sends it checked meeting commands."""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, hub

client = TestClient(app)
PC = "mac-atrium-test"


@pytest.fixture(autouse=True)
def room_computer():
    hub.devices.clear()
    hub.commands.clear()
    client.post(
        "/api/devices/heartbeat",
        json={
            "id": PC,
            "client": "macroom-0.1.0",
            "role": "roompc",
            "arg": "atrium",
            "native": {"meeting": {"in_call": False}, "volume": 40},
        },
    )


def meeting(**args):
    return client.post(f"/api/devices/{PC}/commands", json={"type": "meeting", "args": args})


def test_a_meet_code_becomes_its_meet_link():
    r = meeting(action="join", meeting="ods-feaa-fxd")
    assert r.status_code == 200
    assert r.json()["args"] == {"action": "join", "url": "https://meet.google.com/ods-feaa-fxd"}
    link = meeting(action="join", meeting="https://meet.google.com/eym-bkhr-gud?authuser=0").json()
    assert link["args"]["url"] == "https://meet.google.com/eym-bkhr-gud"


def test_a_zoom_id_or_link_opens_zoom():
    assert (
        meeting(action="join", meeting="123 456 7890").json()["args"]["url"]
        == "https://zoom.us/j/1234567890"
    )
    out = meeting(action="join", meeting="https://example.zoom.us/j/98765432101?pwd=abc").json()
    assert out["args"]["url"] == "https://example.zoom.us/j/98765432101?pwd=abc"


def test_only_meetings_and_known_actions_get_through():
    assert meeting(action="join", meeting="https://evil.example.com/x").status_code == 400
    assert meeting(action="join", meeting="").status_code == 400
    assert meeting(action="rm -rf").status_code == 400
    # extra arguments are dropped: the computer only ever sees the action (and a checked URL)
    assert meeting(action="mute", url="https://evil.example.com").json()["args"] == {
        "action": "mute"
    }
    assert meeting(action="volume", volume=180).json()["args"] == {
        "action": "volume",
        "volume": 100,
    }


def test_a_room_computer_is_not_a_screen_and_needs_a_signed_in_person_or_a_remote(monkeypatch):
    from app.config import settings

    assert hub.devices[PC]["role"] == "roompc"
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    assert meeting(action="mute").status_code == 401


def test_the_mac_agent_is_served():
    r = client.get("/os/mac/xos-room.py")
    assert r.status_code == 200 and "Xos Room" in r.text


@pytest.fixture
def mac_agent():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "xos_room_test", Path(__file__).parents[1] / "os/mac/xos-room.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mac_toggle_confirms_state_and_is_idempotent(mac_agent):
    meet = mac_agent.SimulatedMeet()
    meet.open("https://meet.google.com/test-meet-code")
    meet.press_join()
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    assert room.meeting({"action": "mute"}) == "microphone off"
    assert meet.state()["muted"] is True
    assert room.meeting({"action": "mute"}) == "microphone already off"
    assert room.meeting({"action": "unmute"}) == "microphone on"
    assert room.meeting({"action": "camera_off"}) == "camera off"
    assert meet.state()["camera_off"] is True
    assert room.meeting({"action": "camera_on"}) == "camera on"


def test_mac_does_not_ack_success_when_meet_ignores_the_toggle(mac_agent, monkeypatch):
    class Ignored(mac_agent.SimulatedMeet):
        toggles = 0

        def toggle(self, which):
            self.toggles += 1
            return "sent"

    meet = Ignored()
    meet.open("https://meet.google.com/test-meet-code")
    meet.press_join()
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    acknowledgements = []
    monkeypatch.setattr(room, "ack", lambda *args: acknowledgements.append(args))
    clock = iter([0, 1, 6])
    monkeypatch.setattr(mac_agent.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(mac_agent.time, "sleep", lambda _: None)
    room.run({"id": "ignored", "type": "meeting", "args": {"action": "mute"}})
    assert meet.toggles == 1  # never undo a potentially delayed toggle by retrying it
    assert acknowledgements == [
        ("ignored", False, "The meeting didn't confirm microphone off; check the room browser")
    ]
    assert meet.state()["muted"] is False


def test_dedicated_browser_sends_trusted_input_to_the_selected_meet_tab(mac_agent, monkeypatch):
    meet = mac_agent.DevToolsMeet(9223)
    calls = []
    monkeypatch.setattr(meet, "_tab", lambda: {"id": "room-meet"})
    monkeypatch.setattr(meet, "js", lambda code: '{"x":120,"y":250}')
    monkeypatch.setattr(meet, "_call", lambda *args: calls.append(args) or {})
    assert meet.toggle("microphone") == "sent"
    assert [args[1] for args in calls] == [
        "Page.bringToFront",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert calls[1][2]["type"] == "mousePressed"
    assert calls[2][2]["type"] == "mouseReleased"
    assert all(args[0]["id"] == "room-meet" for args in calls)


def test_dedicated_browser_rejects_non_local_debugging_urls(mac_agent):
    meet = mac_agent.DevToolsMeet(9223)
    with pytest.raises(RuntimeError, match="non-local"):
        meet._call(
            {"webSocketDebuggerUrl": "ws://evil.example:9223/devtools/page/x"}, "Runtime.evaluate"
        )


def test_dedicated_browser_keeps_other_tabs_out_of_meet_control(mac_agent, monkeypatch):
    meet = mac_agent.DevToolsMeet(9223)
    monkeypatch.setattr(
        meet,
        "_http",
        lambda path: [
            {"id": "other", "type": "page", "url": "https://example.com/"},
            {"id": "room", "type": "page", "url": "https://meet.google.com/abc-defg-hij"},
        ],
    )
    assert meet._tab()["id"] == "room"


def test_existing_waiting_room_is_reported_without_rejoining(mac_agent):
    class Waiting(mac_agent.SimulatedMeet):
        def state(self):
            return {"in_call": False, "waiting": True}

        def press_join(self):
            return "in"  # Google also exposes Leave call while awaiting admission

    room = mac_agent.Room("http://unused", "atrium", Waiting(), "test-mac")
    assert room._wait_in_call(1) == "asked to join; waiting for the host to let the room in"


@pytest.mark.parametrize("action,device", [("unmute", "microphone"), ("camera_on", "camera")])
def test_missing_media_controls_report_permissions_instead_of_not_in_meeting(
    mac_agent, action, device
):
    meet = mac_agent.SimulatedMeet()
    meet.s = {"in_call": True, "muted": None, "camera_off": None}
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    with pytest.raises(
        RuntimeError,
        match=f"Meeting {device} unavailable; check room browser permissions and devices",
    ):
        room.meeting({"action": action})


def test_atrium_guest_name_defaults_before_join_and_can_be_overridden(mac_agent):
    class Named(mac_agent.SimulatedMeet):
        prepared = []

        def prepare_guest_name(self):
            self.prepared.append(self.display_name)

        def press_join(self):
            assert self.prepared[-1] == self.display_name
            return super().press_join()

    meet = Named()
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    room.meeting({"action": "join", "url": "https://meet.google.com/abc-defg-hij"})
    assert meet.prepared[-1] == "Atrium Room"
    custom = Named()
    mac_agent.Room("http://unused", "atrium", custom, "test-mac", "Atrium Conference Room")
    assert custom.display_name == "Atrium Conference Room"


def test_dedicated_profile_fills_room_name_with_trusted_input_and_readback(mac_agent, monkeypatch):
    meet = mac_agent.DevToolsMeet(9223)
    meet.display_name = "Atrium Room"
    readings = iter(['{"x":100,"y":200,"value":"Atrium"}', '{"value":"Atrium Room"}'])
    monkeypatch.setattr(meet, "js", lambda _: next(readings))
    monkeypatch.setattr(meet, "_tab", lambda: {"id": "room-tab"})
    calls = []
    monkeypatch.setattr(meet, "_call", lambda *args: calls.append(args) or {})
    meet.prepare_guest_name()
    assert calls[-1] == ({"id": "room-tab"}, "Input.insertText", {"text": "Atrium Room"})
    assert all(call[0]["id"] == "room-tab" for call in calls)


def test_guest_name_does_not_touch_a_signed_in_lobby_or_already_correct_name(
    mac_agent, monkeypatch
):
    meet = mac_agent.DevToolsMeet(9223)
    meet.display_name = "Atrium Room"
    for field in ("missing", '{"x":100,"y":200,"value":"Atrium Room"}'):
        monkeypatch.setattr(meet, "js", lambda _, result=field: result)
        monkeypatch.setattr(meet, "_press", lambda _: pytest.fail("should not change this lobby"))
        meet.prepare_guest_name()


def test_copper_cli_calls_are_explicitly_scoped_to_the_room_tab(mac_agent, monkeypatch):
    import json
    from types import SimpleNamespace

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"content": [{"type": "text", "text": "ok"}]})
        )

    monkeypatch.setattr(mac_agent.subprocess, "run", run)
    meet = mac_agent.CopperMeet("room-tab", "/copper")
    assert meet._tool("browser_click", {"selector": "button"}) == "ok"
    assert calls[0][:4] == ["/copper", "--json", "call", "browser_click"]
    assert json.loads(calls[0][4]) == {"selector": "button", "tab": "room-tab"}


@pytest.mark.parametrize("url", ["https://example.com/", "https://meet.google.com.evil.example/"])
def test_copper_refuses_to_navigate_or_control_an_unrelated_tab(mac_agent, monkeypatch, url):
    meet = mac_agent.CopperMeet("room-tab", "/copper")
    calls = []
    monkeypatch.setattr(meet, "_tool", lambda tool, args: calls.append(tool) or url)
    with pytest.raises(RuntimeError, match="tab changed"):
        meet.open("https://meet.google.com/new")
    assert calls == ["browser_evaluate"]


def test_copper_click_uses_real_browser_input_and_preserves_tab_on_leave(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room-tab", "/copper")
    calls = []
    monkeypatch.setattr(meet, "js", lambda _: "body > button:nth-child(2)")
    monkeypatch.setattr(
        meet,
        "_tool",
        lambda tool, args: calls.append((tool, args)) or "https://meet.google.com/abc-defg-hij",
    )
    assert meet.toggle("microphone") == "sent"
    assert calls[0] == ("browser_click", {"selector": "body > button:nth-child(2)"})
    meet.close()
    assert calls[-1] == ("browser_navigate", {"url": "https://meet.google.com/"})
    assert not any(tool == "browser_close" for tool, _ in calls)


def test_copper_reports_google_sign_in_as_actionable_error(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room-tab", "/copper")
    monkeypatch.setattr(meet, "_tool", lambda *_: "https://accounts.google.com/signin")
    assert "Complete Google sign-in" in meet.state()["error"]


def test_teams_join_and_service_start_are_validated(monkeypatch):
    monkeypatch.setattr(settings, "XOS_INSTANT_MEETINGS", True)
    url = "https://teams.microsoft.com/meet/123456789?p=abc"
    assert meeting(action="join", meeting=url).json()["args"] == {"action": "join", "url": url}
    assert meeting(action="meet_now", provider="zoom").json()["args"] == {
        "action": "meet_now",
        "provider": "zoom",
    }
    assert meeting(action="meet_now", provider="arbitrary").status_code == 400


def test_private_invitation_is_not_in_public_device_list(monkeypatch):
    from app.config import settings

    url = "https://zoom.us/j/123456789?pwd=private"
    hub.devices[PC]["native"]["meeting"] = {"in_call": True, "join_url": url}
    assert "join_url" not in client.get("/api/devices").json()[0]["native"]["meeting"]
    invite = client.get("/api/rooms/atrium/meeting")
    assert invite.status_code == 200
    assert invite.json()["url"].endswith("?pwd=private")
    assert invite.json()["qr"].startswith("data:image/svg+xml;base64,")
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    assert client.get("/api/rooms/atrium/meeting").status_code in (401, 403)


def test_share_start_never_opens_a_picker_on_the_room_computer(mac_agent):
    class Sharing(mac_agent.SimulatedMeet):
        def click(self, pattern):
            raise AssertionError("Share must not click anything on the TV")

    meet = Sharing()
    meet.s = {"in_call": True, "sharing": False}
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    with pytest.raises(RuntimeError, match="scan the meeting QR code"):
        room.meeting({"action": "share_start"})
    assert meet.state()["sharing"] is False
    meet.s["in_call"] = False
    with pytest.raises(RuntimeError, match="Join a meeting"):
        room.meeting({"action": "share_start"})


def test_copper_zoom_uses_web_client_and_retains_explicit_room_tab(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room-tab", "/copper")
    calls = []
    monkeypatch.setattr(meet, "_require_meet", lambda: None)
    monkeypatch.setattr(meet, "_tool", lambda tool, args: calls.append((tool, args)))
    meet.open("https://company.zoom.us/j/123456789?pwd=abc")
    assert calls == [
        ("browser_navigate", {"url": "https://company.zoom.us/wc/join/123456789?pwd=abc"})
    ]


def test_start_does_not_replace_a_current_call(mac_agent):
    meet = mac_agent.SimulatedMeet()
    meet.s = {"in_call": True}
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    with pytest.raises(RuntimeError, match="Leave the current meeting"):
        room.meeting({"action": "meet_now", "provider": "zoom"})
    assert meet.state()["in_call"] is True


def test_zoom_controls_activate_once_with_copper_keyboard(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room", "/copper")
    meet.provider = "zoom"
    calls = []
    monkeypatch.setattr(meet, "js", lambda _: "body > button:nth-child(1)")
    monkeypatch.setattr(meet, "_tool", lambda tool, args: calls.append((tool, args)) or "focused")
    assert meet.toggle("microphone") == "sent"
    assert [tool for tool, _ in calls] == ["browser_evaluate", "browser_press_key"]
    assert calls[-1][1] == {"key": "Enter"}
    calls.clear()
    meet._button("^new meeting$", join=True, keyboard=False)
    assert [tool for tool, _ in calls] == ["browser_click"]


def test_copper_adopts_only_the_tab_created_by_start(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room-home", "/copper")
    before = {"personal-call": "https://app.zoom.us/wc/111111111/start"}
    after = {**before, "new-room-call": "https://app.zoom.us/wc/222222222/start?from=pwa"}
    monkeypatch.setattr(meet, "_meeting_tabs", lambda provider: after)
    meet._follow_opened_meeting("zoom", before)
    assert meet.tab == "new-room-call"
    assert meet.home_tab == "room-home"
    after["another-new"] = "https://app.zoom.us/wc/333333333/start"
    meet.tab = meet.home_tab
    with pytest.raises(RuntimeError, match="Multiple meeting tabs"):
        meet._follow_opened_meeting("zoom", before)
    assert meet.tab == "room-home"


def test_copper_returns_to_home_and_closes_only_its_created_tab(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room-home", "/copper")
    meet.tab = "created-room-call"
    meet.provider = "zoom"
    calls = []
    monkeypatch.setattr(meet, "_require_meet", lambda: None)
    monkeypatch.setattr(meet, "_tool", lambda tool, args: calls.append((meet.tab, tool, args)))
    meet.close()
    assert calls == [
        ("created-room-call", "browser_close", {}),
        ("room-home", "browser_navigate", {"url": "https://app.zoom.us/wc/"}),
    ]


def test_only_recently_opened_owned_tab_allows_blank_loading_state(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room", "/copper")
    monkeypatch.setattr(meet, "_tool", lambda *args: "about:blank")
    monkeypatch.setattr(mac_agent.time, "monotonic", lambda: 10)
    meet.navigation_until = 20
    with pytest.raises(mac_agent.BrowserLoading):
        meet._require_meet()
    meet.navigation_until = 0
    with pytest.raises(RuntimeError, match="tab changed"):
        meet._require_meet()


def test_zoom_host_conflict_is_reported_without_ending_another_meeting(mac_agent):
    class Blocked(mac_agent.SimulatedMeet):
        def state(self):
            return {"in_call": False, "needs_end_other": True}

    room = mac_agent.Room("http://unused", "atrium", Blocked(), "test-mac")
    with pytest.raises(RuntimeError, match="already hosts another meeting"):
        room._wait_in_call(1)


def test_zoom_popup_loading_redirect_is_observed_before_adoption(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room-home", "/copper")
    pages = iter(
        [
            {"new-room": "https://app.zoom.us/wc/loading"},
            {"new-room": "https://app.zoom.us/wc/123456789/start?from=pwa"},
        ]
    )
    monkeypatch.setattr(meet, "_meeting_tabs", lambda provider: next(pages))
    monkeypatch.setattr(mac_agent.time, "sleep", lambda _: None)
    meet._follow_opened_meeting("zoom", {})
    assert meet.tab == "new-room"


def test_missing_optional_invitation_does_not_fail_an_admitted_call(mac_agent):
    class Joined(mac_agent.SimulatedMeet):
        def capture_invitation(self):
            raise RuntimeError("Information popup unavailable")

    meet = Joined()
    meet.s = {"in_call": True}
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    assert room._wait_in_call(1) == "in the meeting"


def test_teams_cannot_start_before_room_setup_is_enabled(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room-home", "/copper")
    monkeypatch.setattr(meet, "state", lambda: {"in_call": False})
    monkeypatch.setattr(meet, "start", lambda _: pytest.fail("Teams must stay disabled"))
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    with pytest.raises(RuntimeError, match="Teams setup is pending"):
        room.meeting({"action": "meet_now", "provider": "teams"})


def test_zoom_name_waits_for_controls_without_repeating_activation(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room", "/copper")
    results = iter(["missing", "missing", "clicked"])
    calls = []
    monkeypatch.setattr(
        meet, "_button", lambda pattern, **kwargs: calls.append(pattern) or next(results)
    )
    monkeypatch.setattr(mac_agent.time, "sleep", lambda _: None)
    assert meet._wait_button("^rename$", self_only=True) == "clicked"
    assert len(calls) == 3


def test_zoom_leave_reports_host_handoff_without_assigning_anyone(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room", "/copper")
    meet.provider = "zoom"
    choices = []
    monkeypatch.setattr(
        meet, "_button", lambda pattern, **kwargs: choices.append(pattern) or "clicked"
    )
    monkeypatch.setattr(meet, "state", lambda: {"in_call": True, "needs_host": True})
    with pytest.raises(RuntimeError, match="Zoom needs a new host"):
        meet.leave()
    assert choices == ["^(end|end meeting)$", "^leave meeting$"]
    assert not any("assign" in choice for choice in choices)


def test_zoom_end_uses_the_explicit_end_for_everyone_choice(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room", "/copper")
    meet.provider = "zoom"
    choices = []
    monkeypatch.setattr(
        meet, "_button", lambda pattern, **kwargs: choices.append(pattern) or "clicked"
    )
    monkeypatch.setattr(meet, "state", lambda: {"in_call": False})
    meet.leave(end=True)
    assert choices == ["^(end|end meeting)$", "^end meeting for all$"]


@pytest.mark.parametrize(
    "state, says",
    [
        ({"signin": True}, "guest-access invitation"),
        ({"missing": True}, "can't find that meeting"),
        ({"denied": True}, "host has started the meeting"),
    ],
)
def test_a_join_that_cant_happen_says_why_and_returns_to_the_room_screen(mac_agent, state, says):
    """Describe the observed provider response without assuming an organization policy."""

    class Refused(mac_agent.SimulatedMeet):
        closed = 0

        def state(self):
            return {"in_call": False, **state}

        def close(self):
            Refused.closed += 1

    meet = Refused()
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    with pytest.raises(RuntimeError, match=says):
        room.meeting({"action": "join", "url": "https://meet.google.com/abc-defg-hij"})
    assert Refused.closed == 1  # not left on the service's error page


def test_controller_invitation_uses_its_selected_online_room_computer():
    first = "https://meet.google.com/aaa-bbbb-ccc"
    second = "https://meet.google.com/ddd-eeee-fff"
    hub.devices[PC]["native"]["meeting"] = {"in_call": True, "join_url": first}
    client.post(
        "/api/devices/heartbeat",
        json={
            "id": "second-room-pc",
            "role": "roompc",
            "arg": "atrium",
            "native": {"meeting": {"in_call": True, "join_url": second}},
        },
    )
    assert client.get("/api/rooms/atrium/meeting?device=second-room-pc").json()["url"] == second
    assert client.get("/api/rooms/other-room/meeting?device=second-room-pc").json()["url"] == ""
    hub.devices["second-room-pc"]["last_seen"] = 0
    assert client.get("/api/rooms/atrium/meeting?device=second-room-pc").json()["url"] == ""


def test_controller_qr_accepts_google_current_live_meet_path():
    hub.devices[PC]["native"]["meeting"] = {
        "in_call": True,
        "join_url": "https://meet.google.com/meet/abc-defg-hij?authuser=1",
    }
    response = client.get(f"/api/rooms/atrium/meeting?device={PC}")
    assert response.status_code == 200
    assert response.json()["url"] == "https://meet.google.com/abc-defg-hij"
    assert response.json()["qr"].startswith("data:image/svg+xml;base64,")


def test_provider_start_failure_restores_idle_screen(mac_agent):
    class FailedStart(mac_agent.SimulatedMeet):
        closed = False

        def start(self, provider):
            raise RuntimeError("Provider did not open the room tab")

        def close(self):
            self.closed = True

    meet = FailedStart()
    meet.s = {"in_call": False}
    room = mac_agent.Room("http://unused", "atrium", meet, "test-pc")
    with pytest.raises(RuntimeError, match="did not open"):
        room.meeting({"action": "meet_now", "provider": "meet"})
    assert meet.closed


def test_zoom_start_reports_host_conflict_without_ending_other_meeting(mac_agent, monkeypatch):
    meeting = mac_agent.CopperMeet("room-home", "/copper")
    monkeypatch.setattr(
        meeting, "_meeting_tabs", lambda provider: {"room-home": "https://app.zoom.us/wc/"}
    )
    monkeypatch.setattr(meeting, "state", lambda: {"in_call": False, "needs_end_other": True})
    with pytest.raises(RuntimeError, match="already hosts another meeting"):
        meeting._follow_opened_meeting("zoom", {"room-home": "https://app.zoom.us/wc/"})


def test_provider_controls_are_private_current_and_uncached(monkeypatch):
    import time

    from app.config import settings

    controls = [
        {"id": "abcdefab-1234-1234-1234-abcdefabcdef", "label": "Admit", "context": "Guest"}
    ]
    hub.devices[PC]["native"]["meeting"] = {
        "in_call": True,
        "controls": controls,
        "control_labels": ["Show everyone"],
        "control_diagnostics": [{"label": "1"}],
        "control_back": True,
    }
    assert "controls" not in client.get("/api/devices").json()[0]["native"]["meeting"]
    response = client.get(f"/api/devices/{PC}/meeting-controls")
    assert response.status_code == 200 and response.json()["controls"] == controls
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["labels"] == ["Show everyone"]
    assert response.json()["back"] is True
    assert "control_diagnostics" not in client.get("/api/devices").json()[0]["native"]["meeting"]
    assert "control_labels" not in client.get("/api/devices").json()[0]["native"]["meeting"]
    hub.devices[PC]["native"]["meeting"]["in_call"] = False
    assert client.get(f"/api/devices/{PC}/meeting-controls").json()["back"] is False
    assert client.get(f"/api/devices/{PC}/meeting-controls").json()["controls"] == []
    hub.devices[PC]["native"]["meeting"]["in_call"] = True
    hub.devices[PC]["last_seen"] = time.time() - 1000
    assert client.get(f"/api/devices/{PC}/meeting-controls").json()["controls"] == []
    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    assert client.get(f"/api/devices/{PC}/meeting-controls").status_code in (401, 403)


def test_browser_rooms_join_as_guests_and_cannot_create_calls(monkeypatch):
    monkeypatch.setattr(settings, "XOS_INSTANT_MEETINGS", False)
    assert meeting(action="meet_now", provider="meet").status_code == 409
    assert meeting(action="meet_now", provider="zoom").status_code == 409
    assert meeting(action="join", meeting="abc-defg-hij").status_code == 200
    assert client.get("/api/rooms/atrium/zoom").json()["instant_meetings"] is False


def test_agent_command_path_obeys_guest_policy_too(monkeypatch):
    from fastapi import HTTPException

    from app.main import command_device

    monkeypatch.setattr(settings, "XOS_INSTANT_MEETINGS", False)
    with pytest.raises(HTTPException) as error:
        command_device(PC, "meeting", {"action": "meet_now", "provider": "zoom"})
    assert error.value.status_code == 409
    monkeypatch.setattr(settings, "XOS_INSTANT_MEETINGS", True)
    assert command_device(PC, "meeting", {"action": "meet_now"})["args"]["action"] == "meet_now"


def test_guest_verification_stays_visible_for_manual_completion(mac_agent):
    class Verification(mac_agent.SimulatedMeet):
        closed = False

        def state(self):
            return {"in_call": False, "verification": True}

        def close(self):
            self.closed = True

    meet = Verification()
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    with pytest.raises(mac_agent.GuestVerificationRequired, match="human verification"):
        room.meeting({"action": "join", "url": "https://meet.google.com/abc-defg-hij"})
    assert meet.closed is False


def test_zoom_connects_computer_audio_before_reporting_joined(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room", "/copper")
    meet.provider = "zoom"
    states = iter([{"in_call": True, "muted": None}, {"in_call": True, "muted": True}])
    monkeypatch.setattr(meet, "state", lambda: next(states))
    clicked = []
    monkeypatch.setattr(meet, "_button", lambda pattern: clicked.append(pattern) or "clicked")
    monkeypatch.setattr(mac_agent.time, "sleep", lambda _: None)
    meet.connect_audio()
    assert clicked == [r"^join (?:audio by computer|with computer audio)$"]


def test_zoom_does_not_report_joined_when_computer_audio_never_connects(mac_agent, monkeypatch):
    meet = mac_agent.CopperMeet("room", "/copper")
    meet.provider = "zoom"
    monkeypatch.setattr(meet, "state", lambda: {"in_call": True, "muted": None})
    monkeypatch.setattr(meet, "_button", lambda pattern: "missing")
    ticks = iter([0, 1, 13])
    monkeypatch.setattr(mac_agent.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(mac_agent.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="computer audio did not connect"):
        meet.connect_audio()


def test_admitted_guest_connects_audio_once_on_the_next_heartbeat(mac_agent):
    class Admitted(mac_agent.SimulatedMeet):
        connections = 0

        def connect_audio(self):
            self.connections += 1
            self.s["muted"] = True

    meet = Admitted()
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    room.audio_pending = True
    meet.s = {"in_call": False, "waiting": True}
    assert room.meeting_state()["waiting"] is True
    assert meet.connections == 0
    meet.s = {"in_call": True, "waiting": False}
    assert room.meeting_state()["muted"] is True
    assert room.audio_pending is False
    room.meeting_state()
    assert meet.connections == 1


def test_host_ended_call_returns_home_once(mac_agent):
    meet = mac_agent.SimulatedMeet()
    meet.s = {"in_call": False, "ended": True}
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    assert room.meeting_state()["ended"] is True
    assert not room.meeting_state().get("ended")


def test_explicit_leave_reports_one_actual_end(mac_agent):
    meet = mac_agent.SimulatedMeet()
    meet.open("https://meet.google.com/abc-defg-hij")
    meet.press_join()
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    room.meeting({"action": "leave"})
    assert room.meeting_state()["ended"] is True
    assert not room.meeting_state().get("ended")


def test_leave_from_a_lobby_closes_the_page_without_waiting_for_a_call_to_end(mac_agent):
    class Lobby(mac_agent.SimulatedMeet):
        def leave(self, end=False):
            raise AssertionError("a lobby has no call to leave")

        def close(self):
            self.s = {"in_call": False}

    meet = Lobby()
    meet.s = {"in_call": False, "waiting": True, "provider": "teams"}
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    assert room.meeting({"action": "leave"}) == "left the meeting"
    assert not room.meeting_state().get("ended")  # nothing was joined, so nothing ended


def test_an_unconfirmed_leave_still_returns_the_room_but_a_host_handoff_does_not(mac_agent):
    class Stubborn(mac_agent.SimulatedMeet):
        def leave(self, end=False):
            raise RuntimeError("The meeting has not confirmed leaving; check the room browser")

        def close(self):
            self.s = {"in_call": False}

    meet = Stubborn()
    meet.s = {"in_call": True, "provider": "teams"}
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    assert room.meeting({"action": "leave"}) == "left the meeting"
    meet.s = {"in_call": True, "provider": "zoom", "needs_host": True}
    with pytest.raises(RuntimeError):
        room.meeting({"action": "leave"})
    meet.s = {"in_call": True, "provider": "zoom"}
    with pytest.raises(RuntimeError):
        room.meeting({"action": "end"})


def test_waiting_and_transient_controls_do_not_close_a_call(mac_agent):
    class Pending(mac_agent.SimulatedMeet):
        closed = False

        def close(self):
            self.closed = True

    meet = Pending()
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    for state in [{"in_call": False}, {"waiting": True, "in_call": False, "ended": True}]:
        meet.s = state
        room.meeting_state()
        assert meet.closed is False


def test_return_home_retries_without_stopping_heartbeats(mac_agent):
    class Closing(mac_agent.SimulatedMeet):
        attempts = 0

        def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("browser is still closing")
            super().close()

    meet = Closing()
    meet.s = {"in_call": False, "ended": True}
    room = mac_agent.Room("http://unused", "atrium", meet, "test-mac")
    assert "Returning to the room screen" in room.meeting_state()["error"]
    assert room.meeting_state()["ended"] is True
    assert not room.meeting_state().get("ended")
    assert meet.attempts == 2
