"""The Xos Linux appliance (os/appliance/): updates with rollback, the DevTools pipe, identity.

Hardware-free: no Chromium, systemd or audio here. os/appliance/test/run.sh drives real Chromium
in a Debian 13 container; the physical checklist is in README.md.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import tarfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


upd = load("xos_update", ROOT / "os/appliance/xos_update.py")
room = load("xos_room_linux", ROOT / "os/appliance/xos_room_linux.py")


def bundle(files: dict[str, str]) -> tuple[bytes, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


class Server:
    """The Xos server's /os/xos-node.* as the updater sees them."""

    def __init__(self, data: bytes, digest: str):
        self.data, self.digest = data, digest

    def get(self, url: str, timeout: float = 0) -> bytes:
        return self.digest.encode() + b"\n" if url.endswith(".sha256") else self.data


@pytest.fixture
def box(tmp_path):
    root, state = tmp_path / "opt", tmp_path / "state"
    (root / "releases" / "old1").mkdir(parents=True)
    upd.switch(root, "old1")
    return root, state


def run(box, server, healthy=True, busy=False):
    root, state = box
    restarts = []
    out = upd.update(
        "https://xos",
        "room",
        root,
        state,
        get=server.get,
        restart_fn=lambda role: restarts.append(upd.current(root)),
        healthy_fn=lambda role, name, since: healthy,
        busy_fn=lambda: busy,
        wait=0,
    )
    return out, restarts


def test_a_healthy_release_takes_over(box):
    data, digest = bundle({"os/appliance/x.py": "print(1)"})
    out, restarts = run(box, Server(data, digest))
    assert out == f"updated to {digest[:12]}"
    assert upd.current(box[0]) == digest and restarts == [digest]
    assert (box[0] / "releases" / digest / "os/appliance/x.py").read_text() == "print(1)"
    assert run(box, Server(data, digest))[0] == "up to date"


def test_an_unhealthy_release_rolls_back_and_is_not_tried_again(box):
    data, digest = bundle({"os/a": "broken"})
    out, restarts = run(box, Server(data, digest), healthy=False)
    assert out.startswith("rolled back to old1")
    assert upd.current(box[0]) == "old1" and restarts == [digest, "old1"]
    assert run(box, Server(data, digest))[0].startswith("skipped: ")


def test_a_corrupt_download_changes_nothing(box):
    data, digest = bundle({"os/a": "x"})
    with pytest.raises(upd.UpdateError):
        run(box, Server(data[:-5] + b"xxxxx", digest))
    assert upd.current(box[0]) == "old1"
    assert not any(p.name.startswith(digest[:8]) for p in (box[0] / "releases").iterdir())


def test_a_room_in_a_meeting_is_not_updated(box):
    data, digest = bundle({"os/a": "x"})
    assert run(box, Server(data, digest), busy=True)[0] == "skipped: the room is in a meeting"
    assert upd.current(box[0]) == "old1"


def test_a_bundle_cannot_write_outside_its_release(box):
    data, digest = bundle({"../../evil": "x"})
    with pytest.raises(tarfile.TarError):
        run(box, Server(data, digest))
    assert not (box[0].parent / "evil").exists() and upd.current(box[0]) == "old1"


def test_old_releases_are_pruned(box):
    root, _ = box
    for i in range(5):
        (root / "releases" / f"r{i}").mkdir()
        os.utime(root / "releases" / f"r{i}", (i, i))
    upd.switch(root, "r4")
    upd.prune(root, keep=3)
    assert sorted(p.name for p in (root / "releases").iterdir()) == ["old1", "r3", "r4"]


