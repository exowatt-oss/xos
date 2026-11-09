// Xos Admin: screens, what they show, rooms, and integrations.
//   Overview     what's up and what needs attention
//   Dashboard    all TVs with regularly refreshed snapshots
//   Content      the media library (photos, videos) and pages: slideshows, announcements, links,
//                uploaded HTML, playlists. Welcome is built in.
//   Screens      every screen as a card; Control opens a drawer to change what it shows, share
//                settings and tools
//   Rooms        meeting rooms and spaces
//   Integrations Zoom today; Google Calendar and others next; the MCP surface for agents
//   People       People & roles: role assignments and Preview as (people.js)
// Everything here is also an MCP tool (src/app/mcp_server.py).
// Each tab, section and button shows only when the signed-in role has its permission (/api/me
// → perms; the matrix is src/app/access.py). The server checks the same permissions.

import * as S from "../state.js";
import { visibleTabs } from "../access-views.js";
import { renderHowTo, handleGuide, handleGuideInput } from "./howto.js";
import { createDashboardCapture } from "../dashboard.js";
import { switchInputProblem, SWITCH_LABEL, SWITCH_HELP } from "../tv-input.js";
import { esc, time, dayShort } from "../ui.js";
import { getPages, loadPages, pageSrc, mediaSrc } from "./page.js";
import { KIND_LABELS, kindOf, members, displayMembers, roomOf } from "../groups.js";
import { userFetch, whoAmI, signInUrl } from "../api.js";
import { renderPeople, renderPreviews, handlePeople, handlePeopleInput } from "./people.js";
import { renderToday } from "./today.js";

// Every call goes through the sign-in gate (/u/api, api.js) so the server knows who is asking.
const fetch = userFetch;
let me = null; // who is signed in, once known: {email, name, role, perms, preview}
const can = (perm) => Boolean(me?.perms?.includes(perm));
// A preview started or ended (people.js): render from the new perspective.
window.addEventListener("xos-me", (e) => { me = e.detail; boxes = wifiNets = sshKeys = null; selected = null; });

// Older bookmarks keep working.
const ALIASES = { devices: "screens", pages: "content", zoom: "integrations" };

export function renderAdmin(tab = "", sub = "") {
  tab = ALIASES[tab] || tab;
  const gate = signInGate();
  if (gate) return gate;
  if (!live.loaded) refreshLive();
  const online = live.list.filter((d) => d.online).length;
  const today = () => renderToday({ me, can, devices: live.list, siteArg: sub });
  const views = { today, overview, dashboard, content, screens, demos: demoStudio, rooms, integrations, people: renderPeople, previews: renderPreviews, howto: renderHowTo };
  const tabs = visibleTabs(me?.perms);
  if (!tab) tab = tabs[0]?.[0] || "";
  if (!tabs.some(([id]) => id === tab)) {
    if (!tabs.length) return '<div class="gate"><h1>No Xos tabs enabled</h1><p>Ask an Admin to grant tab visibility and the permissions for your work.</p></div>';
    return `<div class="adm"><main class="adm-main"><h1>This view is unavailable</h1><p>This user does not have access to that Xos tab.</p>${tabs.map(([id,label])=>`<a class="btn-sm" href="#/admin/${id}">${esc(label)}</a>`).join(" ")}</main></div>`;
  }
  return `
    <div class="adm">
      <aside class="adm-side">
        <div class="adm-brand"><img src="../static/img/logo-color.svg" alt="Xos" /><span>Xos</span></div>
        ${tabs.map(([id, label]) => `<a href="#/admin/${id}" class="${tab === id ? "on" : ""}">${label}${id === "screens" && online ? `<span class="badge badge-ok">${online}</span>` : ""}</a>`).join("")}
        <div class="adm-side-foot">${me ? `<span class="small muted">${esc(me.name || me.email)} · ${esc(me.role_name)}</span><br />` : ""}<a href="https://github.com/exowatt-oss/xos#readme" target="_blank" rel="noopener">Docs ↗</a></div>
      </aside>
      <main class="adm-main">${(views[tab] || overview)()}</main>
    </div>
    ${(tab === "screens" || tab === "overview" || tab === "dashboard") && selected && live.list.find((d) => d.id === selected) ? controlPanel(live.list.find((d) => d.id === selected)) : ""}`;
}

// A dedicated document keeps headset previews and editor inputs stable across Admin repaints.
function demoStudio() {
  return '<iframe title="Quest Demo Studio" src="/app/demos/?embed=1" allow="fullscreen" style="width:100%;height:calc(100dvh - 48px);min-height:700px;border:0;display:block"></iframe>';
}

// Signed out: a sign-in button (a real page load, so Google can do its redirects). Signed in as a
// Member: Admin isn't for you, Control is.
function signInGate() {
  if (!S.isHub()) return "";
  if (!me) {
    whoAmI().then((m) => { me = m; S.poke(); });
    return `<div class="gate"><div class="gate-card"><img src="../static/img/logo-color.svg" alt="Xos" /><p class="muted">Checking who you are…</p></div></div>`;
  }
  if (!me.email) {
    // The iPad's Workspace console (?console=1) goes straight to Google: signing in is the only
    // thing it ever asks, once, and its browser keeps the session.
    if (new URLSearchParams(location.search).has("console")) { location.replace(signInUrl()); return ""; }
    return `<div class="gate"><div class="gate-card">
      <img src="../static/img/logo-color.svg" alt="Xos" />
      <h1>Xos</h1>
      <p>Sign in with your organization account to use Xos.</p>
      <a class="btn-sm btn-primary gate-btn" href="${signInUrl()}">Sign in with Google</a>
    </div></div>`;
  }
  if (!can("admin.view")) {
    return `<div class="gate"><div class="gate-card">
      <img src="../static/img/logo-color.svg" alt="Xos" />
      <h1>Hi ${esc(me.name.split("@")[0])}</h1>
      <p>Your Xos role is ${esc(me.role_name || "Member")}. Ask an Xos Admin to enable the tabs and actions you need.</p>
      <a class="btn-sm btn-primary gate-btn" href="#/control">Control a screen</a>
    </div></div>`;
  }
  return "";
}

const hubOnly = (title) => `<h1>${title}</h1><div class="ex-card callout">Open Xos from your server address.</div>`;

function statTile(label, value, sub = "", href = "") {
  const inner = `<div class="ex-tile-label">${esc(label)}</div><div class="ex-tile-value">${value}</div>${sub ? `<div class="ex-tile-sub">${sub}</div>` : ""}`;
  return href ? `<a class="ex-tile tile-link" href="${href}">${inner}</a>` : `<div class="ex-tile">${inner}</div>`;
}

// Each room is a control group: its screens, and the remotes that drive only those (groups.js).
const roomScreens = (roomId) => displayMembers(live.list, S.rooms(), roomId);
const roomRemotes = (roomId) => members(live.list, S.rooms(), roomId, "controller");
const devRoom = (d) => S.room(roomOf(d, live.list, S.rooms()));
const dotList = (list, none) => list.map((d) => `<span class="st st-${d.online ? "ok" : "err"}">${d.online ? "✓" : "×"}</span> ${esc(devName(d))}`).join("<br />") || `<span class="muted">${none}</span>`;

let pagesAsked = false;

function overview() {
  if (!S.isHub()) return hubOnly("Overview");
  if (!live.loaded) return `<h1>Overview</h1><p class="muted">Loading…</p>`;
  if (!pagesAsked) { pagesAsked = true; loadPages(); }
  if (!lib.loaded) refreshLibrary();
  const now = Date.now() / 1000;
  const recent = live.list.filter((d) => now - d.last_seen < 7 * DAY);
  const online = recent.filter((d) => d.online);
  const down = recent.filter((d) => !d.online);
  const asleep = online.filter((d) => d.native?.displayOn === false);
  const sharing = online.filter((d) => d.sharing);
  const rooms = S.rooms();
  return `
    <h1>Overview</h1>
    <p class="small"><a href="#/admin/howto">New device? Follow the setup walkthrough →</a></p>
    <div class="adm-tiles">
      ${statTile("Screens online", `${online.length} / ${recent.length}`, "Seen in the last 7 days", "#/admin/screens")}
      ${statTile("Offline", down.length, down.length ? esc(down.slice(0, 3).map(devName).join(", ")) : "All good", "#/admin/screens")}
      ${statTile("Displays asleep", asleep.length, "Fire TV sticks with the TV off")}
      ${statTile("Being shared to", sharing.length, sharing.length ? esc(sharing.map(devName).join(", ")) : "Nothing right now")}
      ${statTile("Content", getPages().length || "—", `${lib.items.filter((m) => !m.builtin).length} uploaded photos & videos`, "#/admin/content")}
      ${statTile("Rooms", rooms.length, `${rooms.filter((r) => roomScreens(r.id).length).length} with screens`, "#/admin/rooms")}
    </div>
    ${down.length ? `<h2>Needs attention</h2><div class="sc-grid">${down.map(screenCard).join("")}</div>` : ""}
    <h2>Rooms</h2>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Room</th><th>Screens</th><th>Remotes</th><th></th></tr></thead><tbody>${rooms.map((r) => {
      return `<tr><td><b>${esc(r.name)}</b><div class="muted small">${esc(S.site(r.site)?.name || r.site)}</div></td>
        <td>${dotList(roomScreens(r.id), "None yet")}</td><td>${dotList(roomRemotes(r.id), "None yet")}</td>
        <td class="nowrap"><a class="btn-sm" href="./#/tablet/${esc(r.id)}" target="_blank" rel="noopener">Remote ↗</a></td></tr>`;
    }).join("") || `<tr><td class="muted">No rooms yet. Add them under Rooms.</td></tr>`}</tbody></table></div>`;
}

// Admin → Rooms: the rooms screens can be put in (Screens → Control → Room).
let roomNote = "";

function rooms() {
  if (!S.isHub()) return hubOnly("Rooms");
  const siteOpts = (cur) => S.sites().map((s) => `<option value="${esc(s.id)}" ${s.id === cur ? "selected" : ""}>${esc(s.name)}</option>`).join("");
  return `
    <h1>Rooms</h1>
    <p class="lead">Each room is a control group: its <b>screens</b>, and the <b>remotes</b> that drive them. A remote only ever sees its own room's screens. Put a screen or an iPad in a room from its <b>Control</b> drawer; the room's remote is <code>#/tablet/&lt;room&gt;</code> (open it on the table iPad, or any phone), and the room can be linked to its Zoom Room under Integrations.</p>
    ${roomNote ? `<div class="ex-card callout">${esc(roomNote)}</div>` : ""}
    <div class="tbl-wrap"><table class="tbl rm-tbl">
      <thead><tr><th>Room</th><th>Site</th><th>Screens</th><th>Remotes</th><th></th></tr></thead>
      <tbody>${S.rooms().map((r) => `<tr>
        <td><input id="rn-${esc(r.id)}" maxlength="60" value="${esc(r.name)}" aria-label="Name" /><div class="muted small">${esc(r.id)}</div></td>
        <td><select id="rs-${esc(r.id)}" aria-label="Site">${siteOpts(r.site)}</select></td>
        <td class="small">${dotList(roomScreens(r.id), "—")}</td>
        <td class="small">${dotList(roomRemotes(r.id), "—")}</td>
        <td class="nowrap"><a class="btn-sm" href="./#/tablet/${esc(r.id)}" target="_blank" rel="noopener">Open remote ↗</a> <button class="btn-sm" data-act="room-save" data-id="${esc(r.id)}">Save</button> <button class="btn-sm" data-act="room-delete" data-id="${esc(r.id)}">Delete</button></td>
      </tr>`).join("") || `<tr><td colspan="5" class="muted">No rooms yet.</td></tr>`}</tbody>
    </table></div>
    <div class="ex-card"><h3>Add a room</h3>
      <div class="dc-row"><label>Name <input id="room-new" maxlength="60" placeholder="e.g. Lamar" /></label>
        <label>Site <select id="room-new-site">${siteOpts(S.sites()[0]?.id)}</select></label>
        <button class="btn-sm btn-primary" data-act="room-add">Add room</button></div></div>`;
}

async function roomCall(method, path, body, done) {
  const res = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined }).catch(() => null);
  roomNote = res?.ok ? done : `Couldn't save: ${res ? (await res.json()).detail : "network error"}`;
  await S.loadConfig();
  refreshLive();
}

