#!/usr/bin/env python3
"""xos-room (Linux): the room computer appliance's agent.

A Linux box drives the room TV (HDMI) and uses the room's camera, microphones and speaker (a
Logitech Rally Bar in USB mode). This agent checks in with the Xos server as the room's computer
and runs what the room tablet asks: join or start a Google Meet, Zoom or Teams meeting, join a
Webex meeting, microphone, camera, share, volume, leave.
It reports the meeting's real state back. It is the Linux twin of os/mac/xos-room.py and shares
its server contract and meeting logic (the Room class and CopperMeet are loaded from there),
so the tablet and the server can't tell the two apart except by `client`.

What's different from the Mac:

  * The browser is Chromium in Cage (one full-screen Wayland app on the HDMI output), started and
    supervised by this agent. If Chromium or Cage dies, the agent starts it again and reports
    "browser": "restarting" until it's back.
  * The agent drives Chromium over --remote-debugging-pipe: the DevTools protocol on two inherited
    file descriptors. There is no debugging port, so nothing else on the box or the network can
    drive the browser.
  * Volume is the default PipeWire sink (wpctl), i.e. the Rally Bar.
  * Chromium hands no link to another app: its xdg-open is os/appliance/no-apps/xdg-open, so a
    meeting page's "open the app" launch (Webex's webex:) leaves the meeting in the browser.
  * Identity, health and the browser profile live under /var/lib/xos (StateDirectory) and
    /run/xos; the systemd unit restarts the agent and feeds its watchdog.

    xos-room-linux --room atrium [--server https://xos.example.org]

Python standard library only. --headless runs Chromium without Cage (tests, CI).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
MEETING_DISPLAY_JS = (HERE / "meeting_display.js").read_text()
NO_APPS = HERE / "no-apps"  # first on Chromium's PATH: its xdg-open opens nothing


def _load_shared():
    """The Mac agent: its Room class (server contract) and Meet scripts are the shared source."""
    path = HERE.parent / "mac" / "xos-room.py"
    spec = importlib.util.spec_from_file_location("xos_room_shared", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


shared = _load_shared()
_native_spec = importlib.util.spec_from_file_location("xos_native_meet", HERE / "native_meet.py")
native_meet = importlib.util.module_from_spec(_native_spec)
_native_spec.loader.exec_module(native_meet)
log = shared.log


# ---------- the DevTools protocol over --remote-debugging-pipe ----------


class CdpError(RuntimeError):
    pass


class Cdp:
    """JSON messages separated by NUL bytes: commands to Chromium's fd 3, replies from its fd 4."""

    def __init__(self, wfd: int, rfd: int):
        self.wfd, self.rfd = wfd, rfd
        self.next_id = 0
        self.lock = threading.Lock()
        self.waiting: dict[int, queue.Queue] = {}
        self.closed = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        buf = b""
        try:
            while True:
                chunk = os.read(self.rfd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\0" in buf:
                    raw, buf = buf.split(b"\0", 1)
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    q = self.waiting.pop(msg.get("id"), None) if "id" in msg else None
                    if q:
                        q.put(msg)
        except OSError:
            pass
        self.closed.set()
        for q in list(self.waiting.values()):
            q.put({"error": {"message": "browser closed"}})

    def call(
        self,
        method: str,
        params: dict | None = None,
        session: str | None = None,
        timeout: float = 15,
    ) -> dict:
        if self.closed.is_set():
            raise CdpError("browser closed")
        with self.lock:
            self.next_id += 1
            mid = self.next_id
            q: queue.Queue = queue.Queue()
            self.waiting[mid] = q
            msg = {"id": mid, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            try:
                os.write(self.wfd, json.dumps(msg).encode() + b"\0")
            except OSError as e:
                self.waiting.pop(mid, None)
                raise CdpError(f"browser closed: {e}") from e
        try:
            reply = q.get(timeout=timeout)
        except queue.Empty as e:
            self.waiting.pop(mid, None)
            raise CdpError(f"{method}: no answer from the browser") from e
        if "error" in reply:
            raise CdpError(f"{method}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result", {})

    def close(self) -> None:
        for fd in (self.wfd, self.rfd):
            try:
                os.close(fd)
            except OSError:
                pass


# ---------- Chromium in Cage, supervised ----------

CHROMIUM_FLAGS = [
    "--no-first-run",
    "--noerrdialogs",
    "--disable-infobars",
    "--disable-notifications",
    "--disable-session-crashed-bubble",
    "--disable-features=Translate,MediaRouter",
    "--password-store=basic",
    "--autoplay-policy=no-user-gesture-required",
    "--check-for-update-interval=31536000",
    "--remote-debugging-pipe",
]


def room_ui_scale(physical_width: float) -> float:
    """Aim for about 1280 logical pixels across a room TV; keep small displays usable."""
    return max(1.0, min(3.0, round(physical_width / 1280 * 4) / 4))


class Browser:
    def __init__(
        self, profile: Path, start_url: str, headless: bool = False, extra: list[str] | None = None,
        ui_scale: float | None = None
    ):
        self.profile, self.start_url, self.headless = profile, start_url, headless
        self.extra = extra or []
        if ui_scale is not None and not 1 <= ui_scale <= 3:
            raise ValueError("Room UI scale must be between 1 and 3")
        self.ui_scale = ui_scale if ui_scale is not None else (1.0 if headless else None)
        self.screen_pixels = {}
        self.media_origins: set[str] = set()  # origins granted camera and microphone this launch
        self.proc: subprocess.Popen | None = None
        self.cdp: Cdp | None = None
        self.home = ""  # the kiosk window's page: the room screen, and where meetings open
        self.sessions: dict[str, str] = {}
        self.restarts = -1  # the first start isn't a restart
        self.last_error = ""
        self.lock = threading.RLock()

    def command(self) -> list[str]:
        chromium = shutil.which("chromium") or shutil.which("chromium-browser") or "chromium"
        flags = [*CHROMIUM_FLAGS, f"--user-data-dir={self.profile}",
                 "--force-device-scale-factor=1", *self.extra]
        if self.headless:
            return [chromium, "--headless=new", *flags, self.start_url]
        return [
            "cage",
            "-s",
            "--",
            chromium,
            "--kiosk",
            "--ozone-platform=wayland",
            *flags,
            self.start_url,
        ]

    def alive(self) -> bool:
        return bool(
            self.proc and self.proc.poll() is None and self.cdp and not self.cdp.closed.is_set()
        )

    def start(self) -> None:
        with self.lock:
            self._launch()
            # Measure the actual compositor output, not the TV's list of supported modes.
            # The one calibration relaunch happens before readiness, never during a meeting.
            metrics = self.send("Runtime.evaluate", {"expression":
                "({width:screen.width,height:screen.height})", "returnByValue": True})
            self.screen_pixels = metrics.get("result", {}).get("value", {})
            if self.ui_scale is None:
                width = self.screen_pixels.get("width", 1280)
                self.ui_scale = room_ui_scale(width)
                if self.ui_scale != 1:
                    restarts = self.restarts
                    self._launch()
                    self.restarts = restarts  # calibration is not crash recovery
            log("room display scale", self.ui_scale, self.screen_pixels)

    def configure_zoom(self) -> None:
        """Write Chromium's content zoom only while this private profile is stopped.

        Ozone/Wayland can change buffer density without changing layout for a forced device
        scale. Profile page zoom actually reflows the viewport and enlarges meeting controls.
        """
        path = self.profile / "Default" / "Preferences"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        prefs = json.loads(path.read_text()) if path.exists() else {}
        partition = prefs.setdefault("partition", {})
        partition.setdefault("default_zoom_level", {})["x"] = math.log(self.ui_scale or 1) / math.log(1.2)
        # Old manual per-provider zoom must not override the room's calibrated default.
        hosts = partition.get("per_host_zoom_levels", {}).get("x", {})
        for host in list(hosts):
            if shared.provider_of("https://" + host):
                del hosts[host]
        # Meet benefits from a closer room view; keep the verified Zoom fit unchanged.
        partition.setdefault("per_host_zoom_levels", {}).setdefault("x", {})["meet.google.com"] = {
            "zoom_level": math.log((self.ui_scale or 1) * 1.2) / math.log(1.2)
        }
        temporary = path.with_suffix(".xos-tmp")
        temporary.write_text(json.dumps(prefs))
        temporary.chmod(0o600)
        temporary.replace(path)

    def _launch(self) -> None:
        with self.lock:
            self.stop()
            self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.configure_zoom()
            # Only this agent runs this profile: a lock left by a power cut or a hostname change
            # would stop Chromium at "profile in use by another computer".
            for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                (self.profile / lock).unlink(missing_ok=True)
            r_cmd, w_cmd = os.pipe()  # agent writes -> Chromium reads on fd 3
            r_evt, w_evt = os.pipe()  # Chromium writes on fd 4 -> agent reads

            def child():
                os.dup2(r_cmd, 3)
                os.dup2(w_evt, 4)
                for fd in (r_cmd, w_cmd, r_evt, w_evt):
                    if fd not in (3, 4):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                os.set_inheritable(3, True)
                os.set_inheritable(4, True)

            if not os.access(NO_APPS / "xdg-open", os.X_OK):
                log("warning:", NO_APPS / "xdg-open", "isn't executable; meeting app links may prompt")
            env = {**os.environ, "PATH": f"{NO_APPS}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
            self.proc = subprocess.Popen(
                self.command(),
                preexec_fn=child,
                close_fds=False,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            os.close(r_cmd)
            os.close(w_evt)
            self.cdp = Cdp(w_cmd, r_evt)
            self.restarts += 1
            self._attach()
            log("browser started", f"(pid {self.proc.pid})")

    def _attach(self, wait: float = 30) -> None:
        """Find the one page Chromium opens (the kiosk window)."""
        deadline = time.time() + wait
        while time.time() < deadline:
            page = next(iter(self._pages()), None)
            if page:
                self.home = page["targetId"]
                self._session(self.home)
                self.configure_permissions()
                return
            time.sleep(0.5)
        raise CdpError("the browser opened no page")

    def configure_permissions(self) -> None:
        """Room AV is provisioned once, scoped to supported meeting origins. Never auto-capture
        the display: people present from their own laptops via the meeting invitation."""
        for name in ("camera", "microphone"):
            self.cdp.call(
                "Browser.setPermission",
                {"permission": {"name": name}, "setting": "denied"},
            )
        self.media_origins = set()
        for origin in (
            "https://meet.google.com",
            "https://app.zoom.us",
            "https://zoom.us",
            "https://teams.microsoft.com",
            "https://teams.live.com",
            "https://teams.cloud.microsoft",
            "https://web.webex.com",
        ):
            self.grant_media(origin)
        for name in ("notifications", "display-capture", "geolocation"):
            self.cdp.call(
                "Browser.setPermission",
                {"permission": {"name": name}, "setting": "denied"},
            )

    def grant_media(self, origin: str) -> None:
        """Camera and microphone for one supported meeting origin. Webex sites each have their own
        host (acme.webex.com), so the agent grants a Webex origin when the room opens it."""
        with self.lock:
            if origin in self.media_origins or not shared.provider_of(origin + "/") or not self.cdp:
                return
            for name in ("camera", "microphone"):
                self.cdp.call(
                    "Browser.setPermission",
                    {"permission": {"name": name}, "setting": "granted", "origin": origin},
                )
            self.media_origins.add(origin)

    def _pages(self) -> list[dict]:
        targets = self.cdp.call("Target.getTargets").get("targetInfos", [])
        return [t for t in targets if t.get("type") == "page"]

    def _session(self, target: str) -> str:
        if target not in self.sessions:
            sid = self.cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})[
                "sessionId"
            ]
            self.cdp.call("Page.enable", session=sid)
            self.cdp.call(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": MEETING_DISPLAY_JS}, session=sid,
            )
            self.sessions[target] = sid
        return self.sessions[target]

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 15)
                self.proc.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, 9)
                except OSError:
                    pass
        if self.cdp:
            self.cdp.close()
        self.proc, self.cdp, self.home, self.sessions = None, None, "", {}

    def ensure(self) -> None:
        """Start the browser if it isn't running (called every few seconds)."""
        with self.lock:
            if self.alive():
                return
            try:
                self.start()
                self.last_error = ""
            except Exception as e:  # noqa: BLE001 - report it, try again next time
                self.last_error = f"browser didn't start: {e}"[:200]
                log(self.last_error)
                self.stop()

    def send(self, method: str, params: dict | None = None, target: str = "") -> dict:
        """A DevTools command to one page (default: the kiosk window)."""
        with self.lock:
            if not self.alive():
                raise CdpError("the browser is restarting")
            target = target or self.home
            try:
                return self.cdp.call(method, params, session=self._session(target))
            except CdpError as e:
                if "session" not in str(e).lower():
                    raise
                self.sessions.pop(target, None)  # the page navigated away from its session
                return self.cdp.call(method, params, session=self._session(target))

    def pages(self) -> list[dict]:
        with self.lock:
            if not self.alive():
                raise CdpError("the browser is restarting")
            pages = self._pages()
            ids = {p["targetId"] for p in self.cdp.call("Target.getTargets").get("targetInfos", [])}
            self.sessions = {t: sid for t, sid in self.sessions.items() if t in ids}
            return pages

    def meeting_frame(self, target: str = "") -> tuple[str, int | None, float, float]:
        """Zoom's web app and Webex's join page embed their meeting UI in a frame (Webex puts
        its name box, Join button and the call itself in a web.webex.com/meeting frame). Only a
        unique meeting frame owned by the same provider is eligible; input remains on the owning
        page's viewport."""
        target = target or self.home
        tree = self.send("Page.getFrameTree", target=target).get("frameTree", {})
        provider = shared.provider_of(tree.get("frame", {}).get("url", ""))
        meeting_path = {"zoom": "/wc/", "webex": "/meeting"}.get(provider or "")
        if not meeting_path:
            return target, None, 0, 0
        candidates = []
        def visit(node):
            for child in node.get("childFrames", []):
                frame = child["frame"]
                url = frame.get("url", "")
                u = urllib.parse.urlsplit(url)
                if provider == "webex" and u.hostname != "web.webex.com":
                    pass  # Webex's own helper frames (extension bridge, blanks) are not the meeting
                elif shared.provider_of(url) == provider and u.path.startswith(meeting_path):
                    candidates.append(frame)
                visit(child)
        visit(tree)
        if len(candidates) != 1:
            return target, None, 0, 0
        frame = candidates[0]
        owner = self.send("DOM.getFrameOwner", {"frameId": frame["id"]}, target)
        # The live frame URL can differ from its original src, or live in a shadow tree.
        # Fit the actual trusted frame owner from CDP, not just a DOM selector match.
        resolved = self.send("DOM.resolveNode", {"backendNodeId": owner["backendNodeId"]}, target)
        obj = resolved["object"]["objectId"]
        try:
            fitted = self.send("Runtime.callFunctionOn", {
                "objectId": obj, "returnByValue": True,
                "functionDeclaration": """function() {
                  if (this.tagName !== 'IFRAME') return false;
                  const rules = {position:'fixed',inset:'0',width:'100vw',height:'100vh',
                    'max-width':'none','max-height':'none',padding:'0',margin:'0',border:'0',
                    'border-radius':'0','box-sizing':'border-box','box-shadow':'none',
                    transform:'none',translate:'none',scale:'none','z-index':'2147483000'};
                  for (const [key,value] of Object.entries(rules)) this.style.setProperty(key,value,'important');
                  // A transformed shell ancestor would otherwise contain position:fixed.
                  const parent = e => e.parentElement || e.getRootNode()?.host;
                  for (let e=parent(this); e; e=parent(e)) {
                    for (const [key,value] of Object.entries({transform:'none',filter:'none',
                      perspective:'none',contain:'none','will-change':'auto'})) {
                      e.style.setProperty(key,value,'important');
                    }
                  }
                  return true;
                }""",
            }, target)
            if "exceptionDetails" in fitted:
                raise CdpError("The meeting frame could not fill its window")
        finally:
            self.send("Runtime.releaseObject", {"objectId": obj}, target)
        model = self.send("DOM.getBoxModel", {"backendNodeId": owner["backendNodeId"]}, target)
        quad = model["model"]["content"]
        frame_target = target
        try:
            world = self.send("Page.createIsolatedWorld", {"frameId": frame["id"], "worldName": "xos-meeting"}, target)
        except CdpError:
            # Site-isolated frames have their own DevTools target (the frame id is the target id).
            frame_target = frame["id"]
            world = self.send("Page.createIsolatedWorld", {"frameId": frame["id"], "worldName": "xos-meeting"}, frame_target)
        return frame_target, world["executionContextId"], quad[0], quad[1]

    def evaluate(self, js: str, target: str = "") -> str:
        # A Zoom or Webex shell can contain the meeting iframe; fit it before measuring offsets.
        root_display = self.send("Runtime.evaluate", {"expression": MEETING_DISPLAY_JS}, target)
        if "exceptionDetails" in root_display:
            raise CdpError("meeting display rules could not be installed")
        frame_target, context, _, _ = self.meeting_frame(target)
        params = {"expression": js, "returnByValue": True}
        if context:
            params["contextId"] = context
        # Apply to the current document too, including Zoom's isolated meeting iframe.
        display = {**params, "expression": MEETING_DISPLAY_JS}
        display_result = self.send("Runtime.evaluate", display, frame_target)
        if "exceptionDetails" in display_result:
            raise CdpError("meeting display rules could not be installed")
        out = self.send("Runtime.evaluate", params, frame_target)
        if "exceptionDetails" in out:
            raise CdpError(out["exceptionDetails"].get("text", "script failed"))
        value = out.get("result", {}).get("value", "")
        return value if isinstance(value, str) else json.dumps(value)

    def navigate(self, url: str, target: str = "") -> None:
        """Webex asks "Leave site?" before its call page unloads, which holds a navigation until
        someone answers. The room is leaving on purpose: stop the prompt in the page and its
        meeting frame first, and accept one that still appears."""
        quiet = ("addEventListener('beforeunload', e => e.stopImmediatePropagation(), {capture: true});"
                 "window.onbeforeunload = null; true")
        for run in (lambda: self.send("Runtime.evaluate", {"expression": quiet}, target),
                    lambda: self.evaluate(quiet, target)):
            try:
                run()
            except CdpError:
                pass
        try:
            self.send("Page.navigate", {"url": url}, target)
        except CdpError as e:
            if "no answer" not in str(e):
                raise
            self.send("Page.handleJavaScriptDialog", {"accept": True}, target)

    def forget_history(self, target: str = "") -> None:
        """Only the page on screen stays in the kiosk window's history. A real Webex page goes
        back in history on its own (its "meeting not found" page did, after ~6 s), which on the
        kiosk window is the room screen or an earlier meeting, never somewhere to go back to."""
        try:
            self.send("Page.resetNavigationHistory", {}, target)
        except CdpError:
            pass

    def url(self, target: str = "") -> str:
        try:
            return self.evaluate("location.href", target)
        except CdpError:
            return ""

    def hover(self, x: float, y: float, target: str = "") -> None:
        _, _, offset_x, offset_y = self.meeting_frame(target)
        self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x + offset_x, "y": y + offset_y}, target)

    # Real input, as from a mouse and keyboard on the box: meeting apps ignore scripted clicks.
    def click(self, x: float, y: float, target: str = "") -> None:
        _, _, offset_x, offset_y = self.meeting_frame(target)
        x, y = x + offset_x, y + offset_y
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.send(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
                target,
            )

    KEYS = {"Enter": (13, "\r"), "Escape": (27, ""), "Tab": (9, "")}

    def key(self, key: str, target: str = "") -> None:
        code, text = self.KEYS[key]
        down = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": key}
        down.update(windowsVirtualKeyCode=code, nativeVirtualKeyCode=code)
        if text:
            down["text"] = text
        self.send("Input.dispatchKeyEvent", down, target)
        self.send("Input.dispatchKeyEvent", {**down, "type": "keyUp", "text": ""}, target)

    def insert_text(self, text: str, target: str = "") -> None:
        self.send("Input.insertText", {"text": text}, target)

    def root_viewport(self, target: str) -> dict:
        result = self.send("Runtime.evaluate", {"expression":
            "({width:innerWidth,height:innerHeight,pixelRatio:devicePixelRatio,"
            "outerWidth,outerHeight,screenWidth:screen.width,screenHeight:screen.height})",
            "returnByValue": True}, target)
        return result.get("result", {}).get("value", {})

    def fullscreen(self, target: str) -> bool:
        """A Zoom-created browser window must fill the TV just like the original kiosk window."""
        if self.headless:
            return False
        with self.lock:
            window = self.cdp.call("Browser.getWindowForTarget", {"targetId": target})
            if window.get("bounds", {}).get("windowState") != "fullscreen":
                self.cdp.call("Browser.setWindowBounds", {
                    "windowId": window["windowId"], "bounds": {"windowState": "fullscreen"},
                })
            bounds = self.cdp.call("Browser.getWindowBounds", {"windowId": window["windowId"]})
            return bounds.get("bounds", {}).get("windowState") == "fullscreen"

    def activate(self, target: str) -> None:
        with self.lock:
            if self.alive():
                self.cdp.call("Target.activateTarget", {"targetId": target})

    def close_page(self, target: str) -> None:
        with self.lock:
            if self.alive() and target != self.home:
                self.cdp.call("Target.closeTarget", {"targetId": target})
                self.sessions.pop(target, None)