def test_health_needs_the_new_release_with_its_browser(tmp_path):
    h = tmp_path / "health.json"
    since = time.time()
    h.write_text(json.dumps({"at": since + 1, "release": "abc123def456", "browser": "running"}))
    assert upd.healthy("room", "abc123def456" + "0" * 52, since, h)
    h.write_text(json.dumps({"at": since + 1, "release": "older1234567", "browser": "running"}))
    assert not upd.healthy("room", "abc123def456" + "0" * 52, since, h)
    h.write_text(json.dumps({"at": since + 1, "release": "abc123def456", "browser": "restarting"}))
    assert not upd.healthy("room", "abc123def456" + "0" * 52, since, h)


# ---------- the DevTools pipe ----------


def fake_chromium(rfd: int, wfd: int, replies):
    """Reads NUL-separated commands like Chromium's fd 3 and answers on fd 4."""
    buf = b""
    while True:
        chunk = os.read(rfd, 65536)
        if not chunk:
            os.close(wfd)
            return
        buf += chunk
        while b"\0" in buf:
            raw, buf = buf.split(b"\0", 1)
            msg = json.loads(raw)
            os.write(wfd, b'{"method":"Page.frameNavigated","params":{}}\0')  # events are ignored
            os.write(wfd, json.dumps({"id": msg["id"], **replies(msg)}).encode() + b"\0")


def cdp_pair(replies):
    to_chromium_r, to_chromium_w = os.pipe()
    from_chromium_r, from_chromium_w = os.pipe()
    threading.Thread(
        target=fake_chromium, args=(to_chromium_r, from_chromium_w, replies), daemon=True
    ).start()
    return room.Cdp(to_chromium_w, from_chromium_r)


def test_cdp_matches_replies_and_passes_sessions():
    def replies(msg):
        if msg["method"] == "Runtime.evaluate":
            assert msg["sessionId"] == "S1"
            return {"result": {"result": {"value": msg["params"]["expression"].upper()}}}
        if msg["method"] == "Bad.method":
            return {"error": {"message": "'Bad.method' wasn't found"}}
        return {"result": {}}

    cdp = cdp_pair(replies)
    assert (
        cdp.call("Runtime.evaluate", {"expression": "ok"}, session="S1")["result"]["value"] == "OK"
    )
    with pytest.raises(room.CdpError, match="wasn't found"):
        cdp.call("Bad.method")
    cdp.close()
    time.sleep(0.2)
    with pytest.raises(room.CdpError):
        cdp.call("Runtime.evaluate", {"expression": "x"}, timeout=1)


# ---------- the room computer ----------


class FakeBrowser:
    def root_viewport(self, target):
        return {"width": 1280, "height": 720, "pixelRatio": 1}

    def fullscreen(self, target):
        return True

    def __init__(self, url="https://xos/app/#/tv/atrium", alive=True):
        self.u, self.up, self.last_error, self.went = url, alive, "", []
        self.home, self.closed = "HOME", []
        self.extra: list[dict] = []  # pages a meeting app opened
        self.granted: list[str] = []  # origins given camera and microphone

    def grant_media(self, origin):
        self.granted.append(origin)

    def alive(self):
        return self.up

    def pages(self):
        return [{"targetId": "HOME", "url": self.u, "title": "Room"}, *self.extra]

    def url(self, target=""):
        return next((p["url"] for p in self.extra if p["targetId"] == target), self.u)

    def evaluate(self, js, target=""):
        if js == "location.href" or js.startswith("(() => location.href)"):
            return self.url(target)
        return json.dumps({"in_call": True, "muted": False})

    def navigate(self, url, target=""):
        self.went.append(url)

    def forget_history(self, target=""):
        self.went.append("(history cleared)")

    def activate(self, target):
        self.activated = target

    def close_page(self, target):
        self.closed.append(target)
        self.extra = [p for p in self.extra if p["targetId"] != target]


