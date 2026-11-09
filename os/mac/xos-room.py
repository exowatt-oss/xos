#!/usr/bin/env python3
"""Xos Room: a Mac as a meeting room's computer.

The Mac drives the room TV (HDMI) and uses the room's camera, microphones and speaker (a Logitech
Rally Bar in USB mode). This agent checks in with the Xos server as the room's computer and does
what the room iPad asks: join Meet, Teams, Webex or Zoom, mute, camera, share, volume and leave.
It reports the meeting's real state (in the call, muted, camera) back, so the iPad shows it.

    curl -fsSL https://xos.example.org/os/mac/xos-room.py -o ~/xos-room.py
    python3 ~/xos-room.py --room atrium

Meetings run in a dedicated Copper tab or Google Chrome profile. Sign in to Google Meet there.
Copper uses its supported CLI (--browser copper --copper-tab ID). Chrome can use a loopback
DevTools port (--chrome-port, optional websockets dependency), or legacy AppleScript control.
Legacy Chrome mode needs View → Developer → Allow JavaScript from Apple Events.

--simulate runs without Chrome or macOS (tests and demos).
"""

from __future__ import annotations

import argparse
import json
import platform
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VERSION = "0.2.1"
STATE_FILE = Path.home() / ".xos-room.json"
HEARTBEAT_S = 5
MEET = "https://meet.google.com/"
PROVIDER_HOME = {
    "meet": MEET,
    "teams": "https://teams.microsoft.com/v2/",
    "zoom": "https://app.zoom.us/wc/",
    "webex": "https://web.webex.com/",
}
# Webex is joined as a guest only: starting one needs a host account the room doesn't have.
GUEST_ONLY = {"webex"}


def provider_of(url: str) -> str | None:
    try:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
            return None
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host == "meet.google.com":
        return "meet"
    if host in {"teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft"}:
        return "teams"
    if host in {"zoom.us", "zoom.com"} or host.endswith((".zoom.us", ".zoom.com")):
        return "zoom"
    if host == "webex.com" or host.endswith(".webex.com"):
        return "webex"
    return None


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------- Google Meet in Chrome, through AppleScript ----------

# Run JavaScript in the first Meet tab. The script is passed as an argument (no quoting games).
# The state check runs every few seconds: it never starts Chrome, only joining a meeting does.
RUN_IN_MEET = """
on run argv
  if application "Google Chrome" is not running then return "NO_TAB"
  tell application "Google Chrome"
    repeat with w in windows
      repeat with t in tabs of w
        if URL of t starts with "https://meet.google.com/" then
          return execute t javascript (item 1 of argv)
        end if
      end repeat
    end repeat
  end tell
  return "NO_TAB"
end run
"""

OPEN_WINDOW = """
on run argv
  tell application "Google Chrome"
    activate
    set w to make new window
    set URL of active tab of w to (item 1 of argv)
  end tell
end run
"""

CLOSE_MEET = """
if application "Google Chrome" is not running then return
tell application "Google Chrome"
  repeat with w in windows
    repeat with t in tabs of w
      if URL of t starts with "https://meet.google.com/" then close t
    end repeat
  end repeat
end tell
"""

# Full screen (Ctrl-Cmd-F) needs Accessibility permission for Terminal; without it the window
# just stays a window.
FULL_SCREEN = """
tell application "System Events" to keystroke "f" using {command down, control down}
"""

