"""Plays the room tablet against a local Xos server (signed in as the team via XOS_DEV_USER) and
checks the Linux room computer in the container does each thing and reports it back."""

import json, subprocess, sys, time, urllib.request

SERVER, CONTAINER = sys.argv[1], sys.argv[2]
results = []


def api(path, body=None):
    req = urllib.request.Request(
        SERVER + path,
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body else "GET",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def pc():
    return next((d for d in api("/api/devices") if d.get("role") == "roompc"), None)


def until(what, check, secs=60):
    end = time.time() + secs
    while time.time() < end:
        try:
            v = check()
            if v:
                return v
        except Exception:  # noqa: BLE001 - not there yet
            pass
        time.sleep(1)
    raise AssertionError(f"timed out: {what}")


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)


def tablet(action, **extra):
    d = pc()
    cmd = api(
        f"/api/devices/{d['id']}/commands", {"type": "meeting", "args": {"action": action, **extra}}
    )
    done = until(
        f"{action} answered",
        lambda: next(
            c
            for c in api(f"/api/devices/{d['id']}/commands")
            if c["id"] == cmd["id"] and c["status"] in ("done", "failed")
        ),
        70,
    )
    time.sleep(6)  # the next heartbeats carry the new state
    return done, (pc().get("native") or {})


d = until(
    "room computer checks in with its browser running",
    lambda: (
        (x := pc()) and x["online"] and (x.get("native") or {}).get("browser") == "running" and x
    ),
    120,
)
n = d["native"]
check(
    "checks in as the atrium room computer",
    d["arg"] == "atrium" and d["client"].startswith("xosroom-linux"),
    d["id"],
)
check(
    "reports health",
    all(k in n for k in ("release", "os", "uptime_s", "disk_free_gb")),
    n.get("os", ""),
)

probe = subprocess.run(
    ["docker", "exec", CONTAINER, "python3", "/xos/os/appliance/test/prompts.py"],
    capture_output=True, text=True, timeout=60,
)
check("real Chromium browser prompts suppressed", probe.returncode == 0,
      probe.stdout.strip() if probe.returncode == 0 else (probe.stdout + probe.stderr)[-2000:])
probe = subprocess.run(
    ["docker", "exec", CONTAINER, "dbus-run-session", "--", "python3",
     "/xos/os/appliance/test/protocols.py"],
    capture_output=True, text=True, timeout=90,
)
check("meeting app launches never prompt on the TV", probe.returncode == 0,
      "\n".join(l for l in probe.stdout.splitlines() if l.startswith("PASS"))
      if probe.returncode == 0 else (probe.stdout + probe.stderr)[-2000:])

done, n = tablet("join", meeting="abc-defg-hij")
check(
    "join a Meet code",
    done["status"] == "done" and n["meeting"].get("in_call") is True,
    done.get("result"),
)
check(
    "Meet gets camera and microphone by policy, no prompt",
    "camera ok" in n["meeting"].get("title", ""),
    n["meeting"].get("title"),
)
check("Meet guest joins under the room name", "Atrium Room" in n["meeting"].get("title", ""))
invite = api("/api/rooms/atrium/meeting")
check("controller gets a meeting QR and invitation", bool(invite.get("url") and invite.get("qr")))
done, n = tablet("share_start")
check("old controllers cannot open a TV capture picker",
      done["status"] == "failed" and "scan the meeting QR code" in done.get("result", ""),
      done.get("result"))

done, n = tablet("mute")
check(
    "mute → reported muted",
    done["status"] == "done" and n["meeting"].get("muted") is True,
    done.get("result"),
)
done, n = tablet("unmute")
check("unmute → reported live", n["meeting"].get("muted") is False, done.get("result"))
done, n = tablet("camera_off")
check("camera off → reported off", n["meeting"].get("camera_off") is True, done.get("result"))
done, n = tablet("camera_on")
check("camera on → reported on", n["meeting"].get("camera_off") is False, done.get("result"))
done, n = tablet("volume", volume=30)
if "didn't take" in str(done.get("result")):
    # The container's virtual sink ignores volume; the agent must say so, not tick (hardware: checklist).
    print(f"SKIP volume on a real speaker  (simulation: {done['result']})", flush=True)
    check(
        "a volume the speaker ignores is reported, not ticked",
        done["status"] == "failed",
        done.get("result"),
    )
