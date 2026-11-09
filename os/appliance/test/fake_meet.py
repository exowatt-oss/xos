"""Stand-ins for meet.google.com, Zoom's web client (app.zoom.us), Teams (teams.microsoft.com) and
Webex (*.webex.com), tests only, chosen by the Host header. Each has the join screen and the
in-call controls labelled the way the agent looks for them, and asks for the camera and microphone
so the managed policy is exercised. Like the real Zoom, the Zoom and Webex controls ignore scripted
clicks: only real input (the agent's DevTools mouse and keyboard events) works. These are not the
real apps; the physical acceptance checklist in README.md covers those."""

import http.server
import ssl
import sys

MEDIA = """navigator.mediaDevices.getUserMedia({audio: true, video: true})
  .then(() => { document.title += ' · camera ok'; })
  .catch((e) => { document.title += ' · camera blocked ' + e.name; });
const $ = (id) => document.getElementById(id);"""

MEET = (
    """<!doctype html><meta charset=utf-8><title>Meet - Test</title>
<body><div id=lobby><input placeholder="Your name" id=name><button id=join disabled>Ask to join</button></div><div id=call></div><p id=out></p>
<script>%s
// Like Meet, the in-call controls only exist once the room is in the call.
const flip = (el, what) => el.onclick = () => el.setAttribute('aria-label',
  /turn off/i.test(el.getAttribute('aria-label')) ? `Turn on ${what}` : `Turn off ${what}`);
$('name').oninput = () => { $('join').disabled = !$('name').value; };
$('join').onclick = () => {
  document.title += ' · ' + $('name').value;
  $('lobby').innerHTML = '<p>Asking to join…</p>';
  setTimeout(() => {
  $('lobby').remove();
  $('call').innerHTML = '<button id=mic aria-label="Turn off microphone (ctrl + d)">mic</button>' +
    '<button id=cam aria-label="Turn off camera (ctrl + e)">cam</button>' +
    '<button id=leave aria-label="Leave call">leave</button>';
  flip($('mic'), 'microphone'); flip($('cam'), 'camera');
  $('leave').onclick = () => { $('call').remove(); $('out').textContent = 'You left the meeting'; };
}, 4000);
};
</script>"""
    % MEDIA
)