def test_meeting_state_only_from_a_meet_page():
    idle = room.ChromiumMeet(FakeBrowser(), "https://xos/app/#/tv/atrium")
    assert idle.state() == {"in_call": False}
    meet = room.ChromiumMeet(FakeBrowser("https://meet.google.com/new-ojef-jha"), "idle")
    assert meet.state()["in_call"] is True
    down = room.ChromiumMeet(FakeBrowser(alive=False), "idle")
    assert down.state()["in_call"] is False and "restarting" in down.state()["error"]
    b = FakeBrowser("https://meet.google.com/x")
    room.ChromiumMeet(b, "https://xos/idle").close()
    assert b.went == ["https://xos/idle"]  # leaving goes back to the room screen


def test_the_room_computer_runs_meet_zoom_teams_and_webex():
    m = room.ChromiumMeet(FakeBrowser(), "https://xos/idle")
    assert set(m.providers) == {"meet", "zoom", "teams", "webex"}
    m.open("https://zoom.us/j/1234567890?pwd=abc")  # the web client, never the Zoom app
    # Then only the meeting page is in the kiosk window's history: a real Webex page goes back.
    assert m.b.went[-2:] == ["https://zoom.us/wc/join/1234567890?pwd=abc", "(history cleared)"]
    assert m.provider == "zoom"
    m.open("https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0")
    assert m.provider == "teams" and m.tab == "HOME"
    m.open("https://acme.webex.com/acme/j.php?MTID=m3f1a2b")
    assert (
        m.provider == "webex" and m.b.went[-2] == "https://acme.webex.com/acme/j.php?MTID=m3f1a2b"
    )
    assert m.b.granted == ["https://acme.webex.com"]  # each Webex site is its own origin
    with pytest.raises(RuntimeError, match="Only Meet, Teams, Webex and Zoom"):
        m.open("https://example.com/j/1234567890")


def test_a_meeting_window_is_followed_and_closed_on_leave():
    b = FakeBrowser("https://app.zoom.us/wc/home")
    m = room.ChromiumMeet(b, "https://xos/idle")
    b.extra = [
        {"targetId": "Z1", "url": "https://app.zoom.us/wc/1234567890/start", "title": "Zoom"}
    ]
    # Copper's tab listing, which the shared Zoom logic reads to find the window it opened
    assert m._meeting_tabs("zoom") == {
        "HOME": "https://app.zoom.us/wc/home",
        "Z1": "https://app.zoom.us/wc/1234567890/start",
    }
    m.tab = "Z1"
    assert m.state()["in_call"] is True
    m.close()
    assert b.closed == ["Z1"] and m.tab == "HOME" and b.went == ["https://xos/idle"]
    m.tab = "GONE"  # a window that closed by itself: back to the room screen
    assert m._target() == "HOME"


def test_volume_reads_sets_and_checks_the_default_sink(monkeypatch):
    calls, level, sticks = [], [0.60], [True]

    class R:
        def __init__(self, out="", rc=0):
            self.stdout, self.stderr, self.returncode = out, "", rc

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "get-volume":
            return R(f"Volume: {level[0]:.2f}\n")
        if sticks[0]:
            level[0] = int(cmd[3].rstrip("%")) / 100
        return R()

    monkeypatch.setattr(room.subprocess, "run", fake_run)
    m = room.ChromiumMeet(FakeBrowser(), "idle")
    assert m.volume() == 60
    m.set_volume(35)
    assert ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "35%"] in calls and m.volume() == 35
    sticks[0] = False  # a speaker that ignores it: say so, don't tick
    with pytest.raises(RuntimeError, match="didn't take the volume"):
        m.set_volume(80)


def test_identity_is_made_once_and_private(tmp_path):
    first = room.identity(tmp_path)
    assert first.startswith("xos-") and room.identity(tmp_path) == first
    assert stat.S_IMODE((tmp_path / "identity.json").stat().st_mode) == 0o600


def test_the_browser_never_listens_on_a_port():
    cmd = room.Browser(Path("/tmp/p"), "https://xos/idle").command()
    assert "--remote-debugging-pipe" in cmd
    assert not any("remote-debugging-port" in c or "remote-debugging-address" in c for c in cmd)
    for f in (ROOT / "os").rglob("*"):
        if f.is_file() and f.suffix not in (".md",):
            assert "remote-debugging-port" not in f.read_text(errors="ignore"), f