else:
    check(
        "volume 30 on the default speaker",
        done["status"] == "done" and n.get("volume") == 30,
        f"{done.get('result')} / reported {n.get('volume')}",
    )
done, n = tablet("leave")
check(
    "leave → back to the room screen",
    done["status"] == "done" and n["meeting"].get("in_call") is False,
    done.get("result"),
)

# Zoom, as a guest: the web client in the browser (never the Zoom app), real keyboard input.
done, n = tablet("join", meeting="1234567890", passcode="abc")
n = until("Zoom guest computer audio after admission", lambda: (x := pc()["native"]) and x["meeting"].get("in_call") and x["meeting"].get("muted") is not None and x)
m = n["meeting"]
check(
    "join a Zoom ID in the browser",
    done["status"] == "done" and m.get("in_call") is True and m.get("provider") == "zoom",
    done.get("result"),
)
check(
    "Zoom gets camera and microphone by policy", "camera ok" in m.get("title", ""), m.get("title")
)
check("Zoom shows the room's name", "Atrium Room" in m.get("title", ""), m.get("title"))
check("Zoom's Join is pressed once, never again while it is joining (greyed by its class)",
      "Join pressed twice" not in m.get("title", ""), m.get("title"))
check("Zoom's Join waits for its preview's camera and microphone",
      "joined before its camera was ready" not in m.get("title", ""), m.get("title"))
controls = api(f"/api/devices/{pc()['id']}/meeting-controls")["controls"]
opener = next(c for c in controls if c.get("kind") == "reaction_opener")
done, n = tablet("control", control=opener["id"])
choices = api(f"/api/devices/{pc()['id']}/meeting-controls")["controls"]
check("Zoom reaction picker opens from the tablet", done["status"] == "done" and
      len([c for c in choices if c["group"] == "Reactions"]) == 6, done.get("result"))
clap = next(c for c in choices if c.get("reaction", {}).get("key") == "clap")
done, n = tablet("control", control=clap["id"])
after = api(f"/api/devices/{pc()['id']}/meeting-controls")["controls"]
check("Zoom reaction sends through the real agent and closes its picker",
      done["status"] == "done" and not any(c["group"] == "Reactions" for c in after), done.get("result"))
done, n = tablet("mute")
check("Zoom mute → reported muted", n["meeting"].get("muted") is True, done.get("result"))
done, n = tablet("camera_off")
check("Zoom camera off → reported off", n["meeting"].get("camera_off") is True, done.get("result"))
done, n = tablet("leave")
check(
    "Zoom leave → back to the room screen",
    done["status"] == "done" and n["meeting"].get("in_call") is False,
    done.get("result"),
)

# Zoom joins that need a person, not the room: said plainly, never guessed, back to the room screen.
for meeting, says, what in (
    ("1112223333", "needs its passcode", "a Zoom meeting that needs its passcode asks for it"),
    ("4445556666", "doesn't allow joining from a browser", "a Zoom host who turned off browser joins is named"),
    ("7778889999", "asking for sign-in", "a Zoom meeting for signed-in attendees asks for sign-in"),
):
    done, n = tablet("join", meeting=meeting)
    check(what, done["status"] == "failed" and says in str(done.get("result"))
          and not n["meeting"].get("in_call") and not n["meeting"].get("provider"), done.get("result"))

# Zoom's bot check (what real Zoom answered the container's browser): named on the tablet as a
# check for a person, never solved; in no call.
done, n = tablet("join", meeting="2223334444")
check("Zoom's bot check is named for a person, not taken for a passcode, sign-in or waiting room",
      done["status"] == "failed" and "human verification" in str(done.get("result"))
      and not n["meeting"].get("in_call") and not n["meeting"].get("waiting"), done.get("result"))