# Join-screen name fields: Meet ("Your name"), Zoom ("Your Name"), Teams ("Type your name"),
# Webex ("Your full name").
NAME_FIELD_JS = r"""
(() => {
  // Webex draws its join form in web components, so look through shadow roots too.
  const deep = (root, sel, out = []) => {
    for (const e of root.querySelectorAll('*')) { if (e.matches(sel)) out.push(e); if (e.shadowRoot) deep(e.shadowRoot, sel, out); }
    return out;
  };
  deep(document, '[data-xos-name]').forEach(e => e.removeAttribute('data-xos-name'));
  const inputs = deep(document, 'input').filter(e => !e.disabled && e.getClientRects().length);
  let e = inputs.find(e =>
    /^(your (?:full )?name|(?:full |display )?name|type your name|enter your (?:full )?name)$/i.test(e.getAttribute('placeholder') || e.getAttribute('aria-label') || '')
    || e.id === 'input-for-name');
  // Webex's guest screen: one unlabelled text box under "Enter your name and join".
  if (!e && location.hostname === 'web.webex.com' && /enter your name/i.test(document.body.innerText)) {
    const text = inputs.filter(e => e.type === 'text');
    if (text.length === 1) e = text[0];
  }
  if (!e) return 'missing';
  e.setAttribute('data-xos-name', '');
  return JSON.stringify({value: e.value});
})()
"""