def test_the_installer_is_served():
    r = TestClient(app).get("/os/appliance/install.sh")
    assert r.status_code == 200 and "--role room" in r.text


def test_reload_and_signin_done_bring_the_room_screen_back(tmp_path):
    b = FakeBrowser("https://myaccount.google.com/")  # where Google leaves it after signing in
    meet = room.ChromiumMeet(b, "https://xos/app/#/tv/atrium")
    r = room.LinuxRoom("https://xos", "atrium", meet, "xos-test", b, tmp_path, tmp_path)
    acks = []
    r.ack = lambda cid, ok, result: acks.append((ok, result))
    r.run({"id": "c1", "type": "refresh"})
    assert (
        acks == [(True, "back on the room screen")] and b.went[-1] == "https://xos/app/#/tv/atrium"
    )
    b.u = "https://meet.google.com/abc-defg-hij"  # in a meeting: Reload never drops it
    b.evaluate = lambda js, target="": (
        b.u if "location.href" in js else json.dumps({"in_call": True, "provider": "meet"})
    )
    r.run({"id": "c2", "type": "refresh"})
    assert acks[-1] == (True, "in a meeting: staying on it") and len(b.went) == 1


def test_room_permissions_are_scoped_and_never_grant_display_capture(tmp_path):
    calls = []

    class Pipe:
        def call(self, method, params):
            calls.append((method, params))

    browser = room.Browser(tmp_path, "https://xos/app/#/tv/atrium")
    browser.cdp = Pipe()
    browser.configure_permissions()
    assert len(calls) == 19  # camera and mic for each meeting origin, Webex's web app included
    for method, params in calls:
        assert method == "Browser.setPermission"
        name = params["permission"]["name"]
        if not params.get("origin"):
            assert params["setting"] == "denied"
        else:
            assert name in ("camera", "microphone")
            assert params["setting"] == "granted"
            assert params["origin"].startswith("https://")
    assert "--disable-notifications" in browser.command()


def test_browser_policy_tracks_release_and_rollback(tmp_path):
    root = tmp_path / "opt"
    managed = tmp_path / "managed"
    for release, allow_capture in (("old", True), ("new", False)):
        policy = root / "releases" / release / "os/appliance/policies/room.json"
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({"ScreenCaptureAllowed": allow_capture}))
    for release, expected in (("old", True), ("new", False), ("old", True)):
        upd.switch(root, release)
        upd.install_policy("room", root, managed)
        assert (
            json.loads((managed / "xos-appliance.json").read_text())["ScreenCaptureAllowed"]
            is expected
        )


def test_failed_start_closes_only_its_new_windows_and_activates_room(monkeypatch):
    browser = FakeBrowser()
    browser.extra = [{"targetId": "EXISTING", "url": "https://accounts.google.com/"}]
    meeting = room.ChromiumMeet(browser, "https://xos/app/#/tv/atrium")

    def fail(self, provider):
        browser.extra.append({"targetId": "NEW", "url": "about:blank"})
        raise RuntimeError("Provider did not open its meeting tab")

    monkeypatch.setattr(room.shared.CopperMeet, "start", fail)
    with pytest.raises(RuntimeError, match="did not open"):
        meeting.start("zoom")
    assert browser.closed == ["NEW"]
    assert browser.activated == "HOME"
    assert browser.extra[0]["targetId"] == "EXISTING"


