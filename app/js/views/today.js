// Xos → Today (#/admin/today/<site>): the home for running a site, made for an iPad in the hand.
// Every room in the site with what's happening in it now and next, its screens and meeting
// computer, and the one-tap controls a workspace manager needs; the demo on stage; and anything
// that needs attention. Admin's Screens, Content and Demo Studio stay one tap away.

import * as S from "../state.js";
import { esc, untilWhen } from "../ui.js";
import { members } from "../groups.js";
import { userFetch } from "../api.js";

const fetch = userFetch;
const SITE_KEY = "xos-today-site";
const calendars = {}; // room id -> {at, bookings, error}
let demo = null, demoAt = 0, note = "", busy = new Set();

const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const greeting = () => { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening"; };
const firstName = (me) => (me?.name || me?.email || "").split(/[ @.]/)[0].replace(/^./, (c) => c.toUpperCase());

export function todaySite(arg) {
  const sites = S.sites().map((s) => s.id);
  const chosen = arg || localStorage.getItem(SITE_KEY) || "hq";
  return sites.includes(chosen) ? chosen : sites[0] || "hq";
}

// Every render calls this, so it repaints only after a read: poking with nothing due would
// render again at once and lock the page in a loop.
async function refreshCalendars(rooms) {
  const now = Date.now();
  const due = rooms.filter((r) => now - (calendars[r.id]?.at || 0) > 60_000);
  if (!due.length) return;
  await Promise.all(due.map(async (r) => {
    calendars[r.id] = { ...(calendars[r.id] || {}), at: now };
    try {
      const c = await (await fetch(`/api/rooms/${encodeURIComponent(r.id)}/calendar`, { cache: "no-store" })).json();
      calendars[r.id] = { at: now, bookings: c.bookings || [], error: c.error || "", configured: Boolean(c.calendar && c.configured) };
    } catch (_) { calendars[r.id] = { at: now, bookings: [], error: "Calendar unreachable" }; }
  }));
  S.poke();
}

async function refreshDemo() {
  if (Date.now() - demoAt < 4000) return;
  demoAt = Date.now();
  try { demo = await (await fetch("/api/demos/studio", { cache: "no-store" })).json(); S.poke(); } catch (_) { /* keep the last */ }
}

/** What the room is doing now, from its calendar and its meeting computer. */
function roomNow(room, computer) {
  const now = Date.now() / 1000;
  const cal = calendars[room.id];
  const list = (cal?.bookings || []).filter((b) => b.end > now).sort((a, b) => a.start - b.start);
  const cur = list.find((b) => b.start <= now);
  const next = list.find((b) => b.start > now);
  const call = computer?.native?.meeting;
  const inCall = Boolean(call?.in_call), waiting = Boolean(call?.waiting);
  const service = { meet: "Meet", zoom: "Zoom", teams: "Teams", webex: "Webex" }[call?.provider] || "";
  let tone = "free", pill = "Free", line = next ? `Free until ${clock(next.start)}` : cal?.configured ? "Free for the rest of the day" : "";
  if (cur) { tone = "busy"; pill = `In use · ${untilWhen(cur.end, now)}`; line = cur.title; }
  if (inCall) { tone = "busy"; pill = service ? `In a ${service} call` : "In a call"; line = cur ? cur.title : line; }
  else if (waiting) { tone = "wait"; pill = `Waiting in ${service || "the"} lobby`; }
  return { tone, pill, line, upcoming: cur || inCall ? next : null, inCall };
}

function roomCard(room, devices, rooms, can) {
  const screens = members(devices, rooms, room.id, "screen");
  const computer = members(devices, rooms, room.id, "computer")[0];
  const tv = screens[0];
  const st = roomNow(room, computer);
  const tvState = !tv ? "" : !tv.online ? "TV offline" : tv.native?.displayOn === false ? "TV off" : "TV on";
  const gear = [tvState, computer ? (computer.online ? "Room computer ready" : "Room computer offline") : ""].filter(Boolean);
  const tvButton = tv && tv.online && can("screens.operate")
    ? `<button class="td-btn" data-today="${tv.native?.displayOn === false ? "display_on" : "display_off"}" data-id="${esc(tv.id)}" ${busy.has(tv.id) ? "disabled" : ""}>${tv.native?.displayOn === false ? "Turn TV on" : "Turn TV off"}</button>` : "";
  const leave = st.inCall && computer && can("screens.operate")
    ? `<button class="td-btn td-btn-quiet" data-today="leave" data-id="${esc(computer.id)}" ${busy.has(computer.id) ? "disabled" : ""}>End room's call</button>` : "";
  return `<article class="td-room td-${st.tone}">
    <header><h3>${esc(room.name)}</h3><span class="td-pill">${esc(st.pill)}</span></header>
    <p class="td-line">${esc(st.line || "No calendar connected")}</p>
    ${st.upcoming ? `<p class="td-next">Next · ${esc(st.upcoming.title)} at ${clock(st.upcoming.start)}</p>` : ""}
    ${gear.length ? `<p class="td-gear">${gear.map(esc).join(" · ")}</p>` : ""}
    <div class="td-actions"><a class="td-btn td-btn-primary" href="./#/tablet/${esc(room.id)}">Room controls</a>${tvButton}${leave}</div>
  </article>`;
}

function demoCard(can) {
  if (!can("demos.view")) return "";
  if (!demo) { refreshDemo(); return ""; }
  const units = demo.units || [];
  const online = units.filter((u) => u.paired && u.online).length, paired = units.filter((u) => u.paired).length;
  const s = (demo.sessions || []).find((x) => x && units.some((u) => u.session_id === x.id));
  if (!s) {
    return `<article class="td-demo"><div><h3>Quest demos</h3><p class="td-line">${paired ? `${online} of ${paired} headsets online · ${demo.demos?.length || 0} demos ready` : "No headsets paired yet"}</p></div>
      <a class="td-btn td-btn-primary" href="#/admin/demos">Open Demo Studio</a></article>`;
  }
  const control = can("demos.control") && !s.preparing
    ? `<button class="td-btn td-btn-primary td-play" data-today="demo" data-session="${esc(s.id)}" data-play="${s.playing ? "pause" : "play"}">${s.playing ? "❚❚ Pause" : "▶ Play"}</button>` : "";
  return `<article class="td-demo td-live"><div><span class="td-tag">Now presenting</span><h3>${esc(s.demo_name)}</h3>
    <p class="td-line">Scene ${s.step_index + 1} of ${s.steps} · ${esc(s.step.title)}${s.preparing ? " · getting ready" : ""}</p></div>
    <div class="td-actions">${control}<a class="td-btn" href="#/admin/demos">Demo Studio</a></div></article>`;
}

export function renderToday({ me, can, devices, siteArg }) {
  const site = todaySite(siteArg);
  const rooms = S.rooms().filter((r) => r.site === site);
  refreshCalendars(rooms);
  if (can("demos.view")) refreshDemo();
  const now = Date.now() / 1000;
  const roomDevices = rooms.flatMap((r) => [...members(devices, S.rooms(), r.id, "screen"), ...members(devices, S.rooms(), r.id, "computer")]);
  const down = roomDevices.filter((d) => !d.online && now - (d.last_seen || 0) < 7 * 86400);
  const busyRooms = rooms.filter((r) => roomNow(r, members(devices, S.rooms(), r.id, "computer")[0]).tone !== "free").length;
  const date = new Date().toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  return `<div class="td">
    <header class="td-head">
      <div><p class="td-date">${esc(date)}</p><h1>${esc(greeting())}${firstName(me) ? `, ${esc(firstName(me))}` : ""}</h1></div>
      <nav class="td-sites" aria-label="Site">${S.sites().map((s) => `<a href="#/admin/today/${esc(s.id)}" class="${s.id === site ? "on" : ""}" data-today-site="${esc(s.id)}">${esc(s.name)}</a>`).join("")}</nav>
    </header>
    <div class="td-summary">
      <span><b>${rooms.length - busyRooms}</b> of ${rooms.length} rooms free</span>
      <span><b>${busyRooms}</b> in use</span>
      <span class="${down.length ? "td-warn" : ""}">${down.length ? `<b>${down.length}</b> screen${down.length === 1 ? "" : "s"} offline` : "Every screen online"}</span>
    </div>
    ${note ? `<p class="td-note" role="status">${esc(note)}</p>` : ""}
    ${demoCard(can)}
    <h2 class="td-h2">Rooms</h2>
    ${rooms.length ? `<div class="td-rooms">${rooms.map((r) => roomCard(r, devices, S.rooms(), can)).join("")}</div>` : `<p class="td-line">No rooms in this site yet.</p>`}
    ${down.length ? `<h2 class="td-h2">Needs attention</h2><ul class="td-down">${down.map((d) => `<li><b>${esc(d.name || d.label || d.id)}</b><span>Offline since ${clock(d.last_seen)}</span></li>`).join("")}</ul>` : ""}
    <nav class="td-more">${can("views.screens") ? `<a href="#/admin/screens">All screens</a>` : ""}${can("content.edit") ? `<a href="#/admin/content">Content</a>` : ""}${can("demos.view") ? `<a href="#/admin/demos">Demo Studio</a>` : ""}</nav>
  </div>`;
}

// Taps on Today's buttons (the page re-renders after each command is acknowledged).
document.addEventListener("click", async (e) => {
  const site = e.target.closest("[data-today-site]");
  if (site) { localStorage.setItem(SITE_KEY, site.dataset.todaySite); return; }
  const b = e.target.closest("[data-today]");
  if (!b) return;
  e.preventDefault();
  const act = b.dataset.today;
  try {
    if (act === "demo") {
      const r = await fetch(`/api/demos/sessions/${encodeURIComponent(b.dataset.session)}/control`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: b.dataset.play, position: 0 }) });
      if (!r.ok) throw Error((await r.json().catch(() => ({}))).detail || "The demo didn't respond");
      demoAt = 0; await refreshDemo(); return;
    }
    const id = b.dataset.id;
    busy.add(id); S.poke();
    const body = act === "leave" ? { type: "meeting", args: { action: "leave" } } : { type: act, args: {} };
    const r = await fetch(`/api/devices/${encodeURIComponent(id)}/commands`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw Error((await r.json().catch(() => ({}))).detail || "The screen didn't respond");
    note = act === "leave" ? "Asked the room computer to leave its call." : act === "display_off" ? "Turning the TV off." : "Turning the TV on.";
  } catch (err) { note = err.message; }
  finally { if (b.dataset.id) setTimeout(() => { busy.delete(b.dataset.id); S.poke(); }, 4000); S.poke(); setTimeout(() => { note = ""; S.poke(); }, 6000); }
});