# Zoom's waiting room: reported as waiting (not in the call) until the host admits the room.
done, n = tablet("join", meeting="5556667777")
check("Zoom waiting room: asks to join and waits",
      done["status"] == "done" and "waiting" in str(done.get("result"))
      and n["meeting"].get("waiting") is True and n["meeting"].get("in_call") is False,
      f"{done.get('result')} / waiting={n['meeting'].get('waiting')} in_call={n['meeting'].get('in_call')}")
m = until("admitted from the Zoom waiting room",
          lambda: (x := pc()["native"]["meeting"])["in_call"] and x.get("muted") is not None and x, 40)
check("Zoom waiting room: in the call once admitted", not m.get("waiting"), m.get("title"))
done, n = tablet("leave")
check("Zoom leave after the waiting room", done["status"] == "done" and n["meeting"].get("in_call") is False,
      done.get("result"))

# Zoom, starting a meeting: Zoom opens it in a new window; End ends it for everyone.
done, n = tablet("meet_now", provider="zoom")
m = n["meeting"]
check(
    "start a Zoom meeting (its new window)",
    done["status"] == "done" and m.get("in_call") is True and m.get("can_end") is True,
    done.get("result"),
)
done, n = tablet("end")
check(
    "end the Zoom meeting for everyone, its window closed",
    done["status"] == "done" and n["meeting"].get("in_call") is False,
    done.get("result"),
)

# Teams, as a guest: light meetings' pre-join, the room's name, the lobby, then the call.
done, n = tablet(
    "join", meeting="https://teams.microsoft.com/l/meetup-join/19%3ameeting_test%40thread.v2/0"
)
check(
    "join a Teams link: asks to join from the lobby",
    done["status"] == "done" and "waiting" in str(done.get("result")),
    done.get("result"),
)
m = until(
    "let into the Teams meeting", lambda: (x := pc()["native"]["meeting"])["in_call"] and x, 30
)
check("Teams: in the call once let in", m.get("provider") == "teams", m.get("title"))
check("Teams shows the room's name", "Atrium Room" in m.get("title", ""), m.get("title"))
check(
    "Teams gets camera and microphone by policy", "camera ok" in m.get("title", ""), m.get("title")
)
done, n = tablet("mute")
check("Teams mute → reported muted", n["meeting"].get("muted") is True, done.get("result"))
done, n = tablet("unmute")
check("Teams unmute → reported live", n["meeting"].get("muted") is False, done.get("result"))
done, n = tablet("camera_off")
check(
    "Teams camera off → reported off",
    done.get("result") == "camera off" and n["meeting"].get("camera_off") is True,
    done.get("result"),
)
done, n = tablet("camera_on")
check(
    "Teams camera on → reported on",
    done.get("result") == "camera on" and n["meeting"].get("camera_off") is False,
    done.get("result"),
)
done, n = tablet("leave")
check(
    "Teams leave → back to the room screen",
    done["status"] == "done" and n["meeting"].get("in_call") is False,
    done.get("result"),
)
# A Teams meeting Teams can't connect the room to: said on the tablet, back to the room screen.
done, n = tablet("join", meeting="https://teams.microsoft.com/l/meetup-join/19%3ameeting_fail%40thread.v2/0")
check("a Teams meeting that can't connect the room is named, not waited out",
      done["status"] == "failed" and "rejected this join attempt" in str(done.get("result"))
      and not n["meeting"].get("in_call") and not n["meeting"].get("provider"), done.get("result"))
# Webex, as a guest: the landing page's app launch goes nowhere, Join from your browser, the
# room's name (no email), the preview, the lobby, then the call. Its controls ignore scripted clicks.
webex = "https://acme.webex.com/acme/j.php?MTID=m0123456789abcdef0123456789abcdef"
done, n = tablet("join", meeting=webex)
check(
    "join a Webex link in the browser: asks to join from the lobby",
    done["status"] == "done" and "waiting" in str(done.get("result")),
    done.get("result"),
)
m = until(
    "let into the Webex meeting", lambda: (x := pc()["native"]["meeting"])["in_call"] and x, 30
)
check("Webex: in the call once let in", m.get("provider") == "webex" and not m.get("waiting"),
      m.get("title"))
check("Webex: the web app, never the desktop app",
      "Webex" in m.get("title", "") and m.get("error") is None, m.get("title"))