@pytest.mark.parametrize(
    "urls",
    [
        ["https://evilzoom.us/wc/123456789/start"],
        ["https://zoom.us/wc/123456789/start", "https://zoom.us/wc/987654321/start"],
    ],
)
def test_meeting_frame_rejects_untrusted_or_ambiguous_children(tmp_path, monkeypatch, urls):
    browser = room.Browser(tmp_path, "https://app.zoom.us/wc/")
    browser.home = "HOME"
    tree = {
        "frame": {"id": "HOME", "url": "https://app.zoom.us/wc/"},
        "childFrames": [{"frame": {"id": str(i), "url": url}} for i, url in enumerate(urls)],
    }
    monkeypatch.setattr(browser, "send", lambda method, *args, **kwargs: {"frameTree": tree})
    assert browser.meeting_frame() == ("HOME", None, 0, 0)


def test_room_tv_messages_are_rejected_without_a_browser_popup():
    instance = object.__new__(room.LinuxRoom)
    acks = []
    instance.ack = lambda *args: acks.append(args)
    for kind in ("identify", "message"):
        instance.run({"id": kind, "type": kind, "args": {"text": "Overlay"}})
    assert len(acks) == 2
    assert all(not ok and "popups are disabled" in result for _, ok, result in acks)


def test_room_notice_does_not_evaluate_an_overlay():
    class NoOverlay:
        def evaluate(self, *_):
            pytest.fail("A TV notice must not execute overlay JavaScript")

    instance = object.__new__(room.ChromiumMeet)
    instance.b = NoOverlay()
    instance.notify("Message")


@pytest.mark.parametrize(
    "pixels,scale", [(800, 1), (1280, 1), (1920, 1.5), (2560, 2), (3840, 3), (7680, 3)]
)
def test_room_scale_keeps_controls_readable_without_cropping_small_outputs(pixels, scale):
    assert room.room_ui_scale(pixels) == scale


def test_room_scale_calibrates_once_before_ready_without_counting_a_crash(tmp_path, monkeypatch):
    browser = room.Browser(tmp_path, "https://xos/idle")
    launches = []

    def launch():
        launches.append(browser.ui_scale)
        browser.restarts += 1

    monkeypatch.setattr(browser, "_launch", launch)
    monkeypatch.setattr(
        browser, "send", lambda *a, **k: {"result": {"value": {"width": 3840, "height": 2160}}}
    )
    browser.start()
    assert launches == [None, 3]
    assert browser.restarts == 0
    assert browser.ui_scale == 3
    assert "--force-device-scale-factor=1" in browser.command()
    browser.start()
    assert launches == [None, 3, 3]


def test_zoom_window_is_made_fullscreen_by_its_own_target(tmp_path):
    browser = room.Browser(tmp_path, "https://xos/idle", ui_scale=1.5)
    calls = []
    bounds = {"windowState": "normal"}

    class Protocol:
        def call(self, method, args):
            calls.append((method, args))
            if method == "Browser.getWindowForTarget":
                return {"windowId": 42, "bounds": dict(bounds)}
            if method == "Browser.setWindowBounds":
                bounds.update(args["bounds"])
            return {"bounds": dict(bounds)}

    browser.cdp = Protocol()
    assert browser.fullscreen("ZOOM") is True
    assert calls[0] == ("Browser.getWindowForTarget", {"targetId": "ZOOM"})
    assert calls[1] == (
        "Browser.setWindowBounds",
        {"windowId": 42, "bounds": {"windowState": "fullscreen"}},
    )


def test_room_page_zoom_preserves_other_profile_settings_and_clears_only_provider_overrides(
    tmp_path,
):
    browser = room.Browser(tmp_path, "https://xos/idle", ui_scale=1.5)
    preferences = tmp_path / "Default" / "Preferences"
    preferences.parent.mkdir()
    preferences.write_text(
        json.dumps(
            {
                "profile": {"name": "Room"},
                "partition": {
                    "per_host_zoom_levels": {
                        "x": {"meet.google.com": {"zoom_level": 0}, "other.example": 2}
                    }
                },
            }
        )
    )
    browser.configure_zoom()
    result = json.loads(preferences.read_text())
    assert result["profile"] == {"name": "Room"}
    hosts = result["partition"]["per_host_zoom_levels"]["x"]
    assert hosts["other.example"] == 2
    assert 1.2 ** hosts["meet.google.com"]["zoom_level"] == pytest.approx(1.8)
    assert 1.2 ** result["partition"]["default_zoom_level"]["x"] == pytest.approx(1.5)
    assert stat.S_IMODE(preferences.stat().st_mode) == 0o600


