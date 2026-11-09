import { readCalendar } from "../calendar-read.js";
// Built-in screens for the role routes the Fire TV menu, the iOS app and Linux nodes open:
// #/tv/<room>, #/panel/<room>, #/signage/<site> and #/wall. Signage plays the welcome page; the
// others are clean branded idle screens;
// put a page or playlist on a screen in Admin → Screens to show something else. Room TVs and lobby
// TVs show their share link (views/share.js) in the corner; any screen can show it as a badge.

import * as S from "../state.js";
import { renderPage } from "./page.js";
import { esc, clock, dateLong, bookingWhen, untilWhen } from "../ui.js";

const titleCase = (id) => String(id || "").replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const roomName = (id) => S.room(id)?.name || titleCase(id) || "Xos";
const siteName = (id) => S.site(id)?.name || titleCase(id);

// "Share to this screen": the screen's own share link and QR code, once its heartbeat has said
// what they are (kiosk screens only; a preview in Admin doesn't have a device record).
function shareFoot() {
  const me = S.self();
  if (!me?.share_code || me.share_off) return "";
  const link = `${location.host}/s/${me.share_code}`;
  return `<div class="idle-share">
    <img src="/api/share/${esc(me.share_code)}/qr.svg" alt="" class="idle-qr" />
    <div><div class="idle-share-k">Share to this screen</div><div class="idle-share-link">${esc(link)}</div>
    ${me.airplay ? `<div class="idle-share-air">or AirPlay to <b>${esc(me.airplay)}</b></div>` : ""}</div>
  </div>`;
}

// The room's calendar on its TV and door panel: busy until when, or free until the next booking.
// These screens aren't signed in, so they get titles and times only (never join links).
const calendars = {}; // room id -> {at, data}
function roomCalendar(roomId, refresh = false) {
  const c = calendars[roomId];
  if ((!c || refresh) && !c?.loading) {
    calendars[roomId] = { at: Date.now(), data: c?.data, loading: true };
    readCalendar(`/api/rooms/${encodeURIComponent(roomId)}/calendar`, fetch)
      .then((data) => { calendars[roomId] = { at: Date.now(), data }; S.poke(); })
      .catch(() => { calendars[roomId] = { at: Date.now(), data: { error: true } }; S.poke(); });
  }
  return c?.data;
}
// Fetch on a fixed cadence rather than waiting for a render after the cache expires.
// In particular, a completed first read must paint immediately, not a minute later.
function refreshScreenCalendar() {
  if (globalThis.document?.visibilityState === "hidden") return;
  const route = location.hash.match(/^#\/(?:tv|panel)\/([^/?#]+)/);
  if (route) roomCalendar(decodeURIComponent(route[1]), true);
}
// Catch an actual call end or an iPad display-setting change promptly; Google still caches reads.
setInterval(refreshScreenCalendar, 5000);
globalThis.document?.addEventListener("visibilitychange", refreshScreenCalendar);
for (const event of ["focus", "online", "pageshow", "hashchange"]) globalThis.window?.addEventListener(event, refreshScreenCalendar);
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function calStrip(roomId) {
  const c = roomCalendar(roomId);
  if (!c) return "";
  if (!c.configured || !c.calendar || c.error) return `<section class="idle-cal idle-cal-unavailable" aria-label="Room schedule"><div class="idle-cal-next">${c.error ? "Room schedule temporarily unavailable" : "Room calendar not connected"}</div></section>`;
  const now = Date.now() / 1000;
  const list = (c.bookings || []).filter((b) => b.end > now).sort((a, b) => a.start - b.start);
  const cur = list.find((b) => b.start <= now);
  const next = list.find((b) => b.start > now);
  const when = (b) => bookingWhen(b, now);
  const upcoming = list.filter((b) => b.start > now).slice(0, 3);
  return `<section class="idle-cal ${cur ? "is-busy" : "is-free"}">
    <div class="idle-cal-state"><i></i>${cur ? `In use ${untilWhen(cur.end, now)}` : next ? `Free until ${hhmm(next.start)}` : "No upcoming bookings"}</div>
    ${cur ? `<div class="idle-cal-now">${esc(cur.title)}</div>` : ""}
    ${upcoming.length ? `<ol class="idle-agenda" aria-label="Upcoming meetings">${upcoming.map((b, i) => `<li><span class="idle-agenda-k">${i ? "Then" : "Up next"}</span><div><div class="idle-agenda-title">${esc(b.title)}</div><div class="idle-agenda-time">${when(b)}</div></div></li>`).join("")}</ol>` : ""}
  </section>`;
}

function idle({ kicker, title, sub = "", foot = "", cls = "", cal = "" }) {
  return `<div class="idle ${cls}">
    <header class="idle-top"><img src="../static/img/logo-white.svg" alt="Xos" class="idle-logo" /><div class="idle-clock">${clock()}</div></header>
    <main class="idle-main">
      ${kicker ? `<div class="idle-k">${esc(kicker)}</div>` : ""}
      <h1 class="idle-title">${esc(title)}</h1>
      <div class="idle-sub">${esc(sub || dateLong())}</div>
      ${cal}
    </main>
    <footer class="idle-foot">${foot}</footer>
  </div>`;
}

export function meetingWindow(calendar, display, now = Date.now()/1000) {
  if (!calendar?.configured || calendar.error) return true;
  const lead = (display.meeting_lead_minutes ?? 15) * 60;
  const ended = display.last_meeting_end || 0;
  return (calendar.bookings || []).some(b => b.end > now && b.start <= now + lead
    && !(b.start <= ended && ended < b.end));
}

export function renderRoomTV(roomId) {
  const r = S.room(roomId);
  const calendar = roomCalendar(roomId);
  const display = calendar?.display || r?.display || {};
  if (display.idle_page && calendar && !meetingWindow(calendar, display)) return renderPage(display.idle_page);
  return idle({ kicker: r ? siteName(r.site) : "", title: roomName(roomId), foot: shareFoot(), cal: calStrip(roomId) });
}

export function renderPanel(roomId) {
  const r = S.room(roomId);
  return idle({ kicker: r ? siteName(r.site) : "Room", title: roomName(roomId), cls: "idle-panel", cal: calStrip(roomId) });
}

// Lobby signage shows the built-in welcome page full-screen. Its markup only changes with
// the site name, so the 1 s re-render tick never restarts the iframe.
export function renderSignage(siteId) {
  const site = encodeURIComponent(siteName(siteId || "hq"));
  return `<iframe class="page-frame welcome-frame" title="Welcome page" src="/static/welcome/index.html?site=${site}" allow="autoplay; fullscreen"></iframe>`;
}

export function renderWall() {
  return idle({ kicker: "Xos", title: dateLong(), sub: " ", foot: "Choose what this screen shows in Xos Admin → Screens." });
}