# /wc/home: New meeting opens the meeting in a new window, as Zoom does. /wc/join/<id>: the guest
# join screen (name, Join), then Leave. /wc/<id>/start: the host, whose control is End.
# Guest meeting IDs that fail or wait the way real Zoom meetings can: 1112223333 asks for its
# passcode unless the link carries pwd, 5556667777 holds the room in the waiting room, 4445556666's
# host turned off joining from a browser, 7778889999 is for signed-in attendees only, 2223334444
# answers Join the way real Zoom answered the room's container browser (its bot check).
ZOOM = (
    """<!doctype html><meta charset=utf-8><title>Zoom</title><body><div id=ui></div><p id=out></p>
<script>%s
const real = (f) => (e) => { if (e.isTrusted) f(e); };  // Zoom ignores scripted clicks
const path = location.pathname, host = /\\/start$/.test(path);
function call() {
  $('ui').innerHTML = `<button id=mic aria-label="mute my microphone" ${host ? '' : 'hidden'}>Mute</button>` +
    (host ? '' : '<button id=join-audio>Join Audio</button>') +
    '<button id=cam aria-label="stop my video">Stop Video</button>' +
    `<button id=end aria-label="${host ? 'End' : 'Leave'}">${host ? 'End' : 'Leave'}</button><button id=reactions aria-label="Reactions">Reactions</button><div id=dlg></div><div id=reaction-host></div>`;
  $('reactions').onclick = real(() => {
    $('reaction-host').innerHTML = '<div class="reactions-menu" style="position:fixed;left:30px;top:120px;width:340px">' +
      ['clap','thumbsup','heart','joy','tada','open_mouth'].map((name,i) => `<div class="reaction-item"><button aria-label="${name}">${['👏','👍','❤','😂','🎉','😮'][i]}</button></div>`).join('') +
      '<button aria-label="thumbsdown" disabled>👎</button></div>';
    $('reaction-host').querySelectorAll('button').forEach(button => button.onclick = real(e => {
      document.body.setAttribute('data-last-reaction', JSON.stringify({name:button.getAttribute('aria-label'),trusted:e.isTrusted}));
      $('reaction-host').innerHTML = '';
    }));
  });
  if (!host) $('join-audio').onclick = real(() => {
    $('dlg').innerHTML = '<button id=computer-audio>Join Audio by Computer</button>';
    $('computer-audio').onclick = real(() => {
      $('mic').hidden = false; $('join-audio').remove(); $('dlg').innerHTML = '';
    });
  });
  $('mic').onclick = real(() => $('mic').setAttribute('aria-label',
    /^mute/.test($('mic').getAttribute('aria-label')) ? 'unmute my microphone' : 'mute my microphone'));
  $('cam').onclick = real(() => $('cam').setAttribute('aria-label',
    /^stop/.test($('cam').getAttribute('aria-label')) ? 'start my video' : 'stop my video'));
  $('end').onclick = real(() => {
    $('dlg').innerHTML = '<button id=lv>Leave Meeting</button>' + (host ? '<button id=all>End Meeting for All</button>' : '');
    const out = () => { $('ui').innerHTML = ''; $('out').textContent = 'You left the meeting'; };
    $('lv').onclick = real(out); if (host) $('all').onclick = real(out);
  });
}
if (/^\\/wc\\/(home)?$/.test(path)) {
  $('ui').innerHTML = '<button id=new>New meeting</button>';
  $('new').onclick = real(() => window.open('/wc/9876543210/start', '_blank'));
} else if (host) { setTimeout(call, 500); }
else {
  const id = (path.match(/\\/(\\d{9,11})/) || [])[1], pwd = new URLSearchParams(location.search).get('pwd');
  $('ui').innerHTML = '<nav><a href="/join" id=public-join>Join</a></nav><div id=guest-entry></div>';
  // A public navigation Join must never steal the invitation during this loading phase.
  setTimeout(() => {
  if (id === '4445556666') {
    $('guest-entry').innerHTML = '<p>This meeting does not support joining from a browser.</p><a href="#download">Download Now</a>';
    return;
  }
  $('guest-entry').innerHTML = '<a href="#guest" id=browser>Join from browser</a>';
  $('browser').onclick = real(e => {
  e.preventDefault();
  if (id === '7778889999') {
    $('ui').innerHTML = '<p>This meeting is for authorized attendees only.</p><button id=signin>Sign In to Join</button>';
    return;
  }
  const passcode = id === '1112223333' && !pwd;
  // Like the real join screen: the preview's own Mute / Stop Video (not the call's), and a Join
  // that stays a live <button> greyed out by its class (zm-btn--disabled), never by `disabled`,
  // both before there's a name and while it is joining (its spinner).
  // The preview's Mute / Stop Video spin while it gets the camera and microphone; a Join pressed
  // before then joins without them, as the real one did.
  const spin = '<svg class=spinner width=16 height=16></svg>';
  $('ui').innerHTML = `<button id=preview-audio-control-button aria-label="Mute">${spin}Mute</button>` +
    `<button id=preview-video-control-button aria-label="Stop Video">${spin}Stop Video</button>` +
    '<h2>Enter Meeting Info</h2><input id=input-for-name type=text>' +
    (passcode ? '<input id=input-for-pwd placeholder="Meeting Passcode">' : '') +
    '<button id=go type=button class="zm-btn preview-join-button zm-btn--disabled" tabindex=-1>Join</button>' +
    '<button type=button class="zm-btn preview-join-button preview-join-app-button">Join from Zoom Workplace App</button>';
  setTimeout(() => document.querySelectorAll('.spinner').forEach(e => e.remove()), 3000);
  let joining = false;
  const ready = () => { const off = joining || !$('input-for-name').value || (passcode && !$('input-for-pwd').value);
    $('go').classList.toggle('zm-btn--disabled', off); $('go').tabIndex = off ? -1 : 0; };
  $('input-for-name').oninput = ready;
  if (passcode) $('input-for-pwd').oninput = ready;
  $('go').onclick = real(() => {
    if (joining) { document.title += ' · Join pressed twice'; return; }
    if ($('go').classList.contains('zm-btn--disabled')) return;
    if (document.querySelector('.spinner')) document.title += ' · joined before its camera was ready';
    joining = true; ready(); $('go').insertAdjacentHTML('beforeend', '<span class=loading></span>');
    document.title = 'Zoom · ' + $('input-for-name').value + document.title.slice(4);
    setTimeout(() => {
    if (id === '2223334444') {  // Zoom's reCAPTCHA took the room for a bot (the real text)
      $('ui').innerHTML = "<h2>Enter Meeting Info</h2><div>Automated bots aren't allowed to join this meeting.</div>" +
        '<div>If this was a mistake and you are a human, sign in to join the meeting. <button>Or report a problem to us.</button></div>' +
        '<button type=button>Sign in to join</button><p>OR</p><button type=button>Join from Zoom Workplace App</button>';
      return;
    }
    const waitingRoom = id === '5556667777';
    $('ui').innerHTML = waitingRoom
      ? '<p>Please wait, the meeting host will let you in soon.</p><button id=wr-leave aria-label="Leave">Leave</button>'
      : '<p>Waiting for the host to let you in</p>';
    setTimeout(call, waitingRoom ? 12000 : 4000);
    }, 2500);
  });
  });
  }, 1600);
}
</script>"""
    % MEDIA
)