def test_zoom_fits_the_actual_frame_owner_and_releases_the_remote_object(tmp_path, monkeypatch):
    browser = room.Browser(tmp_path, "https://app.zoom.us/wc/")
    browser.home = "HOME"
    calls = []
    tree = {
        "frame": {"id": "HOME", "url": "https://app.zoom.us/wc/"},
        "childFrames": [{"frame": {"id": "ZOOM", "url": "https://zoom.us/wc/123456789/start"}}],
    }

    def send(method, params=None, target=""):
        calls.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": tree}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 42}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "OWNER"}}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [0, 0, 1280, 0, 1280, 720, 0, 720]}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        return {}

    monkeypatch.setattr(browser, "send", send)
    assert browser.meeting_frame() == ("HOME", 7, 0, 0)
    assert ("Runtime.releaseObject", {"objectId": "OWNER"}) in calls
    fit = next(params for method, params in calls if method == "Runtime.callFunctionOn")
    assert fit["objectId"] == "OWNER"
    assert calls.index(("DOM.resolveNode", {"backendNodeId": 42})) < next(
        i for i, (method, _) in enumerate(calls) if method == "DOM.getBoxModel"
    )


def test_advanced_control_arguments_reject_selectors_and_normalize_tokens():
    from fastapi import HTTPException

    from app.main import _meeting_args

    token = "abcdefab-1234-1234-1234-abcdefabcdef"
    assert _meeting_args({"action": "control", "control": token, "selector": "button"}) == {
        "action": "control",
        "control": token,
    }
    for value in (None, [], "button", "a" * 36):
        with pytest.raises(HTTPException):
            _meeting_args({"action": "control", "control": value})


def test_meeting_back_requires_a_call_and_uses_only_escape():
    meeting = object.__new__(room.ChromiumMeet)
    meeting.state = lambda: {"in_call": False}
    calls = []
    meeting._tool = lambda tool, args: calls.append((tool, args))
    with pytest.raises(RuntimeError, match="Join a meeting"):
        meeting.back()
    assert calls == []
    meeting.state = lambda: {"in_call": True}
    assert meeting.back() == "back sent to meeting"
    assert calls == [("browser_press_key", {"key": "Escape"})]


def test_gemini_preference_uses_only_the_current_opt_out_and_bounds_retries():
    meeting = object.__new__(room.ChromiumMeet)
    selectors = iter(
        [
            '[data-xos-gemini-dismiss="11111111-1111-4111-8111-111111111111"]',
            '[data-xos-gemini-dismiss="22222222-2222-4222-8222-222222222222"]',
        ]
    )
    meeting.js = lambda expression: next(selectors)
    calls = []
    meeting._tool = lambda tool, args: calls.append((tool, args))
    meeting._dismiss_gemini()
    assert calls == [
        (
            "browser_click",
            {"selector": '[data-xos-gemini-dismiss="11111111-1111-4111-8111-111111111111"]'},
        ),
        (
            "browser_click",
            {"selector": '[data-xos-gemini-dismiss="22222222-2222-4222-8222-222222222222"]'},
        ),
    ]
    calls.clear()
    meeting.js = lambda expression: (
        '[data-xos-gemini-dismiss="33333333-3333-4333-8333-333333333333"]'
    )
    meeting._dismiss_gemini()
    assert len(calls) == 1
    calls.clear()
    for invalid in ("", "button", '{"in_call":true}', None):
        meeting.js = lambda expression: invalid
        meeting._dismiss_gemini()
        assert calls == []