# Teams labels its call controls differently from Meet and Zoom ("Mic", "Camera", "Leave", with the
# state in the tooltip or aria-pressed), so read them by their stable ids and every label.
TEAMS_STATE_JS = r"""
(() => {
  const label = (e) => [e.getAttribute('aria-label'), e.getAttribute('title'), e.getAttribute('data-state'),
                        e.innerText].filter(Boolean).join(' ');
  const shown = (e) => e && e.getClientRects().length;
  const btn = (id, re) => [document.getElementById(id), ...document.querySelectorAll('button, [role=button]')]
    .find(e => shown(e) && (e.id === id || re.test(label(e))));
  const mic = btn('microphone-button', /^(mic|microphone|mute|unmute)\b/i);
  const cam = btn('video-button', /^(camera|turn camera|video)\b/i);
  // The label says which it is ("Unmute mic", "Turn camera on"); else aria-pressed means on.
  const off = (e, offRe, onRe) => !e ? null : offRe.test(label(e)) ? true : onRe.test(label(e)) ? false
    : e.getAttribute('aria-pressed') !== null ? e.getAttribute('aria-pressed') === 'false' : null;
  const mute = off(mic, /\bunmute\b|\bmic-off\b/i, /\bmute\b|\bmic-on\b/i);
  const camOff = off(cam, /turn camera on|\bcamera-off\b|start video/i, /turn camera off|\bcamera-on\b|stop video/i);
  const text = document.body ? document.body.innerText : '';
  return JSON.stringify({
    muted: mute,
    camera_off: camOff,
    leave: !!(document.getElementById('hangup-button') && shown(document.getElementById('hangup-button'))),
    waiting: /should let you in soon|let people in the meeting know you're waiting|waiting for (the )?(host|organizer)/i.test(text),
  });
})()
"""