# A Teams invitation as a guest, the way the real one went in Chromium on Linux (2026-10): the
# invitation shows the Teams splash, moves to /v2/?meetingjoin=true, then to light meetings
# (/light-meetings/launch), with no "Continue on this browser" step. Its pre-join: "Type your
# name", the preview's camera and mic (a "Turn camera off" / "Mute mic" button and a switch each),
# Computer audio, Cancel and Join now (#prejoin-join-button, disabled until there's a name). Then
# the lobby until someone lets the room in, then the call. The call's controls ("Mic", "Camera",
# "Leave", the state in the tooltip) are still a best reading: no real Teams call has been seen.
# A meeting Teams can't connect to (meeting_fail) answers Join with its real "Sorry, we couldn't
# connect you."
TEAMS = (
    """<!doctype html><meta charset=utf-8><title>Microsoft Teams</title><body><div id=ui></div><p id=out></p>
<script>%s
const path = location.pathname;
if (!path.startsWith('/light-meetings/')) {
  $('ui').innerHTML = '<p>Microsoft Teams</p>';  // the splash, then light meetings
  const fail = /meeting_fail/.test(location.href) ? '&fail=1' : '';
  setTimeout(() => history.replaceState(null, '', '/v2/?meetingjoin=true#' + path), 300);
  setTimeout(() => location.replace('/light-meetings/launch?anon=true&lightExperience=true' + fail), 1500);
} else {
  $('ui').innerHTML = '<h2>Microsoft Teams meeting</h2><input id=field-r4__control type=text placeholder="Type your name">' +
    '<video></video><button type=button title="Turn camera off"></button>' +
    '<input type=checkbox role=switch id=switch-rk title="Turn camera off (Ctrl+Shift+O)" checked>' +
    '<input type=radio id=radio-rb aria-label="Computer audio" checked>' +
    '<button type=button title="Mute mic"></button><input type=checkbox role=switch id=switch-rh title="Mute mic (Ctrl+Shift+M)" checked>' +
    '<button type=button>Cancel</button><button type=button id=prejoin-join-button aria-label="Join now" disabled>Join now</button>' +
    '<button type=button>Sign in</button>';
  $('field-r4__control').oninput = () => { $('prejoin-join-button').disabled = !$('field-r4__control').value; };
  $('prejoin-join-button').onclick = () => {
    document.title = 'Teams · ' + $('field-r4__control').value + document.title.slice(15);
    if (location.search.includes('fail=1')) {
      $('ui').innerHTML = "<h2>Sorry, we couldn't connect you.</h2><button>Rejoin call</button><button>Dismiss</button>";
      return;
    }
    $('ui').innerHTML = '<p>Someone in the meeting should let you in soon.</p>';
    setTimeout(() => {
      $('ui').innerHTML = '<button id=microphone-button aria-label="Mic" title="Mute mic">Mic</button>' +
        '<button id=video-button aria-label="Camera" title="Turn camera off">Camera</button>' +
        '<button id=hangup-button aria-label="Leave">Leave</button>';
      const flip = (el, a, b) => el.onclick = () => el.title = el.title === a ? b : a;
      flip($('microphone-button'), 'Mute mic', 'Unmute mic');
      flip($('video-button'), 'Turn camera off', 'Turn camera on');
      $('hangup-button').onclick = () => { $('ui').innerHTML = ''; $('out').textContent = 'You left'; };
    }, 4000);
  };
}
</script>"""
    % MEDIA
)