# What the Meet tab shows. Meet's buttons carry stable aria-labels ("Turn off microphone (⌘ + d)",
# "Leave call"); match on those rather than on its generated class names.
STATE_JS = r"""
(() => {
  const all = [...document.querySelectorAll('button, [role=button], [role=menuitem], [aria-label]')]
    .filter(e => !e.getClientRects || e.getClientRects().length);
  const labels = (e) => [e.getAttribute('aria-label'), e.getAttribute('title'), e.innerText].filter(Boolean).map(s => s.trim());
  const matches = (e, re) => labels(e).some(s => re.test(s));
  const find = (re) => all.find(e => matches(e, re));
  // Name every provider: an unknown host is no provider, never another one by elimination.
  const host = location.hostname || '';
  const provider = host === 'meet.google.com' ? 'meet'
    : /(^|\.)zoom\.(us|com)$/.test(host) ? 'zoom'
    : /(^|\.)webex\.com$/.test(host) ? 'webex'
    : /^teams\.(microsoft\.com|live\.com|cloud\.microsoft)$/.test(host) ? 'teams' : null;
  const leave = find(/^(leave call|leave meeting|leave|hang up)(?:$|[ (])/i)
    || (provider === "zoom" && find(/^(end|end meeting)$/i));
  const mic = find(/^(turn (on|off) microphone|mute(?: microphone| my audio| my microphone)?|unmute(?: microphone| my audio| my microphone)?)(?:$|[ (])/i);
  const cam = find(/^(turn (on|off) camera|turn camera (on|off)|start(?: my)? video|stop(?: my)? video|turn (on|off) video)(?:$|[ (])/i);
  const text = document.body ? document.body.innerText : '';
  // Meet and Teams lobbies, Zoom's waiting room ("the meeting host will let you in soon") and
  // Webex's lobby ("you're in the lobby", "the meeting hasn't started").
  const waiting = /asking to join|waiting for (the host|someone)|waiting for host to (?:start|let you in)|waiting in the lobby|someone will let you in|wait for the host to start|you'll join when|please wait until a meeting host brings you into the call|host will let you in|you(?:['’]re| are) in the (?:waiting room|(?:meeting )?lobby)|meeting has(?:n['’]t| not) started/i.test(text);
  // Join screens that need a person, not the room: checked only before the room is in a call.
  const field = (re) => [...document.querySelectorAll('input')].some(e =>
    (!e.getClientRects || e.getClientRects().length) && !e.value
    && (re.test([e.getAttribute('placeholder'), e.getAttribute('aria-label')].filter(Boolean).join(' ')) || e.id === 'input-for-pwd'));
  return JSON.stringify({
    provider,
    can_end: provider === "zoom" && !!find(/^(end|end meeting)$/i),
    needs_host: /assign a new host/i.test(text),
    // Zoom's "Meeting Passcode", Webex's "meeting password": never guessed.
    needs_passcode: !leave && (/enter (?:the )?meeting (?:passcode|password)|(?:passcode|password) (?:is )?(?:wrong|incorrect|invalid)|(?:wrong|incorrect|invalid) (?:meeting )?(?:passcode|password)/i.test(text)
      || field(/^(?:meeting )?passcode$|^meeting password$/i)),
    // A Webex site that requires guests' email (Webex asks only then). The room joins as itself,
    // by name, and gives no email, so this needs the host.
    needs_email: !leave && provider === 'webex' && [...document.querySelectorAll('input')].some(e =>
      (!e.getClientRects || e.getClientRects().length) && !e.value
      && (e.type === 'email' || /email/i.test([e.getAttribute('placeholder'), e.getAttribute('aria-label')].filter(Boolean).join(' ')))
      && !/optional/i.test([e.getAttribute('placeholder'), e.getAttribute('aria-label')].filter(Boolean).join(' '))),
    // The host turned off the web client: only the desktop app could join.
    browser_blocked: !leave && /(?:doesn['’]t|does not|can['’]t|cannot) (?:support |allow )?(?:joining |join )?(?:this meeting )?from (?:a |your |the )?(?:web ?browser|browser|web client|web app|web)\b|join(?:ing)? from (?:a |your |the )?(?:web ?browser|browser|web) (?:is|has been) (?:disabled|turned off|not (?:allowed|supported|available))|(?:disabled|turned off) (?:the option to )?join(?:ing)? from (?:a |your |the )?(?:web ?browser|browser|web)/i.test(text),
    needs_end_other: /meeting that is currently in-progress|end it to start a new meeting/i.test(text),
    error: /meeting that is currently in-progress|end it to start a new meeting/i.test(text)
      ? 'This Zoom account already hosts another meeting. End that meeting in Zoom before starting a new one.' : undefined,
    sharing: [...document.querySelectorAll('button, [role=button], [role=menuitem]')].some(e =>
      (!e.getClientRects || e.getClientRects().length) &&
      /^(stop sharing|stop share)(?:$|[ (])/i.test((e.getAttribute('aria-label') || e.innerText || '').trim())),
    in_call: !!leave && !waiting,
    ended: !leave && !waiting && /(?:you(?: have|'ve)? left (?:the )?(?:meeting|call)|meeting has (?:already )?(?:been )?ended|host has ended (?:this |the )?meeting|call has ended)/i.test(text),
    waiting,
    // Teams' light meetings when it can't put the room in the meeting it was sent to.
    denied: /denied your request|you can't join (?:this|the) (?:video )?call|sorry, we couldn['’]t connect you/i.test(text),
    missing: /we can't find this meeting|this meeting (?:id|link) (?:is(?:n['’]t| not) valid|is invalid)|invalid meeting id|check your meeting code|meeting has (?:already )?ended|meeting (?:doesn['’]t|does not) exist|(?:can['’]t|couldn['’]t) find (?:this|that|the) meeting|meeting number is (?:not valid|invalid)/i.test(text),
    verification: /automated bots aren't allowed to join|verify (?:that )?you(?:'re| are) (?:a )?human/i.test(text),
    signin: !leave && /sign in to join (?:the |this )?(?:meeting|webinar)|(?:meeting|webinar) is for authori[sz]ed attendees only|only (?:authenticated|signed[- ]in) (?:users|attendees|participants)|(?:meeting|host) requires (?:you to )?(?:sign(?:ing)? in|authentication)|sign in to (?:zoom|webex) to join|you (?:must|need to) sign in to join/i.test(text),
    muted: mic ? matches(mic, /turn on microphone|^unmute/i) : null,
    camera_off: cam ? matches(cam, /turn on camera|turn camera on|^start(?: my)? video|turn on video/i) : null,
    title: document.title.replace(/^Meet\s*[-–]\s*/, ''),
    code: provider === 'meet' || provider === 'zoom' ? location.pathname.replace(/^\//, '') : '',
  });
})()
"""

# On Meet's lobby: press Join now / Ask to join (or Join here too, when the same account is already
# in the call on a laptop).
JOIN_JS = r"""
(() => {
  const btns = [...document.querySelectorAll('button, [role=button], [role=menuitem]')];
  const name = (b) => (b.innerText || b.getAttribute('aria-label') || '').trim();
  if (btns.some((b) => /^leave call/i.test(b.getAttribute('aria-label') || ''))) return 'in';
  const join = btns.find((b) => /^(join now|ask to join|join here too|join anyway|switch here)$/i.test(name(b)));
  if (join) { join.click(); return 'clicked: ' + name(join); }
  return 'waiting';
})()
"""

# Only the guest-name field: signed-in Google account names are managed by Google.
GUEST_NAME_JS = r"""
(() => {
  const e = [...document.querySelectorAll('input')].find(e =>
    /^(your name|name)$/i.test(e.getAttribute('placeholder') || e.getAttribute('aria-label') || '')
    && !e.disabled && e.getClientRects().length);
  if (!e) return 'missing';
  const r = e.getBoundingClientRect();
  return JSON.stringify({x:r.x+r.width/2, y:r.y+r.height/2, value:e.value});
})()
"""

CLICK_JS = r"""
((re) => {
  const el = [...document.querySelectorAll('[aria-label]')].find((e) => new RegExp(re, 'i').test(e.getAttribute('aria-label') || ''));
  if (!el) return 'missing';
  el.click();
  return 'clicked';
})(%s)
"""