# Webex's call controls: "Mute"/"Unmute", "Stop video"/"Start video", "Leave meeting", sometimes with
# data-test ids. Their accessible names now describe the state ("Microphone is currently muted -
# click to unmute", "Sending video is currently disabled - click to enable") while the visible text
# is the short word, so read either. Mark them for real input.
WEBEX_STATE_JS = r"""
(() => {
  const names = (e) => [e.innerText, e.getAttribute('aria-label'), e.getAttribute('title')].filter(Boolean).map(t => t.trim());
  const all = [...document.querySelectorAll('button, [role=button]')].filter(e => !e.disabled && e.getClientRects().length);
  const find = (ids, re) => all.find(e => ids.includes(e.getAttribute('data-test'))) || all.find(e => names(e).some(t => re.test(t)));
  document.querySelectorAll('[data-xos-webex]').forEach(e => e.removeAttribute('data-xos-webex'));
  const mic = find(['microphone-button', 'mute-button'], /^(mute|unmute)$|^microphone is currently (muted|unmuted)\b/i);
  const cam = find(['camera-button', 'video-button'], /^(start|stop)(?: my)? video$|^turn (on|off) (camera|video)$|^sending video is currently (enabled|disabled)\b/i);
  const leave = find(['leave-button', 'leave-meeting-button'], /^leave(?: meeting)?$/i);
  for (const [e, tag] of [[mic, 'mic'], [cam, 'cam'], [leave, 'leave']]) if (e) e.setAttribute('data-xos-webex', tag);
  const said = (e, re) => e && names(e).some(t => re.test(t));
  const text = document.body ? document.body.innerText : '';
  return JSON.stringify({
    muted: !mic ? null : said(mic, /^microphone is currently muted\b/i) || (!said(mic, /^microphone is currently unmuted\b/i) && said(mic, /^unmute$/i)),
    camera_off: !cam ? null : said(cam, /^sending video is currently disabled\b/i) || (!said(cam, /^sending video is currently enabled\b/i) && said(cam, /^(start(?: my)? video|turn on (camera|video))$/i)),
    leave: !!leave,
    waiting: /host will let you in|you(?:['’]re| are) in the (?:meeting )?lobby|meeting has(?:n['’]t| not) started|waiting for (?:the )?host/i.test(text),
  });
})()
"""

# Webex's cookie banner sits over the call's Mute button and on the room screen: decline it
# (essential cookies only). Its buttons take an ordinary click.
WEBEX_COOKIES_JS = r"""
(() => {
  const deep = (root, out = []) => {
    for (const e of root.querySelectorAll('*')) { out.push(e); if (e.shadowRoot) deep(e.shadowRoot, out); }
    return out;
  };
  const all = deep(document);
  if (!all.some(e => !e.children.length && /we use cookies/i.test(e.innerText || '') && e.getClientRects().length)) return 'none';
  const reject = all.find(e => /^(reject|reject all)$/i.test((e.innerText || e.getAttribute('aria-label') || '').trim())
    && e.getClientRects().length && (e.tagName === 'BUTTON' || e.getAttribute('role') === 'button' || /BUTTON/.test(e.tagName)));
  if (!reject) return 'none';
  reject.click();
  return 'declined';
})()
"""

# Webex asks before a guest leaves: its "Leave meeting" confirmation, never "End meeting".
WEBEX_CONFIRM_LEAVE_JS = r"""
(() => {
  const all = [...document.querySelectorAll('button, [role=button], [role=menuitem]')].filter(e =>
    !e.disabled && e.getClientRects().length && e.getAttribute('data-xos-webex') !== 'leave'
    && /^leave(?: meeting)?$/i.test((e.getAttribute('aria-label') || e.innerText || '').trim()));
  const e = all.find(e => e.closest('[role=dialog], [role=alertdialog], [role=menu]')) || all[0];
  if (!e) return 'missing';
  e.setAttribute('data-xos-webex', 'confirm');
  return 'ready';
})()
"""