# A Webex invitation (/meet/<name>, /<site>/j.php?MTID=…) lands on "Open Cisco Webex Meetings?":
# the page launches the desktop app (a top-level webex: navigation, which the room policy and its
# xdg-open turn into nothing) and then offers "Join from your browser." to the web app on the
# site's other host (acme.my.webex.com). There: the guest's name (Webex Suite asks for an email only
# when the site requires one; /meet/needs-email stands in for such a site), Next, a preview that
# asks for the camera and microphone, Join meeting, the lobby until the host lets the room in, then
# the call: Mute, Stop video, Leave meeting with its confirmation. A guest has no End.
WEBEX_LANDING = """<!doctype html><meta charset=utf-8><title>Cisco Webex</title><body><div id=ui>
<h1>Open Cisco Webex Meetings?</h1><p>Launching meeting…</p></div>
<script>
location.href = 'webex://meeting/join?site=acme&path=' + encodeURIComponent(location.pathname + location.search);
setTimeout(() => {
  document.getElementById('ui').insertAdjacentHTML('beforeend', '<button id=launch>Launch meeting</button>' +
    '<p>Having trouble? <a id=browser href="#">Join from your browser.</a></p>');
  document.getElementById('launch').onclick = () => { location.href = 'webex://meeting/join?site=acme'; };
  document.getElementById('browser').onclick = (e) => {  // like the real page, only a real click
    e.preventDefault();
    if (e.isTrusted) location.href = 'https://acme.my.webex.com/webappng/sites/acme/meeting/info/atrium'
      + (location.pathname.endsWith('/needs-email') ? '?email=required' : '');
  };
}, 1500);
</script>"""

WEBEX_APP = """<!doctype html><meta charset=utf-8><title>Webex</title><body><div id=ui></div><p id=out></p>
<script>
const $ = (id) => document.getElementById(id);
const real = (f) => (e) => { if (e.isTrusted) f(e); };  // the web app ignores scripted clicks
const needsEmail = location.search.includes('email=required');
$('ui').innerHTML = '<h2>Enter your information</h2><input id=name aria-label="Your full name">' +
  (needsEmail ? '<input id=email type=email required aria-label="Email address">' : '') + '<button id=next disabled>Next</button>';
const ready = () => { $('next').disabled = !$('name').value || (needsEmail && !/^[^@\\s]+@[^@\\s]+$/.test($('email').value)); };
$('name').oninput = ready; if (needsEmail) $('email').oninput = ready;
const toggle = (el, a, b) => el.onclick = real(() => el.setAttribute('aria-label', el.getAttribute('aria-label') === a ? b : a));
$('next').onclick = real(() => {
  document.title = 'Webex · ' + $('name').value + (needsEmail ? ' · ' + $('email').value : '');
  // The preview asks for the camera and microphone, on this host (not the invitation's).
  navigator.mediaDevices.getUserMedia({audio: true, video: true})
    .then(() => { document.title += ' · camera ok'; })
    .catch((e) => { document.title += ' · camera blocked ' + e.name; });
  $('ui').innerHTML = '<video></video><button data-test=microphone-button aria-label="Mute">mic</button>' +
    '<button data-test=camera-button aria-label="Stop video">cam</button><button id=join>Join meeting</button>';
  $('join').onclick = real(() => {
    $('ui').innerHTML = "<p>You're in the lobby. The host will let you in soon.</p>" +
      '<button data-test=leave-button aria-label="Leave meeting">Leave</button>';
    setTimeout(() => {
      $('ui').innerHTML = '<button data-test=microphone-button aria-label="Mute">mic</button>' +
        '<button data-test=camera-button aria-label="Stop video">cam</button>' +
        '<button data-test=leave-button aria-label="Leave meeting">Leave</button><div id=dlg></div>';
      toggle(document.querySelector('[data-test=microphone-button]'), 'Mute', 'Unmute');
      toggle(document.querySelector('[data-test=camera-button]'), 'Stop video', 'Start video');
      document.querySelector('[data-test=leave-button]').onclick = real(() => {
        $('dlg').innerHTML = '<div role=dialog><p>Are you sure you want to leave?</p>' +
          '<button id=confirm>Leave meeting</button><button id=stay>Cancel</button></div>';
        $('stay').onclick = real(() => { $('dlg').innerHTML = ''; });
        $('confirm').onclick = real(() => { $('ui').innerHTML = ''; $('out').textContent = "You've left the meeting"; });
      });
    }, 5000);
  });
});
</script>"""