// Real devices connected to this Xos server (Fire TV, iPad, browsers), refreshed every few seconds.
let live = { loaded: false, list: [] };
let remoteNote = "";
let switchNote = ""; // why Switch TV to Xos couldn't start, for the open drawer
let selected = null; // device id open in the control drawer
let history = [];
let shotAt = 0; // cache-buster for the screenshot <img>

// Typing (or a chosen file) must not be wiped by the periodic refresh re-render.
const editing = () => (/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName || "") && document.activeElement.closest(".adm, .dc-panel"))
  || [...document.querySelectorAll("input[type=file]")].some((i) => i.files.length);

let liveRefreshing = false;
async function refreshLive() {
  if (!S.isHub() || liveRefreshing) return;
  liveRefreshing = true;
  try {
    live = { loaded: true, list: await dashboardList() };
    if (selected) history = await (await fetch(`/api/devices/${encodeURIComponent(selected)}/commands`, { cache: "no-store", signal: AbortSignal.timeout(8000) })).json();
    liveError = "";
    if (!editing()) S.poke();
  } catch (_) { liveError = "Connection lost. Showing the last known screen state; reconnecting…"; if (!editing()) S.poke(); }
  finally { liveRefreshing = false; }
}
setInterval(() => { if (/^#\/admin\/(dashboard|screens|devices|content|pages|today)/.test(location.hash) || selected) refreshLive(); }, 4000);
setInterval(() => { if (/^(#\/?)?$|^#\/admin\/(today|overview|rooms)?$/.test(location.hash)) refreshLive(); }, 15000);

async function sendCommand(id, type, args = {}) {
  await fetch(`/api/devices/${encodeURIComponent(id)}/commands`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type, args }),
  }).catch(() => {});
  if (type === "screenshot") [2500, 5000].forEach((ms) => setTimeout(() => { shotAt = Date.now(); refreshLive(); }, ms));
  setTimeout(refreshLive, 800);
}

const CLIENT_NAMES = { firetv: "Fire TV · Xos TV", ios: "iPad / iPhone · Xos", browser: "Browser" };
const clientName = (c) => {
  const [kind, ver] = String(c || "browser").split("-");
  return `${CLIENT_NAMES[kind] || kind}${ver ? ` ${ver}` : ""}`;
};
const ago = (t) => { if (!t) return "never"; const s = Math.round(Date.now() / 1000 - t); return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 2 * DAY ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / DAY)} days ago`; };
const kb = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);

const nameSlug = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

// Roles a "show on every screen" action reaches: what is on a wall, not remotes or cameras.
const DISPLAY_ROLES = new Set(["page", "tv", "signage", "wall", "feed", "media", "setup", ""]);

const devName = (d) => d.name || d.label || d.id;

// What a screen is showing, in words.
function showing(d) {
  const [role, arg] = [d.role || "", d.arg || ""];
  if (role === "page") return `Page · ${getPages().find((p) => p.id === arg)?.name || arg}`;
  if (role === "media") return `Photo / video · ${lib.items.find((m) => m.id === arg)?.name || arg}`;
  if (role === "signage") return `Lobby signage · ${S.site(arg)?.name || arg} (welcome page)`;
  if (role === "tv") return `Room TV · ${S.room(arg)?.name || arg}`;
  return d.label || role || "Setup (waiting to be added)";
}

// ---------- Dashboard ----------
let liveError = "";
async function dashboardList() {
  const res = await fetch("/api/devices", { cache: "no-store", signal: AbortSignal.timeout(8000) });
  if (!res.ok) throw new Error("Could not load screens");
  return res.json();
}
const dashboardActive = () => location.hash === "#/admin/dashboard" && !document.hidden && can("screens.show") && !me?.preview && S.isHub();
const dashboardScreens = () => live.list.filter((d) => kindOf(d) === "screen")
  .sort((a, b) => Number(b.online) - Number(a.online) || devName(a).localeCompare(devName(b)));
const captures = createDashboardCapture({
  request: (id) => fetch(`/api/devices/${encodeURIComponent(id)}/commands`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: "screenshot" }), signal: AbortSignal.timeout(8000),
  }),
  changed: () => { if (dashboardActive() && !editing()) S.poke(); },
});
setInterval(() => captures.tick(dashboardScreens(), dashboardActive()), 1000);

function dashboard() {
  if (!S.isHub()) return hubOnly("Dashboard");
  if (!pagesAsked) { pagesAsked = true; loadPages(); }
  if (!lib.loaded) refreshLibrary();
  const list = dashboardScreens();
  const online = list.filter((d) => d.online).length;
  return `<div class="dash-head"><div><h1>Dashboard</h1><p class="muted">All TVs in one view · ${online} online · ${list.length - online} offline</p></div></div>
    <p class="small muted">Xos app snapshots refresh about every 10 seconds while this page is visible. External TV inputs and some video overlays aren't captured.</p>
    ${liveError ? `<div class="ex-card callout" role="status">${esc(liveError)}</div>` : ""}
    ${!live.loaded ? `<p class="muted">Loading screens…</p>` : !list.length ? `<div class="ex-card">No TVs registered yet. <a href="#/admin/screens">Add a screen</a> to see it here.</div>` : `<div class="dash-grid">${list.map(dashboardCard).join("")}</div>`}`;
}

function dashboardCard(d) {
  const asleep = d.native?.displayOn === false;
  const supported = d.client?.startsWith("firetv");
  const stale = !d.screenshot || Date.now() / 1000 - d.screenshot.at > 30;
  const state = !d.online ? "Offline" : asleep ? "Display asleep" : !d.connected ? "Reconnecting" : "Online";
  const note = !d.online ? `Last seen ${ago(d.last_seen)}` : asleep ? "Display is off" : !supported ? "Snapshots need the Xos TV app" : !d.connected ? "Waiting for command connection" : captures.error(d.id) || (stale ? "Waiting for a fresh snapshot…" : "Updating automatically");
  const sharing = d.sharing ? `${d.sharing.from || "Someone"} is sharing` : showing(d);
  return `<article class="dash-card">
    <div class="dash-preview">${d.screenshot ? `<img src="/api/devices/${encodeURIComponent(d.id)}/screenshot?t=${d.screenshot.at}" alt="Last snapshot of ${esc(devName(d))}" />` : `<div class="dash-placeholder">${esc(note)}</div>`}
      <span class="dash-status badge ${d.online && !asleep ? "badge-ok" : ""}">${state}</span></div>
    <div class="dash-info"><h2>${esc(devName(d))}</h2><p class="muted small">${esc([devRoom(d)?.name, S.site(d.site || devRoom(d)?.site)?.name].filter(Boolean).join(" · ") || "No room assigned")}</p>
      <p class="dash-content">${esc(sharing)}</p>
      <p class="small ${stale ? "dash-stale" : "muted"}">${d.screenshot ? `Snapshot ${ago(d.screenshot.at)}${stale ? " · Stale" : ""}` : "No snapshot yet"}</p>
      <p class="small muted" role="status">${esc(note)}</p>
      ${can("screens.show") ? `<button class="btn-sm" data-act="dc-open" data-id="${esc(d.id)}">Control</button>` : ""}
    </div></article>`;
}

// ---------- Screens ----------

// Share to this screen: its /s/<code> link (views/share.js), on/off, the corner badge, a new link.
function shareSection(d) {
  const link = `${location.host}/s/${d.share_code}`;
  const s = d.sharing;
  const what = s ? `${s.from || "Someone"} is sharing ${{ screen: "a screen", url: "a link", image: "a photo" }[s.kind] || ""}` : "";
  const toggle = (key, on, label, hint) => `<label class="sw"><input type="checkbox" data-act="dc-share" data-key="${key}" ${on ? "checked" : ""} /> <span><b>${label}</b><br /><span class="muted small">${hint}</span></span></label>`;
  return `<section class="dr-sec"><h3>Share</h3>
    ${toggle("enabled", !d.share_off, "Anyone can share to this screen", "From a phone or laptop: open the link below, or scan its QR code.")}
    ${d.share_off ? "" : toggle("badge", d.share_badge, "Show the link on screen", "A small QR code and link in the bottom-right corner, over whatever is showing.")}
    ${d.share_off ? "" : `<div class="dc-row dc-share"><img src="/api/share/${esc(d.share_code)}/qr.svg" alt="" class="dc-qr" />
      <span><a href="/s/${esc(d.share_code)}" target="_blank" rel="noopener"><b>${esc(link)}</b></a>${d.airplay ? `<br /><span class="muted small">AirPlay: ${esc(d.airplay)}</span>` : ""}<br />
      <button class="link small" data-act="dc-share-new">Make a new link</button></span></div>`}
    ${s ? `<div class="dc-row"><span class="small">${esc(what)}</span> <button class="btn-sm" data-act="dc-share-stop" data-code="${esc(d.share_code)}">Stop sharing</button></div>` : ""}
  </section>`;
}

// What a screen can show, grouped: content first, then room views, remotes and cameras.
function routeOptions(current, exceptId = "") {
  const groups = [
    ["Content", [
      ...getPages().map((p) => [`page/${p.id}`, `${p.builtin ? "★ " : ""}${p.name}`]),
      ...S.sites().map((site) => [`signage/${site.id}`, `Lobby signage · ${site.name} (welcome page)`]),
      ["wall", "Dashboard wall"],
    ]],
    ["Photos & videos", lib.items.filter((m) => !m.builtin).map((m) => [`media/${m.id}`, `${m.kind === "video" ? "Video" : "Photo"} · ${m.name}`])],
    ["Rooms", S.rooms().flatMap((r) => [
      [`tv/${r.id}`, `Room TV · ${r.name}`],
      [`tablet/${r.id}`, `Table remote · ${r.name} (drives the room's TV)`],
      [`panel/${r.id}`, `Door panel · ${r.name}`],
    ])],
    ["Remote for a named screen", live.list.filter((d) => d.name && d.id !== exceptId && d.role !== "remote")
      .map((d) => [`remote/${nameSlug(d.name)}`, `Remote · ${d.name}`])],
    ["Cameras", [
      ["camera", "Live camera · use this screen's camera (e.g. a Rally Bar over USB)"],
      ...live.list.filter((d) => d.role === "camera" && d.id !== exceptId)
        .map((d) => [`feed/${d.id}`, `Live feed · ${d.name || d.id}`]),
    ]],
  ];
  // Keep what a screen shows now selectable even if it isn't listed (e.g. a page still loading).
  if (current && !groups.some(([, opts]) => opts.some(([v]) => v === current))) groups.unshift(["Now showing", [[current, current]]]);
  return groups.filter(([, opts]) => opts.length).map(([label, opts]) => `<optgroup label="${esc(label)}">${
    opts.map(([v, l]) => `<option value="${v}" ${v === current ? "selected" : ""}>${esc(l)}</option>`).join("")}</optgroup>`).join("");
}

// An Xos Linux box: its health as it reports it, its maintenance login, Update now.
function boxSection(d) {
  const b = boxOf(d.id);
  if (!b) return "";
  const n = d.native || {};
  const up = n.uptime_s ? `${Math.floor(n.uptime_s / 3600)} h ${Math.floor((n.uptime_s % 3600) / 60)} min` : "—";
  const rows = [
    ["Role", b.role === "room" ? "Room computer" : "Display"], ["Hostname", n.hostname || b.hostname],
    ["Release", n.release || "—"], ["OS", n.os || "—"], ["Model", n.model || "—"], ["Agent up", up],
    ["Browser", n.browser ? `${n.browser}${n.browser_restarts ? ` (restarted ${n.browser_restarts}×)` : ""}` : "—"],
    ["Last update", n.last_update ? `${n.last_update.result} · ${ago(n.last_update.at)}` : "—"],
    ["Disk free", n.disk_free_gb != null ? `${n.disk_free_gb} GB` : "—"], ["Last error", n.last_error || "—"],
  ];
  return `<section class="dr-sec"><h3>Linux box</h3>
    <dl class="dc-facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <p class="small">Maintenance login: ${boxLogin(b)}</p>
    ${b.role === "room" && can("setup") ? `<div class="dc-row"><button class="btn-sm" data-act="dc-cmd" data-type="update" data-args="{}">Update now</button><span class="small muted">Checks for a new release (never during a meeting); rolls back if it isn't healthy.</span></div>` : `<p class="small muted">Updates nightly; a display has no Update now yet.</p>`}
  </section>`;
}

// Switch TV to Xos (app/js/tv-input.js): shown for app screens, disabled with the reason when the
// stick can't. The server runs it; its progress is the latest switch_input in the history.
function switchRow(d) {
  const problem = switchInputProblem(d);
  const run = history.find((c) => c.type === "switch_input");
  const busy = run?.status === "sent";
  const recent = run && Date.now() / 1000 - run.created < 300;
  const status = switchNote || (!recent ? "" : busy ? `${run.result || "Starting"}…`
    : run.status === "done" ? "Switched the TV to Xos." : `Switch TV to Xos didn't work: ${run.result || run.status}`);
  return `<div class="dc-row"><button class="btn-sm" data-act="dc-switch" title="${esc(problem || SWITCH_HELP)}" ${problem || busy ? "disabled" : ""}>${SWITCH_LABEL}</button><span class="small muted">${esc(problem || SWITCH_HELP)}</span></div>
    ${status ? `<p class="small" role="status">${esc(status)}</p>` : ""}`;
}