@pytest.mark.parametrize(
    "waiting_text",
    [
        "Someone in the meeting should let you in soon",
        "Please wait until a meeting host brings you into the call",
    ],
)
def test_native_meet_waiting_and_media_state_are_read_from_owned_controls(waiting_text):
    native = room.native_meet.NativeGuestMeet
    invite = "https://meet.google.com/abc-defg-hij"
    waiting = native.read_state(
        [
            {"role": "static", "name": waiting_text},
            {"role": "button", "name": "Leave call"},
        ],
        invite,
    )
    assert waiting["waiting"] is True
    assert waiting["in_call"] is False
    joined = native.read_state(
        [
            {"role": "document web", "name": "Meet - test"},
            {"role": "button", "name": "Leave call"},
            {"role": "button", "name": "Turn on microphone (ctrl + d)"},
            {"role": "button", "name": "Turn off camera (ctrl + e)"},
        ],
        invite,
    )
    assert joined["in_call"] is True
    assert joined["muted"] is True
    assert joined["camera_off"] is False
    assert joined["join_url"] == invite


def test_native_meet_does_not_treat_optional_signin_as_required():
    state = room.native_meet.NativeGuestMeet.read_state(
        [
            {"role": "heading", "name": "Sign in with your Google account"},
            {"role": "entry", "name": "Your name"},
            {"role": "button", "name": "Ask to join"},
        ],
        "https://meet.google.com/abc-defg-hij",
    )
    assert not state.get("signin")
    assert state["in_call"] is False
    assert state["denied"] is False


def test_native_meet_human_verification_is_reported_without_automatic_action():
    state = room.native_meet.NativeGuestMeet.read_state(
        [
            {"role": "heading", "name": "Select all images with traffic lights"},
        ],
        "https://meet.google.com/abc-defg-hij",
    )
    assert state["verification"] is True
    assert state["in_call"] is False


def test_native_packages_skip_network_on_an_already_provisioned_box(monkeypatch):
    packages = load("xos_room_packages", ROOT / "os/appliance/xos_room_packages.py")
    monkeypatch.setattr(packages, "ready", lambda: True)

    def unexpected(*args, **kwargs):
        pytest.fail("unexpected package/network operation")

    monkeypatch.setattr(packages.subprocess, "run", unexpected)
    packages.main()


def test_starting_meet_preserves_the_existing_host_account_profile(monkeypatch):
    browser = FakeBrowser()
    browser.headless = False
    meet = room.ChromiumMeet(browser, "idle")
    monkeypatch.setattr(room.time, "sleep", lambda _: None)
    meet.start("meet")
    assert browser.went == ["https://meet.google.com/new", "(history cleared)"]
    assert meet.native_guest is None


def test_native_keyboard_input_rejects_a_different_browser_process():
    native = room.native_meet.NativeGuestMeet.__new__(room.native_meet.NativeGuestMeet)

    class Process:
        pid = 123

        def poll(self):
            return None

    class OtherBrowser:
        def get_process_id(self):
            return 456

    class Control:
        def getApplication(self):
            return OtherBrowser()

    native.proc = Process()
    with pytest.raises(RuntimeError, match="control changed"):
        native._focus(Control())


def test_native_guest_reports_a_confirmed_meeting_end():
    read = room.native_meet.NativeGuestMeet.read_state
    nodes = [{"role": "static text", "name": "You left the meeting"}]
    assert read(nodes, "https://meet.google.com/abc-defg-hij")["ended"] is True
    active = nodes + [{"role": "button", "name": "Leave call"}]
    assert read(active, "https://meet.google.com/abc-defg-hij")["ended"] is False


