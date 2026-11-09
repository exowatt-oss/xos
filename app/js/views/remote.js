// Room remote: a tablet / phone controller for the screens of one room (its control group).
//   #/tablet/<room>   the table iPad: every screen in the room, with a switcher when there are several
//   #/remote/<screen> a phone or browser: opens on that screen, and still only sees its room
// It opens on a few big buttons, like the room controllers people already know: join a meeting
// (Zoom ID or link, or a Google Meet code), start one, share a screen, TV on/off; in a meeting,
// mute, camera, volume and leave. "More" opens every control for the room's screens.
// A remote never sees another room's screens (groups.js). It hears about changes live through
// GET /api/devices/events, shows a tap's result as soon as the screen confirms it, and marks the
// tapped control as working instead of locking the whole remote.

import * as S from "../state.js";
import { esc, clock, bookingWhen } from "../ui.js";
import { getPages, loadPages } from "./page.js";
import { findRoom, findScreen, members, displayMembers, selectedDisplay, roomOf as groupRoom } from "../groups.js";
import { controlFetch } from "../api.js";
import { readCalendar } from "../calendar-read.js";
import { switchInputProblem, SWITCH_LABEL, SWITCH_HELP, SWITCH_WAIT_MS } from "../tv-input.js";

let devices = null;
const pending = new Map(); // control key -> label, while the screen hasn't confirmed yet
const optimistic = new Map(); // device id -> {route, at}: what was just chosen, shown at once
const previewing = new Set(); // device ids with a screenshot on its way
let toast = { text: "", kind: "", at: 0 };
let shotAt = 0;

const roomOf = (d) => groupRoom(d, devices || [], S.rooms());
const devName = (d) => d.name || d.label || d.id;
const onRemote = () => /^#\/(remote|tablet|control)\//.test(location.hash);
const typing = () => document.activeElement?.closest?.(".rm") && document.activeElement.tagName === "INPUT";
const repaint = () => { if (!typing()) S.poke(); };

async function refresh() {
  try {
    devices = await (await fetch("/api/devices", { cache: "no-store" })).json();
    // Once the screen reports what it shows, the optimistic choice has done its job.
    for (const [id, o] of optimistic) {
      const d = devices.find((x) => x.id === id);
      if (!d || `${d.role}/${d.arg}` === o.route || Date.now() - o.at > 20000) optimistic.delete(id);
    }
    repaint();
  } catch (_) { /* keep the last list */ }
}
let soonTimer = 0;
const soon = () => { clearTimeout(soonTimer); soonTimer = setTimeout(refresh, 120); };

// ---------- live events ----------
let es = null;
const waiters = new Map(); // command id -> resolve, for a command the remote is waiting on
const finished = new Map(); // command id -> command, answered before anyone waited
const settled = (c) => c && c.status !== "pending" && c.status !== "sent";

function connectLive() {
  if (!onRemote() || !S.isHub()) { es?.close(); es = null; return; }
  if (es && es.readyState !== EventSource.CLOSED) return;
  es = new EventSource("/api/devices/events");
  es.onopen = refresh; // catch up on anything missed while disconnected
  es.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch (_) { return; }
    if (m.kind === "command" && settled(m.command)) {
      const w = waiters.get(m.command.id);
      if (w) w(m.command);
      else { finished.set(m.command.id, m.command); setTimeout(() => finished.delete(m.command.id), 10000); }
    }
    if (m.kind === "screenshot") { shotAt = Date.now(); previewing.delete(m.device); }
    soon();
  };
}
window.addEventListener("hashchange", connectLive);
// Fallback: if the event stream is down, poll; either way, re-read now and then.
setInterval(() => {
  connectLive();
  if (onRemote() && es?.readyState !== EventSource.OPEN) refresh();
}, 4000);
setInterval(() => { if (onRemote()) refresh(); }, 30000);

/** Resolves with the command once the screen answers it, or null after `ms`. */
function waitFor(dev, id, ms = 8000) {
  return new Promise((resolve) => {
    let poll = 0;
    let timer = 0, checking = false, ended = false;
    const started = Date.now();
    const controller = new AbortController();
    const finish = (c) => {
      if (ended) return;
      ended = true;
      waiters.delete(id); clearInterval(poll); clearTimeout(timer); controller.abort(); resolve(c);
    };
    if (finished.has(id)) return finish(finished.get(id));
    waiters.set(id, finish);
    poll = setInterval(async () => {
      // An open event stream can still miss a completion (e.g. a full watcher queue).
      // Give live feedback a head start, then verify history without resending the command.
      if (ended || checking || (es?.readyState === EventSource.OPEN && Date.now() - started < Math.min(2500, ms / 3))) return;
      checking = true;
      try {
        const hist = await (await fetch(`/api/devices/${encodeURIComponent(dev)}/commands`, { cache: "no-store", signal: controller.signal })).json();
        const c = hist.find((x) => x.id === id);
        if (settled(c)) finish(c);
      } catch (_) { /* try again until this command's deadline */ }
      finally { checking = false; }
    }, 1200);
    timer = setTimeout(() => finish(null), ms);
  });
}

// ---------- feedback ----------

/** A tap you can feel: the Xos iPad app's native haptics, else vibration where there is any. */
function haptic(kind = "light") {
  try { window.webkit?.messageHandlers?.xos?.postMessage({ haptic: kind }); } catch (_) { /* not the app */ }
  try { navigator.vibrate?.(kind === "error" ? [20, 60, 20] : 8); } catch (_) { /* no vibration */ }
}

function say(text, kind = "ok") {
  toast = { text, kind, at: Date.now() };
  const at = toast.at;
  setTimeout(() => { if (toast.at === at) { toast = { text: "", kind: "", at: 0 }; repaint(); } }, kind === "err" ? 8000 : 4000);
}

