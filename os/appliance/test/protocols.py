"""Real headed Chromium in Cage, inside the appliance test container: a meeting page's "open the
app" launch never puts a prompt on the TV.

Webex's landing page navigates to webex: to start its desktop app, and Teams' and Zoom's invitation
pages try msteams: and zoommtg:. With the room policy (AutoLaunchProtocolsFromOrigins for each
service's own sites) and the agent's xdg-open (os/appliance/no-apps), the page stays, no "Open
xdg-open?" dialog appears and no other browser starts. The same launch from
another origin still prompts, which shows this check can see a prompt. Accessibility needs a
session bus: run it under dbus-run-session. Isolated profile and output, never the room's.
"""
import importlib.util
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pyatspi
from gi.repository import Gio, GLib

spec = importlib.util.spec_from_file_location("protocol_browser", "/xos/os/appliance/xos_room_linux.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def prompts() -> list[str]:
    """Chromium's own dialogs, as accessibility sees them (they aren't in any page)."""
    found = []
    for app in pyatspi.Registry.getDesktop(0):
        try:
            if "chrom" not in (app.name or "").lower():
                continue
        except Exception:  # noqa: BLE001 - an app that went away
            continue
        stack = [(app, 0)]
        while stack:
            node, depth = stack.pop()
            try:
                if node.getRoleName() in ("dialog", "alert") and node.getState().contains(pyatspi.STATE_SHOWING):
                    found.append(node.name or "")
                if depth < 30:
                    stack.extend((child, depth + 1) for child in node if child)
            except Exception:  # noqa: BLE001 - a node retired mid-walk
                continue
    return found


APP_LINKS = ("webex:", "msteams:", "zoommtg:")


def launched() -> list[str]:
    """Any process given a meeting app link (xdg-open falling back to another browser would be)."""
    out = []
    for proc in Path("/proc").iterdir():
        try:
            args = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if any(link in args for link in APP_LINKS) and "protocols.py" not in args and "no-apps/xdg-open" not in args:
            out.append(args[:160])
    return out


def wait(browser, expression: str, want: str, seconds: float = 15) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if browser.evaluate(expression) == want:
                return
        except module.CdpError:
            pass
        time.sleep(.2)
    raise AssertionError(f"timed out waiting for {expression} == {want}")


with tempfile.TemporaryDirectory(prefix="xos-protocols-") as directory:
    os.environ.update(XDG_RUNTIME_DIR=directory, WLR_BACKENDS="headless", WLR_RENDERER="pixman",
                      WLR_LIBINPUT_NO_DEVICES="1")
    bus_launcher = subprocess.Popen(["/usr/libexec/at-spi-bus-launcher", "--launch-immediately"])
    time.sleep(1)
    Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
        "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set",
        GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled", GLib.Variant("b", True))),
        None, Gio.DBusCallFlags.NONE, 5000, None,
    )
    browser = module.Browser(Path(directory) / "profile", "about:blank", ui_scale=1, extra=[
        "--no-sandbox", "--ignore-certificate-errors", "--force-renderer-accessibility",
        "--host-resolver-rules=MAP acme.webex.com:443 127.0.0.1:8443, MAP meet.google.com:443 127.0.0.1:8443, "
        "MAP teams.microsoft.com:443 127.0.0.1:8443, MAP zoom.us:443 127.0.0.1:8443",
    ])
    try:
        browser.start()
        browser.navigate("https://acme.webex.com/meet/atrium.test")
        wait(browser, "document.getElementById('browser') ? 'offered' : 'loading'", "offered")
        time.sleep(1)
        page = browser.evaluate("location.hostname + ' ' + document.title")
        assert page == "acme.webex.com Cisco Webex", page
        assert prompts() == [], prompts()
        assert launched() == [], launched()
        print("PASS real Chromium: Webex's app launch neither prompts on the TV nor starts another app; "
              "its page stays for Join from your browser", flush=True)
        # Chromium refuses a second launch without a user gesture: a fresh browser for each launch.
        for origin, link in (("https://teams.microsoft.com/", "msteams://teams.microsoft.com/l/meetup-join/x"),
                             ("https://zoom.us/", "zoommtg://zoom.us/join?confno=1234567890")):
            browser.stop()
            browser.start()
            browser.navigate(origin)
            host = origin.split("/")[2]
            wait(browser, "location.hostname + '|' + document.readyState", host + "|complete")
            browser.evaluate(f"(() => {{ location.href = {link!r}; return 'asked'; }})()")
            time.sleep(2)
            assert prompts() == [], (link, prompts())
            assert launched() == [], launched()
            print(f"PASS real Chromium: {link.split(':')[0]}: from {host} neither prompts nor starts another app",
                  flush=True)
        browser.stop()
        browser.start()
        browser.navigate("https://meet.google.com/abc-defg-hij")
        wait(browser, "location.hostname + '|' + document.readyState", "meet.google.com|complete")
        browser.evaluate("(() => { location.href = 'webex://meeting/join'; return 'asked'; })()")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not prompts():
            time.sleep(.2)
        assert any("open" in p.lower() for p in prompts()), prompts()
        print("PASS real Chromium: the same launch from a non-Webex origin still prompts "
              "(the policy is scoped to Webex, and this check sees prompts)", flush=True)
    finally:
        browser.stop()
        bus_launcher.terminate()