# Webex's guest path: "Join from your browser" (never the app), Next after the room's name (no
# email: the room joins as itself), then Join meeting.
# Webex's Join button's label also names the meeting ("Join meeting Sam's Personal Room").
WEBEX_JOIN = r"^(join from (?:your |this |the )?browser\.?|join (?:from|with|using) the web app|join meeting(?: .*)?|join|next)$"

SIGN_IN = (
    "https://accounts.google.com/",
    "https://login.microsoftonline.com/",
    "https://login.live.com/",
    "https://zoom.us/signin",
    "https://app.zoom.us/signin",
    "https://idbroker.webex.com/",
    "https://signin.webex.com/",
)


class ChromiumMeet(shared.CopperMeet):
    """The meeting controller the shared Room drives: Google Meet, Zoom, Microsoft Teams and
    Webex (as a guest) in the room's Chromium. Unsigned Meet uses an owned normal Chromium window with native
    accessibility and Wayland keyboard input. Zoom and Teams reuse the Mac agent's meeting logic (CopperMeet: joining,
    Zoom's keyboard-driven controls, Zoom's new meeting window, leaving and ending, the
    invitation link) and swaps Copper's browser tools for the DevTools pipe. Clicks and keys
    are real input events on the box (meeting apps ignore scripted clicks). Volume is PipeWire.

    A tab is a DevTools page target id; the room screen is the kiosk window's page (home), and
    a meeting either opens there or in the window the meeting app opens (Zoom's new meeting)."""

    display_name = ""
    native_guest = None
    providers = ("meet", "zoom", "teams", "webex")

    def __init__(self, browser: Browser, idle_url: str):
        self.b, self.idle_url = browser, idle_url
        self.tab = ""
        self.provider = "meet"
        self.invitation = ""
        self.navigation_until = 0.0
        self.native_guest = None

    @property
    def home_tab(self) -> str:
        return self.b.home

    def _target(self) -> str:
        if self.tab not in {p["targetId"] for p in self.b.pages()}:
            self.tab = self.b.home  # the meeting window closed, or the browser restarted
        return self.tab

    def _tool(self, tool: str, args: dict) -> str:
        """Copper's browser tools, on the room's Chromium."""
        try:
            return self._run_tool(tool, args, self._target())
        except CdpError as e:
            if time.monotonic() < self.navigation_until:
                raise shared.BrowserLoading("the meeting page is still loading") from e
            raise RuntimeError(f"the room browser: {e}") from e

    def _run_tool(self, tool: str, args: dict, tab: str) -> str:
        b = self.b
        if tool == "browser_evaluate":
            return b.evaluate("(" + args["function"] + ")()", tab)
        if tool == "browser_navigate":
            b.navigate(args["url"], tab)
            return ""
        if tool in ("browser_click", "browser_type"):
            box = b.evaluate(
                r"""((sel) => {
              const deep = (root, out = []) => {
                for (const e of root.querySelectorAll('*')) { if (e.matches(sel)) out.push(e); if (e.shadowRoot) deep(e.shadowRoot, out); }
                return out;
              };
              const e = deep(document).find(e => !e.disabled && e.getClientRects().length);
              if (!e) return 'missing';
              e.scrollIntoView({block: 'center', inline: 'center'});
              const r = e.getBoundingClientRect();
              return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});
            })(%s)"""
                % json.dumps(args["selector"]),
                tab,
            )
            if box == "missing":
                raise RuntimeError("the meeting's control went away; try again")
            at = json.loads(box)
            if self.provider == "webex":
                # Webex's call controls wake on pointer movement and ignore a click that arrives
                # with the first move; its tooltip then stays up until the pointer leaves.
                b.hover(at["x"] - 40, at["y"] - 40, tab)
                time.sleep(0.15)
                b.hover(at["x"], at["y"], tab)
                time.sleep(0.6)
            b.click(at["x"], at["y"], tab)
            if self.provider == "webex" and tool == "browser_click":
                time.sleep(0.3)
                b.hover(2, 2, tab)
            if tool == "browser_type":
                b.evaluate(
                    "(() => { let e = document.activeElement;"
                    " while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;"
                    " e && e.select && e.select(); })()",
                    tab,
                )
                b.insert_text(args["text"], tab)
            return ""
        if tool == "browser_press_key":
            b.key(args["key"], tab)
            return ""
        if tool == "browser_tabs":  # Copper's listing format, which CopperMeet reads
            return "\n".join(
                f"- {p.get('title', '')} ({p.get('url', '')}) {{tab {p['targetId']}}}"
                for p in b.pages()
            )
        if tool == "browser_close":
            b.close_page(tab)
            return ""
        raise RuntimeError(f"{tool} isn't a room browser tool")

    def _require_meet(self) -> None:
        url = self.b.url(self._target())
        provider = shared.provider_of(url)
        if provider:
            self.provider = provider
            if provider == "webex":  # the web app can move to another Webex host (web.webex.com)
                self.b.grant_media("https://" + urllib.parse.urlsplit(url).hostname)
            return
        if url.startswith(SIGN_IN):
            raise RuntimeError("Sign in on the room screen first (sudo xos-room-signin on the box)")
        # Still on the room screen or a blank page: the meeting is on its way.
        if time.monotonic() < self.navigation_until:
            raise shared.BrowserLoading("the meeting page is still loading")
        raise RuntimeError("The room screen isn't showing a meeting")

    def _follow_opened_meeting(self, provider: str, before: dict[str, str]) -> None:
        super()._follow_opened_meeting(provider, before)
        self.b.fullscreen(self.tab)
        self.b.activate(self.tab)  # Zoom's meeting window on the TV, not the page that opened it

    def open(self, url: str) -> None:
        """Navigate the room screen to a Meet, Zoom, Teams or Webex link (the link the server
        checked)."""
        provider = shared.provider_of(url)
        if not provider:
            raise RuntimeError("Only Meet, Teams, Webex and Zoom links are allowed")
        if self.native_guest:
            self.native_guest.close()
            self.native_guest = None
        guest_invitation = provider == "meet" and re.fullmatch(r"/(?:meet/)?[a-z]{3}-[a-z]{4}-[a-z]{3}/?", urllib.parse.urlsplit(url).path)
        if guest_invitation and not getattr(self.b, "headless", True) and os.environ.get("XOS_NATIVE_MEET", "1") != "0":
            guest = Browser(self.b.profile.parent / "meet-guest", url, extra=self.b.extra, ui_scale=self.b.ui_scale)
            guest.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            guest.configure_zoom()
            command = [arg for arg in guest.command()[3:] if arg != "--remote-debugging-pipe"]
            command.extend(["--force-renderer-accessibility", "--lang=en-US"])
            self.native_guest = native_meet.NativeGuestMeet(command, url, self.display_name)
            self.provider, self.invitation = provider, url
            return
        if provider == "zoom":  # the web client in the browser, never "open the Zoom app"
            url = re.sub(r"/(?:j|w|s)/(\d{9,11})", r"/wc/join/\1", url)
        if provider == "webex":
            # The web app in the browser, never "open the Webex app": the page's app launch goes
            # nowhere (no-apps/xdg-open, the room policy) and press_join takes "Join from your
            # browser". Each Webex site is its own host, so grant this one camera and microphone.
            self.b.grant_media("https://" + urllib.parse.urlsplit(url).hostname)
        self.provider = provider
        self.invitation = (
            url
            if provider == "webex"
            or re.search(r"/(?:wc/join|meet|l/meetup-join)/", url)
            or re.search(r"/[a-z]{3}-[a-z]{4}-[a-z]{3}$", url)
            else ""
        )
        self.navigation_until = time.monotonic() + 20
        self.tab = self.b.home
        self.b.navigate(url, self.tab)  # answers once the meeting page has committed
        self.b.forget_history(self.tab)
        time.sleep(1)

    def start(self, provider: str) -> None:
        if self.native_guest:
            self.native_guest.close()
            self.native_guest = None
        before = {p["targetId"] for p in self.b.pages()}
        try:
            super().start(provider)
        except RuntimeError:
            # A failed launch may leave a loading/app-launch popup. Only close windows this
            # start action created, never an existing browser window or the room home.
            for page in self.b.pages():
                if page["targetId"] not in before:
                    self.b.close_page(page["targetId"])
            self.b.activate(self.b.home)
            raise

    def state(self) -> dict:
        if self.native_guest:
            return self.native_guest.state()
        if not self.b.alive():
            return {
                "in_call": False,
                "error": self.b.last_error or "the room computer's browser is restarting",
            }
        try:
            url = self.b.url(self._target())
        except CdpError:
            return {"in_call": False}
        if not shared.provider_of(url):
            return {"in_call": False}  # the room screen between meetings
        fullscreen = self.b.fullscreen(self._target())
        st = super().state()
        st["display_fullscreen"] = fullscreen
        st["display_gemini_dismissals"] = getattr(self, "gemini_dismissals", 0)
        if st.get("in_call") and st.get("provider") == "meet":
            self._dismiss_gemini()
            st["display_gemini_dismissals"] = getattr(self, "gemini_dismissals", 0)
        if st.get("provider") == "teams" and not st.get("error"):
            st = self._teams(st)
        if st.get("provider") == "webex" and not st.get("error"):
            st = self._webex(st)
            st["signin"] = bool(st.get("signin") or url.startswith(SIGN_IN))
        try:
            display = json.loads(self.js(
                "JSON.stringify({installed:!!window.__xosMeetingDisplay?.refresh,"
                "hidden:window.__xosMeetingDisplay?.hidden || 0,"
                "remoteSurfaces:window.__xosMeetingDisplay?.remoteSurfaces || 0,"
                "attention:window.__xosMeetingDisplay?.attention || '',"
                "controls:window.__xosMeetingDisplay?.controls?.() || [],"
                "labels:window.__xosMeetingDisplay?.controlLabels?.() || [],"
                "diagnostics:window.__xosMeetingDisplay?.controlDiagnostics?.() || [],"
                "viewport:{width:innerWidth,height:innerHeight,pixelRatio:devicePixelRatio}})"
            ))
            st["control_back"] = bool(st.get("in_call"))
            st["controls"] = display.get("controls", []) if st.get("in_call") else []
            st["control_labels"] = display.get("labels", []) if st.get("in_call") else []
            st["control_diagnostics"] = display.get("diagnostics", []) if st.get("in_call") else []
            st["admission_pending"] = any(re.match(r"^(admit|allow)(?:\s|$)", c.get("label", ""), re.I) for c in st["controls"]) or any(re.search(r"\bguests? waiting|waiting to be admitted", label, re.I) for label in st["control_labels"])
            st["display_viewport"] = display.get("viewport", {})
            st["display_outer_viewport"] = self.b.root_viewport(self._target())
            st["display_rules"] = bool(display.get("installed"))
            st["display_hidden_notices"] = int(display.get("hidden", 0))
            st["display_remote_surfaces"] = int(display.get("remoteSurfaces", 0))
            if display.get("attention") in ("connection", "media"):
                st["display_attention"] = display["attention"]
        except (RuntimeError, CdpError, ValueError, TypeError):
            pass
        return st

    def _dismiss_gemini(self) -> None:
        """Persist the user's Meet opt-out, using real input on its validated preference control."""
        previous = ""
        for _ in range(2):  # checkbox preference + its confirmation, or a single opt-out button
            try:
                selector = self.js("window.__xosMeetingDisplay?.geminiDismissal?.() || ''")
                if (
                    not isinstance(selector, str)
                    or not re.fullmatch(r'\[data-xos-gemini-dismiss="[a-f0-9-]{36}"\]', selector)
                    or selector == previous
                ):
                    break
                previous = selector
                self._tool("browser_click", {"selector": selector})
                self.gemini_dismissals = getattr(self, "gemini_dismissals", 0) + 1
            except (RuntimeError, CdpError):
                break  # Provider changed mid-click: re-find it on the next heartbeat.

    def back(self) -> str:
        if not self.state().get("in_call"):
            raise RuntimeError("Join a meeting before using meeting controls")
        self._tool("browser_press_key", {"key": "Escape"})
        return "back sent to meeting"

    def control(self, token: str) -> str:
        if not self.state().get("in_call"):
            raise RuntimeError("Join a meeting before using meeting controls")
        selector = self.js("window.__xosMeetingDisplay?.resolveControl(" + json.dumps(token) + ") || ''")
        if not selector:
            raise RuntimeError("That meeting control changed or is unavailable; refresh the controls")
        self._tool("browser_click", {"selector": selector})
        return "meeting control sent"

    def _teams(self, st: dict) -> dict:
        try:
            t = json.loads(self.js(TEAMS_STATE_JS))
        except (RuntimeError, ValueError):
            return st
        for k in ("muted", "camera_off"):
            if st.get(k) is None:
                st[k] = t[k]
        st["waiting"] = bool(st.get("waiting") or t["waiting"])
        st["in_call"] = bool((st.get("in_call") or t["leave"]) and not st["waiting"])
        return st

    def _webex(self, st: dict) -> dict:
        try:
            self.js(WEBEX_COOKIES_JS)
            w = json.loads(self.js(WEBEX_STATE_JS))
        except (RuntimeError, ValueError):
            return st
        for k in ("muted", "camera_off"):
            if w[k] is not None:  # the call's own control, not a participant's Mute
                st[k] = w[k]
        st["waiting"] = bool(st.get("waiting") or w["waiting"])
        st["in_call"] = bool((st.get("in_call") or w["leave"]) and not st["waiting"])
        st["can_end"] = False  # a guest can only leave
        return st

    def toggle(self, which: str) -> str:
        if self.native_guest:
            return self.native_guest.toggle(which)
        if self.provider == "webex":
            self.js(WEBEX_STATE_JS)  # marks the current controls
            tag = "mic" if which == "microphone" else "cam"
            if self.js(f"(() => !!document.querySelector('[data-xos-webex={tag}]'))()") == "true":
                self._tool("browser_click", {"selector": f'[data-xos-webex="{tag}"]'})
                return "sent"
        if self.provider == "teams":
            ident = "microphone-button" if which == "microphone" else "video-button"
            if self.js(f"(() => !!document.getElementById('{ident}'))()") == "true":
                self._tool("browser_click", {"selector": "#" + ident})
                return "sent"
        return super().toggle(which)

    def leave(self, end: bool = False) -> None:
        """Zoom shows its host End, a guest Leave; both then ask in a dialog."""
        if self.native_guest:
            return self.native_guest.leave()
        if self.provider == "webex":
            return self._leave_webex(end)
        has_end = bool(self.state().get("can_end"))
        if self.provider != "zoom" or has_end:
            return super().leave(end)
        if self._button(r"^leave$") != "clicked":
            raise RuntimeError("Zoom's Leave control is unavailable")
        self._wait_button(r"^leave meeting$", seconds=3)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not self.state().get("in_call"):
                return
            time.sleep(0.2)
        raise RuntimeError("Zoom has not confirmed leaving; check the room screen")

    def _leave_webex(self, end: bool) -> None:
        """Webex's Leave, then its Leave meeting confirmation, with real input."""
        if end:
            raise RuntimeError("A Webex guest can only leave; the host ends the meeting")
        self.js(WEBEX_STATE_JS)
        if self.js("(() => !!document.querySelector('[data-xos-webex=leave]'))()") != "true":
            raise RuntimeError("Webex's Leave control is unavailable")
        self._tool("browser_click", {"selector": '[data-xos-webex="leave"]'})
        deadline = time.monotonic() + 8
        confirmed = False
        while time.monotonic() < deadline:
            if not confirmed and self.js(WEBEX_CONFIRM_LEAVE_JS) == "ready":
                self._tool("browser_click", {"selector": '[data-xos-webex="confirm"]'})
                confirmed = True
            elif not (st := self.state()).get("in_call") and not st.get("waiting"):
                return
            time.sleep(0.2)
        raise RuntimeError("Webex has not confirmed leaving; check the room screen")

    def press_join(self) -> str:
        if self.native_guest:
            return self.native_guest.press_join()
        if self.provider == "webex":
            self.js(WEBEX_COOKIES_JS)
            result = self._button(WEBEX_JOIN, join=True)
            return "waiting" if result == "missing" else result
        return super().press_join()

    def prepare_guest_name(self) -> None:
        """The room's name on a join screen that asks (a guest in Meet, Zoom, Teams or Webex). Webex
        gets the name only: the room joins as itself, never with an email address."""
        if self.native_guest:
            return self.native_guest.prepare_guest_name()
        if shared.provider_of(self.b.url(self._target())):
            self.b.forget_history(self._target())  # and the pages the meeting's own redirects added
        if self.display_name:
            self._fill(NAME_FIELD_JS, "[data-xos-name]", self.display_name, "name")

    def _fill(self, find_js: str, selector: str, text: str, what: str) -> None:
        out = self.js(find_js)
        if out == "missing" or json.loads(out)["value"] == text:
            return
        self._tool("browser_type", {"selector": selector, "text": text})
        confirmed = self.js(find_js)
        if confirmed == "missing" or json.loads(confirmed)["value"] != text:
            raise RuntimeError(f"The meeting didn't take the room's {what}; check the room screen")

    def close(self) -> None:
        """Back to the room screen: close a meeting window, show the room's TV view."""
        if self.native_guest:
            self.native_guest.close()
            self.native_guest = None
        tab = self._target() if self.b.alive() else ""
        if tab and tab != self.b.home:
            self.b.close_page(tab)
        self.tab = self.b.home
        self.b.navigate(self.idle_url)
        self.b.activate(self.b.home)

    def volume(self) -> int | None:
        try:
            out = subprocess.run(
                ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout  # "Volume: 0.60" (+ " [MUTED]")
            return round(float(out.split()[1]) * 100)
        except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
            return None

    def set_volume(self, level: int) -> None:
        r = subprocess.run(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{int(level)}%"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            raise RuntimeError(f"no speaker to set: {(r.stderr or r.stdout).strip()[:120]}")
        now = self.volume()  # say so rather than tick when the speaker ignores it
        if now is not None and abs(now - int(level)) > 2:
            raise RuntimeError(f"the speaker didn't take the volume (it reads {now})")

    def notify(self, text: str) -> None:
        log("room TV notice suppressed; use the controller for feedback")


# ---------- the appliance: identity, health, watchdog ----------


def identity(state: Path) -> str:
    """This box's device id, made once and kept across updates and reinstalls of the release."""
    path = state / "identity.json"
    try:
        return json.loads(path.read_text())["id"]
    except (OSError, ValueError, KeyError):
        host = socket.gethostname().split(".")[0].lower().removeprefix("xos").lstrip("-")
        name = "".join(c if c.isalnum() else "-" for c in host)[:20]
        did = "xos-" + (f"{name}-" if name else "") + uuid.uuid4().hex[:6]
        state.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": did, "created": time.time()}))
        tmp.chmod(0o600)
        tmp.replace(path)
        return did


def sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), addr)
    except OSError:
        pass