// The control drawer: what's on screen, share, tools, details.
function controlPanel(d) {
  const n = d.native || {};
  const current = [d.role, d.arg].filter(Boolean).join("/");
  const isApp = Boolean(d.native);
  const facts = [
    ...(["tablet", "remote"].includes(d.role) ? [["Remote access", ({ready: "Ready", needs_reset: "Reset remote access", needs_setup: "Name this remote first"})[d.remote_access] || "Reload to check"]] : []),
    ...(["tablet", "remote"].includes(d.role) ? [["Keep awake", d.wake_lock === "held" ? "Active" : d.wake_lock || "Reload to check"]] : []),
    ["App", clientName(d.client)],
    ["Device", [n.manufacturer, n.model].filter(Boolean).join(" ") || "—"], ["OS", n.os || "—"],
    ["LAN address", n.ip || "—"], ["Seen from", d.ip], ["Screen id", d.id],
    ["Display", n.displayOn === undefined ? "—" : n.displayOn ? "On" : "Off (asleep)"],
    ["Auto-start", n.autoStart === undefined ? "—" : n.autoStart ? "Allowed" : "Not allowed — run firetv/provision.sh"],
    ["Display control", n.displayControl === undefined ? "—" : n.displayControl ? "Ready" : "Off — run firetv/provision.sh"],
  ];
  const btn = (type, label, args = {}, disabled = false) =>
    `<button class="btn-sm" data-act="dc-cmd" data-type="${type}" data-args='${esc(JSON.stringify(args))}' ${disabled ? "disabled title=\"Needs the Xos TV app\"" : ""}>${label}</button>`;
  const steps = ["page", "media", "signage"].includes(d.role);
  return `<div class="dr-back" data-act="dc-close"></div>
  <aside class="dc-panel drawer" aria-label="Control ${esc(devName(d))}">
    <div class="dc-head"><div><h2><span class="st st-${d.online ? "ok" : "err"}">${d.online ? "✓" : "×"}</span> ${esc(devName(d))}</h2>
      <div class="muted small">${esc(devRoom(d)?.name || "No room")} · ${d.connected ? "connected now" : d.online ? "online" : `offline, last seen ${ago(d.last_seen)}`}</div></div>
      <button class="link" data-act="dc-close">Close ✕</button></div>

    <section class="dr-sec"><h3>On screen</h3>
      <div class="dc-shot">${d.screenshot ? `<img src="/api/devices/${encodeURIComponent(d.id)}/screenshot?t=${Math.max(shotAt, Math.round(d.screenshot.at * 1000))}" alt="Screenshot of ${esc(devName(d))}" /><div class="muted small">Screenshot ${ago(d.screenshot.at)} · ${btn("screenshot", "Take a new one", {}, !isApp)}</div>`
        : `<div class="dc-noshot muted">${isApp ? `${btn("screenshot", "Take a screenshot")}` : "Screenshots need the Xos TV app."}</div>`}</div>
      <p class="small"><b>Now:</b> ${esc(showing(d))}${d.controlled_by ? ` · <b>${esc(d.controlled_by.name)}</b> is controlling it` : ""}</p>
      ${can("screens.show") ? `<div class="dc-row"><select id="dc-route" aria-label="Show">${routeOptions(current, d.id)}</select><button class="btn-sm btn-primary" data-act="dc-route">Show</button></div>
      ${steps ? `<div class="dc-row">${btn("page_key", "◀ Previous", { key: "ArrowLeft" })} ${btn("page_key", "Next ▶", { key: "ArrowRight" })}
        ${d.role === "page" ? `${btn("page_scroll", "Scroll ↑", { dir: "up" })} ${btn("page_scroll", "Scroll ↓", { dir: "down" })} ${btn("page_scroll", "Top", { dir: "top" })}` : ""}</div>` : ""}
      <div class="dc-row"><input id="dc-msg" maxlength="200" placeholder="Message to show on this screen" /><button class="btn-sm" data-act="dc-msg">Show for 15 s</button></div>` : ""}
    </section>

    ${can("setup") && DISPLAY_ROLES.has(d.role || "") && d.share_code ? shareSection(d) : ""}
    ${can("setup") ? boxSection(d) : ""}

    <section class="dr-sec"><h3>Tools</h3>
      ${can("setup") && ["tablet", "remote"].includes(d.role) ? `<div class="dc-row"><button class="btn-sm" data-act="dc-reset-access">Reset remote access</button><span class="small muted">Restores this tablet's controls and reloads it.</span></div>${remoteNote ? `<p class="small" role="status">${esc(remoteNote)}</p>` : ""}` : ""}
      <div class="dc-row">${can("screens.show") ? `${btn("identify", "Identify")} ${btn("reload", "Reload")}` : ""} ${can("screens.operate") ? `${btn("display_on", "Display on", {}, !isApp)} ${btn("display_off", "Display off", {}, !isApp)}` : ""}</div>
      ${can("screens.operate") && isApp ? switchRow(d) : ""}
      ${can("setup") ? `<div class="dc-row"><label>Name <input id="dc-name" maxlength="60" value="${esc(d.name || "")}" placeholder="e.g. Atrium room" /></label><button class="btn-sm" data-act="dc-rename">Rename</button></div>
      <div class="dc-row"><label>Room <select id="dc-room"><option value="">—</option>${S.rooms().map((r) => `<option value="${esc(r.id)}" ${r.id === (d.room || devRoom(d)?.id) ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></label><button class="btn-sm" data-act="dc-room">Set room</button></div>` : ""}
      ${can("screens.operate") ? `<div class="dc-row"><label>Return to Xos after someone leaves it <select id="dc-return" ${isApp ? "" : "disabled"}>${[0, 1, 2, 5, 15].map((m) => `<option value="${m}" ${m === n.returnMinutes ? "selected" : ""}>${m ? `${m} min` : "Off"}</option>`).join("")}</select></label><button class="btn-sm" data-act="dc-return" ${isApp ? "" : "disabled"}>Set</button></div>` : ""}
      <details class="dr-more dr-details"><summary>Details and recent commands</summary>
        <dl class="dc-facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
        <table class="tbl dc-hist"><tbody>${history.slice(0, 8).map((c) => `<tr><td>${esc(c.type)}${c.args?.route ? ` → ${esc(c.args.route)}` : c.args?.text ? ` “${esc(c.args.text)}”` : ""}</td><td><span class="dc-st dc-${c.status}">${esc(c.status)}</span></td><td class="muted small">${esc(c.result ?? "")}</td><td class="muted small">${ago(c.created)}</td></tr>`).join("") || `<tr><td class="muted">None yet</td></tr>`}</tbody></table>
        ${can("setup") ? `<button class="btn-sm" data-act="dc-forget" data-id="${esc(d.id)}">Forget this screen</button>` : ""}
      </details>
    </section>
  </aside>`;
}

const ROLE_ICON = { tv: "📺", signage: "🪧", page: "🖼️", media: "🖼️", wall: "📊", tablet: "📱", remote: "📱", panel: "🚪", camera: "🎥", feed: "🎥", roompc: "💻" };

function screenCard(d) {
  const room = devRoom(d);
  const ctl = kindOf(d) === "controller";
  const meeting = kindOf(d) === "computer" ? d.native?.meeting || {} : null;
  const q = [devName(d), d.label, d.id, room?.name, d.native?.model, d.native?.ip, d.ip].filter(Boolean).join(" ").toLowerCase();
  return `<div class="sc-card ${d.online ? "" : "sc-off"} ${d.id === selected ? "sc-sel" : ""}" data-q="${esc(q)}" ${filter && !q.includes(filter.toLowerCase()) ? "hidden" : ""}>
    <div class="sc-top"><span class="sc-icon" aria-hidden="true">${ROLE_ICON[d.role] || "🖥️"}</span>
      <div class="sc-name"><b>${esc(devName(d))}</b><div class="muted small">${esc(room?.name || "No room")} · ${esc(clientName(d.client))}</div></div>
      <span class="sc-dot ${d.online ? "on" : ""}" title="${d.online ? "Online" : `Offline, last seen ${ago(d.last_seen)}`}"></span></div>
    <div class="sc-show small">${ctl ? (room ? `Remote for ${esc(room.name)}'s screens` : "Remote · put it in a room to control that room's screens")
      : meeting ? (meeting.in_call ? `In a meeting${meeting.title ? ` · ${esc(meeting.title)}` : ""}` : meeting.error ? esc(meeting.error) : "Room computer · ready for a meeting") : esc(showing(d))}</div>
    <div class="sc-meta small muted">${d.online ? (d.native?.displayOn === false ? "Display asleep" : d.connected ? "Connected" : "Online") : `Offline · ${ago(d.last_seen)}`}${d.sharing ? ` · <b class="sc-live">Sharing now</b>` : ""}${d.share_badge ? " · Share badge on" : ""}</div>
    <button class="btn-sm btn-primary" data-act="dc-open" data-id="${esc(d.id)}">Control</button>
  </div>`;
}

let showOffline = false;
let addOpen = false;
let addNote = "";

// Set up by IP (src/app/provision.py): the server installs and opens Xos over network ADB.
let ipJobs = [];
let ipNote = "";
let ipTimer = null;
async function refreshIpJobs() {
  try { ipJobs = await (await fetch("/api/provision", { cache: "no-store" })).json(); } catch (_) { return; }
  const running = ipJobs.some((j) => j.state === "running");
  if (running && !ipTimer) ipTimer = setInterval(refreshIpJobs, 1000);
  if (!running && ipTimer) { clearInterval(ipTimer); ipTimer = null; refreshLive(); }
  if (!editing()) S.poke();
}
function ipJob(j) {
  const icon = j.state === "running" ? `<span class="rm-spin"></span>` : j.state === "done" ? "✓" : "✗";
  const who = j.name || j.info?.model || "";
  const open = j.state === "done" && j.device ? ` <button class="link" data-act="dc-open" data-id="${esc(j.device)}">Open ↗</button>` : "";
  return `<li class="ip-job is-${esc(j.state)}"><span class="ip-icon">${icon}</span>
    <b>${esc(j.ip)}</b>${who ? ` · ${esc(who)}` : ""} <span class="ip-step">${esc(j.state === "done" ? "Set up and showing Xos" : j.step)}</span>${open}</li>`;
}
function addByIp() {
  const recent = ipJobs.filter((j) => j.state === "running" || Date.now() / 1000 - j.started < 3600).slice(0, 6);
  return `<div class="add-ip">
    <h3>Fire TV, Android TV or Android smart board: set it up by IP address</h3>
    <div class="dc-row ip-row">
      <label>IP <input id="ip-addr" inputmode="decimal" maxlength="40" placeholder="192.0.2.102" /></label>
      <label>Name <input id="ip-name" maxlength="60" placeholder="e.g. Meeting room TV" /></label>
      <label>Show <select id="ip-route">${routeOptions("page/welcome")}</select></label>
      <button class="btn-sm btn-primary" data-act="ip-setup">Set up</button>
    </div>
    <p class="muted small">Turn on <b>ADB debugging</b> on the screen first (Fire TV: Settings → My Fire TV → About, click the name 7 times, then Developer options). The first time, accept <b>Allow USB debugging?</b> on the screen and tick <b>Always allow</b>. Xos installs, opens at boot and shows what you picked; there's no code to enter.</p>
    ${ipNote ? `<p class="small">${esc(ipNote)}</p>` : ""}
    ${recent.length ? `<ul class="ip-jobs">${recent.map(ipJob).join("")}</ul>` : ""}
  </div>
  <h3 class="add-or">Or open Xos on the screen and enter its code</h3>`;
}
let bulkNote = "";
let filter = "";
/** Screens → filter box: hides cards in place so typing isn't interrupted by a re-render. */
export function handleAdminInput(el) {
  if (handleGuideInput(el)) return;
  if (handlePeopleInput(el)) return;
  if (otherInput(el)) return;
  if (el.id !== "dc-filter") return;
  filter = el.value.trim();
  const needle = filter.toLowerCase();
  document.querySelectorAll(el.dataset.filter).forEach((c) => { c.hidden = Boolean(needle) && !(c.dataset.q || "").includes(needle); });
}
const DAY = 24 * 3600;

// Xos Linux boxes (src/app/appliances.py): set up here, flashed or installed with a one-time code.
let boxes = null; // GET /api/appliances (software team)
let boxNote = "";
const revealed = new Set(); // device ids whose maintenance password is showing
async function refreshBoxes() {
  try {
    const r = await fetch("/api/appliances", { cache: "no-store" });
    boxes = r.ok ? await r.json() : [];
  } catch (_) { boxes = boxes || []; }
  if (!editing()) S.poke();
}
const boxOf = (id) => (boxes || []).find((b) => b.device_id === id);
const codeStr = (c) => `${c.slice(0, 5)}-${c.slice(5)}`;
function boxLogin(b) {
  const shown = revealed.has(b.device_id);
  return `<code>${esc(b.user)}</code> / <code>${shown ? esc(b.password) : "••••-••••-••••-••••"}</code>
    <button class="link small" data-act="box-reveal" data-id="${esc(b.device_id)}">${shown ? "Hide" : "Show"}</button>`;
}
function boxRow(b) {
  const room = S.room(b.room)?.name || b.room || "—";
  const state = b.enrolled ? (b.online ? `<span class="dc-st dc-done">online</span>` : `<span class="dc-st">set up ${ago(b.enrolled.at)}, offline</span>`)
    : b.expires * 1000 < Date.now() ? `<span class="dc-st dc-failed">code expired</span>` : `<span class="dc-st">waiting to be set up</span>`;
  const setup = b.enrolled ? "" : `<div class="box-setup small">
      <div><b>Flash:</b> boot the Debian 13 USB → <i>Advanced options → Automated install</i>, and enter <code>${esc(b.preseed_url)}</code>. It erases the disk and sets everything up.</div>
      <div><b>Or on a Debian 13 box:</b> <code>${esc(b.install)}</code></div>
      <div class="muted">Setup code <b>${esc(codeStr(b.code))}</b> works once, until ${new Date(b.expires * 1000).toLocaleDateString()}.</div></div>`;
  return `<tr><td><b>${esc(b.name)}</b><div class="muted small">${b.role === "room" ? "Room computer" : "Display"} · ${esc(room)} · <code>${esc(b.hostname)}</code></div>${setup}</td>
    <td class="small">${state}</td>
    <td class="small">Maintenance login<br />${boxLogin(b)}</td>
    <td class="small"><button class="btn-sm" data-act="dc-open" data-id="${esc(b.device_id)}">Open</button>
      <button class="btn-sm" data-act="box-renew" data-id="${esc(b.device_id)}" title="A new setup code, to reflash or reinstall this box">New code</button>
      <button class="btn-sm" data-act="box-delete" data-id="${esc(b.device_id)}">Delete</button></td></tr>`;
}
function linuxBoxes() {
  if (boxes === null) { refreshBoxes(); return ""; }
  if (!boxes.length) return "";
  return `<h2 class="sc-kind">Linux boxes <span class="muted">· ${boxes.length}</span></h2>
    <p class="muted small">Xos appliances set up from here (README.md). Logins are visible to the software team only; the room's Google password is never stored in Xos.</p>
    <div class="tbl-wrap"><table class="tbl box-tbl"><tbody>${boxes.map(boxRow).join("")}</tbody></table></div>`;
}
// Wi-Fi new boxes join, preferred first (GET/PUT /api/appliances/wifi; team only).
let wifiNets = null;
let wifiNote = "";
let wifiShown = false;
async function refreshWifi() {
  try {
    const r = await fetch("/api/appliances/wifi", { cache: "no-store" });
    wifiNets = r.ok ? (await r.json()).networks : [];
  } catch (_) { wifiNets = wifiNets || []; }
  if (!editing()) S.poke();
}
function wifiEditor() {
  if (wifiNets === null) { refreshWifi(); return ""; }
  const rows = [...wifiNets, {}, {}, {}, {}].slice(0, 4);
  return `<div class="box-wifi">
    <h4>Wi-Fi for Xos boxes <span class="muted small">· first is preferred; Ethernet wins when plugged in</span></h4>
    ${rows.map((n, i) => `<div class="dc-row ip-row"><span class="small muted">${i + 1}</span>
      <label>Network <input id="wifi-ssid-${i}" maxlength="32" value="${esc(n.ssid || "")}" placeholder="${i ? "e.g. Guest Wi-Fi (fallback)" : "e.g. Office"}" /></label>
      <label>Password <input id="wifi-psk-${i}" type="${wifiShown ? "text" : "password"}" maxlength="63" value="${esc(n.psk || "")}" autocomplete="off" /></label></div>`).join("")}
    <div class="dc-row"><button class="btn-sm btn-primary" data-act="wifi-save">Save Wi-Fi</button>
      <button class="link small" data-act="wifi-show">${wifiShown ? "Hide passwords" : "Show passwords"}</button>
      ${wifiNote ? `<span class="small" role="status">${esc(wifiNote)}</span>` : ""}</div>
    <p class="muted small">Stored on the Xos server only (software team). A box gets them when it's set up with its code, so it needs no cable; it joins the first one in range and falls back to the next.</p>
  </div>`;
}

// SSH keys for every box's maintenance login (xosadmin): the ones in Git plus the ones added here.
let sshKeys = null; // {repo: [...], keys: [...]}
let sshNote = "";
async function refreshSshKeys() {
  try {
    const r = await fetch("/api/appliances/ssh-keys", { cache: "no-store" });
    sshKeys = r.ok ? await r.json() : { repo: [], keys: [] };
  } catch (_) { sshKeys = sshKeys || { repo: [], keys: [] }; }
  if (!editing()) S.poke();
}
const keyName = (k) => `${k.split(" ")[0].replace("ssh-", "")} …${k.split(" ")[1].slice(-8)}${k.split(" ")[2] ? ` · ${k.split(" ").slice(2).join(" ")}` : ""}`;
function sshEditor() {
  if (sshKeys === null) { refreshSshKeys(); return ""; }
  return `<div class="box-wifi">
    <h4>SSH keys for Xos boxes <span class="muted small">· log in as <code>xosadmin</code> on every box</span></h4>
    ${sshKeys.repo.length ? `<p class="small">From Git (<code>os/appliance/ssh/authorized_keys</code>): ${sshKeys.repo.map((k) => `<code>${esc(keyName(k))}</code>`).join(", ")}</p>` : ""}
    <label class="small">Added here, one public key per line
      <textarea id="ssh-keys" rows="3" spellcheck="false" placeholder="ssh-ed25519 AAAA… you@laptop">${esc(sshKeys.keys.join("\n"))}</textarea></label>
    <div class="dc-row"><button class="btn-sm btn-primary" data-act="ssh-save">Save keys</button>
      ${sshNote ? `<span class="small" role="status">${esc(sshNote)}</span>` : ""}</div>
    <p class="muted small">Public keys only. Every box picks up changes within 10 minutes; a key removed here is removed from every box.</p>
  </div>`;
}

function addLinuxBox() {
  return `<div class="add-ip">
    <h3>Xos Linux box (room computer or display): set it up here, then flash it</h3>
    <div class="dc-row ip-row">
      <label>Role <select id="box-role"><option value="room">Room computer · Meet + Rally Bar</option><option value="display">Display · Xos screen</option></select></label>
      <label>Room <select id="box-room"><option value="">—</option>${S.rooms().map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join("")}</select></label>
      <label>Name <input id="box-name" maxlength="60" placeholder="e.g. Atrium room computer" /></label>
      <label>Show (display) <select id="box-show"><option value="">Pairing code until picked</option>${routeOptions("")}</select></label>
      <button class="btn-sm btn-primary" data-act="box-create">Create</button>
    </div>
    <p class="muted small">Xos makes the box's name, hostname, device key and a maintenance login (<code>xosadmin</code>) and a one-time setup code. Flash a Debian 13 USB, choose <b>Automated install</b> and enter the link it shows: the box installs, names itself, joins its room and boots into Xos. Updates then arrive by themselves (nightly, or <b>Update now</b>).</p>
    ${boxNote ? `<p class="small" role="status">${esc(boxNote)}</p>` : ""}
    ${wifiEditor()}
  </div>`;
}

// Screens showing a pairing code right now (GET /api/devices/pairing), polled while Add is open.
let waiting = [];
let addCode = "";
let waitTimer = null;
async function refreshWaiting() {
  try { const r = await fetch("/api/devices/pairing", { cache: "no-store" }); waiting = r.ok ? await r.json() : []; } catch (_) { /* keep */ }
  if (!addOpen && waitTimer) { clearInterval(waitTimer); waitTimer = null; }
  if (!editing()) S.poke();
}
function openAdd(code = "") {
  addOpen = true; addNote = ""; ipNote = ""; if (code) addCode = code;
  refreshIpJobs(); refreshWaiting();
  if (!waitTimer) waitTimer = setInterval(refreshWaiting, 4000);
}
const kindOfClient = (c) => /^xos-display/.test(c) ? "Xos box" : /^firetv/.test(c) ? "Fire TV" : /^ios/.test(c) ? "iPad / iPhone" : /^pi-/.test(c) ? "Raspberry Pi" : "Browser";
let addUse = "screen";
// What's been typed and picked, so the waiting list's refresh (every 4 s) can't clear it.
let addForm = { name: "", room: "", route: "page/welcome" };
const pickedBox = () => waiting.some((w) => w.code === addCode && /^xos-display/.test(w.client));
const roomUse = () => pickedBox() && addUse === "room";
function waitingList() {
  if (!waiting.length) return `<p class="muted small add-none">No screen is showing a code right now. Turn one on (or flash an Xos box) and it appears here.</p>`;
  return `<ul class="add-waiting">${waiting.map((w) => `<li class="${w.code === addCode ? "on" : ""}">
      <span class="add-wcode">${esc(w.code.slice(0, 3))} ${esc(w.code.slice(3))}</span>
      <span class="add-wwho"><b>${esc(kindOfClient(w.client))}</b><span class="muted small">${esc([w.model, w.ip, w.screen].filter(Boolean).join(" · ") || w.device)}</span></span>
      <span class="muted small">${ago(w.since)}</span>
      <button class="btn-sm ${w.code === addCode ? "btn-primary" : ""}" data-act="add-pick" data-code="${esc(w.code)}">${w.code === addCode ? "Selected" : "Add this one"}</button></li>`).join("")}</ul>`;
}

function addScreen() {
  if (!addOpen) return "";
  const more = (key, title, body) => `<details class="add-more add-more-${key}"><summary>${title}</summary><div class="add-more-body">${body}</div></details>`;
  return `<div class="ex-card add-screen">
    <div class="dc-head"><h2>Add a screen</h2><button class="link" data-act="add-toggle">Close ✕</button></div>
    <section class="add-main">
      <h3>Waiting to be added</h3>
      ${waitingList()}
      <div class="add-form">
        <label class="add-code-l">Code on the screen <input id="add-code" inputmode="numeric" maxlength="7" placeholder="123 456" value="${esc(addCode ? `${addCode.slice(0, 3)} ${addCode.slice(3)}` : "")}" /></label>
        <label>Name <input id="add-name" maxlength="60" placeholder="e.g. Atrium room TV" value="${esc(addForm.name)}" /></label>
        ${pickedBox() ? `<label>Use as <select id="add-use">
          <option value="screen" ${addUse === "screen" ? "selected" : ""}>A screen</option>
          <option value="room" ${addUse === "room" ? "selected" : ""}>Meeting room computer</option></select></label>` : ""}
        <label>Room <select id="add-room"><option value="">—</option>${S.rooms().map((r) => `<option value="${esc(r.id)}" ${addForm.room === r.id ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></label>
        <label class="add-shows" ${roomUse() ? "hidden" : ""}>Shows <select id="add-route">${routeOptions(addForm.route)}</select></label>
        <button class="btn-primary add-go" data-act="add-claim">${roomUse() ? "Make it the meeting computer" : "Add screen"}</button>
        <p class="small muted add-use-note" ${roomUse() ? "" : "hidden"}>Runs Google Meet, Zoom, Teams and Webex on the room TV. The box installs the meeting software and restarts as the room's computer (a few minutes). The TV, Rally Bar and the room tablet's Join, mute and leave then run through it.</p>
      </div>
      ${addNote ? `<p class="small" role="status">${esc(addNote)}</p>` : ""}
    </section>
    <h3 class="add-other">Setting up a new device</h3>
    ${more("box", "Xos box (mini PC): flash the Xos USB", `<p class="small">Boot the mini PC from the Xos USB and press Enter on <b>Xos install</b>. It installs by itself (Wi-Fi built in) and shows a code here under <b>Waiting to be added</b>. Make the USB once with <code>os/image/make-usb.sh</code> (docs: Linux appliance).</p>`)}
    ${more("firetv", "Fire TV, Android TV or smart board", `${addByIp().replace('<h3 class="add-or">Or open Xos on the screen and enter its code</h3>', "")}
      <p class="small">Or without ADB: install <i>Downloader</i> from the Amazon Appstore, enter <code>${esc(location.host)}/firetv/xos-tv.apk</code> and open <b>Xos TV</b>: it shows a code.</p>`)}
    ${more("ipad", "iPad, iPhone, smart TV or any browser", `<p class="small">Open <code>${esc(location.host)}/setup</code>. On an iPad, <i>Share → Add to Home Screen</i> and open it from there for full screen, then use Guided Access to lock it to Xos. It shows a code.</p>`)}
    ${more("room", "Room computer (Meet, Zoom, Teams, Webex + Rally Bar): per-box setup code", addLinuxBox().replace(wifiEditor(), ""))}
    ${more("wifi", "Wi-Fi for Xos boxes", wifiEditor())}
    ${more("ssh", "SSH keys for Xos boxes", sshEditor())}
  </div>`;
}

function screens() {
  if (!S.isHub()) return hubOnly("Screens");
  const deep = location.hash.match(/\/pair-(\d{6})$/);
  if (deep) { window.history.replaceState(null, "", "#/admin/screens"); openAdd(deep[1]); }
  if (!live.loaded) { refreshLive(); return `<h1>Screens</h1><p class="muted">Loading screens…</p>`; }
  if (!lib.loaded) refreshLibrary();
  const now = Date.now() / 1000;
  const sorted = [...live.list].sort((a, b) => (b.online - a.online) || (S.room(a.room)?.name || "~").localeCompare(S.room(b.room)?.name || "~") || (b.last_seen - a.last_seen));
  const stale = sorted.filter((d) => !d.online && now - d.last_seen > DAY);
  const shown = showOffline ? sorted : sorted.filter((d) => !stale.includes(d));
  return `<div class="adm-head"><h1>Screens <span class="muted">· ${live.list.filter((d) => d.online).length} online</span></h1>
      ${can("setup") ? `<button class="btn-sm btn-primary" data-act="add-toggle">${addOpen ? "Close" : "+ Add a screen"}</button>` : ""}</div>
    <p class="lead">Every screen running Xos: Fire TV sticks, iPads, browsers and Linux nodes. <b>Control</b> changes what a screen shows, its share link, and more.</p>
    ${can("setup") ? addScreen() : ""}
    <div class="dc-row dc-tools"><input id="dc-filter" type="search" data-filter=".sc-card" placeholder="Filter by name, room, model or address" value="${esc(filter)}" />
      <span class="grow"></span>
      ${can("screens.show") ? `<input id="dc-bulk-msg" maxlength="200" placeholder="Message every screen" /><button class="btn-sm" data-act="dc-bulk" data-type="message">Show for 30 s</button>
      <button class="btn-sm" data-act="dc-bulk" data-type="reload" title="Reload every online display, e.g. after a deploy">Reload all</button>` : ""}
      ${bulkNote ? `<span class="small muted">${esc(bulkNote)}</span>` : ""}</div>
    ${Object.keys(KIND_LABELS).map((k) => {
      const list = shown.filter((d) => kindOf(d) === k);
      return list.length ? `<h2 class="sc-kind">${KIND_LABELS[k]} <span class="muted">· ${list.length}</span></h2><div class="sc-grid">${list.map(screenCard).join("")}</div>` : "";
    }).join("") || `<p class="muted">No screens yet. Use <b>+ Add a screen</b>.</p>`}
    ${can("setup") ? linuxBoxes() : ""}
    ${stale.length ? `<button class="link small" data-act="dc-stale">${showOffline ? "Hide" : "Show"} ${stale.length} screen${stale.length > 1 ? "s" : ""} offline for over a day</button>` : ""}`;
}

// ---------- Content: media library and pages ----------

let lib = { loaded: false, items: [], used_bytes: 0, max_bytes: 0 };
let contentNote = "";
let uploads = []; // [{name, pct, error}] in progress or just failed
let target = ""; // screen chosen in "Show on"
let form = null; // {kind, id?} the open New / Edit form
let picks = []; // media ids chosen for a slideshow, in order

async function refreshLibrary() {
  if (!S.isHub()) return;
  try {
    lib = { loaded: true, ...(await (await fetch("/api/media", { cache: "no-store" })).json()) };
    if (!editing()) S.poke();
  } catch (_) { /* keep the last list */ }
}

const KINDS = [
  ["slides", "Slideshow", "Photos and videos from the library, one after another. Videos play to the end."],
  ["announcement", "Announcement", "A headline and message, laid out in the Xos style."],
  ["url", "Web link", "Another website or internal app. Some sites refuse to be shown inside other pages."],
  ["html", "HTML page", "One self-contained .html file: a dashboard, deck, report or anything you build."],
  ["playlist", "Playlist", "Rotate pages, each for its own number of seconds. Screens showing it stay in sync."],
];
const kindLabel = (p) => (p.builtin ? "Built in" : { slides: "Slideshow", announcement: "Announcement", url: "Web link", html: "HTML page", playlist: "Playlist" }[p.kind] || p.kind);

function thumb(m, cls = "") {
  if (m.kind === "video") {
    return m.poster ? `<img class="${cls}" src="${esc(m.poster)}" alt="" loading="lazy" />`
      : `<video class="${cls}" src="${esc(m.url)}#t=0.5" muted preload="metadata" playsinline></video>`;
  }
  return `<img class="${cls}" src="${esc(m.url)}" alt="" loading="lazy" />`;
}

function pageDetail(p, list) {
  if (p.builtin) return esc(p.note || "");
  if (p.kind === "slides") {
    const n = p.items?.length || 0;
    return `${n} item${n === 1 ? "" : "s"} · ${p.fit === "contain" ? "whole picture" : "fills the screen"}`;
  }
  if (p.kind === "announcement") return `“${esc(p.announcement?.title || "")}”`;
  if (p.kind === "html") return `${Math.round((p.bytes || 0) / 1024)} KB`;
  if (p.kind === "playlist") return (p.items || []).map((i) => `${esc(list.find((x) => x.id === i.page)?.name || i.page)} ${i.seconds}s`).join(" → ");
  return `<span class="small">${esc(p.url)}</span>`;
}

function pageCard(p, list) {
  const preview = p.kind === "playlist" ? `./#/page/${esc(p.id)}` : esc(pageSrc(p));
  const editable = !p.builtin && p.kind !== "html";
  return `<div class="pg-card">
    <div class="pg-top"><span class="pg-kind pg-${esc(p.builtin ? "builtin" : p.kind)}">${kindLabel(p)}</span><b>${esc(p.name)}</b></div>
    <div class="small muted pg-detail">${pageDetail(p, list)}</div>
    <div class="small muted">${p.builtin ? "Built in" : `Updated ${dayShort(p.updated * 1000)} ${time(p.updated * 1000)}`}</div>
    <div class="dc-row pg-acts">
      ${can("screens.show") ? `<button class="btn-sm btn-primary" data-act="ct-show" data-route="page/${esc(p.id)}" ${target ? "" : "disabled title=\"Choose a screen in Show on\""}>Show</button>` : ""}
      <a class="btn-sm" href="${preview}" target="_blank" rel="noopener">Preview ↗</a>
      ${!can("content.edit") ? "" : editable ? `<button class="btn-sm" data-act="pg-edit" data-id="${esc(p.id)}">Edit</button>` : ""}
      ${p.kind === "html" && can("content.edit") ? `<label class="btn-sm file-btn">New version<input type="file" accept=".html,.htm,text/html" data-replace="${esc(p.id)}" data-name="${esc(p.name)}" hidden /></label>` : ""}
      ${p.builtin || !can("content.edit") ? "" : `<button class="btn-sm" data-act="pg-delete" data-id="${esc(p.id)}">Delete</button>`}
    </div>
  </div>`;
}

function mediaTile(m) {
  return `<div class="md-tile">
    <div class="md-thumb">${thumb(m)}${m.kind === "video" ? `<span class="md-badge">▶ Video</span>` : ""}</div>
    <div class="md-info"><b title="${esc(m.name)}">${esc(m.name)}</b>
      <div class="muted small">${m.builtin ? "Built in" : kb(m.bytes || 0)}${m.used_by?.length ? ` · in ${esc(m.used_by.join(", "))}` : ""}</div></div>
    <div class="dc-row md-acts">
      ${can("screens.show") ? `<button class="btn-sm" data-act="ct-show" data-route="media/${esc(m.id)}" ${target ? "" : "disabled title=\"Choose a screen in Show on\""}>Show</button>` : ""}
      <a class="btn-sm" href="${esc(mediaSrc(m.id))}" target="_blank" rel="noopener">View ↗</a>
      ${m.builtin || !can("content.edit") ? "" : `<button class="btn-sm" data-act="md-rename" data-id="${esc(m.id)}" data-name="${esc(m.name)}">Rename</button> <button class="btn-sm" data-act="md-delete" data-id="${esc(m.id)}">Delete</button>`}
    </div>
  </div>`;
}

function slideshowFields(p) {
  const items = lib.items;
  return `<div class="dc-row"><label>Each photo shows for <input id="f-secs" type="number" min="2" max="3600" value="${p?.items?.[0]?.seconds || 10}" /> seconds</label>
      <label>Fit <select id="f-fit"><option value="cover" ${p?.fit !== "contain" ? "selected" : ""}>Fill the screen (crop)</option><option value="contain" ${p?.fit === "contain" ? "selected" : ""}>Whole picture (letterbox)</option></select></label></div>
    <p class="small muted">Click photos and videos in the order they should play (${picks.length} chosen). Upload more below the form.</p>
    <div class="md-pick">${items.map((m) => {
      const k = picks.indexOf(m.id);
      return `<button class="md-pick-item ${k >= 0 ? "on" : ""}" data-act="sl-pick" data-id="${esc(m.id)}" title="${esc(m.name)}">${thumb(m)}${k >= 0 ? `<span class="md-num">${k + 1}</span>` : ""}${m.kind === "video" ? `<span class="md-badge">▶</span>` : ""}</button>`;
    }).join("") || `<span class="muted small">Upload photos or videos first.</span>`}</div>`;
}

function contentForm(list) {
  if (!form) return "";
  const p = form.id ? list.find((x) => x.id === form.id) : null;
  const a = p?.announcement || {};
  const [, label, hint] = KINDS.find((k) => k[0] === form.kind);
  const fields = {
    slides: () => slideshowFields(p),
    announcement: () => `<div class="dc-row"><label>Tag <input id="f-tag" maxlength="30" placeholder="People" value="${esc(a.tag || "")}" /></label>
        <label>Style <select id="f-style">${[["dark", "Dark"], ["gradient", "Gradient"], ["light", "Light"]].map(([v, l]) => `<option value="${v}" ${a.style === v ? "selected" : ""}>${l}</option>`).join("")}</select></label></div>
      <div class="dc-row"><label class="grow">Headline <input id="f-title" maxlength="120" placeholder="Staff offsite flights" value="${esc(a.title || "")}" /></label></div>
      <div class="dc-row"><label class="grow">Message <textarea id="f-body" rows="3" maxlength="600" placeholder="Book through Navan by Friday. Details in #general.">${esc(a.body || "")}</textarea></label></div>`,
    url: () => `<div class="dc-row"><label class="grow">Link <input id="f-url" placeholder="https://… or /path on this server" value="${esc(p?.url || "")}" /></label></div>`,
    html: () => `<div class="dc-row"><label>HTML file <input type="file" id="f-file" accept=".html,.htm,text/html" /></label></div>`,
    playlist: () => `<div class="pl-pick">${list.filter((x) => x.kind !== "playlist" && x.id !== form.id).map((x) => {
      const it = p?.items?.find((i) => i.page === x.id);
      return `<label class="pl-item"><input type="checkbox" class="pl-on" value="${esc(x.id)}" ${it ? "checked" : ""} /> <span>${esc(x.name)}</span> <input type="number" class="pl-sec" data-id="${esc(x.id)}" min="5" max="3600" value="${it?.seconds || (x.builtin ? 130 : 20)}" /> s</label>`;
    }).join("")}</div>`,
  };
  return `<div class="ex-card pg-add">
    <div class="dc-head"><h3>${p ? `Edit ${esc(p.name)}` : `New ${label.toLowerCase()}`}</h3><button class="link" data-act="f-close">Cancel ✕</button></div>
    <p class="muted small">${hint}</p>
    <div class="dc-row"><label class="grow">Name <input id="f-name" maxlength="80" placeholder="e.g. Lobby welcome" value="${esc(p?.name || "")}" /></label></div>
    ${fields[form.kind]()}
    <div class="dc-row"><button class="btn-sm btn-primary" data-act="f-save">${p ? "Save changes" : "Save"}</button>${contentNote ? `<span class="small">${esc(contentNote)}</span>` : ""}</div>
  </div>`;
}

function content() {
  if (!S.isHub()) return hubOnly("Content");
  const list = getPages();
  if (!list.length) loadPages();
  if (!lib.loaded) refreshLibrary();
  if (!live.loaded) refreshLive();
  const displays = live.list.filter((d) => d.online && DISPLAY_ROLES.has(d.role || ""));
  if (target && !displays.some((d) => d.id === target)) target = "";
  const uploaded = lib.items.filter((m) => !m.builtin);
  const builtin = lib.items.filter((m) => m.builtin);
  const edit = can("content.edit");
  return `<div class="adm-head"><h1>Content</h1>
      ${!can("screens.show") ? "" : `<label class="ct-target">Show on <select id="ct-screen"><option value="">Choose a screen…</option>${displays.map((d) => `<option value="${esc(d.id)}" ${d.id === target ? "selected" : ""}>${esc(devName(d))}</option>`).join("")}</select></label>`}</div>
    <p class="lead">Upload photos and videos, then make a slideshow, an announcement, a link or a playlist. Pick a screen in <b>Show on</b>, then press <b>Show</b> on anything. Lobby signage plays the welcome page until you choose something else.</p>
    ${contentNote && !form ? `<div class="ex-card callout">${esc(contentNote)}</div>` : ""}
    ${!edit ? "" : `<div class="ct-new"><span class="small muted">New</span>
      <label class="chip-sm chip-primary file-btn">⬆ Upload photos &amp; videos<input type="file" id="md-file" accept="image/jpeg,image/png,image/gif,image/webp,video/mp4,video/webm,video/quicktime" multiple hidden /></label>
      ${KINDS.map(([k, l]) => `<button class="chip-sm ${form?.kind === k && !form.id ? "on" : ""}" data-act="f-new" data-kind="${k}">${l}</button>`).join("")}</div>
    ${contentForm(list)}`}
    <h2>Pages <span class="muted small">· what screens can show</span></h2>
    <div class="pg-grid">${list.map((p) => pageCard(p, list)).join("") || `<p class="muted">Loading…</p>`}</div>
    <h2>Photos &amp; videos <span class="muted small">· ${kb(lib.used_bytes || 0)} of ${kb(lib.max_bytes || 0)} used</span></h2>
    ${!edit ? "" : `<div class="md-drop" id="md-drop"><b>Drop photos or videos here</b>
      <span class="muted small">or use <i>Upload</i> above. JPEG, PNG, GIF, WebP up to ${kb(lib.max_image_bytes || 25e6)}; MP4, WebM, MOV up to ${kb(lib.max_video_bytes || 3e8)}. MP4 (H.264) plays everywhere.</span>
      <div class="dc-row"><input id="md-url" placeholder="…or paste a link to a photo or video" /><button class="btn-sm" data-act="md-import">Add from link</button></div>
      ${uploads.map((u) => `<div class="small md-up">${esc(u.name)} · ${u.error ? `<span class="dc-st dc-failed">${esc(u.error)}</span>` : `${u.pct}%`}</div>`).join("")}</div>`}
    <div class="md-grid">${uploaded.map(mediaTile).join("")}</div>
    ${builtin.length ? `<details class="md-builtin"><summary>Built-in photos &amp; videos (${builtin.length})</summary><div class="md-grid">${builtin.map(mediaTile).join("")}</div></details>` : ""}`;
}

function uploadFiles(files) {
  [...files].forEach((file) => {
    const u = { name: file.name, pct: 0, error: "" };
    uploads.push(u);
    const xhr = new XMLHttpRequest();
    const name = file.name.replace(/\.[a-z0-9]+$/i, "");
    xhr.open("POST", `/u/api/media?name=${encodeURIComponent(name)}&filename=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { u.pct = Math.round((e.loaded / e.total) * 100); S.poke(); } };
    xhr.onload = () => {
      if (xhr.status === 200) {
        uploads = uploads.filter((x) => x !== u);
        const item = JSON.parse(xhr.responseText);
        if (form?.kind === "slides") picks.push(item.id); // uploading while making a slideshow adds it
      } else {
        try { u.error = JSON.parse(xhr.responseText).detail; } catch (_) { u.error = `failed (${xhr.status})`; }
        setTimeout(() => { uploads = uploads.filter((x) => x !== u); S.poke(); }, 15000);
      }
      refreshLibrary();
    };
    xhr.onerror = () => { u.error = "network error"; S.poke(); };
    xhr.send(file);
  });
  S.poke();
}

// Drag and drop onto the Content page.
document.addEventListener("dragover", (e) => { if (location.hash.startsWith("#/admin/content") && e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); document.getElementById("md-drop")?.classList.add("over"); } });
document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) document.getElementById("md-drop")?.classList.remove("over"); });
document.addEventListener("drop", (e) => {
  if (!location.hash.startsWith("#/admin/content") || !e.dataTransfer?.files?.length) return;
  e.preventDefault();
  document.getElementById("md-drop")?.classList.remove("over");
  uploadFiles(e.dataTransfer.files);
});

async function api(method, path, body) {
  const res = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) }).catch(() => null);
  if (res?.ok) return { ok: true, body: await res.json() };
  let detail = "network error";
  if (res) { try { const d = (await res.json()).detail; detail = Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d; } catch (_) { detail = `HTTP ${res.status}`; } }
  return { ok: false, detail };
}

async function saveForm() {
  const $ = (id) => document.getElementById(id);
  const name = $("f-name").value.trim();
  const fail = (msg) => { contentNote = msg; S.poke(); };
  let body;
  if (form.kind === "slides") {
    if (!picks.length) return fail("Choose at least one photo or video.");
    const seconds = Math.max(2, Number($("f-secs").value) || 10);
    body = { name: name || "Slideshow", slides: picks.map((media) => ({ media, seconds })), fit: $("f-fit").value };
  } else if (form.kind === "announcement") {
    const title = $("f-title").value.trim();
    if (!title) return fail("Add a headline.");
    body = { name: name || title, announcement: { title, body: $("f-body").value.trim(), tag: $("f-tag").value.trim(), style: $("f-style").value } };
  } else if (form.kind === "url") {
    const url = $("f-url").value.trim();
    if (!url) return fail("Enter a link.");
    body = { name: name || url, url };
  } else if (form.kind === "html") {
    const f = $("f-file").files[0];
    if (!f) return fail("Choose an HTML file.");
    body = { name: name || f.name.replace(/\.html?$/i, ""), html: await f.text() };
  } else {
    const playlist = [...document.querySelectorAll(".pl-on:checked")].map((c) => ({
      page: c.value, seconds: Math.max(5, Number(document.querySelector(`.pl-sec[data-id="${c.value}"]`).value) || 20),
    }));
    if (!playlist.length) return fail("Tick at least one page for the playlist.");
    body = { name: name || "Playlist", playlist };
  }
  const r = form.id ? await api("PUT", `/api/pages/${encodeURIComponent(form.id)}`, body) : await api("POST", "/api/pages", body);
  if (!r.ok) return fail(`Couldn't save: ${r.detail}`);
  contentNote = form.id ? `Saved “${body.name}”. Screens showing it update within a minute.` : `Saved “${body.name}”. Choose a screen in Show on, then press Show.`;
  form = null;
  picks = [];
  await loadPages();
  refreshLibrary();
}

// ---------- Integrations: Zoom, Google Calendar (next), MCP ----------

let zoomSt = null;
let zoomList = [];
let zoomLog = [];
let zoomNote = "";

async function refreshZoomAdmin() {
  try {
    zoomSt = await (await fetch("/api/zoom/status", { cache: "no-store" })).json();
    zoomList = zoomSt.connected ? await (await fetch("/api/zoom/rooms", { cache: "no-store" })).json() : [];
    zoomLog = (await (await fetch("/api/zoom/log", { cache: "no-store" })).json()).slice(0, 12);
  } catch (_) { /* keep last */ }
  if (!editing()) S.poke();
}
setInterval(() => { if (/^#\/admin\/(integrations|zoom)/.test(location.hash)) refreshZoomAdmin(); }, 15000);

function zoomSection() {
  if (!zoomSt) { refreshZoomAdmin(); return `<p class="muted">Checking the Zoom connection…</p>`; }
  const banner = !zoomSt.configured
    ? `<div class="zm-banner zm-wait"><b>Not connected yet.</b> Xos needs the Zoom app credentials (<code>ZOOM_ACCOUNT_ID</code>, <code>ZOOM_CLIENT_ID</code>, <code>ZOOM_CLIENT_SECRET</code>) set as app secrets. Until then every Zoom button is a logged dry run. <a href="https://github.com/exowatt-oss/xos#readme" target="_blank" rel="noopener">Setup steps ↗</a></div>`
    : zoomSt.connected ? `<div class="zm-banner zm-ok"><b>Connected to Zoom.</b> ${zoomSt.zoom_rooms} Zoom Room${zoomSt.zoom_rooms === 1 ? "" : "s"} in the account.</div>`
    : `<div class="zm-banner zm-err"><b>Zoom error.</b> ${esc(zoomSt.error)}</div>`;
  const zoomName = (id) => zoomList.find((z) => z.id === id)?.name || id;
  const rows = S.rooms().map((r) => {
    const mapped = zoomSt.rooms[r.id] || "";
    const picker = zoomSt.connected
      ? `<select id="zm-${esc(r.id)}"><option value="">— not linked —</option>${zoomList.map((z) => `<option value="${esc(z.id)}" ${z.id === mapped ? "selected" : ""}>${esc(z.name)} · ${esc(z.status || "")}</option>`).join("")}</select>`
      : `<input id="zm-${esc(r.id)}" value="${esc(mapped)}" placeholder="Zoom Room ID (link once connected)" />`;
    return `<tr><td><b>${esc(r.name)}</b></td>
      <td>${picker} <button class="btn-sm" data-act="zm-save" data-room="${esc(r.id)}">Save</button></td>
      <td class="small">${mapped ? `Linked to <b>${esc(zoomName(mapped))}</b>` : `<span class="muted">Not linked: controls stay dry runs</span>`}
        ${zoomSt.connected ? ` <button class="btn-sm" data-act="zm-setup" data-room="${esc(r.id)}" data-new="${mapped ? "" : "1"}" data-name="${esc(r.name)}">${mapped ? "Activation code" : "Set up Zoom Room"}</button>` : ""}</td></tr>`;
  }).join("");
  return `${banner}
    ${zoomNote ? `<div class="callout small">${esc(zoomNote)}</div>` : ""}
    <h3 class="adm-h2">Rooms</h3>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Xos room</th><th>Zoom Room</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <details class="dr-more zm-log"><summary>Recent Zoom activity</summary>
    <table class="tbl"><tbody>${zoomLog.map((e) => `<tr><td class="small">${new Date(e.t * 1000).toLocaleTimeString()}</td><td>${esc(e.room)}</td><td>${esc(e.action)}</td>
      <td class="small">${e.live?.error ? `<span class="dc-st dc-failed">failed</span> ${esc(e.live.error)}` : e.sent ? `<span class="dc-st dc-done">sent to Zoom</span> ${esc(e.live?.method || "")}` : `<span class="dc-st">dry run</span>`}</td></tr>`).join("") || `<tr><td class="muted">Nothing yet.</td></tr>`}</tbody></table></details>
    <details class="dr-more zm-how"><summary>Connecting Zoom (Zoom admin, once)</summary><ol class="small">
      <li><a href="https://marketplace.zoom.us/" target="_blank" rel="noopener">marketplace.zoom.us</a> → <b>Develop → Build App → Server-to-Server OAuth</b>, name it “Xos”.</li>
      <li>Scopes: <code>room:read:admin</code> and <code>room:write:admin</code>. Activate the app.</li>
      <li>Put the <b>Account ID, Client ID and Client secret</b> in the Xos app secrets (environment variables on the server). Never paste them in chat.</li>
      <li>Come back here: the banner turns green. Link each room to its Zoom Room and its remote's Zoom buttons go live.</li></ol></details>`;
}

// Room calendars: each room's Google resource calendar (src/app/calendars.py).
let calSt = null;
let calRooms = {}; // room id -> GET /api/rooms/{room}/calendar
let calNote = "";
let creds = null; // GET /api/integrations/credentials: each secret set or not, never a value
const credOf = (key) => creds?.find((c) => c.key === key);
async function refreshCalAdmin() {
  try {
    calSt = await (await fetch("/api/calendar/status", { cache: "no-store" })).json();
    const mapped = Object.entries(calSt.rooms).filter(([, c]) => c);
    const got = await Promise.all(mapped.map(([id]) => fetch(`/api/rooms/${encodeURIComponent(id)}/calendar`, { cache: "no-store" }).then((r) => r.json()).catch(() => null)));
    calRooms = Object.fromEntries(mapped.map(([id], i) => [id, got[i]]));
  } catch (_) { /* keep last */ }
  // After the calendar reads, so a refresh Google just refused shows here too.
  try {
    const response = await fetch("/api/integrations/credentials", { cache: "no-store" });
    if (response.ok) creds = (await response.json()).items;
  } catch (_) { /* keep last */ }
  if (!editing()) S.poke();
}
setInterval(() => { if (/^#\/admin\/integrations/.test(location.hash)) refreshCalAdmin(); }, 60000);

function calSection() {
  if (!calSt) { refreshCalAdmin(); return `<p class="muted small">Checking…</p>`; }
  const cal = credOf("google_calendar");
  const since = cal?.connected_at ? `, connected ${new Date(cal.connected_at * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}${cal.connected_by ? ` by ${esc(cal.connected_by)}` : ""}` : "";
  const banner = cal?.status === "action" && calSt.oauth_connected
    ? `<div class="zm-banner zm-err"><b>Google Calendar needs reconnecting.</b> ${esc(cal.detail)}</div>`
    : calSt.configured
    ? `<div class="zm-banner zm-ok"><b>Reading room calendars</b> as ${esc(cal?.account || calSt.user)}${since}.</div>`
    : `<div class="zm-banner zm-wait"><b>Not connected yet.</b> Connect the Google account that can read the room calendars. This does not sign Meet or Zoom into the room computer.</div>`;
  const connect = calSt.oauth_available
    ? `<p><a class="btn-sm btn-primary" href="/u/api/calendar/oauth/start">${calSt.oauth_connected ? "Reconnect" : "Connect"} Google Calendar</a></p>`
    : `<p class="small muted">An administrator must configure the Google Calendar OAuth client, or an existing read-only service account. <a href="https://github.com/exowatt-oss/xos#readme" target="_blank" rel="noopener">Setup steps ↗</a></p>`;
  const rows = S.rooms().map((r) => {
    const cal = calSt.rooms[r.id] || "";
    const got = calRooms[r.id];
    const next = got?.bookings?.[0];
    const state = !cal ? `<span class="muted">No calendar</span>` : !calSt.configured ? `<span class="muted">Waiting for the connection</span>`
      : got?.error ? `<span class="dc-st dc-failed">error</span> ${esc(got.error)}`
      : next ? `Next: <b>${esc(next.title)}</b> at ${new Date(next.start * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : `Nothing more booked today`;
    return `<tr><td><b>${esc(r.name)}</b></td>
      <td><input id="cal-${esc(r.id)}" value="${esc(cal)}" placeholder="c_…@resource.calendar.google.com" /> <button class="btn-sm" data-act="cal-save" data-room="${esc(r.id)}">Save</button></td>
      <td class="small">${state}</td></tr>`;
  }).join("");
  return `${banner}${connect}${calNote ? `<div class="callout small">${esc(calNote)}</div>` : ""}
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Xos room</th><th>Resource calendar</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function integrations() {
  if (!S.isHub()) return hubOnly("Integrations");
  return `<h1>Integrations</h1>
    <p class="lead">What Xos connects to. Each one adds controls to the rooms' remotes and screens.</p>
    <div class="ig-grid">
      <section class="ex-card ig-card ig-wide"><div class="ig-head"><h2>Zoom Rooms</h2><span class="dc-st ${zoomSt?.connected ? "dc-done" : ""}">${zoomSt?.connected ? "Connected" : zoomSt?.configured ? "Error" : "Not connected"}</span></div>
        <p class="small muted">From a room's table remote: join a meeting (Zoom ID or link, or a Google Meet code), start one, mute, camera, volume, leave and end, through Zoom's cloud API. <b>Set up Zoom Room</b> creates a room's Zoom Room and gives the activation code for its PC.</p>
        ${zoomSection()}</section>
      <section class="ex-card ig-card ig-wide"><div class="ig-head"><h2>Google Calendar</h2><span class="dc-st ${calSt?.configured ? "dc-done" : ""}">${calSt?.configured ? "Connected" : "Not connected"}</span></div>
        <p class="small muted">Each room's resource calendar: its remote shows what's on now and next, with one-tap <b>Join</b> for the booking's Meet (or Zoom) link.</p>
        ${calSection()}</section>
      <section class="ex-card ig-card"><div class="ig-head"><h2>AI agents (MCP)</h2><span class="dc-st ${credOf("mcp")?.status === "ok" ? "dc-done" : ""}">${credOf("mcp")?.status === "ok" ? "Token set" : "Sign-in only"}</span></div>
        <p class="small muted">Everything here is also an MCP tool: list screens, put content on them, make slideshows and announcements, share, screenshots, rooms and Zoom. Add this server to Claude, phi or Cursor:</p>
        <code class="ig-code">${esc(location.origin)}/mcp</code>
        <p class="small muted">Send <code>Authorization: Bearer</code> with the Xos MCP token (ask the Xos owner). Setup is in the README.</p></section>
      <section class="ex-card ig-card"><div class="ig-head"><h2>AirPlay</h2><span class="dc-st">Linux nodes</span></div>
        <p class="small muted">Mini PC and Pi screens installed with <code>install.sh --airplay</code> show up as AirPlay targets. Fire TV screens use their share link instead.</p></section>
      <section class="ex-card ig-card ig-wide"><div class="ig-head"><h2>Credentials</h2><span class="dc-st ${creds && !creds.some((c) => c.status === "action") ? "dc-done" : creds ? "dc-sent" : ""}">${!creds ? "Checking…" : creds.some((c) => c.status === "action") ? `${creds.filter((c) => c.status === "action").length} need attention` : "All set"}</span></div>
        <p class="small muted">Every app secret Xos reads, whether it's set and whether it works. Values never leave the server; change them in the server's environment.</p>
        ${credentialsTable()}</section>
    </div>`;
}

const CRED_STATES = { ok: ["dc-done", "Working"], action: ["dc-failed", "Needs attention"], optional: ["", "Optional"], unused: ["dc-sent", "Set, not used"] };
function credentialsTable() {
  if (!creds) return `<p class="muted small">Checking…</p>`;
  return `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Integration</th><th>Status</th><th>What Xos sees</th><th>App secrets</th></tr></thead><tbody>${creds.map((c) => {
    const [cls, label] = CRED_STATES[c.status] || ["", c.status];
    const keys = Object.entries(c.secrets).map(([k, on]) => `<code class="${on ? "" : "muted"}" title="${on ? "Set" : "Not set"}">${on ? "✓" : "–"} ${esc(k)}</code>`).join(" ");
    return `<tr><td><b>${esc(c.name)}</b></td><td><span class="dc-st ${cls}">${label}</span></td><td class="small">${esc(c.detail)}</td><td class="small">${keys}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}

// Inputs that act as soon as they change (main.js passes every "input" event here).
function otherInput(el) {
  if (["add-name", "add-room", "add-route"].includes(el.id)) { addForm[el.id.slice(4)] = el.value; return true; }
  if (el.id === "add-use") {
    // Switched in place, so the name and room already typed stay.
    addUse = el.value;
    const form = el.closest(".add-form");
    form.querySelector(".add-shows").hidden = roomUse();
    form.querySelector(".add-use-note").hidden = !roomUse();
    form.querySelector(".add-go").textContent = roomUse() ? "Make it the meeting computer" : "Add screen";
    return true;
  }
  if (el.id === "ct-screen") { target = el.value; S.poke(); return true; }
  if (el.id === "md-file" && el.files.length) { uploadFiles(el.files); el.value = ""; return true; }
  if (el.dataset.replace && el.files.length) {
    const file = el.files[0];
    file.text().then((html) => api("PUT", `/api/pages/${encodeURIComponent(el.dataset.replace)}`, { name: el.dataset.name, html }))
      .then((r) => { contentNote = r.ok ? `Uploaded a new version of “${el.dataset.name}”. Screens update within a minute.` : `Couldn't save: ${r.detail}`; el.value = ""; loadPages(); });
    return true;
  }
  if (el.dataset.act === "dc-share") {
    fetch(`/api/devices/${encodeURIComponent(selected)}/share`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [el.dataset.key]: el.checked }) }).then(refreshLive);
    return true;
  }
  return false;
}

const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export function handleAdmin(act, el) {
  if (handleGuide(act, el)) return "local";
  if (handlePeople(act, el)) return "local";
  if (act === "zm-setup") {
    // Creates the room's Zoom Room (one Zoom Rooms licence) if it has none, then shows the code
    // that signs in the Zoom Rooms app on the room's PC.
    const room = el.dataset.room;
    if (el.dataset.new && !confirm(`Create a Zoom Room for ${el.dataset.name} in your Zoom account? It uses one Zoom Rooms licence.`)) return "local";
    zoomNote = "Asking Zoom…";
    fetch("/api/zoom/setup", json("POST", { room })).then(async (res) => {
      const out = await res.json().catch(() => ({}));
      zoomNote = !res.ok ? `Couldn't set up the Zoom Room: ${out.detail || res.statusText}`
        : `${out.created ? `Created a Zoom Room for ${el.dataset.name}. ` : ""}Activation code: ${out.activation_code || "(Zoom didn't return one; see the Zoom web portal)"}. On the room's PC, open Zoom Rooms → Sign in → Enter activation code. Codes expire after about 10 days; press Activation code again for a new one.`;
      refreshZoomAdmin();
    });
    return "local";
  }
  if (act === "cal-save") {
    const room = S.room(el.dataset.room);
    const calendar = document.getElementById(`cal-${room.id}`).value.trim();
    fetch(`/api/rooms/${encodeURIComponent(room.id)}`, json("PUT", { name: room.name, site: room.site, calendar }))
      .then(async (res) => { calNote = res.ok ? `Saved ${room.name}'s calendar.` : (await res.json()).detail; refreshCalAdmin(); });
    return "local";
  }
  if (act === "zm-save") {
    const room = el.dataset.room;
    const value = document.getElementById(`zm-${room}`).value.trim();
    fetch("/api/zoom/map", json("PUT", { room, zoom_room_id: value || null }))
      .then(() => { zoomNote = value ? `Linked ${room} to its Zoom Room.` : `Unlinked ${room}.`; refreshZoomAdmin(); });
    return "local";
  }
  // Content
  if (act === "f-new") { form = { kind: el.dataset.kind }; picks = []; contentNote = ""; return "local"; }
  if (act === "f-close") { form = null; picks = []; contentNote = ""; return "local"; }
  if (act === "f-save") { saveForm(); return "local"; }
  if (act === "pg-edit") {
    const p = getPages().find((x) => x.id === el.dataset.id);
    form = { kind: p.kind, id: p.id };
    picks = p.kind === "slides" ? (p.items || []).map((i) => i.media) : [];
    contentNote = "";
    window.scrollTo({ top: 0, behavior: "smooth" });
    return "local";
  }
  if (act === "sl-pick") {
    const k = picks.indexOf(el.dataset.id);
    if (k >= 0) picks.splice(k, 1); else picks.push(el.dataset.id);
    return "local";
  }
  if (act === "pg-delete") {
    const p = getPages().find((x) => x.id === el.dataset.id);
    if (!confirm(`Delete “${p?.name || el.dataset.id}”? Screens showing it will say it's missing.`)) return "local";
    fetch(`/api/pages/${encodeURIComponent(el.dataset.id)}`, { method: "DELETE" }).then(() => { loadPages(); refreshLibrary(); });
    return "local";
  }
  if (act === "ct-show") {
    if (!target) return "local";
    sendCommand(target, "route", { route: el.dataset.route });
    contentNote = `Sent to ${devName(live.list.find((d) => d.id === target) || { id: target })}. Its Control drawer has Next / Previous.`;
    return "local";
  }
  if (act === "md-import") {
    const input = document.getElementById("md-url");
    const url = input.value.trim();
    if (!url) return "local";
    const u = { name: url.split("/").pop().slice(0, 60) || url, pct: 0, error: "" };
    uploads.push(u);
    input.value = "";
    api("POST", "/api/media/import", { url, name: "" }).then((r) => {
      if (r.ok) { uploads = uploads.filter((x) => x !== u); if (form?.kind === "slides") picks.push(r.body.id); } else u.error = r.detail;
      refreshLibrary();
    });
    return "local";
  }
  if (act === "md-rename") {
    const name = prompt("New name", el.dataset.name);
    if (name?.trim()) api("PUT", `/api/media/${encodeURIComponent(el.dataset.id)}`, { name: name.trim() }).then(refreshLibrary);
    return "local";
  }
  if (act === "md-delete") {
    const m = lib.items.find((x) => x.id === el.dataset.id);
    const used = m?.used_by?.length ? ` It's in ${m.used_by.join(", ")}; those slideshows will skip it.` : "";
    if (!confirm(`Delete “${m?.name || el.dataset.id}”?${used}`)) return "local";
    fetch(`/api/media/${encodeURIComponent(el.dataset.id)}`, { method: "DELETE" }).then(refreshLibrary);
    return "local";
  }
  // Screens
  if (act === "box-create") {
    const role = document.getElementById("box-role").value;
    const body = { role, room: document.getElementById("box-room").value, name: document.getElementById("box-name").value.trim(),
      show: role === "display" ? document.getElementById("box-show").value : "" };
    if (!body.name) { boxNote = "Give the box a name, e.g. Atrium room computer."; return "local"; }
    fetch("/api/appliances", json("POST", body)).then(async (r) => {
      const out = await r.json();
      boxNote = r.ok ? `Created ${out.name}. Setup code ${codeStr(out.code)}: flash link and login are in Linux boxes below.` : out.detail || "Couldn't create it.";
      refreshBoxes(); refreshLive();
    }).catch(() => { boxNote = "Network error"; S.poke(); });
    return "local";
  }
  if (act === "wifi-show") { wifiShown = !wifiShown; return "local"; }
  if (act === "ssh-save") {
    fetch("/api/appliances/ssh-keys", json("PUT", { keys: document.getElementById("ssh-keys").value })).then(async (r) => {
      const out = await r.json();
      if (r.ok) { sshKeys = out; sshNote = `Saved ${out.keys.length} key${out.keys.length === 1 ? "" : "s"}. Boxes pick them up within 10 minutes.`; }
      else sshNote = out.detail || "Couldn't save.";
      S.poke();
    }).catch(() => { sshNote = "Network error"; S.poke(); });
    return "local";
  }
  if (act === "wifi-save") {
    const networks = [0, 1, 2, 3].map((i) => ({ ssid: document.getElementById(`wifi-ssid-${i}`).value.trim(),
      psk: document.getElementById(`wifi-psk-${i}`).value })).filter((n) => n.ssid);
    fetch("/api/appliances/wifi", json("PUT", { networks })).then(async (r) => {
      const out = await r.json();
      if (r.ok) { wifiNets = out.networks; wifiNote = `Saved: ${out.networks.map((n) => n.ssid).join(", then ") || "no Wi-Fi"}. New boxes get it when they're set up.`; }
      else wifiNote = out.detail || "Couldn't save.";
      S.poke();
    }).catch(() => { wifiNote = "Network error"; S.poke(); });
    return "local";
  }
  if (act === "box-reveal") { const id = el.dataset.id; revealed.has(id) ? revealed.delete(id) : revealed.add(id); return "local"; }
  if (act === "box-renew") {
    if (!confirm("Make a new setup code? The box's current device key stops working until it's set up again with the new code.")) return "local";
    fetch(`/api/appliances/${encodeURIComponent(el.dataset.id)}/renew`, { method: "POST" }).then(refreshBoxes);
    return "local";
  }
  if (act === "box-delete") {
    if (!confirm("Delete this box from Xos? Its setup code, login record and screen entry go away.")) return "local";
    fetch(`/api/appliances/${encodeURIComponent(el.dataset.id)}`, { method: "DELETE" }).then(() => { refreshBoxes(); refreshLive(); });
    return "local";
  }
  if (act === "add-toggle") { if (addOpen) { addOpen = false; addCode = ""; } else openAdd(); return "local"; }
  if (act === "add-pick") { addCode = el.dataset.code; addNote = ""; setTimeout(() => document.getElementById("add-name")?.focus(), 50); return "local"; }
  if (act === "ip-setup") {
    const ip = document.getElementById("ip-addr").value.trim();
    const name = document.getElementById("ip-name").value.trim();
    const route = document.getElementById("ip-route").value;
    if (!ip) { ipNote = "Enter the screen's IP address (on Fire TV: Settings → My Fire TV → About → Network)."; return "local"; }
    ipNote = "";
    fetch("/api/provision", json("POST", { ip, name, route }))
      .then(async (r) => {
        if (!r.ok) ipNote = (await r.json()).detail || "Couldn't start.";
        else { document.getElementById("ip-addr").value = ""; document.getElementById("ip-name").value = ""; }
        refreshIpJobs();
      }).catch(() => { ipNote = "Network error"; S.poke(); });
    return "local";
  }
  if (act === "dc-room") {
    const room = document.getElementById("dc-room").value;
    fetch(`/api/devices/${encodeURIComponent(selected)}`, json("PUT", { room })).then(refreshLive);
    return "local";
  }
  if (act === "dc-stale") { showOffline = !showOffline; return "local"; }
  if (act === "dc-reset-access") {
    remoteNote = "Restoring remote access…";
    fetch(`/api/devices/${encodeURIComponent(selected)}/remote-access/reset`, json("POST", {}))
      .then(async (r) => { const body = await r.json(); remoteNote = r.ok ? "Access reset. The tablet will reload and enroll again." : body.detail || "Couldn't reset access."; refreshLive(); })
      .catch(() => { remoteNote = "Network error. Try again."; S.poke(); });
    return "local";
  }
  if (act === "dc-forget") {
    const d = live.list.find((x) => x.id === el.dataset.id);
    if (!confirm(`Forget “${devName(d)}”? If it is still running it will reappear on its next check-in.`)) return "local";
    if (selected === el.dataset.id) selected = null;
    fetch(`/api/devices/${encodeURIComponent(el.dataset.id)}`, { method: "DELETE" }).then(refreshLive);
    return "local";
  }
  if (act === "add-claim") {
    const code = document.getElementById("add-code").value.replace(/\D/g, "");
    const name = document.getElementById("add-name").value.trim();
    const route = document.getElementById("add-route").value;
    const room = document.getElementById("add-room").value;
    const use = roomUse() ? "room" : "screen";
    if (code.length !== 6) { addNote = "Enter the 6-digit code shown on the screen, or pick it under Waiting to be added."; S.poke(); return "local"; }
    if (use === "room" && !room) { addNote = "Pick the room this computer runs meetings for."; S.poke(); return "local"; }
    fetch("/api/devices/claim", json("POST", { code, name, route, room, use }))
      .then(async (r) => {
        const body = await r.json();
        addNote = !r.ok ? body.detail || "Couldn't add the screen."
          : use === "room" ? `${body.name} is being set up. The box restarts as the room's meeting computer in a few minutes; it appears under Room computers when it's ready.`
          : `Added ${body.name || body.id}. It will switch to the chosen screen in a moment.`;
        // A meeting computer isn't online until it has restarted as one: keep the note in view.
        if (r.ok && use === "room") { addCode = ""; addUse = "screen"; addForm = { name: "", room: "", route: "page/welcome" }; refreshWaiting(); }
        else if (r.ok) { selected = body.id; history = []; addOpen = false; addCode = ""; addUse = "screen"; addForm = { name: "", room: "", route: "page/welcome" }; }
        refreshLive();
      }).catch(() => { addNote = "Network error"; S.poke(); });
    return "local";
  }
  if (act === "dc-switch") {
    switchNote = "Starting…";
    S.poke();
    fetch(`/api/devices/${encodeURIComponent(selected)}/commands`, json("POST", { type: "switch_input", args: {} }))
      .then(async (r) => { switchNote = r.ok ? "" : (await r.json()).detail || "Couldn't start it."; refreshLive(); })
      .catch(() => { switchNote = "Network error. Try again."; S.poke(); });
    return "local";
  }
  if (act === "dc-open") { remoteNote = ""; switchNote = ""; selected = el.dataset.id; history = []; refreshLive(); return "local"; }
  if (act === "dc-close") { selected = null; return "local"; }
  if (act === "dc-share-new") {
    if (!confirm("Make a new share link? The current link and QR code stop working.")) return "local";
    fetch(`/api/devices/${encodeURIComponent(selected)}/share`, json("PUT", { new_code: true })).then(refreshLive);
    return "local";
  }
  if (act === "dc-share-stop") {
    fetch(`/api/share/${encodeURIComponent(el.dataset.code)}/stop`, json("POST", {})).then(refreshLive);
    return "local";
  }
  if (act === "dc-cmd") { sendCommand(selected, el.dataset.type, JSON.parse(el.dataset.args || "{}")); return "local"; }
  if (act === "dc-rename") {
    const name = document.getElementById("dc-name").value.trim();
    fetch(`/api/devices/${encodeURIComponent(selected)}`, json("PUT", { name })).then(refreshLive);
    return "local";
  }
  if (act === "dc-route") { sendCommand(selected, "route", { route: document.getElementById("dc-route").value }); return "local"; }
  if (act === "dc-msg") {
    const input = document.getElementById("dc-msg");
    if (input.value.trim()) sendCommand(selected, "message", { text: input.value.trim(), seconds: 15 });
    input.value = "";
    return "local";
  }
  if (act === "dc-return") { sendCommand(selected, "return_min", { minutes: Number(document.getElementById("dc-return").value) }); return "local"; }
  if (act === "dc-bulk") {
    const targets = live.list.filter((d) => d.online && DISPLAY_ROLES.has(d.role || ""));
    if (el.dataset.type === "message") {
      const input = document.getElementById("dc-bulk-msg");
      const text = input.value.trim();
      if (!text) return "local";
      targets.forEach((d) => sendCommand(d.id, "message", { text, seconds: 30 }));
      input.value = "";
    } else {
      if (!confirm(`Reload ${targets.length} screen${targets.length === 1 ? "" : "s"}?`)) return "local";
      targets.forEach((d) => sendCommand(d.id, "reload"));
    }
    bulkNote = `Sent to ${targets.length} screen${targets.length === 1 ? "" : "s"}.`;
    return "local";
  }
  // Rooms
  if (act === "room-add") {
    const name = document.getElementById("room-new").value.trim();
    if (!name) { roomNote = "Enter a room name."; return "local"; }
    roomCall("POST", "/api/rooms", { name, site: document.getElementById("room-new-site").value }, `Added ${name}.`);
    return "local";
  }
  if (act === "room-save") {
    const id = el.dataset.id;
    const name = document.getElementById(`rn-${id}`).value.trim();
    roomCall("PUT", `/api/rooms/${encodeURIComponent(id)}`, { name, site: document.getElementById(`rs-${id}`).value }, `Saved ${name}.`);
    return "local";
  }
  if (act === "room-delete") {
    const r = S.room(el.dataset.id);
    if (!confirm(`Delete ${r?.name || el.dataset.id}? Its screens keep running but lose their room and Zoom link.`)) return "local";
    roomCall("DELETE", `/api/rooms/${encodeURIComponent(el.dataset.id)}`, null, `Deleted ${r?.name || el.dataset.id}.`);
    return "local";
  }
  return false;
}