class ChromeMeet:
    """The real thing: Google Chrome on this Mac."""

    display_name = ""
    providers = ("meet",)

    def _osa(self, script: str, *args: str, timeout: float = 20) -> str:
        r = subprocess.run(
            ["osascript", "-e", script, *args], capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0:
            err = r.stderr.strip()
            if (
                "JavaScript through AppleScript is turned off" in err
                or "Executing JavaScript" in err
            ):
                raise RuntimeError(
                    "In Chrome, turn on View → Developer → Allow JavaScript from Apple Events"
                )
            if "Not authorized" in err or "-1743" in err:
                raise RuntimeError(
                    "Allow Terminal to control Google Chrome (System Settings → Privacy → Automation)"
                )
            raise RuntimeError(err or "AppleScript failed")
        return r.stdout.strip()

    def js(self, code: str) -> str:
        return self._osa(RUN_IN_MEET, code)

    def state(self) -> dict:
        try:
            out = self.js(STATE_JS)
        except Exception as e:  # noqa: BLE001 - report it, keep running
            return {"in_call": False, "error": str(e)[:200]}
        if out == "NO_TAB":
            return {"in_call": False}
        try:
            return json.loads(out)
        except ValueError:
            return {"in_call": False}

    def open(self, url: str) -> None:
        if url.startswith(MEET):
            self._osa(CLOSE_MEET)  # one meeting at a time
            self._osa(OPEN_WINDOW, url)
            try:
                self._osa(FULL_SCREEN, timeout=5)
            except Exception:  # noqa: BLE001 - no Accessibility permission: stay windowed
                pass
        else:
            subprocess.run(["open", url], check=False)  # a Zoom link opens the Zoom app

    def prepare_guest_name(self) -> None:
        if not self.display_name:
            return
        self.js(
            r"""(() => {
          const name = %s;
          const e = [...document.querySelectorAll('input')].find(e =>
            /^(your name|name)$/i.test(e.getAttribute('placeholder') || e.getAttribute('aria-label') || '')
            && !e.disabled && e.getClientRects().length);
          if (!e || e.value === name) return;
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, name);
          e.dispatchEvent(new Event('input', {bubbles:true}));
          e.dispatchEvent(new Event('change', {bubbles:true}));
        })()"""
            % json.dumps(self.display_name)
        )

    def press_join(self) -> str:
        return self.js(JOIN_JS)

    def click(self, aria_regex: str) -> str:
        return self.js(CLICK_JS % json.dumps(aria_regex))

    def toggle(self, which: str) -> str:
        pattern = (
            r"^(turn (on|off) microphone|mute(?: microphone| my audio| my microphone)?|unmute(?: microphone| my audio| my microphone)?)(?:$|[ (])"
            if which == "microphone"
            else r"^(turn (on|off) camera|turn camera (on|off)|start(?: my)? video|stop(?: my)? video|turn (on|off) video)(?:$|[ (])"
        )
        return "sent" if self.click(pattern) == "clicked" else "NO_TAB"

    def close(self) -> None:
        self._osa(CLOSE_MEET)

    def volume(self) -> int | None:
        try:
            return int(self._osa("output volume of (get volume settings)"))
        except Exception:  # noqa: BLE001
            return None

    def set_volume(self, level: int) -> None:
        self._osa(f"set volume output volume {int(level)}")

    def notify(self, text: str) -> None:
        self._osa(
            'on run argv\ndisplay notification (item 1 of argv) with title "Xos"\nend run', text
        )


class DevToolsMeet(ChromeMeet):
    """Trusted input scoped to an isolated room Chrome profile's loopback DevTools port."""

    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self.active = None

    def _http(self, path: str, method: str = "GET"):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(self.base + path, method=method), timeout=5
            ) as r:
                payload = r.read()
                try:
                    return json.loads(payload)
                except ValueError:
                    return payload.decode()
        except OSError as e:
            raise RuntimeError(
                "Open the dedicated Xos Chrome profile with its local debugging port"
            ) from e

    def _tabs(self):
        return [
            t
            for t in self._http("/json/list")
            if t.get("type") == "page" and t.get("url", "").startswith(MEET)
        ]

    def _tab(self):
        tabs = self._tabs()
        return next((t for t in tabs if t["id"] == self.active), tabs[0] if tabs else None)

    def _call(self, tab: dict, method: str, params: dict | None = None):
        try:
            from websockets.sync.client import connect
        except ImportError as e:
            raise RuntimeError(
                "Run this controller with: uv run --with websockets xos-room.py …"
            ) from e
        # Connect only to this explicitly selected loopback browser, never a URL supplied by a meeting.
        ws = tab["webSocketDebuggerUrl"]
        parsed = urllib.parse.urlsplit(ws)
        if (
            parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.port != urllib.parse.urlsplit(self.base).port
        ):
            raise RuntimeError("Chrome returned a non-local debugging address")
        with connect(ws, open_timeout=5, close_timeout=1) as connection:
            connection.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                response = json.loads(connection.recv(timeout=10))
                if response.get("id") != 1:
                    continue
                if "error" in response:
                    raise RuntimeError(
                        "Chrome control failed: "
                        + response["error"].get("message", "unknown error")
                    )
                return response.get("result", {})

    def js(self, code: str) -> str:
        tab = self._tab()
        if not tab:
            return "NO_TAB"
        out = self._call(tab, "Runtime.evaluate", {"expression": code, "returnByValue": True})
        if out.get("exceptionDetails"):
            raise RuntimeError("Could not inspect the Meet tab")
        return str(out.get("result", {}).get("value", ""))

    def _press(self, box: dict) -> None:
        tab = self._tab()
        if not tab:
            raise RuntimeError("No Meet tab")
        self._call(tab, "Page.bringToFront")
        for kind in ("mousePressed", "mouseReleased"):
            self._call(
                tab,
                "Input.dispatchMouseEvent",
                {
                    "type": kind,
                    "x": box["x"],
                    "y": box["y"],
                    "button": "left",
                    "clickCount": 1,
                },
            )

    def click(self, aria_regex: str) -> str:
        out = self.js(
            r"""(() => {
          const re = new RegExp(%s, 'i');
          const e = [...document.querySelectorAll('button[aria-label], [role=button][aria-label]')]
            .find(e => re.test(e.getAttribute('aria-label') || '') && !e.disabled && e.getClientRects().length);
          if (!e) return 'missing';
          const r = e.getBoundingClientRect();
          return JSON.stringify({x:r.x+r.width/2, y:r.y+r.height/2});
        })()"""
            % json.dumps(aria_regex)
        )
        if out in ("missing", "NO_TAB"):
            return "missing"
        self._press(json.loads(out))
        return "clicked"

    def prepare_guest_name(self) -> None:
        if not self.display_name:
            return
        out = self.js(GUEST_NAME_JS)
        if out in ("missing", "NO_TAB"):
            return
        box = json.loads(out)
        if box["value"] == self.display_name:
            return
        self._press(box)
        tab = self._tab()
        # Select and replace inside this input/tab, without desktop-global keystrokes.
        for kind in ("keyDown", "keyUp"):
            self._call(
                tab,
                "Input.dispatchKeyEvent",
                {
                    "type": kind,
                    "key": "a",
                    "code": "KeyA",
                    "modifiers": 4,
                    "windowsVirtualKeyCode": 65,
                },
            )
        self._call(tab, "Input.insertText", {"text": self.display_name})
        confirmed = self.js(GUEST_NAME_JS)
        if (
            confirmed in ("missing", "NO_TAB")
            or json.loads(confirmed)["value"] != self.display_name
        ):
            raise RuntimeError("Meet did not accept the room name; check the room browser")

    def press_join(self) -> str:
        out = self.js(r"""(() => {
          const buttons = [...document.querySelectorAll('button, [role=button], [role=menuitem]')];
          if (buttons.some(b => /^leave call/i.test(b.getAttribute('aria-label') || ''))) return 'in';
          const name = b => (b.innerText || b.getAttribute('aria-label') || '').trim();
          const b = buttons.find(b => /^(join now|ask to join|join here too|join anyway|switch here)$/i.test(name(b)) && !b.disabled && b.getClientRects().length);
          if (!b) return 'waiting';
          const r = b.getBoundingClientRect();
          return JSON.stringify({x:r.x+r.width/2, y:r.y+r.height/2, name:name(b)});
        })()""")
        if out in ("in", "waiting", "NO_TAB"):
            return out
        box = json.loads(out)
        self._press(box)
        return "clicked: " + box["name"]

    def open(self, url: str) -> None:
        if not url.startswith(MEET):
            subprocess.run(["open", url], check=False)
            return
        self.close()
        self.active = self._http("/json/new?" + urllib.parse.quote(url, safe=":/"), "PUT")["id"]

    def close(self) -> None:
        for tab in self._tabs():
            self._http("/json/close/" + tab["id"])
        self.active = None