def os_name() -> str:
    try:
        rel = dict(
            line.split("=", 1)
            for line in Path("/etc/os-release").read_text().splitlines()
            if "=" in line
        )
        return rel.get("PRETTY_NAME", "").strip('"') or platform.platform()
    except OSError:
        return platform.platform()


def model() -> str:
    for p in ("/sys/class/dmi/id/product_name", "/proc/device-tree/model"):
        try:
            text = Path(p).read_text().strip("\0 \n")
            if text:
                return text
        except OSError:
            pass
    return platform.machine()


def release() -> str:
    """The Xos release this agent runs from (/opt/xos/releases/<sha>), else 'dev'."""
    parts = HERE.parts
    if "releases" in parts:
        return parts[parts.index("releases") + 1][:12]
    return os.environ.get("XOS_RELEASE", "dev")


class LinuxRoom(shared.Room):
    def __init__(self, server, room, meet, device_id, browser: Browser, state: Path, run: Path):
        super().__init__(server, room, meet, device_id)
        self.browser, self.state, self.run_dir = browser, state, run
        self.started = time.time()
        self.server_ok = False

    def _headers(self) -> dict:
        """A device key, once the server issues one to room computers (see README.md)."""
        try:
            key = (self.state / "device-key").read_text().strip()
        except OSError:
            return {}
        return {"X-Xos-Device": self.id, "X-Xos-Key": key} if key else {}

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.server + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **self._headers()},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")

    def _run_update(self, cmd: dict) -> None:
        if cmd.get("type") != "update":
            return super().run(cmd)
        try:
            (self.run_dir / "update-now").write_text(str(time.time()))
            self.ack(cmd["id"], True, "checking for a new release; Admin shows it when done")
        except OSError as e:
            self.ack(cmd["id"], False, f"couldn't start the update: {e}")

    def last_update(self) -> dict | None:
        try:
            return json.loads((self.state / "update" / "last.json").read_text())
        except (OSError, ValueError):
            return None

    def health(self, meeting: dict, vol: int | None) -> dict:
        disk = shutil.disk_usage(self.state if self.state.exists() else "/")
        return {
            "app": VERSION,
            "release": release(),
            "os": os_name(),
            "model": model(),
            "uptime_s": int(time.time() - self.started),
            "browser": "running" if self.browser.alive() else "restarting",
            "browser_restarts": max(0, self.browser.restarts),
            "display_scale": self.browser.ui_scale,
            "display_pixels": self.browser.screen_pixels,
            "last_error": self.browser.last_error,
            "disk_free_gb": round(disk.free / 1e9, 1),
            "meeting": meeting,
            "meeting_providers": list(self.meet.providers),
            "volume": vol,
            "hostname": socket.gethostname(),
            "last_update": self.last_update(),
        }

    def heartbeat(self) -> None:
        with self.lock:
            meeting = self.meeting_state()
            vol = self.meet.volume()
        native = self.health(meeting, vol)
        try:
            self._post(
                "/api/devices/heartbeat",
                {
                    "id": self.id,
                    "client": f"xosroom-linux-{VERSION}",
                    "role": "roompc",
                    "arg": self.room,
                    "label": f"Room computer · {self.room}",
                    "native": native,
                },
            )
            self.server_ok = True
        except Exception as e:  # noqa: BLE001 - offline: keep running, keep trying
            self.server_ok = False
            log("heartbeat failed:", e)
        self.write_health(native)
        sd_notify("WATCHDOG=1")

    def write_health(self, native: dict) -> None:
        """For xos-update's health check and for people on the box: /run/xos/health.json."""
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.run_dir / "health.json.tmp"
            tmp.write_text(
                json.dumps(
                    {
                        "at": time.time(),
                        "id": self.id,
                        "server_ok": self.server_ok,
                        "in_call": bool((native.get("meeting") or {}).get("in_call")),
                        **{k: native[k] for k in ("app", "release", "browser", "last_error")},
                    }
                )
            )
            tmp.replace(self.run_dir / "health.json")
        except OSError as e:
            log("health file:", e)

    def listen(self) -> None:
        while True:
            try:
                req = urllib.request.Request(
                    f"{self.server}/api/devices/{self.id}/stream",
                    headers={"Accept": "text/event-stream", **self._headers()},
                )
                with urllib.request.urlopen(req, timeout=60) as r:
                    log("connected to", self.server, "as", self.id)
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data:"):
                            try:
                                self.jobs.put(json.loads(line[5:].strip()))
                            except ValueError:
                                pass
            except Exception as e:  # noqa: BLE001
                log("stream:", e, "— reconnecting")
            time.sleep(3)

    def go_home(self) -> str:
        """Back to the room screen, unless a meeting is on (Admin's Reload, xos-room-signin done)."""
        with self.lock:
            st = self.meet.state()
            if st.get("in_call") or st.get("waiting"):
                return "in a meeting: staying on it"
            self.meet.close()
        self.signing_in_since = 0.0
        return "back on the room screen"

    def supervise(self) -> None:
        """Keep the browser up; take a sign-in or setup page someone on the box asked for, and come
        back to the room screen once that sign-in is finished."""
        self.signing_in_since = 0.0
        settled = 0.0  # since when the browser has been off the sign-in pages
        while True:
            self.browser.ensure()
            req = self.run_dir / "open-url"
            if req.exists():
                url = req.read_text().strip()
                req.unlink(missing_ok=True)
                if url == "home":
                    log("room screen:", self.go_home())
                elif url.startswith((*SIGN_IN, self.server + "/")) or shared.provider_of(url):
                    try:
                        self.browser.navigate(url)
                        log("opened", url)
                        homes = {h.rstrip("/") for h in shared.PROVIDER_HOME.values()}
                        signin = url.startswith(SIGN_IN) or url.rstrip("/") in homes
                        self.signing_in_since = time.time() if signin else 0.0
                    except CdpError as e:
                        log("couldn't open", url, e)
            if self.signing_in_since:
                # Google, Microsoft and Zoom leave the browser on an account page once someone has
                # signed in: when it has stayed off the sign-in pages for 20 s, go home.
                current = self.browser.url()
                if not current or current.startswith(SIGN_IN) or "signin" in current.lower():
                    settled = 0.0
                elif not settled:
                    settled = time.time()
                elif time.time() - settled > 20:
                    log("sign-in finished:", self.go_home())
                    settled = 0.0
                if time.time() - self.signing_in_since > 1800:
                    self.signing_in_since = 0.0  # left half-way: don't jump away later
            time.sleep(2)

    def run(self, cmd: dict) -> None:
        """Update now (Admin): leave the trigger xos-update-now.path watches. Reload
        (Admin): back to the room screen when there's no meeting."""
        if cmd.get("type") in ("identify", "message"):
            self.ack(cmd["id"], False, "Room TV popups are disabled; use the room controller")
            return
        if cmd.get("type") == "refresh":
            try:
                self.ack(cmd["id"], True, self.go_home())
            except Exception as e:  # noqa: BLE001
                self.ack(cmd["id"], False, str(e)[:300])
            self.beat_now.set()
            return
        return self._run_update(cmd)