def test_webex_join_screen_frame_is_the_meeting_and_helper_frames_are_not(tmp_path, monkeypatch):
    # Webex's join page (on the meeting site) embeds the name box, Join and the call itself in
    # a web.webex.com/meeting frame, beside an extension bridge frame and blank frames.
    browser = room.Browser(tmp_path, "https://web.webex.com/")
    browser.home = "HOME"
    site = "https://meet1779463518314-8326.webex.com"
    tree = {
        "frame": {"id": "HOME", "url": f"{site}/wbxmjs/joinservice/sites/x/meeting/download/y"},
        "childFrames": [
            {"frame": {"id": "BRIDGE", "url": f"{site}/WBXmjsasset/js/nativemsg-iframe.html"}},
            {"frame": {"id": "BLANK", "url": "about:blank"}},
            {"frame": {"id": "MEET", "url": "https://web.webex.com/meeting?joinInfoOrigin=x"}},
        ],
    }

    def send(method, params=None, target=""):
        if method == "Page.getFrameTree":
            return {"frameTree": tree}
        if method == "DOM.getFrameOwner":
            assert params == {"frameId": "MEET"}
            return {"backendNodeId": 9}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "OWNER"}}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [0, 0, 1280, 0, 1280, 720, 0, 720]}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 11}
        return {}

    monkeypatch.setattr(browser, "send", send)
    assert browser.meeting_frame() == ("HOME", 11, 0, 0)
    # A frame claiming the meeting path on any other host is never the meeting.
    tree["childFrames"][2]["frame"]["url"] = f"{site}/meeting"
    assert browser.meeting_frame() == ("HOME", None, 0, 0)


def test_webex_join_button_matches_its_long_label():
    for label in ("Join meeting Sam's Personal Room", "Join meeting", "Join"):
        assert re.match(room.WEBEX_JOIN, label, re.I), label
    for label in ("Leave meeting", "Joined", "Join audio"):
        assert not re.match(room.WEBEX_JOIN, label, re.I), label


class InputRecorder(FakeBrowser):
    """Records the pointer input a click sends, in order."""

    def __init__(self, url):
        super().__init__(url)
        self.input = []

    def evaluate(self, js, target=""):
        if "getBoundingClientRect" in js:
            return json.dumps({"x": 500, "y": 680})
        return super().evaluate(js, target)

    def hover(self, x, y, target=""):
        self.input.append(("hover", x, y))

    def click(self, x, y, target=""):
        self.input.append(("click", x, y))


def test_webex_controls_wake_before_a_click_and_the_pointer_leaves_after(monkeypatch):
    # Webex's call bar ignores a click that arrives with the first pointer move, and leaves its
    # tooltip on the TV while the pointer stays.
    monkeypatch.setattr(room.time, "sleep", lambda s: None)
    m = room.ChromiumMeet(InputRecorder("https://web.webex.com/meeting"), "https://xos/idle")
    m.provider = "webex"
    m._tool("browser_click", {"selector": '[data-xos-webex="mic"]'})
    assert m.b.input == [
        ("hover", 460, 640),
        ("hover", 500, 680),
        ("click", 500, 680),
        ("hover", 2, 2),
    ]
    zoom = room.ChromiumMeet(InputRecorder("https://app.zoom.us/wc/1/join"), "https://xos/idle")
    zoom.provider = "zoom"
    zoom._tool("browser_click", {"selector": "#mute"})
    assert zoom.b.input == [("click", 500, 680)]  # other services click as before


def test_leaving_a_page_stops_and_answers_its_leave_site_prompt(tmp_path, monkeypatch):
    browser = room.Browser(tmp_path, "https://xos/idle")
    browser.home = "HOME"
    sent = []

    def send(method, params=None, target=""):
        sent.append(method)
        if method == "Page.navigate" and sent.count("Page.navigate") == 1:
            raise room.CdpError("Page.navigate: no answer from the browser")
        return {}

    monkeypatch.setattr(browser, "send", send)
    monkeypatch.setattr(browser, "evaluate", lambda js, target="": sent.append("frame:quiet") or "")
    browser.navigate("https://xos/idle")
    assert sent == [
        "Runtime.evaluate",
        "frame:quiet",
        "Page.navigate",
        "Page.handleJavaScriptDialog",
    ]