check("Webex joins as the room, by name, with no email",
      m.get("title", "").startswith("Webex · Atrium Room ·") and "@" not in m.get("title", ""), m.get("title"))
check("Webex gets camera and microphone on its web app's host",
      "camera ok" in m.get("title", ""), m.get("title"))
check("Webex: a guest can't end the meeting for everyone", m.get("can_end") is False, m.get("can_end"))
invite = api("/api/rooms/atrium/meeting")
check("controller gets the Webex invitation", invite.get("provider") == "webex" and invite.get("url") == webex
      and bool(invite.get("qr")), invite.get("url"))
done, n = tablet("mute")
check("Webex mute → reported muted",
      done.get("result") == "microphone off" and n["meeting"].get("muted") is True, done.get("result"))
done, n = tablet("unmute")
check("Webex unmute → reported live",
      done.get("result") == "microphone on" and n["meeting"].get("muted") is False, done.get("result"))
done, n = tablet("camera_off")
check("Webex camera off → reported off",
      done.get("result") == "camera off" and n["meeting"].get("camera_off") is True, done.get("result"))
done, n = tablet("camera_on")
check("Webex camera on → reported on",
      done.get("result") == "camera on" and n["meeting"].get("camera_off") is False, done.get("result"))
done, n = tablet("end")
check("Webex: End for everyone is refused, the room stays in the call",
      done["status"] == "failed" and "only leave" in str(done.get("result"))
      and n["meeting"].get("in_call") is True, done.get("result"))
done, n = tablet("leave")
check(
    "Webex leave (with its confirmation) → back to the room screen",
    done["status"] == "done" and n["meeting"].get("in_call") is False and not n["meeting"].get("provider"),
    done.get("result"),
)
# A Webex site that requires guests' email: the room never invents one, and says why.
done, n = tablet("join", meeting="https://acme.webex.com/meet/needs-email")
check(
    "a Webex site that requires an email is named, and no email is typed",
    done["status"] == "failed" and "asks guests for an email" in str(done.get("result"))
    and n["meeting"].get("in_call") is False,
    done.get("result"),
)
# Webex invitations that aren't meetings, with the real sites' wording.
for meeting, what in (
    ("https://acme.webex.com/acme/j.php?MTID=m00000000000000000000000000000000",
     "a Webex meeting that ended or was canceled is named"),
    ("https://acme.webex.com/meet/no-such-room", "a Webex personal room link that isn't valid is named"),
):
    done, n = tablet("join", meeting=meeting)
    check(what, done["status"] == "failed" and "can't find that meeting" in str(done.get("result"))
          and not n["meeting"].get("in_call") and not n["meeting"].get("provider"), done.get("result"))
check(
    "reports Meet, Zoom, Teams and Webex to the tablet",
    set(n.get("meeting_providers", [])) == {"meet", "zoom", "teams", "webex"},
    n.get("meeting_providers"),
)

ports = subprocess.run(
    ["docker", "exec", CONTAINER, "ss", "-ltnpH"], capture_output=True, text=True
).stdout
chrome_ports = [l for l in ports.splitlines() if "chrom" in l or "cage" in l]
check(
    "Chromium listens on no TCP port",
    not chrome_ports,
    "; ".join(chrome_ports) or "only the stand-in Meet listens",
)

subprocess.run(["docker", "exec", CONTAINER, "pkill", "-KILL", "-x", "chromium"], check=False)
until(
    "browser reported down after a crash",
    lambda: (
        (pc().get("native") or {}).get("browser") == "restarting"
        or (pc().get("native") or {}).get("browser_restarts", 0) >= 1
    ),
    30,
)
n = until(
    "browser back after the crash",
    lambda: (
        (x := pc()["native"]) and x["browser"] == "running" and x["browser_restarts"] >= 1 and x
    ),
    60,
)
check("recovers from a browser crash", True, f"restarts={n['browser_restarts']}")
done, n = tablet("join", meeting="abc-defg-hij")
check("joins again after the crash", n["meeting"].get("in_call") is True, done.get("result"))
tablet("leave")

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