def main() -> None:
    ap = argparse.ArgumentParser(description="Xos room computer (Linux appliance)")
    ap.add_argument("--room", required=True)
    ap.add_argument("--server", default="https://xos.example.org")
    ap.add_argument("--state-dir", default=os.environ.get("STATE_DIRECTORY", "/var/lib/xos"))
    ap.add_argument("--run-dir", default=os.environ.get("RUNTIME_DIRECTORY", "/run/xos"))
    ap.add_argument(
        "--idle-url", help="what the TV shows between meetings (default: the room's Xos TV view)"
    )
    ap.add_argument("--headless", action="store_true", help="no Cage or screen (tests)")
    ap.add_argument("--ui-scale", type=float, default=os.environ.get("XOS_ROOM_UI_SCALE"),
                    help="room display scale, 1 to 3 (default: measured automatically)")
    ap.add_argument(
        "--chromium-arg", action="append", default=[], help="extra Chromium flag (tests)"
    )
    a = ap.parse_args()
    server = a.server.rstrip("/")
    state, run = Path(a.state_dir), Path(a.run_dir)
    idle = a.idle_url or f"{server}/app/#/tv/{a.room}"
    browser = Browser(state / "chromium", idle, headless=a.headless, extra=a.chromium_arg, ui_scale=a.ui_scale)
    meet = ChromiumMeet(browser, idle)
    room = LinuxRoom(server, a.room, meet, identity(state), browser, state, run)
    log(f"Xos room computer {VERSION} (release {release()}) · room {a.room} · {room.id}")
    browser.ensure()
    sd_notify("READY=1")
    for target in (room.supervise, room.beats, room.worker):
        threading.Thread(target=target, daemon=True).start()
    try:
        room.listen()
    except KeyboardInterrupt:
        if meet.native_guest:
            meet.native_guest.close()
        browser.stop()


if __name__ == "__main__":
    main()