class BrowserLoading(RuntimeError):
    """A newly opened, owned room tab has not finished navigation."""


class GuestVerificationRequired(RuntimeError):
    """Leave the provider check visible for a person at the room screen."""


class CopperMeet(ChromeMeet):
    """Copper's supported CLI, scoped to one explicitly selected room tab."""

    providers = ("meet", "zoom")

    def __init__(self, tab: str, cli: str):
        self.tab = tab
        self.home_tab = tab
        self.cli = cli
        self.provider = "meet"
        self.invitation = ""
        self.navigation_until = 0.0

    def _tool(self, tool: str, args: dict) -> str:
        result = subprocess.run(
            [self.cli, "--json", "call", tool, json.dumps({**args, "tab": self.tab})],
            capture_output=True,
            text=True,
            timeout=25,
        )
        try:
            payload = json.loads(result.stdout)
        except ValueError as e:
            raise RuntimeError("Copper control unavailable; check Copper is running") from e
        text = "\n".join(
            c.get("text", "") for c in payload.get("content", []) if c.get("type") == "text"
        )
        if result.returncode or payload.get("isError"):
            raise RuntimeError("Copper control failed: " + text[:200])
        return text

    def _require_meet(self) -> None:
        url = self._tool("browser_evaluate", {"function": "() => location.href"})
        if url in {"about:blank", "", "null"} and time.monotonic() < self.navigation_until:
            raise BrowserLoading("Meeting page is still loading")
        provider = provider_of(url)
        if not provider:
            if url.startswith("https://accounts.google.com/"):
                raise RuntimeError("Complete Google sign-in in the room's Copper tab")
            if url.startswith(("https://login.microsoftonline.com/", "https://login.live.com/")):
                raise RuntimeError("Complete Microsoft sign-in in the room's Copper tab")
            raise RuntimeError(
                "The room Copper tab changed; reopen Google Meet, Teams, Webex or Zoom in that tab"
            )
        self.provider = provider

    def js(self, code: str) -> str:
        self._require_meet()
        return self._tool("browser_evaluate", {"function": "() => (" + code.strip() + ")"})

    def open(self, url: str) -> None:
        provider = provider_of(url)
        if not provider:
            raise RuntimeError("Only Meet, Teams, Webex and Zoom links are allowed")
        self._require_meet()
        if provider == "zoom":
            url = re.sub(r"/(?:j|w|s)/(\d{9,11})", r"/wc/join/\1", url)
        self.provider = provider
        self.invitation = (
            url
            if re.search(r"/(?:wc/join|meet|l/meetup-join)/", url)
            or re.search(r"/[a-z]{3}-[a-z]{4}-[a-z]{3}$", url)
            else ""
        )
        self.navigation_until = time.monotonic() + 20
        self._tool("browser_navigate", {"url": url})

    def state(self) -> dict:
        state = super().state()
        if state.get("in_call"):
            url = self.invitation or self._tool(
                "browser_evaluate", {"function": "() => location.href"}
            )
            u = urllib.parse.urlsplit(url)
            if provider_of(url) == "webex":
                # The invitation the server checked (Webex links have several shapes); never
                # the web app's own page, which carries session state.
                if self.invitation:
                    state["join_url"] = self.invitation
            elif provider_of(url) and (
                re.fullmatch(r"/[a-z]{3}-[a-z]{4}-[a-z]{3}", u.path)
                or re.fullmatch(r"/(?:wc/join|j)/\d{9,11}", u.path)
                or u.path.startswith(("/meet/", "/l/meetup-join/"))
            ):
                query = urllib.parse.parse_qs(u.query)
                query = {k: v for k, v in query.items() if k in {"pwd", "p", "context"}}
                state["join_url"] = urllib.parse.urlunsplit(
                    ("https", u.netloc, u.path, urllib.parse.urlencode(query, doseq=True), "")
                )
        return state

    def _meeting_tabs(self, provider: str) -> dict[str, str]:
        listing = self._tool("browser_tabs", {"action": "list"})
        return {
            tab: url
            for url, tab in re.findall(r"\((https://[^\s]+)\) \{tab ([\w-]+)\}", listing)
            if provider_of(url) == provider
        }

    def _follow_opened_meeting(self, provider: str, before: dict[str, str]) -> None:
        # Only a tab newly created by this action is eligible. Never adopt an existing call.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            after = self._meeting_tabs(provider)
            created = [(tab, url) for tab, url in after.items() if tab not in before]
            if len(created) > 1:
                raise RuntimeError("Multiple meeting tabs opened; select the room tab explicitly")
            if len(created) == 1:
                tab, url = created[0]
                path = urllib.parse.urlsplit(url).path
                if provider == "zoom" and not re.fullmatch(r"/wc/\d{9,11}/(?:start|join)", path):
                    # Zoom's popup redirects through a loading URL before its meeting path.
                    # Keep observing this new tab; do not adopt arbitrary provider pages.
                    time.sleep(0.2)
                    continue
                self.tab = tab
                self.navigation_until = time.monotonic() + 20
                return
            state = self.state()
            if state.get("needs_end_other"):
                raise RuntimeError("This Zoom account already hosts another meeting. End it in Zoom before starting a new one.")
            if state.get("in_call"):
                return
            time.sleep(0.4)
        raise RuntimeError("The provider did not open its room meeting tab; check the room browser")

    def _wait_button(self, pattern: str, seconds: float = 5, **kwargs) -> str:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = self._button(pattern, **kwargs)
            if result != "missing":
                return result
            time.sleep(0.2)
        return "missing"

    def connect_audio(self) -> None:
        """Zoom guest entry and computer audio are separate confirmations."""
        if self.provider != "zoom":
            return
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            st = self.state()
            if st.get("muted") is not None:
                return
            if st.get("waiting"):
                return
            # The web client can open the audio chooser automatically, or leave a toolbar
            # Join Audio button. Use only the computer option, never the phone/dial-in path.
            if self._button(r"^join (?:audio by computer|with computer audio)$") != "clicked":
                self._button(r"^join audio$")
            time.sleep(0.3)
        raise RuntimeError("Zoom joined, but computer audio did not connect; check the room screen")

    def apply_room_name(self) -> None:
        if self.provider != "zoom" or not self.display_name:
            return
        pane = self.js(r"""(() => {
          return [...document.querySelectorAll('button[aria-label]')].some(e =>
            /^close the manage participants/i.test(e.getAttribute('aria-label') || '')
            && e.getClientRects().length) ? 'open' : 'closed';
        })()""")
        opened = (
            pane != "open"
            and self._wait_button(r"^open the manage participants", seconds=8) == "clicked"
        )
        try:
            if self._wait_button(r"^more options$", self_only=True) != "clicked":
                raise RuntimeError("The room's own Zoom participant options are unavailable")
            if self._wait_button(r"^rename$") != "clicked":
                raise RuntimeError("Zoom did not offer Rename for this room")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                field = self.js(r"""(() => {
                  const e = document.querySelector('input#newname');
                  return e && e.getClientRects().length ? 'ready' : 'missing';
                })()""")
                if field == "ready":
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("Zoom's participant name field is unavailable")
            self._tool("browser_type", {"selector": "input#newname", "text": self.display_name})
            if self._wait_button(r"^change$") != "clicked":
                raise RuntimeError("Zoom did not accept the room name")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                confirmed = self.js(
                    r"""(() => {
                  const want = %s;
                  return [...document.querySelectorAll('.participants-item__display-name')]
                    .some(e => e.innerText.trim() === want) ? 'confirmed' : 'missing';
                })()"""
                    % json.dumps(self.display_name)
                )
                if confirmed == "confirmed":
                    return
                time.sleep(0.2)
            raise RuntimeError("Zoom has not confirmed the room participant name")
        finally:
            self._tool("browser_press_key", {"key": "Escape"})
            if opened:
                self._button(r"^(open|close) the manage participants")

    def capture_invitation(self) -> None:
        if self.provider != "zoom" or self.invitation:
            return
        if self._button(r"^meeting information$") != "clicked":
            return
        try:
            url = self.js(r"""(() => {
              const text = document.body ? document.body.innerText : '';
              return (text.match(/https:\/\/(?:[\w-]+\.)?zoom\.(?:us|com)\/j\/\d{9,11}(?:\?pwd=[^\s]+)?/) || [''])[0];
            })()""")
            u = urllib.parse.urlsplit(url)
            if provider_of(url) == "zoom" and re.fullmatch(r"/j/\d{9,11}", u.path):
                query = urllib.parse.parse_qs(u.query)
                pwd = query.get("pwd", [""])[0]
                self.invitation = urllib.parse.urlunsplit(
                    (
                        "https",
                        u.netloc,
                        u.path,
                        urllib.parse.urlencode({"pwd": pwd}) if pwd else "",
                        "",
                    )
                )
        finally:
            self._tool("browser_press_key", {"key": "Escape"})

    def start(self, provider: str) -> None:
        if provider in GUEST_ONLY:
            raise RuntimeError("The room joins Webex invitations as a guest; it can't start a Webex meeting")
        if provider == "meet":
            self.open(MEET + "new")
            return
        self.open(PROVIDER_HOME[provider])
        deadline = time.monotonic() + 20
        pattern = r"^(new meeting|start a meeting)$" if provider == "zoom" else r"^meet now$"
        while time.monotonic() < deadline:
            before = self._meeting_tabs(provider)
            if self._button(pattern, join=True, keyboard=False) == "clicked":
                self._follow_opened_meeting(provider, before)
                return
            # Teams exposes Meet now in its Calendar, never start a chat/group call.
            if provider == "teams":
                self._button(r"^calendar$", join=True)
            time.sleep(1)
        raise RuntimeError(
            "Open the room browser and complete " + provider.title() + " setup to start meetings"
        )

    def _button(
        self, pattern: str, join: bool = False, keyboard: bool = True, self_only: bool = False
    ) -> str:
        selector = self.js(
            r"""(() => {
          const buttons = [...document.querySelectorAll(%s)];
          if (%s && buttons.some(b => /^(leave call|leave meeting|hang up|end)$/i.test(b.getAttribute('aria-label') || ''))) return 'in';
          const re = new RegExp(%s, 'i');
          const ownOnly = %s;
          // Zoom's public header contains a generic Join link while its web client loads.
          // Only explicit browser-continuation links belong to this room join flow.
          const allowedLink = e => e.tagName !== 'A'
            || /^(join from (?:your |this |the )?browser\.?|continue on this browser|use web app|join (?:from|with|using) the web app)$/i.test((e.innerText || e.getAttribute('aria-label') || '').trim());
          const isSelf = e => {
            for (let p = e.parentElement, depth = 0; p && depth < 10; p = p.parentElement, depth++) {
              if (/\([^)]*\bme\)/i.test(p.innerText || '')) {
                return p.querySelectorAll('[aria-label="More options"]').length === 1;
              }
            }
            return false;
          };
          // Zoom's Join stays a live <button> while it is greyed out (no name yet) or already
          // joining: its class says so (zm-btn--disabled), so a second press never goes out.
          const off = b => b.disabled || b.getAttribute('aria-disabled') === 'true'
            || /(?:^|\s)[\w-]+--disabled(?:\s|$)/.test(b.getAttribute('class') || '');
          const e = buttons.find(b => re.test(%s) && allowedLink(b) && !off(b) && b.getClientRects().length
            && (!ownOnly || isSelf(b)));
          if (!e) return 'missing';
          const parts = [];
          for (let n = e; n && n !== document.body; n = n.parentElement) {
            parts.unshift(n.tagName.toLowerCase() + ':nth-child(' + ([...n.parentElement.children].indexOf(n) + 1) + ')');
          }
          return 'body > ' + parts.join(' > ');
        })()"""
            % (
                json.dumps("button, [role=button], [role=menuitem]" + (", a[href]" if join else "")),
                "true" if join else "false",
                json.dumps(pattern),
                "true" if self_only else "false",
                "(b.getAttribute('aria-label') || b.innerText || '').trim()",
            )
        )
        if selector in ("missing", "in"):
            return selector
        if self.provider == "zoom" and keyboard:
            # Zoom ignores Copper's DOM click for media controls. Focus the exact visible
            # button, then activate it through Copper's native keyboard input once.
            focused = self._tool(
                "browser_evaluate",
                {
                    "function": "() => { const e = document.querySelector("
                    + json.dumps(selector)
                    + "); if (!e || e.disabled || !e.getClientRects().length) return 'missing'; e.focus(); return document.activeElement === e ? 'focused' : 'missing'; }"
                },
            )
            if focused != "focused":
                self._tool("browser_press_key", {"key": "Escape"})
                focused = self._tool(
                    "browser_evaluate",
                    {
                        "function": "() => { const e = document.querySelector("
                        + json.dumps(selector)
                        + "); if (!e || e.disabled || !e.getClientRects().length) return 'missing'; e.focus(); return document.activeElement === e ? 'focused' : 'missing'; }"
                    },
                )
            if focused != "focused":
                raise RuntimeError("Zoom's control could not receive keyboard focus")
            self._tool("browser_press_key", {"key": "Enter"})
        else:
            self._tool("browser_click", {"selector": selector})
        return "clicked"

    def click(self, aria_regex: str) -> str:
        return self._button(aria_regex)

    def _zoom_preview_loading(self) -> bool:
        """Zoom's join screen first gets the camera and microphone for its preview (a spinner in
        its Mute and Stop Video). Join pressed before that ends leaves both behind ("Enable camera
        access in your browser's address bar"), so wait for it, but never more than 20 seconds."""
        loading = self.js(r"""(() => [...document.querySelectorAll(
          '#preview-audio-control-button, #preview-video-control-button')].some(e =>
            e.getClientRects().length && e.querySelector('.spinner')) ? 'loading' : 'ready')()""")
        if loading != "loading":
            self.preview_since = 0.0
            return False
        now = time.monotonic()
        self.preview_since = getattr(self, "preview_since", 0.0) or now
        return now - self.preview_since < 20

    def press_join(self) -> str:
        if self.provider == "zoom" and self._zoom_preview_loading():
            return "waiting"
        result = self._button(
            r"^(join now|ask to join|join here too|join anyway|switch here|join|join meeting|start meeting|join from (?:your )?browser|continue on this browser|use web app)$",
            join=True,
        )
        return "waiting" if result == "missing" else result

    def prepare_guest_name(self) -> None:
        if not self.display_name:
            return
        out = self.js(GUEST_NAME_JS)
        if out == "missing" or json.loads(out)["value"] == self.display_name:
            return
        self._tool(
            "browser_type",
            {
                "selector": 'input[placeholder="Your name"], input[aria-label="Your name"], input[placeholder="Name"], input[aria-label="Name"]',
                "text": self.display_name,
            },
        )
        confirmed = self.js(GUEST_NAME_JS)
        if confirmed == "missing" or json.loads(confirmed)["value"] != self.display_name:
            raise RuntimeError("Meet did not accept the room name; check Copper")

    def leave(self, end: bool = False) -> None:
        if self.provider == "zoom":
            if self._button(r"^(end|end meeting)$") != "clicked":
                raise RuntimeError("Zoom's Leave control is unavailable")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if (
                    self._button(r"^end meeting for all$" if end else r"^leave meeting$")
                    == "clicked"
                ):
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("Zoom did not offer Leave Meeting; check the room browser")
        else:
            self._button(r"^(leave call|leave meeting|leave|hang up)(?:$|[ (])")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = self.state()
            if state.get("needs_host"):
                raise RuntimeError(
                    "Zoom needs a new host: choose the next host on the room Mac, or use End for everyone"
                )
            if not state.get("in_call") and not state.get("waiting") and not state.get("error"):
                return
            time.sleep(0.2)
        raise RuntimeError("The meeting has not confirmed leaving; check the room browser")

    def close(self) -> None:
        self._require_meet()
        if self.tab != self.home_tab:
            self._tool("browser_close", {})
            self.tab = self.home_tab
        self._tool("browser_navigate", {"url": PROVIDER_HOME[self.provider]})