# What real Webex sites answer an invitation that isn't a meeting (seen 2026-10 on a real site, with
# made-up links): the site's own host, /webappng/…, its header (Join a meeting, Sign in) and one line.
WEBEX_ERRORS = {
    "meetingnotfound": "This meeting has ended or been canceled.<br>Contact the host for details.",
    "pmr": "This meeting link isn't valid.<br>Please double-check the link or contact the host for help.",
}
# Like the real "Meeting Detail - Webex" page, it goes back in history on its own (real: ~6 s after
# its text; here before it, so a room that kept its history would land on the room screen).
WEBEX_ERROR = """<!doctype html><meta charset=utf-8><title>Cisco Webex Meetings</title><body>
<a href="#main">Skip to content</a><nav>English <a href="#">Download</a> <a href="#">Sign in</a>
<button>Join a meeting</button></nav><main id=main><p id=msg hidden>%s</p></main>
<script>setTimeout(() => history.back(), 2500); setTimeout(() => { msg.hidden = false; }, 3500);</script>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/frame-entry":
            self.send_response(302)
            self.send_header("Location", "https://zoom.us/wc/9876543210/start")
            self.end_headers()
            return
        host = (self.headers.get("Host") or "").split(":")[0]
        if host.endswith(".webex.com") and ("MTID=m0000" in self.path or self.path == "/meet/no-such-room"):
            self.send_response(302)  # the real site's own redirect for an invitation that isn't a meeting
            self.send_header("Location", "/webappng/sites/acme/meeting/info/meetingnotfound?errorType=notFound"
                             if "MTID" in self.path else "/webappng/sites/acme/dashboard/pmr/no-such-room/error")
            self.end_headers()
            return
        page = {"app.zoom.us": ZOOM, "zoom.us": ZOOM, "teams.microsoft.com": TEAMS}.get(host, MEET)
        if host.endswith(".webex.com"):
            page = WEBEX_APP if self.path.startswith("/webappng/") else WEBEX_LANDING
            if "/meeting/info/meetingnotfound" in self.path or "/dashboard/pmr/" in self.path:
                page = WEBEX_ERROR % WEBEX_ERRORS["pmr" if "/pmr/" in self.path else "meetingnotfound"]
        if self.path == "/wc/frame-shell":
            page = '<!doctype html><title>Zoom wrapper</title><iframe style="position:absolute;left:120px;top:80px;width:800px;height:500px;border:0" src="https://zoom.us/wc/9876543210/start"></iframe>'
        if self.path == "/wc/shadow-frame-shell":
            page = """<!doctype html><title>Zoom shadow wrapper</title><div id=host
              style="position:absolute;left:100px;top:60px;transform:translate(20px,20px)"></div>
              <script>const shadow=document.querySelector('#host').attachShadow({mode:'closed'});
              shadow.innerHTML='<iframe style="width:800px;height:500px;padding:20px;border:8px solid red" src="https://zoom.us/frame-entry"></iframe>';</script>"""
        b = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # nothing older (CodeQL py/insecure-protocol)
ctx.load_cert_chain(sys.argv[2], sys.argv[3])
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.serve_forever()
