"""Signed-out Meet in an owned Chromium window, using Linux accessibility and Wayland input.

The application is selected by its child PID, never by a desktop-global browser title. No
DevTools endpoint, browser-account session, JavaScript injection or CAPTCHA automation is used.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import urllib.parse
from pathlib import Path


class NativeGuestMeet:
    def __init__(self, command: list[str], invitation: str, display_name: str):
        parsed = urllib.parse.urlsplit(invitation)
        if parsed.scheme != "https" or parsed.hostname != "meet.google.com":
            raise RuntimeError("Native guest controls only support Google Meet")
        try:
            import pyatspi
            from gi.repository import Gio, GLib
        except ImportError as e:
            raise RuntimeError("Install the current room update for native Meet controls") from e
        self.api = pyatspi
        self.invitation, self.display_name = invitation, display_name
        self.proc = None
        self.app = None
        self.cache, self.cached_at = [], 0.0
        self.env = os.environ.copy()
        if not self.env.get("WAYLAND_DISPLAY"):
            runtime = Path(self.env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
            sockets = [p for p in runtime.glob("wayland-*") if p.is_socket() and p.stat().st_uid == os.getuid()]
            if len(sockets) != 1:
                raise RuntimeError("The room display is not ready for a guest meeting")
            self.env["WAYLAND_DISPLAY"] = sockets[0].name
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set",
            GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled", GLib.Variant("b", True))),
            None, Gio.DBusCallFlags.NONE, 5000, None,
        )
        try:
            self.proc = subprocess.Popen(command, env=self.env, start_new_session=True)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and self.proc.poll() is None:
                for app in pyatspi.Registry.getDesktop(0):
                    try:
                        if app.get_process_id() == self.proc.pid:
                            self.app = app
                            return
                    except Exception:
                        continue
                time.sleep(.25)
            raise RuntimeError("The guest browser did not expose its room controls")
        except Exception:
            self.close()
            raise

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _nodes(self) -> list[dict]:
        if not self.alive() or not self.app:
            return []
        if time.monotonic() - self.cached_at < .2:
            return self.cache
        nodes, stack = [], [(self.app, 0)]
        visited = 0
        while stack and visited < 3500:
            node, depth = stack.pop()
            visited += 1
            try:
                state = node.getState()
                if state.contains(self.api.STATE_SHOWING):
                    nodes.append({"node": node, "role": node.getRoleName(), "name": node.name or "",
                                  "enabled": state.contains(self.api.STATE_ENABLED)})
                if depth < 24:
                    stack.extend((child, depth + 1) for child in reversed(list(node)) if child)
            except Exception:
                continue  # A navigation can retire an accessibility node mid-snapshot.
        self.cache, self.cached_at = nodes, time.monotonic()
        return nodes

    def _focus(self, node) -> None:
        if not self.alive() or node.getApplication().get_process_id() != self.proc.pid:
            raise RuntimeError("The room meeting control changed; try again")
        node.queryComponent().grabFocus()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if node.getState().contains(self.api.STATE_FOCUSED):
                return
            time.sleep(.05)
        raise RuntimeError("The meeting control could not receive room keyboard input")

    def _keys(self, node, *args: str) -> None:
        self._focus(node)
        subprocess.run(["wtype", *args], env=self.env, check=True, timeout=5)
        self.cached_at = 0

    def button(self, pattern: str) -> bool:
        candidates = [n for n in self._nodes() if n["role"] in {"button", "push button", "toggle button"}
                      and n["enabled"] and re.search(pattern, n["name"], re.I)]
        if len(candidates) != 1:
            return False
        self._keys(candidates[0]["node"], "-k", "Return")
        return True

    def prepare_guest_name(self) -> None:
        fields = [n for n in self._nodes() if n["role"] == "entry" and n["name"] == "Your name" and n["enabled"]]
        if len(fields) != 1:
            return
        node = fields[0]["node"]
        if node.queryText().getText(0, -1) == self.display_name:
            return
        self._keys(node, "-M", "ctrl", "-k", "a", "-m", "ctrl")
        self._keys(node, "--", self.display_name)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if node.queryText().getText(0, -1) == self.display_name:
                return
            time.sleep(.05)
        raise RuntimeError("Meet did not accept the room guest name")

    def press_join(self) -> str:
        if self.state().get("in_call"):
            return "in"
        return "clicked" if self.button(r"^(ask to join|join now|join anyway)$") else "waiting"

    @staticmethod
    def read_state(nodes: list[dict], invitation: str) -> dict:
        names = [n["name"] for n in nodes]
        buttons = [n["name"] for n in nodes if n["role"] in {"button", "push button", "toggle button"}]
        text = "\n".join(names)
        waiting = bool(re.search(r"asking to join|waiting for (?:the host|someone)|someone.*(?:let|lets) you in|waiting to be admitted|you'll join when|please wait until a meeting host brings you into the call", text, re.I))
        mic = next((n for n in buttons if re.match(r"^turn (?:on|off) microphone\b", n, re.I)), "")
        cam = next((n for n in buttons if re.match(r"^turn (?:on|off) camera\b", n, re.I)), "")
        active = any(re.match(r"^leave call\b", n, re.I) for n in buttons) and not waiting
        title = next((n["name"] for n in nodes if n["role"] == "document web" and n["name"].startswith("Meet")), "Google Meet")
        state = {
            "provider": "meet", "in_call": active, "waiting": waiting,
            "ended": not active and not waiting and bool(re.search(r"you(?: have|'ve)? left (?:the )?(?:meeting|call)|meeting has (?:already )?(?:been )?ended|host has ended (?:this |the )?meeting|call has ended", text, re.I)),
            "muted": bool(re.match(r"^turn on", mic, re.I)) if mic else None,
            "camera_off": bool(re.match(r"^turn on", cam, re.I)) if cam else None,
            "denied": bool(re.search(r"denied your request|you can't join (?:this|the) (?:video )?call", text, re.I)),
            "missing": bool(re.search(r"check your meeting code|meeting has (?:already )?ended|invalid meeting", text, re.I)),
            "verification": bool(re.search(r"verify (?:that )?you(?:'re| are) (?:a )?human|select all images", text, re.I)),
            "title": re.sub(r"^Meet\s*[-–]\s*", "", title),
            "code": urllib.parse.urlsplit(invitation).path.rsplit("/", 1)[-1],
            "display_fullscreen": True, "native_guest": True,
        }
        if active:
            state["join_url"] = invitation
        return state

    def state(self) -> dict:
        if not self.alive():
            return {"provider": "meet", "in_call": False, "error": "The guest browser closed; join the meeting again"}
        return self.read_state(self._nodes(), self.invitation)

    def toggle(self, which: str) -> str:
        label = "microphone" if which == "microphone" else "camera"
        return "sent" if self.button(r"^turn (?:on|off) " + label + r"\b") else "missing"

    def leave(self) -> None:
        if not self.button(r"^leave call\b"):
            raise RuntimeError("Meet's Leave control is unavailable")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not self.state().get("in_call"):
                return
            time.sleep(.25)
        raise RuntimeError("Meet has not confirmed leaving")

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        self.proc, self.app = None, None
        self.cache = []