class SimulatedMeet:
    """No Chrome: keeps the state a real meeting would have (tests and demos)."""

    providers = ("meet", "zoom", "teams", "webex")

    def __init__(self):
        self.s = {"in_call": False}
        self.vol = 50

    def state(self) -> dict:
        return dict(self.s)

    def open(self, url: str) -> None:
        self.s = {"in_call": False, "lobby": True, "code": url.rsplit("/", 1)[-1], "title": url}

    def prepare_guest_name(self) -> None:
        pass

    def press_join(self) -> str:
        if self.s.get("lobby"):
            self.s = {
                "in_call": True,
                "muted": False,
                "camera_off": False,
                "code": self.s["code"],
                "title": self.s["title"],
            }
            return "clicked: Join now"
        return "in" if self.s.get("in_call") else "waiting"

    def click(self, aria_regex: str) -> str:
        if not self.s.get("in_call"):
            return "missing"
        if "leave" in aria_regex:
            self.s = {"in_call": False}
        elif "microphone" in aria_regex:
            self.s["muted"] = not self.s["muted"]
        elif "camera" in aria_regex:
            self.s["camera_off"] = not self.s["camera_off"]
        return "clicked"

    def toggle(self, which: str) -> str:
        return "sent" if self.click(which) == "clicked" else "NO_TAB"

    def close(self) -> None:
        self.s = {"in_call": False}

    def volume(self) -> int | None:
        return self.vol

    def set_volume(self, level: int) -> None:
        self.vol = level

    def notify(self, text: str) -> None:
        log("notification:", text)