// Commands and Zoom need a trusted remote's key or a signed-in person (api.js); the rest is open.
async function post(path, body) {
  const go = /\/(commands|zoom)$/.test(path) ? controlFetch : fetch;
  const res = await go(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const out = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(out.detail || res.statusText);
  return out;
}

const CHANGES_PICTURE = new Set(["route", "page_key", "page_scroll", "reload", "display_on", "message"]);

async function send(d, type, args = {}, label = type, key = `${d.id}:${type}`) {
  if (pending.has(key)) return;
  pending.set(key, label);
  haptic();
  if (type === "route") optimistic.set(d.id, { route: args.route, at: Date.now() });
  repaint();
  try {
    const cmd = await post(`/api/devices/${encodeURIComponent(d.id)}/commands`, { type, args });
    const done = await waitFor(d.id, cmd.id, wakeWait(d, type));
    if (done?.status === "done") {
      if (d.role === "roompc" && type === "route") await S.loadConfig();
      say(`${label} ✓`);
    }
    else if (done) { say(`${label} didn't work: ${done.result || done.status}`, "err"); haptic("error"); optimistic.delete(d.id); }
    else say(d.connected ? `${label}: sent, waiting for ${devName(d)}` : `${label}: ${devName(d)} is offline, it will happen when it's back`, "warn");
  } catch (err) {
    say(`${label} didn't work: ${err.message}`, "err");
    haptic("error");
    optimistic.delete(d.id);
  }
  pending.delete(key);
  repaint();
  refresh();
  if (CHANGES_PICTURE.has(type)) setTimeout(() => preview(d), type === "route" || type === "reload" ? 5000 : 2000);
}

/** How long to wait for a screen to answer: TV on for an unreachable stick wakes it over the network first. */
function wakeWait(d, type) {
  if (type === "switch_input") return SWITCH_WAIT_MS; // the server runs off, a pause, then on
  if (d.connected) return 8000;
  return type === "display_on" && d.native ? 20000 : 1500;
}

async function preview(d) {
  if (!d?.native || !d.connected || d.role === "roompc") return; // only the Xos TV app can take screenshots
  previewing.add(d.id);
  repaint();
  post(`/api/devices/${encodeURIComponent(d.id)}/commands`, { type: "screenshot" }).catch(() => previewing.delete(d.id));
  setTimeout(() => { if (previewing.delete(d.id)) repaint(); }, 10000);
}

// ---------- which screen this remote is driving ----------

const PICK = "xos-remote-pick-";
function pickedIn(group) {
  try { return localStorage.getItem(PICK + group) || ""; } catch (_) { return ""; }
}
function pick(group, id) {
  try { localStorage.setItem(PICK + group, id); } catch (_) { /* private mode */ }
}

const ICONS = { html: "✦", url: "↗", playlist: "↻", slides: "▶", announcement: "✉" };

// ---------- Zoom Room for the room ----------
const zoomInfo = {}; // room id -> GET /api/rooms/{room}/zoom
const zoomReads = new Map();
async function refreshZoom(room) {
  const request = Symbol();
  zoomReads.set(room, request);
  try {
    const status = await (await fetch(`/api/rooms/${encodeURIComponent(room)}/zoom`, { cache: "no-store" })).json();
    if (zoomReads.get(room) !== request) return;
    zoomInfo[room] = status;
    repaint();
  } catch (_) { /* keep the last status */ }
}
setInterval(() => { if (onRemote()) Object.keys(zoomInfo).forEach(refreshZoom); }, 8000);

// ---------- the room's calendar (its Google resource calendar; src/app/calendars.py) ----------
const calInfo = {}; // room id -> GET /api/rooms/{room}/calendar
const calPending = new Set();
async function refreshCal(room) {
  if (calPending.has(room)) return;
  calPending.add(room);
  const url = `/api/rooms/${encodeURIComponent(room)}/calendar`;
  calInfo[room] ||= { loading: true };
  try {
    // As the room's iPad (its device key) or a signed-in person, the bookings carry their join
    // links; anyone else sees titles and times only.
    calInfo[room] = await readCalendar(url, controlFetch, fetch);
    repaint();
  } catch (_) { calInfo[room] = { error: true }; repaint(); }
  finally { calPending.delete(room); }
}
function refreshRemoteCalendars() {
  if (onRemote() && globalThis.document?.visibilityState !== "hidden") Object.keys(calInfo).forEach(refreshCal);
}
setInterval(refreshRemoteCalendars, 60000);
globalThis.document?.addEventListener("visibilitychange", refreshRemoteCalendars);
for (const event of ["focus", "online", "pageshow", "hashchange"]) globalThis.window?.addEventListener(event, refreshRemoteCalendars);

const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

/** Now / next from the room's calendar, with Join when the booking has a meeting link. */
function calCard(roomId, canJoin) {
  const c = calInfo[roomId];
  if (!c) { refreshCal(roomId); return ""; }
  if (c.loading) return `<p class="rm-cal-later">Loading room schedule…</p>`;
  if (c.error) return `<p class="rm-cal-later" role="status">Room schedule temporarily unavailable. Join with a code or link.</p>`;
  if (!c.calendar || !c.configured) return `<p class="rm-cal-later">Room calendar not connected · Join with a code or link</p>`;
  const now = Date.now() / 1000;
  const list = (c.bookings || []).filter((b) => b.end > now).sort((a, b) => a.start - b.start);
  const cur = list.find((b) => b.start <= now);
  const focus = cur || list[0];
  const busy = pending.has("zoom:join") || pending.has("zoom:meet_now");
  const when = (b) => bookingWhen(b, now);
  const code = (b) => {
    const match = (b.join_url || "").match(/\/(?:j\/|wc\/join\/)?([a-z]{3}-[a-z]{4}-[a-z]{3}|\d{9,11})(?:[?/#]|$)/i);
    return match ? ` · Code ${esc(match[1])}` : "";
  };
  const join = (b) => canJoin && b.join_url ? `<button class="rm-cta ${busy ? "is-pending" : ""}" data-act="rm-cal-join" data-room="${esc(roomId)}" data-url="${esc(b.join_url)}" data-title="${esc(b.title)}" aria-label="Join ${esc(b.title)}" ${busy ? "disabled" : ""}>${busy ? spin : ICON.start}Join</button>` : "";
  if (!focus) return `<section class="rm-cal"><div class="rm-cal-what"><span class="rm-cal-k rm-cal-free">Available</span><span class="rm-cal-title">No upcoming bookings</span></div></section>`;
  const soon = focus === cur ? "Now" : focus.start - now <= 600 ? `Starts in ${Math.max(1, Math.ceil((focus.start - now) / 60))} min` : "Up next";
  const later = list.filter((b) => b !== focus).slice(0, 2);
  return `<section class="rm-cal ${cur ? "rm-cal-now" : ""}" aria-label="Room schedule"><div class="rm-cal-what"><span class="rm-cal-k">${soon}</span>
    <span class="rm-cal-title">${esc(focus.title)}</span><span class="rm-cal-when">${when(focus)}${focus.organizer ? ` · ${esc(focus.organizer)}` : ""}${focus.join_url ? ` · ${esc(SERVICES[serviceOf(focus.join_url)] || "")}${code(focus)}` : ""}</span></div>${join(focus)}</section>
    ${later.length ? `<ol class="rm-agenda" aria-label="Upcoming meetings">${later.map((b) => `<li><span class="rm-agenda-t">${when(b)}</span><span class="rm-agenda-n">${esc(b.title)}</span>${join(b)}</li>`).join("")}</ol>` : ""}`;
}

// The meeting service a link opens (for labels; the server checks links properly).
const serviceOf = (url) => /meet\.google\.com/.test(url) ? "meet" : /zoom\.(us|com)/.test(url) ? "zoom" : /teams\.(microsoft|live)\.com|teams\.cloud\.microsoft/.test(url) ? "teams" : /(^|[/.])webex\.com\//.test(url) ? "webex" : "";

// Zoom doesn't report a room's mute, camera or volume, so the remote shows what it last set.
const roomAV = {}; // room id -> {muted, cameraOff, volume}
const av = (room) => (roomAV[room] ||= { muted: false, cameraOff: false, volume: 50 });
const AV_AFTER = { mute: { muted: true }, unmute: { muted: false }, camera_off: { cameraOff: true }, camera_on: { cameraOff: false },
  join: { muted: false, cameraOff: false }, meet_now: { muted: false, cameraOff: false } };

// Which computer runs the room's meetings: its Zoom Room (Zoom's cloud API), or its room computer
// (a Mac running Google Meet, os/mac/xos-room.py, which the iPad commands like a screen). The same
// buttons drive either one.
const roomPc = {}; // room id -> its room computer's device id, while the room has no Zoom Room
const pcOf = (roomId) => members(devices, S.rooms(), roomId, "computer")[0] || null;

function meetingOf(roomId) {
  const z = roomId ? zoomInfo[roomId] : null;
  if (z?.configured && z?.mapped && !z?.error) {
    delete roomPc[roomId];
    const down = ["Offline", "UnderConstruction"].includes(z.status);
    return { kind: "zoom", live: z.status === "InMeeting", down, z,
      status: z.status === "InMeeting" ? ["live", "In a meeting"] : down ? ZOOM_STATES[z.status] : ["on", "Ready"] };
  }
  const pc = roomId ? pcOf(roomId) : null;
  if (!pc) { delete roomPc[roomId]; return null; }
  roomPc[roomId] = pc.id;
  const m = pc.native?.meeting || {};
  // The room computer reports the meeting's real state: show that, not what was last tapped.
  Object.assign(av(roomId), { muted: typeof m.muted === "boolean" ? m.muted : null, cameraOff: typeof m.camera_off === "boolean" ? m.camera_off : null },
    typeof pc.native?.volume === "number" ? { volume: pc.native.volume } : {});
  const down = !pc.online;
  return { kind: "pc", pc, m, live: !down && Boolean(m.in_call), waiting: !down && Boolean(m.waiting), down,
    status: down ? ["off", "Room computer offline"] : m.in_call ? ["live", "In a meeting"]
      : m.waiting ? ["sleep", "Waiting to be let in"] : m.error ? ["off", "Room computer needs attention"] : ["on", "Ready"] };
}

async function pcCall(pcId, action, extra, label, quiet = false) {
  let succeeded = false;
  const key = `zoom:${action}`;
  pending.set(key, label);
  haptic();
  repaint();
  try {
    const cmd = await post(`/api/devices/${encodeURIComponent(pcId)}/commands`, { type: "meeting", args: { action, ...extra } });
    // Joining waits for Meet to open and let the room in.
    const done = await waitFor(pcId, cmd.id, ["join", "meet_now"].includes(action) ? 50000 : 10000);
    if (done?.status === "done") { succeeded = true; if (!quiet) say(["control", "control_back"].includes(action) ? `${label}: sent` : action === "share_start" ? done.result : `${label} ✓`); }
    else if (done) { say(`${label} didn't work: ${done.result || done.status}`, "err"); haptic("error"); }
    else say(`${label}: sent, waiting for the room computer`, "warn");
  } catch (err) {
    say(`${label} didn't work: ${err.message}`, "err");
    haptic("error");
  }
  pending.delete(key);
  refresh();
  return succeeded;
}

async function zoomCall(room, action, extra, label) {
  if (pending.has(`zoom:${action}`)) return false;
  if (roomPc[room]) return pcCall(roomPc[room], action, extra, label);
  const key = `zoom:${action}`;
  pending.set(key, label);
  haptic();
  repaint();
  try {
    await post(`/api/rooms/${encodeURIComponent(room)}/zoom`, { action, ...extra });
    Object.assign(av(room), AV_AFTER[action] || {}, action === "volume" ? { volume: extra.volume } : {});
    say(`${label} ✓`);
    if (["join", "meet_now", "leave", "end"].includes(action)) [3000, 8000].forEach((ms) => setTimeout(() => refreshZoom(room), ms));
  } catch (err) {
    say(`Zoom: ${err.message}`, "err");
    haptic("error");
  }
  pending.delete(key);
  refreshZoom(room);
}

const ZOOM_STATES = { Available: ["on", "Ready"], InMeeting: ["live", "In a meeting"], Offline: ["off", "Offline"], UnderConstruction: ["sleep", "Being set up"] };
const spin = `<span class="rm-spin" aria-hidden="true"></span>`;

function zoomPanel(room) {
  if (!room) return "";
  const z = zoomInfo[room];
  if (!z) { refreshZoom(room); return ""; }
  const off = !z.mapped || !z.configured ? "disabled" : "";
  const [cls, label] = !z.configured ? ["sleep", "Not connected yet"] : !z.mapped ? ["sleep", "No Zoom Room linked"]
    : z.error ? ["off", "Error"] : ZOOM_STATES[z.status] || ["sleep", z.status || "…"];
  const hint = !z.configured ? "Zoom credentials haven't been added yet (Admin → Integrations)."
    : !z.mapped ? "Link this room to its Zoom Room in Admin → Integrations." : z.error || `Zoom Room: ${z.name || z.zoom_room_id}`;
  const b = (action, text, extra = "") => {
    const busy = pending.has(`zoom:${action}`);
    return `<button class="rm-btn ${busy ? "is-pending" : ""}" data-act="rm-zoom" data-room="${esc(room)}" data-zoom="${action}" data-label="${esc(text)}" ${extra} ${off} ${busy ? "disabled" : ""}>${busy ? spin : ""}${text}</button>`;
  };
  return `<section class="rm-group rm-zoom" aria-label="Zoom">
    <div class="rm-group-head"><h2>Zoom</h2><span class="rm-st rm-st-${cls}">● ${esc(label)}</span></div>
    <div class="rm-dim small">${esc(hint)}</div>
    <div class="rm-row"><input id="rm-mid" maxlength="400" placeholder="Meeting ID or link" ${off} /><input id="rm-pwd" maxlength="20" placeholder="Passcode" class="rm-pwd" ${off} />${b("join", "Join")}</div>
    <div class="rm-row">${b("mute", "Mute")}${b("unmute", "Unmute")}${b("leave", "Leave")}${b("end", "End meeting", `data-confirm="1"`)}</div>
  </section>`;
}

// Share to the screen: its share link to open on a laptop or phone, and stop whatever is shared.
function sharePanel(d) {
  if (!d.share_code || d.share_off) return "";
  const s = d.sharing;
  const what = s ? `${s.from || "Someone"} is sharing ${{ screen: "a screen", url: "a link", image: "a photo" }[s.kind] || ""}` : "";
  const busy = pending.has(`${d.id}:share-stop`);
  return `<section class="rm-group rm-share" aria-label="Share">
    <img src="/api/share/${esc(d.share_code)}/qr.svg" alt="" class="rm-qr" />
    <div><h2>Share to ${esc(devName(d))}</h2><div class="rm-dim small">Open <b>${esc(location.host)}/s/${esc(d.share_code)}</b> on your laptop or phone${d.airplay ? `, or AirPlay to <b>${esc(d.airplay)}</b>` : ""}.</div>
    ${s ? `<div class="rm-row"><span class="small">${esc(what)}</span><button class="rm-btn rm-off ${busy ? "is-pending" : ""}" data-act="rm-share-stop" data-dev="${esc(d.id)}">${busy ? spin : ""}Stop sharing</button></div>` : ""}</div>
  </section>`;
}


// ---------- the home screen: a few big buttons ----------

// Line icons for the big buttons (stroke follows the text colour).
const svg = (d) => `<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON = {
  join: svg('<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 8h.01M12 8h.01M16 8h.01M8 12h.01M12 12h.01M16 12h.01M8 16h8"/>'),
  start: svg('<rect x="2.5" y="6" width="13" height="12" rx="2.5"/><path d="m15.5 10 6-3.5v11l-6-3.5M9 9.5v5M6.5 12h5"/>'),
  share: svg('<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8 21h8M12 17v4M12 13V7.5M9.5 10 12 7.5l2.5 2.5"/>'),
  tv: svg('<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>'),
  mic: svg('<rect x="9" y="2.5" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3.5"/>'),
  micOff: svg('<path d="M15 9.5V5.5a3 3 0 0 0-5.7-1.3M9 9v2.5a3 3 0 0 0 4.6 2.5M5 11a7 7 0 0 0 11.5 5.4M19 11a7 7 0 0 1-.6 2.8M12 18v3.5M3 3l18 18"/>'),
  cam: svg('<rect x="2.5" y="6" width="13" height="12" rx="2.5"/><path d="m15.5 10 6-3.5v11l-6-3.5"/>'),
  camOff: svg('<path d="M10.5 6h2.5A2.5 2.5 0 0 1 15.5 8.5v3l6-3.5v9M15.5 15.5a2.5 2.5 0 0 1-2.5 2.5H5A2.5 2.5 0 0 1 2.5 15.5v-7A2.5 2.5 0 0 1 4.5 6M3 3l18 18"/>'),
  vol: svg('<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>'),
  leave: svg('<path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4M10 16l-4-4 4-4M6 12h10"/>'),
};

let mode = "home"; // "home" or "more" (every control for the room's screens)
const roomControls = {};
const controlsLoading = new Map();
let controlsRoom = "";
const reactionsExpanded = {};
const reactionCatalog = {};
const controlsSession = {};
const REACTIONS = [
  {key:"thumbs-up",emoji:"👍",label:"Thumbs up",match:/^(?:👍|thumbs? ?up|\+1)$/i},
  {key:"clap",emoji:"👏",label:"Clap",match:/^(?:👏|clap(?:ping)?(?: hands)?|applause)$/i},
  {key:"heart",emoji:"❤️",label:"Heart",match:/^(?:❤|💖|heart|red heart|sparkling heart)$/i},
  {key:"laugh",emoji:"😂",label:"Laugh",match:/^(?:😂|😆|joy|laugh(?:ing)?|face with tears of joy)$/i},
  {key:"celebrate",emoji:"🎉",label:"Celebrate",match:/^(?:🎉|🎊|tada|party popper|celebrate|celebration)$/i},
  {key:"surprised",emoji:"😮",label:"Surprised",match:/^(?:😮|😲|open mouth|surprised|face with open mouth)$/i},
  {key:"sad",emoji:"😢",label:"Sad",match:/^(?:😢|cry|crying face|sad)$/i},
  {key:"thinking",emoji:"🤔",label:"Thinking",match:/^(?:🤔|thinking|thinking face)$/i},
  {key:"thumbs-down",emoji:"👎",label:"Thumbs down",match:/^(?:👎|thumbs? ?down|-1)$/i},
  {key:"yes",emoji:"✅",label:"Yes",match:/^(?:yes|✅|✔)$/i},
  {key:"no",emoji:"❌",label:"No",match:/^(?:no|❌|✖)$/i},
];
function reactionOf(control) {
  if (control.group !== "Reactions") return null;
  const label = control.label.replace(/[\uFE0E\uFE0F\u{1F3FB}-\u{1F3FF}]/gu, "").trim();
  return REACTIONS.find(r => control.reaction?.key === r.key || (r.match.test(label) && !["yes","no"].includes(r.key))) || null;
}
const reactionOpener = controls => controls?.find(c => c.kind === "reaction_opener" && !/^(close|hide) /i.test(c.label)
  || /^(reactions?|react|send a reaction|(?:show|open)(?: the)? reactions?)$/i.test(c.label));
async function loadControls(room) {
  const pc = roomPc[room], session = controlsSession[room];
  if (!pc) return null;
  const existing = controlsLoading.get(room);
  if (existing?.pc === pc && existing.session === session) return existing.task;
  existing?.controller.abort();
  const request = {pc, session, controller:new AbortController(), task:null};
  controlsLoading.set(room, request);
  const current = () => controlsLoading.get(room) === request
    && roomPc[room] === pc && controlsSession[room] === session;
  request.task = (async () => {
    let timer;
    try {
      // Bound both the request and its body, even when a transport ignores abort.
      const deadline = new Promise((_,reject) => {timer=setTimeout(() => {
        request.controller.abort();reject(new Error("Controls timed out"));
      },15000);});
      const read = async () => {
        const response = await controlFetch(`/api/devices/${encodeURIComponent(pc)}/meeting-controls`, {signal:request.controller.signal});
        if (!response.ok) throw new Error("unavailable");
        return response.json();
      };
      const controls = await Promise.race([read(),deadline]);
      // A response from an ended meeting or replaced room computer must never repopulate the panel.
      if (!current()) return null;
      roomControls[room] = controls;
      const reactions = (controls.controls || []).map(reactionOf).filter(Boolean);
      if (reactions.length) reactionCatalog[room] = new Set(reactions.map(r => r.key));
    } catch (_) {
      if (!current()) return null;
      roomControls[room] = {error:true};
    } finally { clearTimeout(timer); }
    return current() ? roomControls[room] : null;
  })();
  try { return await request.task; }
  finally {
    if (controlsLoading.get(room) === request) { controlsLoading.delete(room); repaint(); }
  }
}
setInterval(() => {
  if (onRemote() && controlsRoom && meetingOf(controlsRoom)?.live) loadControls(controlsRoom);
}, 3000);

function reactionsPanel(room, where = "home") {
  const state = roomControls[room];
  const controls = state?.controls || [];
  const open = reactionOpener(controls);
  const choices = new Map(controls.map(c => [reactionOf(c)?.key,c]));
  const busy = pending.has(`reaction:${room}`) || pending.has("zoom:control") || pending.has("zoom:control_back");
  const enabled = !state?.error && !!(open || controls.some(reactionOf));
  const expanded = !!reactionsExpanded[room];
  const hasChoices = controls.some(reactionOf);
  const known = reactionCatalog[room] || (hasChoices ? new Set([...choices.keys()].filter(Boolean)) : null);
  const extra = REACTIONS.slice(5).filter(r => choices.has(r.key) || known?.has(r.key));
  const buttons = items => items.map(r => `<button class="rm-reaction" data-act="rm-reaction" data-room="${esc(room)}" data-reaction="${r.key}" aria-label="Send ${r.label} reaction" ${busy || !enabled || (known ? !known.has(r.key) : !open) ? "disabled" : ""}><span class="rm-reaction-emoji" aria-hidden="true">${r.emoji}</span><span>${r.label}</span></button>`).join("");
  return `<section class="rm-reactions" aria-label="Reactions"><div class="rm-reactions-head"><h3>Reactions</h3><button class="rm-reactions-more" data-act="rm-reactions-more" data-room="${esc(room)}" aria-expanded="${expanded}" aria-controls="rm-reaction-extra-${esc(room)}-${where}" ${busy || !enabled ? "disabled" : ""}>${expanded ? "Less" : "More"}<span aria-hidden="true">${expanded ? "⌃" : "⌄"}</span></button></div>
    <div class="rm-reaction-grid">${buttons(REACTIONS.slice(0,5))}</div>
    ${expanded ? `<div class="rm-reaction-grid rm-reaction-extra" id="rm-reaction-extra-${esc(room)}-${where}">${buttons(extra)}</div>${!extra.length ? `<p class="rm-reaction-note">${hasChoices ? "No additional reactions are available in this meeting." : "Opening more reactions…"}</p>` : ''}` : `<div id="rm-reaction-extra-${esc(room)}-${where}" hidden></div>`}
    ${!state ? '<p class="rm-reaction-note">Loading reactions…</p>' : state.error || !enabled ? '<p class="rm-reaction-note">Reactions are unavailable in this meeting.</p>' : ''}
  </section>`;
}

async function openReactionPalette(room, state) {
  if (state?.controls?.some(reactionOf)) return state;
  const opener = reactionOpener(state?.controls);
  if (!opener) throw new Error("Reactions are unavailable in this meeting");
  if (!await pcCall(roomPc[room], "control", {control:opener.id}, "Opening reactions", true)) throw new Error("Couldn't open reactions");
  for (let i=0; i<12; i++) {
    state = await loadControls(room);
    if (state?.controls?.some(reactionOf)) return state;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error("This meeting isn't offering reaction choices. Try again shortly.");
}
async function sendReaction(room, key) {
  const reaction = REACTIONS.find(r => r.key === key);
  const lock = `reaction:${room}`;
  if (!reaction || pending.has(lock) || pending.has("zoom:control")) return;
  pending.set(lock, reaction.label); repaint();
  try {
    let state = await loadControls(room);
    state = await openReactionPalette(room, state);
    // Use a fresh, provider-issued token. Never guess an emoji's coordinates or DOM selector.
    const choice = state.controls.find(c => reactionOf(c)?.key === key);
    if (!choice) throw new Error(`${reaction.label} is unavailable in this meeting`);
    await pcCall(roomPc[room], "control", {control:choice.id}, reaction.label);
  } catch (error) { say(error.message, "err"); }
  finally { pending.delete(lock); loadControls(room); repaint(); }
}

function controlsSheet(room) {
  const state = roomControls[room];
  const controls = (state?.controls || []).filter(c => c.group !== "Reactions" && !reactionOpener([c]));
  const people = controls.filter(c => c.group === "People" || /^(people|show everyone|participants|(?:open|close) the manage participants)/i.test(c.label));
  const other = controls.filter(c => !people.includes(c));
  const friendly = c => /^(people|show everyone|participants)/i.test(c.label) ? "People" : /^turn on captions/i.test(c.label) ? "Turn on captions" : /^turn off captions/i.test(c.label) ? "Turn off captions" : c.label;
  const buttons = list => list.map(c => `<button class="rm-btn" data-act="rm-provider-control" data-room="${esc(room)}" data-control="${esc(c.id)}" data-label="${esc(c.label)}" ${c.destructive ? 'data-destructive="1"' : ''} ${pending.has("zoom:control") ? "disabled" : ""}><span>${esc(friendly(c))}</span>${c.context ? `<small>${esc(c.context)}</small>` : ""}</button>`).join("");
  return `<div class="rm-sheet" role="dialog" aria-label="Meeting controls"><div class="rm-sheet-card rm-meeting-controls">
    <div class="rm-sheet-head"><h2>Meeting controls</h2><button class="rm-x" data-act="rm-sheet" data-sheet="" aria-label="Close">✕</button></div>
    <div class="rm-controls-scroll">
      ${reactionsPanel(room,"sheet")}
      ${people.length ? `<section class="rm-control-section"><h3>People & host controls</h3><div class="rm-controls-list">${buttons(people)}</div></section>` : ''}
      ${other.length ? `<section class="rm-control-section"><h3>Meeting options</h3><div class="rm-controls-list">${buttons(other)}</div></section>` : ''}
      ${!state ? '<p class="rm-note">Loading meeting controls…</p>' : state.error ? '<p class="rm-note">Meeting controls are unavailable. Try refreshing.</p>' : !state.controls?.length ? '<p class="rm-note">No additional controls are currently available from this meeting.</p>' : ''}
    </div>
    <div class="rm-controls-footer">${state?.back ? `<button class="rm-btn" data-act="rm-controls-back" data-room="${esc(room)}" ${pending.has("zoom:control_back") ? "disabled" : ""}>Back · Close meeting menu</button>` : ""}<button class="rm-btn" data-act="rm-controls-refresh" data-room="${esc(room)}">Refresh controls</button></div>
  </div></div>`;
}
let sheet = ""; // "", "join" or "share": a full-screen step over the home screen
let joinText = "";
let joinPasscode = "";
let displayDraft = null;
let zoomJoinOptions = false;
const SERVICES = {meet: "Google Meet", teams: "Teams", webex: "Webex", zoom: "Zoom"};
const selectedService = {};
const servicesFor = (room) => {
  const available = pcOf(room)?.native?.meeting_providers;
  return Array.isArray(available) ? available.filter(key => SERVICES[key]) : ["meet"];
};
const serviceFor = (room) => servicesFor(room).includes(selectedService[room]) ? selectedService[room] : "meet";
const meetingInvites = {};
const inviteLoading = new Map();
async function loadInvite(room) {
  const pc = roomPc[room], session = controlsSession[room];
  if (!pc || !session) return null;
  const existing = inviteLoading.get(room);
  if (existing?.pc === pc && existing.session === session) return existing.task;
  existing?.controller.abort();
  const request = {pc, session, controller:new AbortController(), task:null};
  inviteLoading.set(room, request);
  const current = () => inviteLoading.get(room) === request
    && roomPc[room] === pc && controlsSession[room] === session;
  meetingInvites[room] = {loading:true};
  request.task = (async () => {
    let timer;
    try {
      // Include a stalled JSON body, even if the transport ignores abort.
      const deadline = new Promise((_,reject) => {timer=setTimeout(() => {
        request.controller.abort();reject(new Error("Invitation timed out"));
      },15000);});
      const read = async () => {
        const response = await controlFetch(`/api/rooms/${encodeURIComponent(room)}/meeting?device=${encodeURIComponent(pc)}`, {cache:"no-store", signal:request.controller.signal});
        if (!response.ok) throw new Error("Invitation unavailable");
        return response.json();
      };
      const invitation = await Promise.race([read(),deadline]);
      if (!current()) return null;
      meetingInvites[room] = invitation;
    } catch (_) {
      if (!current()) return null;
      meetingInvites[room] = {error:true};
    } finally { clearTimeout(timer); }
    return current() ? meetingInvites[room] : null;
  })();
  try { return await request.task; }
  finally {
    if (inviteLoading.get(room) === request) { inviteLoading.delete(room); repaint(); }
  }
}
function servicePicker(room, disabled = false) {
  const available = servicesFor(room);
  return `<div class="rm-services" role="group" aria-label="Meeting service">${Object.entries(SERVICES).map(([key, label]) => `<button class="rm-service ${serviceFor(room) === key ? "on" : ""}" aria-pressed="${serviceFor(room) === key}" data-act="rm-service" data-room="${esc(room)}" data-service="${key}" ${disabled || !available.includes(key) ? "disabled" : ""}>${label}${available.includes(key) ? "" : `<small>Setup needed</small>`}</button>`).join("")}</div>`;
}


document.addEventListener("input", (e) => {
  if (e.target.id === "rm-join") joinText = e.target.value;
  if (e.target.id === "rm-pass") joinPasscode = e.target.value;
  if (displayDraft && e.target.id === "rm-idle-page") displayDraft.idle_page = e.target.value;
  if (displayDraft && e.target.id === "rm-meeting-lead") displayDraft.meeting_lead_minutes = e.target.value;
});
document.addEventListener("keydown", (e) => {
  if (!sheet || !onRemote()) return;
  if (e.key === "Escape") { e.preventDefault(); document.activeElement?.blur?.(); sheet = ""; repaint(); }
  else if (e.key === "Enter" && ["rm-join", "rm-pass"].includes(e.target.id)) {
    e.preventDefault();
    document.querySelector('[data-act="rm-join-go"]:not(:disabled)')?.click();
  }
});

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "⌫"];

function joinSheet(room) {
  const busy = pending.has("zoom:join");
  const isMeet = Boolean(roomPc[room]);
  const service = serviceFor(room);
  const zoomFields = `<div class="rm-keypad">${KEYS.map((k) => k ? `<button class="rm-key" data-act="rm-key" data-k="${k}" aria-label="${k === "⌫" ? "Delete" : k}">${k}</button>` : "<span></span>").join("")}</div>
      <input id="rm-pass" class="rm-pass-in" value="${esc(joinPasscode)}" aria-label="Zoom passcode" placeholder="Passcode (if it has one)" autocomplete="off" autocapitalize="off" />`;
  return `<div class="rm-sheet" role="dialog" aria-label="Join a meeting">
    <div class="rm-sheet-card">
      <div class="rm-sheet-head"><h2>Join ${esc(isMeet ? SERVICES[service] || "a meeting" : "Zoom")}</h2><button class="rm-x" data-act="rm-sheet" data-sheet="" aria-label="Close">✕</button></div>
      <label class="rm-field-label" for="rm-join">${isMeet ? "Meeting code or link" : "Meeting ID or link"}</label>
      <input id="rm-join" class="rm-join-in" value="${esc(joinText)}" placeholder="${isMeet ? (service === "teams" ? "Paste the Teams invitation link" : service === "webex" ? "Paste the Webex invitation link" : service === "zoom" ? "Zoom meeting ID or invitation link" : "abc-defg-hij or a meeting link") : "Meeting ID or Zoom link"}" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="go" />
      ${isMeet ? `<button class="rm-join-options" data-act="rm-join-options" aria-expanded="${zoomJoinOptions}" aria-controls="rm-zoom-options">${zoomJoinOptions ? "Hide" : "Show"} Zoom keypad and passcode</button><div id="rm-zoom-options" class="rm-zoom-options" ${zoomJoinOptions ? "" : "hidden"}>${zoomFields}</div>` : zoomFields}
      <button class="rm-cta ${busy ? "is-pending" : ""}" data-act="rm-join-go" data-room="${esc(room)}" ${busy ? "disabled" : ""}>${busy ? spin : ""}Join</button>
      <p class="rm-dim small">${isMeet ? "Paste any Meet, Teams, Webex or Zoom invitation link. Meet codes and Zoom IDs work too." : "Type the Zoom meeting ID or paste the invitation link."}</p>
    </div></div>`;
}

function shareSheet(d, zoomOn, live, kind = "zoom", room = "", meeting = {}) {
  const invite = meetingInvites[room];
  const pcShare = live && kind === "pc" ? `
    <div class="rm-present">
      <div class="rm-present-code">
        ${invite?.url ? `<img src="${esc(invite.qr)}" class="rm-qr-big" alt="Scan to join this meeting" /><p>Scan to join this meeting</p><button class="rm-btn" data-act="rm-copy-invite" data-room="${esc(room)}">Copy meeting link</button>` : `<p class="rm-note">${invite?.loading ? "Getting meeting link…" : "Meeting link not available yet."}</p><button class="rm-btn" data-act="rm-invite-refresh" data-room="${esc(room)}" ${invite?.loading ? "disabled" : ""}>Get meeting link</button>`}
      </div>
      <div class="rm-present-steps">
        <h3>Present from your laptop</h3>
        <p><b>1. Join this meeting</b><span>Scan the code or copy the meeting link to your laptop.</span></p>
        <p><b>2. Keep laptop audio off</b><span>Keep your laptop microphone and speakers off. The room handles audio.</span></p>
        <p><b>3. Choose ${meeting?.provider === "meet" ? "Present" : "Share"}</b><span>Choose a tab, window or screen on your laptop.</span></p>
        ${invite?.url ? `<a class="rm-btn rm-invite-link" href="${esc(invite.url)}" target="_blank" rel="noopener noreferrer">Open meeting on this device ↗</a><p class="rm-invite-url">${esc(invite.url)}</p>` : ""}
      </div>
    </div>
    ${meeting?.sharing ? `<button class="rm-btn" data-act="rm-zoom" data-room="${esc(room)}" data-zoom="share_stop" data-label="Stop room presentation" ${pending.has("zoom:share_stop") ? "disabled" : ""}>Stop room presentation</button>` : ""}
    <p class="rm-note">The room display stays in the meeting.</p>` : "";
  const xos = d && d.share_code && !d.share_off;
  // The meeting runs on the room's computer, so sharing into it goes through the meeting app.
  const how = kind === "pc"
    ? `on your laptop, join the same meeting with the microphone and speakers off, then choose <b>Share</b> (or <b>Present</b> in Meet).`
    : `in the Zoom app on your laptop, choose <b>Share screen</b> and type the sharing key shown on the room TV.`;
  const zoomHow = zoomOn ? `<p class="rm-share-zoom"><b>${live ? "Share into the meeting" : "Share in a meeting"}:</b> ${how}</p>` : "";
  return `<div class="rm-sheet" role="dialog" aria-label="Share your screen">
    <div class="rm-sheet-card rm-share-card">
      <div class="rm-sheet-head"><h2>Share your screen</h2><button class="rm-x" data-act="rm-sheet" data-sheet="" aria-label="Close">✕</button></div>
      ${live ? pcShare || zoomHow : ""}
      ${xos && !live ? `<img src="/api/share/${esc(d.share_code)}/qr.svg" alt="" class="rm-qr-big" />
      <p>${zoomOn ? "To show your screen on the TV without a meeting, open" : "On your laptop or phone, open"}<br /><b class="rm-link">${esc(location.host)}/s/${esc(d.share_code)}</b><br />or scan the code.${d.airplay ? ` On a Mac or iPhone you can also AirPlay to <b>${esc(d.airplay)}</b>.` : ""}</p>` : ""}
      ${live ? "" : zoomHow}
      ${d?.sharing ? `<button class="rm-cta rm-cta-stop" data-act="rm-share-stop" data-dev="${esc(d.id)}">Stop sharing</button>` : ""}
    </div></div>`;
}

function displaySheet(room) {
  const d = displayDraft || S.room(room)?.display || {idle_page:"",meeting_lead_minutes:15};
  return `<div class="rm-sheet" role="dialog" aria-label="Room display settings"><div class="rm-sheet-card">
    <div class="rm-sheet-head"><h2>Between meetings</h2><button class="rm-x" data-act="rm-sheet" data-sheet="" aria-label="Close">✕</button></div>
    <label for="rm-idle-page">Default TV screen</label>
    <select id="rm-idle-page" class="rm-input"><option value="">Meeting view all the time</option>${getPages().map(p=>`<option value="${esc(p.id)}" ${d.idle_page===p.id?"selected":""}>${esc(p.name)}</option>`).join("")}</select>
    <label for="rm-meeting-lead">Show meeting view before a booking</label>
    <div class="rm-display-lead"><input id="rm-meeting-lead" class="rm-input" type="number" inputmode="numeric" min="0" max="120" step="1" value="${esc(d.meeting_lead_minutes)}" /><span>minutes before</span></div>
    <p class="rm-note">After a call ends, the default screen returns. The meeting view appears before the next scheduled booking. This never joins a meeting automatically or interrupts a call, screen sharing, or whiteboard.</p>
    <button class="rm-cta" data-act="rm-display-save" data-room="${esc(room)}" ${pending.has("display")?"disabled":""}>${pending.has("display")?"Saving…":"Save room display"}</button>
  </div></div>`;
}

/** `d` is the Xos screen being driven, or null in a room whose TV is its Zoom Room PC. */
function homeView({ roomId, roomName, d, group }) {
  const z = roomId ? zoomInfo[roomId] : null;
  if (roomId && !z) refreshZoom(roomId);
  const meet = meetingOf(roomId);
  const zoomOn = Boolean(meet); // the room has a meeting computer: its Zoom Room or a room computer
  const live = Boolean(meet?.live);
  // Offline, or (Zoom) created but its PC not signed in yet: it can't take a meeting.
  const zOffline = Boolean(meet?.down);
  const canShare = Boolean((d?.share_code && !d.share_off) || zoomOn);
  const tvOff = d?.native?.displayOn === false;
  const a = av(roomId);
  const busy = (k) => pending.has(k);
  const joining = busy("zoom:join") || busy("zoom:meet_now");
  const zb = (action, label, icon, cls = "", extra = "", hint = "") => `<button class="rm-big ${cls} ${busy(`zoom:${action}`) ? "is-pending" : ""}" data-act="rm-zoom" data-room="${esc(roomId)}" data-zoom="${action}" data-label="${esc(label)}" ${extra} ${zOffline || busy(`zoom:${action}`) || (action === "meet_now" && joining) ? "disabled" : ""}>
      <span class="rm-big-ico">${busy(`zoom:${action}`) ? spin : icon}</span><span class="rm-big-l">${label}</span>${hint ? `<span class="rm-big-sub">${hint}</span>` : ""}</button>`;
  const status = meet ? meet.status : d ? statusOf(d) : ["off", "Zoom unavailable"];
  const share = canShare ? `<button class="rm-big" data-act="rm-sheet" data-sheet="share" data-room="${esc(roomId)}"><span class="rm-big-ico">${ICON.share}</span><span class="rm-big-l">${d?.sharing ? "Sharing now" : "Share screen"}</span><span class="rm-big-sub">${live ? "Present from your laptop" : "Your laptop on the TV"}</span></button>` : "";
  const providerKey = live && meet?.kind === "pc" ? meet.m.provider : meet?.kind === "pc" ? serviceFor(roomId) : "zoom";
  const service = SERVICES[providerKey] || "Meeting";

  let main;
  if (live) {
    const provider = meet.kind === "pc" ? SERVICES[meet.m.provider] || "Meeting" : "Zoom";
    main = `<section class="rm-meeting-stage" aria-label="Current meeting">
      <div class="rm-meeting-provider">${ICON.cam}<span>${esc(provider)}</span><span class="rm-meeting-connected"><i></i>Connected</span></div>
      <h1 class="rm-meeting-title">${meet.kind === "pc" && meet.m.title ? esc(meet.m.title) : `${esc(roomName)} meeting`}</h1>
      <div class="rm-meeting-states"><span>${a.muted === null ? "Microphone unavailable" : a.muted ? "Microphone off" : "Microphone on"}</span><span>${a.cameraOff === null ? "Camera unavailable" : a.cameraOff ? "Camera off" : "Camera on"}</span>${meet.m?.sharing ? `<span>Presenting</span>` : ""}</div>
    </section>
    ${meet.kind === "pc" ? reactionsPanel(roomId) : ""}
    <div class="rm-bigs rm-bigs-live">
      ${a.muted === null ? zb("mute", "Microphone unavailable", ICON.micOff, "is-alert", "disabled") : a.muted ? zb("unmute", "Unmute", ICON.micOff, "is-alert", "", "The room is muted") : zb("mute", "Mute", ICON.mic, "", "", "The room can be heard")}
      ${a.cameraOff === null ? zb("camera_off", "Camera unavailable", ICON.camOff, "is-alert", "disabled") : a.cameraOff ? zb("camera_on", "Camera on", ICON.camOff, "is-alert", "", "The camera is off") : zb("camera_off", "Camera off", ICON.cam, "", "", "The room is on camera")}
      <div class="rm-big rm-vol"><span class="rm-big-ico">${ICON.vol}</span><span class="rm-big-l">Volume ${a.volume}</span>
        <div class="rm-vol-row"><button class="rm-btn" data-act="rm-vol" data-room="${esc(roomId)}" data-d="-10" aria-label="Quieter" ${busy("zoom:volume") ? "disabled" : ""}>−</button><button class="rm-btn" data-act="rm-vol" data-room="${esc(roomId)}" data-d="10" aria-label="Louder" ${busy("zoom:volume") ? "disabled" : ""}>+</button></div></div>
      ${share}
      ${meet.kind === "pc" ? `<button class="rm-big" data-act="rm-sheet" data-sheet="controls" data-room="${esc(roomId)}"><span class="rm-big-ico">${ICON.join}</span><span class="rm-big-l">Meeting controls</span><span class="rm-big-sub">People · Hand · Captions</span></button>` : ""}
      ${zb("leave", "Leave", ICON.leave, "rm-big-leave", "", "Hang up in the room")}
    </div>
    ${meet.kind === "pc" && meet.m.admission_pending ? `<button class="rm-btn" data-act="rm-sheet" data-sheet="controls" data-room="${esc(roomId)}">Admission requests · Review people waiting to join</button>` : ""}
    ${meet.kind === "pc" && ["connection", "media"].includes(meet.m.display_attention) ? `<p class="rm-note" role="status">${meet.m.display_attention === "connection" ? "The meeting connection needs attention. Check the room network." : "The meeting reports a microphone or camera issue. Check the room devices."}</p>` : ""}
    ${meet.kind === "pc" && (a.muted === null || a.cameraOff === null) ? `<p class="rm-note">On the room computer, check the meeting’s microphone and camera permissions and selected devices. Allow microphone and camera access in the room browser to enable these controls.</p>` : ""}
    ${meet.kind === "pc" && meet.m.needs_host ? `<p class="rm-note">Zoom needs a new host before the room can leave. Choose the next host on the room computer, or end the meeting for everyone below.</p>` : ""}
    ${meet.kind !== "pc" || (meet.m.provider === "zoom" && meet.m.can_end) ? `<button class="rm-end" data-act="rm-zoom" data-room="${esc(roomId)}" data-zoom="end" data-label="End meeting" data-confirm="1">End the meeting for everyone</button>` : ""}`;
  } else if (meet?.waiting) {
    main = `<section class="rm-wait">${spin}<div><b>Waiting to be let in</b>
      <span>The room asked to join${meet.m.title ? ` ${esc(meet.m.title)}` : ""}. Someone in the meeting lets it in.</span></div></section>
    <div class="rm-bigs rm-bigs-one">${zb("leave", "Cancel", ICON.leave, "rm-big-leave", "", "Stop asking to join")}</div>`;
  } else if (zoomOn) {
    const card = calCard(roomId, !zOffline);
    // The booking's own Join is the one to press; the keypad is for anything else.
    const go = card.includes('data-act="rm-cal-join"') ? "" : "rm-big-go";
    main = `${meet.kind === "pc" ? servicePicker(roomId, pending.has("zoom:join") || pending.has("zoom:meet_now")) : ""}${card}
    <div class="rm-bigs">
      <button class="rm-big ${go}" data-act="rm-sheet" data-sheet="join" ${zOffline || joining ? "disabled" : ""}><span class="rm-big-ico">${ICON.join}</span><span class="rm-big-l">Join a meeting</span><span class="rm-big-sub">Code, ID or invitation link</span></button>
      ${zb("meet_now", "Start a meeting", ICON.start, "", meet.kind === "pc" && z?.instant_meetings !== true ? "disabled" : "", meet.kind === "pc" && z?.instant_meetings !== true ? "Unavailable · Room joins as a guest" : `A new ${esc(service)} meeting`)}
      ${share}
    </div>
    ${meet.kind === "pc" ? `<p class="rm-note">Join from the room calendar above, or enter an invitation code. The meeting host may need to admit the room.</p>` : ""}
    ${meet.kind === "zoom" && z?.status === "Offline" ? `<p class="rm-note">The room's Zoom computer is offline. Check it's switched on and connected.</p>` : ""}
    ${meet.kind === "zoom" && z?.status === "UnderConstruction" ? `<p class="rm-note">The room's Zoom computer isn't signed in yet: enter its activation code in Zoom Rooms (Admin → Integrations shows it).</p>` : ""}
    ${meet.kind === "pc" && meet.down ? `<p class="rm-note">The room computer is offline. Check the room computer is on and Xos Room is running.</p>` : ""}
    ${meet.kind === "pc" && !meet.down && meet.m.error ? `<p class="rm-note">${esc(meet.m.error)}</p>` : ""}`;
  } else if (!d) {
    main = `<p class="rm-note">Zoom isn't answering right now${z?.error ? `: ${esc(z.error)}` : ""}. It retries every few seconds.</p>`;
  } else {
    main = `${roomId ? calCard(roomId, false) : ""}
    <div class="rm-bigs">
      ${share}
      <button class="rm-big" data-act="rm-mode" data-mode="more"><span class="rm-big-ico">${ICON.tv}</span><span class="rm-big-l">Choose what's on the TV</span></button>
    </div>
    ${roomId ? `<p class="rm-note">Meetings aren't set up in this room yet.</p>` : ""}`;
  }

  const tvBusy = d && (busy(`${d.id}:display_on`) || busy(`${d.id}:display_off`) || busy(`${d.id}:switch_input`));
  // Only the Xos TV app can switch the TV itself on and off (HDMI-CEC). Offline too: TV on tries
  // to wake a stick that dropped off while asleep.
  const tv = d?.native && d.role !== "roompc" ? `<div class="rm-tv"><span>TV</span>
      <button class="rm-seg ${!tvOff ? "on" : ""}" data-act="rm-cmd" data-dev="${esc(d.id)}" data-type="display_on" data-key="${esc(`${d.id}:display_on`)}" data-label="TV on">On</button>
      <button class="rm-seg ${tvOff ? "on" : ""}" data-act="rm-cmd" data-dev="${esc(d.id)}" data-type="display_off" data-key="${esc(`${d.id}:display_off`)}" data-label="TV off">Off</button>${tvBusy ? spin : ""}</div>
    ${switchInputProblem(d) ? "" : `<button class="rm-more" data-act="rm-cmd" data-dev="${esc(d.id)}" data-type="switch_input" data-key="${esc(`${d.id}:switch_input`)}" data-label="${SWITCH_LABEL}" title="${esc(SWITCH_HELP)}" ${busy(`${d.id}:switch_input`) ? "disabled" : ""}>${SWITCH_LABEL}</button>`}` : "";

  return `<div class="rm rm-home ${live ? "rm-in-call" : ""}" data-whiteboard-room="${esc(roomId)}" data-provider="${esc(providerKey)}">
    <header class="rm-head">
      <img src="../static/img/logo-white.svg" alt="Xos" class="rm-logo" />
      <div class="rm-title"><b>${esc(roomName)}</b><span class="rm-pill rm-pill-${status[0]}"><i></i>${status[1]}</span></div>
      <div class="rm-clock">${clock()}</div>
    </header>
    <main class="rm-home-body">${main}<div class="rm-home-tools" aria-label="Room tools">${roomId && !live ? `<button class="rm-whiteboard" data-act="wb-start" data-room="${esc(roomId)}" ${live || meet?.waiting || joining ? "disabled" : ""}><span>Whiteboard</span><small>${live || meet?.waiting || joining ? "Available after the meeting" : "Draw on the iPad · Show on the TV"}</small></button>` : ""}</div></main>
    <nav class="rm-home-foot">${tv}${roomId && !live && !meet?.waiting ? `<button class="rm-more" data-act="rm-display-home" data-room="${esc(roomId)}">Show room screen</button>` : ""}<span class="rm-grow"></span>${roomId ? `<button class="rm-more" data-act="rm-sheet" data-sheet="display" data-room="${esc(roomId)}">Room settings</button>` : ""}${d ? `<button class="rm-more" data-act="rm-mode" data-mode="more" data-group="${esc(group)}">More ›</button>` : ""}</nav>
    <footer class="rm-foot rm-toast rm-toast-${pending.size ? "busy" : toast.text ? toast.kind || "ok" : "idle"}" role="status" aria-live="polite">${pending.size ? `${spin}<span>${esc([...pending.values()].at(-1))}…</span>` : toast.text ? `<span>${esc(plain(toast.text))}</span>` : ""}</footer>
    ${sheet === "display" ? displaySheet(roomId) : sheet === "controls" && live && meet?.kind === "pc" ? controlsSheet(roomId) : sheet === "join" && zoomOn ? joinSheet(roomId) : sheet === "share" && canShare ? shareSheet(d, zoomOn, live, meet?.kind, roomId, meet?.m) : ""}
  </div>`;
}

function statusOf(d) {
  if (!d.online) return ["off", "Offline"];
  if (d.sharing) return ["live", "Being shared to"];
  if (d.native?.displayOn === false) return ["sleep", "TV off"];
  return ["on", "On"];
}

const msg = (html) => `<div class="rm-msg">${html}</div>`;

// What the room computer reports is for people: never show its browser's internals.
function plain(text) {
  if (/Target\.|Runtime\.|Page\.|Input\.|CdpError|browser closed|no answer from the browser/.test(text)) {
    return text.replace(/:.*$/, "") + ": the room computer's browser restarted. Try again in a moment.";
  }
  return text;
}

/** #/tablet/<room>: the table remote for every screen in the room. */
export function renderRoomRemote(roomId) {
  return renderController({ room: roomId });
}

/** #/remote/<screen>: a remote that opens on one screen, within that screen's room. */
export function renderRemote(key) {
  return renderController({ key: key || "" });
}

/**
 * #/control/<id>: the remote for the one screen someone took from #/control. `bar` is the
 * "you're in control" strip with its Release button.
 */
export function renderSolo(id, bar, operate = false) {
  return renderController({ key: id, solo: true, bar, operate });
}

// `operate`: may switch the TV's input (screens.operate). Room remotes send their device key, which
// may; on #/control only a screen operator may (control.js).
function renderController({ room, key, solo = false, bar = "", operate = true }) {
  if (!S.isHub()) return msg("The room remote needs the Xos server: open it from your server address.");
  connectLive();
  if (devices === null) { refresh(); loadPages(); return msg(`${spin} Connecting…`); }
  let roomId = room || "";
  let anchor = null;
  if (key !== undefined) {
    anchor = findScreen(devices, key);
    // No such screen, but a room is called that (#/remote/atrium-room after its TV became the
    // Zoom Room's PC): open the room's remote.
    const named = !anchor && !solo ? findRoom(S.rooms(), key) : null;
    if (!anchor && !named) {
      return msg(`<b>No screen called “${esc(key)}”.</b><br />Name it in Admin → Screens, then open
        <code>#/remote/&lt;name&gt;</code>. Screens: ${devices.map((x) => esc(devName(x))).join(", ") || "none yet"}`);
    }
    roomId = anchor ? roomOf(anchor) : named.id;
  }
  const screens = roomId && !solo ? displayMembers(devices, S.rooms(), roomId) : [];
  if (anchor && !screens.includes(anchor)) screens.unshift(anchor);
  controlsRoom = roomId;
  const currentMeeting = meetingOf(roomId);
  const session = currentMeeting?.live && currentMeeting.kind === "pc" ? `${currentMeeting.pc.id}|${currentMeeting.m.provider}|${currentMeeting.m.title || ""}|${currentMeeting.m.join_url || ""}` : "";
  if (controlsSession[roomId] !== session) {
    controlsSession[roomId] = session; roomControls[roomId] = null;
    controlsLoading.get(roomId)?.controller.abort(); controlsLoading.delete(roomId);
    inviteLoading.get(roomId)?.controller.abort(); inviteLoading.delete(roomId);
    delete meetingInvites[roomId];
    delete reactionCatalog[roomId]; reactionsExpanded[roomId] = false;
  }
  if (currentMeeting?.live && currentMeeting.kind === "pc" && !roomControls[roomId]) loadControls(roomId);
  if (sheet === "share" && currentMeeting?.live && currentMeeting.kind === "pc" && !meetingInvites[roomId]) loadInvite(roomId);
  const roomName = (!solo && S.room(roomId)?.name) || (anchor ? devName(anchor) : roomId);
  if (!screens.length) {
    // A meeting room whose TV is driven by its Zoom Room PC has no Xos screen: the remote is the
    // Zoom controller.
    const z = roomId ? zoomInfo[roomId] : null;
    const pc = roomId ? pcOf(roomId) : null;
    if (roomId && !z && !pc) { refreshZoom(roomId); return msg(`${spin} Connecting…`); }
    if (z?.mapped || pc) return homeView({ roomId, roomName, d: null, group: roomId });
    return msg(`<b>${esc(roomName)}</b><br />Nothing is set up in this room yet. In Admin → Screens, open the room's TV and set its room to ${esc(roomName)}, or link the room's Zoom Room in Admin → Integrations.`);
  }
  const group = roomId || anchor.id;
  // A remote link for one screen opens on it; after that, the screen last picked here.
  const d = selectedDisplay(screens, pickedIn(group), anchor);
  const computerDisplay = d.role === "roompc";
  if (!computerDisplay && d.native && !d.screenshot && !previewing.has(d.id) && d.connected) preview(d);

  const pages = getPages();
  const route = optimistic.get(d.id)?.route || (computerDisplay ? `page/${S.room(roomId)?.display?.idle_page || ""}` : `${d.role}/${d.arg}`);
  const showingId = route.startsWith("page/") ? route.slice(5) : "";
  const showing = pages.find((p) => p.id === showingId);
  const [stCls, stText] = statusOf(d);
  const tvOff = d.native?.displayOn === false;
  const btn = (type, label, args = {}, cls = "", id = type) => {
    const k = `${d.id}:${id}`;
    const busy = pending.has(k);
    return `<button class="rm-btn ${cls} ${busy ? "is-pending" : ""}" data-act="rm-cmd" data-dev="${esc(d.id)}" data-type="${type}" data-key="${esc(k)}" data-label="${esc(label.replace(/^[◀▶↻◉]\s*|\s*[◀▶]$/g, ""))}" data-args='${esc(JSON.stringify(args))}' aria-busy="${busy}">${busy ? spin : ""}${label}</button>`;
  };
  const switcher = screens.length > 1 ? `<nav class="rm-screens" role="tablist" aria-label="Screens in ${esc(roomName)}">
      ${screens.map((s) => { const [c, t] = statusOf(s); return `<button role="tab" aria-selected="${s.id === d.id}" class="rm-scr ${s.id === d.id ? "on" : ""}" data-act="rm-pick" data-group="${esc(group)}" data-dev="${esc(s.id)}">
        <span class="rm-dot rm-dot-${c}" title="${esc(t)}"></span><span class="rm-scr-name">${esc(devName(s))}</span></button>`; }).join("")}
      <span class="rm-grow"></span>
      ${["display_on", "display_off"].map((t) => { const k = `all:${t}`; const busy = pending.has(k);
        return `<button class="rm-btn rm-all ${t === "display_on" ? "rm-on" : "rm-off"} ${busy ? "is-pending" : ""}" data-act="rm-all" data-group="${esc(group)}" data-room="${esc(roomId)}" data-type="${t}">${busy ? spin : ""}${t === "display_on" ? "All on" : "All off"}</button>`; }).join("")}
    </nav>` : "";

  if (!solo && mode !== "more") return homeView({ roomId, roomName, d, group });

  return `<div class="rm" data-whiteboard-room="${esc(roomId)}">
    <header class="rm-head">
      <img src="../static/img/logo-white.svg" alt="Xos" class="rm-logo" />
      <div class="rm-title"><b>${esc(roomName)}</b><span class="rm-dim">${screens.length > 1 ? `${screens.length} screens` : `${devName(d) !== roomName ? `${esc(devName(d))} · ` : ""}<span class="rm-st rm-st-${stCls}">● ${stText}</span>`}</span></div>
      ${solo ? "" : `<button class="rm-more" data-act="rm-mode" data-mode="home">‹ Room</button>`}
      <div class="rm-clock">${clock()}</div>
    </header>
    ${bar}
    ${switcher}
    <div class="rm-body">
      <section class="rm-left rm-group" aria-label="${esc(devName(d))}">
        <div class="rm-group-head"><h2>${esc(devName(d))}</h2><span class="rm-st rm-st-${stCls}">● ${stText}</span></div>
        ${computerDisplay ? `<p class="rm-note">The room computer drives this TV.</p>` : `<button class="rm-preview" data-act="rm-preview-btn" data-dev="${esc(d.id)}" aria-label="Update the preview">
          ${d.screenshot ? `<img src="/api/devices/${encodeURIComponent(d.id)}/screenshot?t=${Math.max(shotAt, Math.round(d.screenshot.at * 1000))}" alt="What ${esc(devName(d))} shows" />` : `<div class="rm-noshot">${d.native ? "Tap to see what it shows" : "Preview needs the Xos TV app"}</div>`}
          ${previewing.has(d.id) ? `<span class="rm-shot-spin">${spin}</span>` : ""}
          <div class="rm-now">Now showing · <b>${esc(showing?.name || d.label || "—")}</b></div>
        </button>`}
        ${computerDisplay ? "" : `<div class="rm-row rm-power">${btn("display_on", "TV on", {}, `rm-on ${!tvOff && d.online ? "is-active" : ""}`)}${btn("display_off", "TV off", {}, `rm-off ${tvOff ? "is-active" : ""}`)}</div>`}
        ${computerDisplay || !d.native || !operate ? "" : switchInputProblem(d)
          ? `<p class="rm-note">${esc(switchInputProblem(d))}</p>`
          : `<div class="rm-row">${btn("switch_input", SWITCH_LABEL)}</div><p class="rm-note">${esc(SWITCH_HELP)}</p>`}
        <div class="rm-row">
          ${computerDisplay ? "" : btn("page_key", "◀ Previous", { key: "ArrowLeft" }, "", "prev")}
          ${computerDisplay ? "" : btn("page_key", "Next ▶", { key: "ArrowRight" }, "", "next")}
          ${btn(computerDisplay ? "refresh" : "reload", "↻ Reload")}
        </div>
      </section>
      <section class="rm-right">
        <section class="rm-group" aria-label="Show on ${esc(devName(d))}">
          <div class="rm-group-head"><h2>Show on ${esc(devName(d))}</h2></div>
          <div class="rm-tiles">${pages.map((p) => {
            const k = `${d.id}:route:${p.id}`;
            const busy = pending.has(k);
            return `<button class="rm-tile ${showingId === p.id ? "on" : ""} ${busy ? "is-pending" : ""}" aria-pressed="${showingId === p.id}" data-act="rm-cmd" data-dev="${esc(d.id)}" data-type="route" data-key="${esc(k)}" data-args='${esc(JSON.stringify({ route: `page/${p.id}` }))}' data-label="${esc(p.name)}">
              <span class="rm-ico">${busy ? spin : ICONS[p.kind] || "▣"}</span><span>${esc(p.name)}</span></button>`;
          }).join("") || `<span class="rm-dim">Add pages in Admin → Content.</span>`}</div>
        </section>
        ${computerDisplay ? "" : `<section class="rm-group" aria-label="Message">
          <div class="rm-group-head"><h2>Message on ${esc(devName(d))}</h2></div>
          <div class="rm-presets">${["Meeting starts in 5 minutes", "Room booked — please wrap up", "Welcome"].map((m) =>
            `<button class="rm-chip" data-act="rm-cmd" data-dev="${esc(d.id)}" data-type="message" data-key="${esc(`${d.id}:message`)}" data-args='${esc(JSON.stringify({ text: m, seconds: 15 }))}' data-label="Message">${esc(m)}</button>`).join("")}</div>
          <form class="rm-row" data-rm-say="${esc(d.id)}"><input id="rm-msg" maxlength="200" placeholder="Type a message…" enterkeyhint="send" /><button class="rm-btn" data-act="rm-say" data-dev="${esc(d.id)}">Show</button></form>
        </section>
        ${sharePanel(d)}` }
        ${solo || roomPc[roomId] ? "" : zoomPanel(roomId)}
      </section>
    </div>
    <footer class="rm-foot rm-toast-${toast.kind || "idle"}" role="status" aria-live="polite">${pending.size ? `${spin} ${esc([...pending.values()].at(-1))}…` : esc(toast.text || (screens.length > 1 ? "Pick a screen above, then tap what to show." : "Tap a page to put it on the TV."))}</footer>
  </div>`;
}

export function handleRemote(act, el) {
  if (act === "rm-reaction") { sendReaction(el.dataset.room, el.dataset.reaction); return "local"; }
  if (act === "rm-reactions-more") {
    const room = el.dataset.room;
    reactionsExpanded[room] = !reactionsExpanded[room];
    if (reactionsExpanded[room]) openReactionPalette(room, roomControls[room]).catch(error => say(error.message,"err")).finally(repaint);
    return true;
  }
  if (act === "rm-service") {
    if (!SERVICES[el.dataset.service] || !servicesFor(el.dataset.room).includes(el.dataset.service) || pending.has("zoom:join") || pending.has("zoom:meet_now")) return "local";
    const current = meetingOf(el.dataset.room);
    if (current?.live || current?.waiting) return "local";
    selectedService[el.dataset.room] = el.dataset.service;
    haptic("selection");
    return true;
  }
  if (act === "rm-invite-refresh") { loadInvite(el.dataset.room); return "local"; }
  if (act === "rm-copy-invite") {
    const url = meetingInvites[el.dataset.room]?.url;
    if (url && !navigator.clipboard?.writeText) { say("Scan the meeting code instead", "warn"); return "local"; }
    if (url) navigator.clipboard.writeText(url).then(() => say("Meeting link copied"), () => say("Scan the meeting code instead", "warn"));
    return "local";
  }
  if (act === "rm-display-home") {
    pending.set("display", "Returning to room screen");
    controlFetch(`/api/rooms/${encodeURIComponent(el.dataset.room)}/display/home`, {method:"POST"})
      .then(async r=>{const result=await r.json();if(!r.ok)throw Error(result.detail||"Could not show room screen");await S.loadConfig();say("Room screen selected");})
      .catch(e=>say(e.message,"err")).finally(()=>{pending.delete("display");repaint();});
    return true;
  }
  if (act === "rm-display-save") {
    const minutes=Number(displayDraft?.meeting_lead_minutes);
    if (!Number.isInteger(minutes) || minutes<0 || minutes>120) { say("Choose a whole number from 0 to 120 minutes","err"); return "local"; }
    pending.set("display","Saving room display");
    controlFetch(`/api/rooms/${encodeURIComponent(el.dataset.room)}/display`, {
      method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({...displayDraft,meeting_lead_minutes:minutes})
    }).then(async r=>{const result=await r.json();if(!r.ok)throw Error(result.detail||"Could not save room display");await S.loadConfig();sheet="";say("Room display saved. The TV updates within five seconds.");})
      .catch(e=>say(e.message,"err")).finally(()=>{pending.delete("display");repaint();});
    return true;
  }
  if (act === "rm-mode") { mode = el.dataset.mode; sheet = ""; haptic("selection"); return true; }
  if (act === "rm-sheet") {
    sheet = el.dataset.sheet;
    if (sheet === "display") { displayDraft={...(S.room(el.dataset.room)?.display || {idle_page:"",meeting_lead_minutes:15})}; loadPages(); }
    if (sheet === "controls") { roomControls[el.dataset.room] = null; loadControls(el.dataset.room); }
    if (sheet === "share" && roomPc[el.dataset.room]) loadInvite(el.dataset.room);
    if (sheet === "join") { joinText = ""; joinPasscode = ""; zoomJoinOptions = false; }
    haptic("selection");
    return true;
  }
  if (act === "rm-join-options") { zoomJoinOptions = !zoomJoinOptions; return true; }
  if (act === "rm-key") {
    const input = document.getElementById("rm-join");
    joinText = el.dataset.k === "⌫" ? joinText.slice(0, -1) : joinText + el.dataset.k;
    if (input) input.value = joinText;
    haptic("selection");
    return "local";
  }
  if (act === "rm-join-go") {
    const meeting = (document.getElementById("rm-join")?.value || joinText).trim();
    const passcode = document.getElementById("rm-pass")?.value.trim() || "";
    if (!meeting) { say("Paste a Meet, Teams, Webex or Zoom link, or enter a Meet code or Zoom ID", "warn"); return true; }
    sheet = "";
    zoomCall(el.dataset.room, "join", { meeting, passcode }, "Joining the meeting");
    return true;
  }
  if (act === "rm-cal-join") {
    zoomCall(el.dataset.room, "join", { meeting: el.dataset.url }, `Joining ${el.dataset.title}`);
    return true;
  }
  if (act === "rm-vol") {
    const room = el.dataset.room;
    const volume = Math.max(0, Math.min(100, av(room).volume + Number(el.dataset.d)));
    zoomCall(room, "volume", { volume }, `Volume ${volume}`);
    return true;
  }
  if (act === "rm-pick") {
    pick(el.dataset.group, el.dataset.dev);
    haptic("selection");
    return true;
  }
  if (act === "rm-all") {
    const type = el.dataset.type;
    const k = `all:${type}`;
    const targets = members(devices, S.rooms(), el.dataset.room, "screen")
      .filter((d) => d.online || (type === "display_on" && d.native));
    if (!targets.length || pending.has(k)) return "local";
    pending.set(k, type === "display_on" ? "Turning every screen on" : "Turning every screen off");
    haptic();
    Promise.all(targets.map((d) => post(`/api/devices/${encodeURIComponent(d.id)}/commands`, { type, args: {} })
      .then((c) => waitFor(d.id, c.id, wakeWait(d, type))).catch(() => null)))
      .then((res) => {
        const ok = res.filter((c) => c?.status === "done").length;
        say(`${ok} of ${targets.length} screens ${type === "display_on" ? "on" : "off"}`, ok === targets.length ? "ok" : "warn");
        pending.delete(k);
        refresh();
      });
    return true;
  }
  if (act === "rm-controls-back") {
    const room = el.dataset.room;
    if (roomControls[room]?.back) pcCall(roomPc[room], "control_back", {}, "Back").then(() => loadControls(room));
    return "local";
  }
  if (act === "rm-controls-refresh") { loadControls(el.dataset.room); return "local"; }
  if (act === "rm-provider-control") {
    const room = el.dataset.room;
    const current = roomControls[room]?.controls?.find(c => c.id === el.dataset.control);
    if (!current) { loadControls(room); return "local"; }
    if (current.destructive && !confirm(`${current.label}: ${current.context}?`)) return "local";
    pcCall(roomPc[room], "control", {control:current.id}, current.label).then(() => loadControls(room));
    return true;
  }
  if (act === "rm-zoom") {
    const action = el.dataset.zoom;
    if (el.dataset.confirm && !confirm("End the meeting for everyone?")) return "local";
    const extra = action === "join" ? {
      meeting: document.getElementById("rm-mid").value.trim(),
      passcode: document.getElementById("rm-pwd").value.trim(),
    } : action === "meet_now" && roomPc[el.dataset.room] ? {provider: serviceFor(el.dataset.room)} : {};
    if (action === "join" && !extra.meeting) { say("Enter a meeting ID or link", "warn"); return true; }
    zoomCall(el.dataset.room, action, extra, el.dataset.label);
    return true;
  }
  const d = (devices || []).find((x) => x.id === el.dataset.dev);
  if (!d) return false;
  if (act === "rm-share-stop") {
    const k = `${d.id}:share-stop`;
    pending.set(k, "Stopping the share");
    haptic();
    post(`/api/share/${encodeURIComponent(d.share_code)}/stop`, {})
      .then(() => say("Sharing stopped ✓"), (err) => say(`Couldn't stop: ${err.message}`, "err"))
      .finally(() => { pending.delete(k); refresh(); });
    return true;
  }
  if (act === "rm-cmd") {
    send(d, el.dataset.type, JSON.parse(el.dataset.args || "{}"), el.dataset.label || el.textContent.trim(), el.dataset.key);
    return true;
  }
  if (act === "rm-say") {
    const input = document.getElementById("rm-msg");
    if (input.value.trim()) send(d, "message", { text: input.value.trim(), seconds: 15 }, "Message", `${d.id}:message`);
    input.value = "";
    input.blur();
    return true;
  }
  if (act === "rm-preview-btn") { haptic("selection"); preview(d); return true; }
  return false;
}