# ---------- the room computer ----------


class Room:
    def __init__(
        self, server: str, room: str, meet, device_id: str, display_name: str | None = None
    ):
        self.server = server.rstrip("/")
        self.room = room
        self.meet = meet
        self.meet.display_name = (
            display_name or room.replace("-", " ").replace("_", " ").title() + " Room"
        )
        self.id = device_id
        self.lock = threading.Lock()
        self.jobs: queue.Queue = queue.Queue()
        self.beat_now = threading.Event()
        self.audio_pending = False
        self.ended_pending = False

    # -- talking to the Xos server --

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.server + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")

    def meeting_state(self) -> dict:
        """Finish the one requested audio connection after a host admits the room."""
        st = self.meet.state()
        if st.get("ended") and not st.get("in_call") and not st.get("waiting"):
            try:
                self.meet.close()
            except (RuntimeError, OSError) as exc:
                # Keep heartbeats alive and retry on the next beat if the browser is still closing.
                return {**st, "error": f"Returning to the room screen: {exc}"}
            self.audio_pending = False
            st = {**self.meet.state(), "ended": True}
        if self.ended_pending and not st.get("in_call") and not st.get("waiting"):
            st["ended"] = True
            self.ended_pending = False
        if self.audio_pending and st.get("in_call"):
            try:
                self.meet.connect_audio()
            except RuntimeError as e:
                st["error"] = str(e)
            else:
                self.audio_pending = False
                st = self.meet.state()
        return st

    def heartbeat(self) -> None:
        with self.lock:
            meeting = self.meeting_state()
            vol = self.meet.volume()
        native = {
            "app": VERSION,
            "model": platform.node() or "Mac",
            "os": f"macOS {platform.mac_ver()[0]}".strip(),
            "meeting": meeting,
            "meeting_providers": list(self.meet.providers),
            "volume": vol,
        }
        try:
            self._post(
                "/api/devices/heartbeat",
                {
                    "id": self.id,
                    "client": f"macroom-{VERSION}",
                    "role": "roompc",
                    "arg": self.room,
                    "label": f"Room computer · {self.room}",
                    "native": native,
                },
            )
        except Exception as e:  # noqa: BLE001
            log("heartbeat failed:", e)

    def ack(self, cmd_id: str, ok: bool, result: str) -> None:
        try:
            self._post(
                f"/api/devices/{self.id}/commands/{cmd_id}/ack", {"ok": ok, "result": result}
            )
        except Exception as e:  # noqa: BLE001
            log("ack failed:", e)

    # -- what the iPad asks --

    def _wait_in_call(self, seconds: float) -> str:
        """Press Join on the lobby until Meet lets the room in, or asks the host to."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                with self.lock:
                    self.meet.prepare_guest_name()
                    self.meet.press_join()
                    st = self.meet.state()
            except BrowserLoading:
                time.sleep(0.4)
                continue
            if st.get("in_call"):
                if hasattr(self.meet, "connect_audio"):
                    with self.lock:
                        self.meet.connect_audio()
                        self.audio_pending = False
                if hasattr(self.meet, "apply_room_name"):
                    try:
                        with self.lock:
                            self.meet.apply_room_name()
                    except RuntimeError:
                        log("Meeting joined; room participant name could not be applied")
                if hasattr(self.meet, "capture_invitation"):
                    try:
                        with self.lock:
                            self.meet.capture_invitation()
                    except RuntimeError:
                        log("Meeting joined; invitation link is not available yet")
                return "in the meeting"
            if st.get("needs_end_other"):
                raise RuntimeError(
                    "This Zoom account already hosts another meeting. End it in Zoom before starting a new one."
                )
            if st.get("missing"):
                raise RuntimeError(
                    "The meeting service can't find that meeting: check the link, or it has ended"
                )
            if st.get("needs_passcode"):
                raise RuntimeError(
                    "This Webex meeting needs its password. Ask the host for an invitation link that includes it."
                    if st.get("provider") == "webex"
                    else "This meeting needs its passcode. Join again from the room iPad and enter the passcode."
                )
            if st.get("needs_email"):
                raise RuntimeError(
                    "This Webex meeting asks guests for an email address, and the room joins by its "
                    "name only. Ask the host to let guests join without an email, or to invite the "
                    "room with a Meet, Teams or Zoom link."
                )
            if st.get("browser_blocked"):
                name, other = ("Webex", "Zoom") if st.get("provider") == "webex" else ("Zoom", "Webex")
                raise RuntimeError(
                    f"This {name} meeting's host doesn't allow joining from a browser. Ask them to "
                    f"allow web-client participants, or send a Meet, Teams or {other} invitation."
                )
            if st.get("verification"):
                raise GuestVerificationRequired(
                    "The meeting service is asking for human verification on the room screen. "
                    "Complete that check on the room computer to continue joining."
                )
            if st.get("signin"):
                raise RuntimeError(
                    "This invitation is asking for sign-in on the room screen. "
                    "Ask the host for a guest-access invitation to join without an account."
                )
            if st.get("denied"):
                raise RuntimeError(
                    "The meeting service rejected this join attempt. Check that the invitation "
                    "is current and the host has started the meeting, then retry."
                )
            if st.get("waiting"):
                return "asked to join; waiting for the host to let the room in"
            time.sleep(1)
        raise RuntimeError("The meeting didn't open in time; check the room browser")

    def _toggle(self, want_on: bool, which: str) -> str:
        """Mic or camera to the asked state (Meet only has toggles)."""
        with self.lock:
            st = self.meet.state()
            off = st.get("muted") if which == "microphone" else st.get("camera_off")
            if off is None:
                if st.get("in_call"):
                    raise RuntimeError(
                        f"Meeting {which} unavailable; check room browser permissions and devices"
                    )
                raise RuntimeError("not in a meeting")
            if off == (not want_on):
                return "already " + ("on" if want_on else "off")
            out = self.meet.toggle(which)
        if out != "sent":
            raise RuntimeError(f"couldn't find the meeting tab for {which}")
        deadline = time.monotonic() + 5
        field = "muted" if which == "microphone" else "camera_off"
        while time.monotonic() < deadline:
            with self.lock:
                state = self.meet.state()
            if state.get(field) == (not want_on):
                return "on" if want_on else "off"
            time.sleep(0.2)
        # Do not toggle twice: that could undo a delayed first action.
        raise RuntimeError(
            f"The meeting didn't confirm {which} {'on' if want_on else 'off'}; check the room browser"
        )

    def meeting(self, args: dict) -> str:
        action = args.get("action")
        if action in ("join", "meet_now"):
            with self.lock:
                current = self.meet.state()
            if current.get("in_call") or current.get("waiting"):
                raise RuntimeError("Leave the current meeting before joining or starting another")
            provider = args.get("provider", "meet")
            if provider not in PROVIDER_HOME:
                raise RuntimeError("Choose Meet, Teams, Webex or Zoom")
            url = MEET + "new" if action == "meet_now" else str(args.get("url", ""))
            target = provider if action == "meet_now" else provider_of(url)
            if not target:
                raise RuntimeError("only Google Meet, Teams, Webex and Zoom links")
            if target not in self.meet.providers:
                if target == "teams":
                    raise RuntimeError(
                        "Teams setup is pending: this room needs an account with Teams access"
                    )
                if target == "webex":
                    raise RuntimeError("Webex runs on the Linux room computer")
                raise RuntimeError("Zoom controls require the Copper room browser")
            self.audio_pending = hasattr(self.meet, "connect_audio")
            try:
                with self.lock:
                    if action == "meet_now" and hasattr(self.meet, "start"):
                        self.meet.start(provider)
                    elif provider != "meet" or (
                        not url.startswith(MEET) and not isinstance(self.meet, CopperMeet)
                    ):
                        raise RuntimeError("Teams and Zoom controls require the Copper room browser")
                    else:
                        self.meet.open(url)
                return self._wait_in_call(40)
            except GuestVerificationRequired:
                raise  # Keep the check visible; never solve or discard it automatically.
            except RuntimeError:
                self.audio_pending = False
                # Don't leave the TV on the meeting's error page: back to the room screen.
                try:
                    with self.lock:
                        self.meet.close()
                except Exception:  # noqa: BLE001 - the original error is the one to report
                    pass
                raise
        if action in ("leave", "end"):
            self.audio_pending = False
            with self.lock:
                before = self.meet.state()
                if before.get("in_call") and hasattr(self.meet, "leave"):
                    try:
                        self.meet.leave(end=action == "end")
                    except RuntimeError as e:
                        # A host handoff or End for everyone needs the meeting's answer; any other
                        # unconfirmed Leave is finished by closing the meeting page below.
                        if action == "end" or before.get("needs_host") or "new host" in str(e):
                            raise
                        log("Leave not confirmed, closing the meeting page:", e)
                elif not before.get("waiting"):
                    self.meet.click(r"^(leave call|leave meeting|leave|hang up)(?:$|[ (])")
                # From a lobby or waiting room there is no call to leave: closing the page
                # withdraws the request (Teams' lobby offers no Leave the room can confirm).
                self.meet.close()
                after = self.meet.state()
            if after.get("in_call") or after.get("waiting"):
                raise RuntimeError("The room browser has not confirmed leaving")
            self.ended_pending = bool(before.get("in_call"))
            return (
                "left the meeting"
                if before.get("in_call") or before.get("waiting")
                else "not in a meeting"
            )
        if action == "control_back":
            with self.lock:
                if not hasattr(self.meet, "back"):
                    raise RuntimeError("Back is unavailable on this room computer")
                return self.meet.back()
        if action == "control":
            with self.lock:
                if not hasattr(self.meet, "control"):
                    raise RuntimeError("Advanced controls are unavailable on this room computer")
                return self.meet.control(str(args.get("control", "")))
        if action in ("mute", "unmute"):
            return "microphone " + self._toggle(action == "unmute", "microphone")
        if action in ("camera_off", "camera_on"):
            return "camera " + self._toggle(action == "camera_on", "camera")
        if action in ("share_start", "share_stop"):
            with self.lock:
                state = self.meet.state()
                if not state.get("in_call"):
                    raise RuntimeError("Join a meeting before sharing")
                if action == "share_start":
                    raise RuntimeError(
                        "Open Share screen on the room controller, scan the meeting QR code "
                        "or copy its link, and present from your laptop"
                    )
                if not state.get("sharing"):
                    return "sharing stopped"
                if self.meet.click(r"^(stop sharing|stop share)(?:$|[ (])") != "clicked":
                    raise RuntimeError("The room presentation's Stop control is unavailable")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.meet.state().get("sharing") is False:
                    return "sharing stopped"
                time.sleep(0.2)
            raise RuntimeError("The browser has not confirmed sharing stopped")
        if action == "volume":
            level = max(0, min(100, int(args.get("volume", 50))))
            with self.lock:
                self.meet.set_volume(level)
            return f"volume {level}"
        raise RuntimeError(f"{action} isn't something the room computer does")

    def run(self, cmd: dict) -> None:
        kind, args = cmd.get("type"), cmd.get("args") or {}
        try:
            if kind == "meeting":
                result = self.meeting(args)
            elif kind in ("identify", "message"):
                text = args.get("text") or f"Room computer · {self.room}"
                with self.lock:
                    self.meet.notify(str(text)[:200])
                result = "shown"
            elif kind == "refresh":
                result = "ok"
            else:
                raise RuntimeError(f"{kind} isn't something the room computer does")
            log(kind, args.get("action", ""), "→", result)
            self.ack(cmd["id"], True, result)
        except Exception as e:  # noqa: BLE001
            log(kind, args.get("action", ""), "failed:", e)
            self.ack(cmd["id"], False, str(e)[:300])
        self.beat_now.set()

    # -- loops --

    def beats(self) -> None:
        while True:
            self.heartbeat()
            self.beat_now.wait(HEARTBEAT_S)
            self.beat_now.clear()

    def worker(self) -> None:
        while True:
            self.run(self.jobs.get())

    def listen(self) -> None:
        """The command stream (Server-Sent Events); reconnects for ever."""
        while True:
            try:
                req = urllib.request.Request(
                    f"{self.server}/api/devices/{self.id}/stream",
                    headers={"Accept": "text/event-stream"},
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


def device_id() -> str:
    try:
        return json.loads(STATE_FILE.read_text())["id"]
    except (OSError, ValueError, KeyError):
        host = socket.gethostname().split(".")[0].lower()
        did = (
            "mac-"
            + "".join(c if c.isalnum() else "-" for c in host)[:20]
            + "-"
            + uuid.uuid4().hex[:4]
        )
        try:
            STATE_FILE.write_text(json.dumps({"id": did}))
        except OSError:
            pass
        return did


def main() -> None:
    ap = argparse.ArgumentParser(description="Xos Room: this Mac is a meeting room's computer.")
    ap.add_argument("--room", required=True, help="the Xos room, e.g. atrium")
    ap.add_argument(
        "--display-name", help="Meet guest name (default: room title + Room, e.g. Atrium Room)"
    )
    ap.add_argument("--server", default="https://xos.example.org")
    ap.add_argument("--id", help="device id (default: kept in ~/.xos-room.json)")
    ap.add_argument("--simulate", action="store_true", help="no Chrome: simulate a meeting")
    ap.add_argument("--browser", choices=("chrome", "copper"), default="chrome")
    ap.add_argument(
        "--enable-teams",
        action="store_true",
        help="enable Teams preview after room account access is configured",
    )
    ap.add_argument("--copper-tab", help="explicit Copper room tab id from copper tabs")
    ap.add_argument(
        "--copper-cli", default="/Applications/Copper.app/Contents/Resources/bin/copper"
    )
    ap.add_argument(
        "--chrome-port", type=int, help="loopback DevTools port for a dedicated room Chrome profile"
    )
    a = ap.parse_args()
    if a.browser == "copper" and (not a.copper_tab or a.chrome_port is not None):
        ap.error("Copper needs --copper-tab and cannot use --chrome-port")
    if a.browser != "copper" and a.copper_tab:
        ap.error("--copper-tab requires --browser copper")
    if a.chrome_port is not None and not 1 <= a.chrome_port <= 65535:
        ap.error("--chrome-port must be between 1 and 65535")
    if not a.simulate and sys.platform != "darwin":
        sys.exit("Xos Room runs on a Mac (or pass --simulate).")
    if a.simulate:
        meet = SimulatedMeet()
    elif a.browser == "copper":
        meet = CopperMeet(a.copper_tab, a.copper_cli)
        if a.enable_teams:
            meet.providers = (*meet.providers, "teams")
    else:
        meet = DevToolsMeet(a.chrome_port) if a.chrome_port else ChromeMeet()
    room = Room(a.server, a.room, meet, a.id or device_id(), a.display_name)
    log(f"Xos Room {VERSION} · room {a.room} · {room.id}")
    threading.Thread(target=room.beats, daemon=True).start()
    threading.Thread(target=room.worker, daemon=True).start()
    try:
        room.listen()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
