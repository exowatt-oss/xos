// Xos web app: hash router (#/view/arg/sub), event delegation, and the kiosk runtime.
// Admin lives at #/admin/<tab>. Screens open their view with ?kiosk=1, which also registers them
// with the server (Admin → Screens) and listens for commands. Without ?kiosk=1 a screen view is
// just a preview: full-screen, but it doesn't register.

import * as S from "./state.js";
import { watchWhiteboard } from "./whiteboard.js";
import { esc, clockStr, dur } from "./ui.js";
import { renderRoomTV, renderPanel, renderSignage, renderWall } from "./views/screens.js";
import { renderAdmin, handleAdmin, handleAdminInput } from "./views/admin.js";
import { renderPage, renderMedia, loadPages, controlPage, playlistTick, stepPlaylist } from "./views/page.js";
import { renderRemote, renderRoomRemote, handleRemote } from "./views/remote.js";
import { renderCamera, renderFeed } from "./views/camera.js";
import { renderShare, showShare, stopShare } from "./views/share.js";
import { renderControl, handleControl } from "./views/control.js";
import { keepRoomRemoteAwake, screenWakeState } from "./keep-awake.js";
import { saveDeviceKey, prepareDeviceKey, whoAmI } from "./api.js";
import { syncPreviewBanner } from "./views/people.js";
const root = document.getElementById("root");
const params = new URLSearchParams(location.search);
const kiosk = params.has("kiosk");
keepRoomRemoteAwake();
// A Pi screen run from the server (?restore=1) reopens the last screen Admin chose after a reboot;
// the Fire TV app saves its own.
if (kiosk && params.has("restore") && localStorage.getItem("xos-route")) {
  history.replaceState(null, "", `#/${localStorage.getItem("xos-route")}`);
}
let lastHtml = "";

function roomScene() {
  const stage = root.querySelector('.rm-meeting-stage');
  if (stage) return {element: stage, key: `meeting|${location.hash}|${root.querySelector('.rm-home')?.dataset.provider}|${stage.querySelector('h1')?.textContent}`};
  const idle = root.querySelector('.idle-main');
  return idle ? {element: idle, key: `idle|${location.hash}|${idle.querySelector('.idle-title')?.textContent}`} : null;
}

function paint(html) {
  if (html === lastHtml) return;
  const previousScene = roomScene()?.key;
  lastHtml = html;
  // Scrolled panels (the remote's right column, Admin's control drawer) and opened <details>
  // keep their state across repaints.
  const scrolled = ".rm-right, .drawer, .md-pick, .rm-controls-list, .rm-controls-scroll, .rm-home-body";
  const kept = [...root.querySelectorAll(scrolled)].map((el) => el.scrollTop);
  const open = new Set([...root.querySelectorAll("details[open]")].map((el) => el.className));
  root.innerHTML = html;
  const scene = roomScene();
  if (scene && scene.key !== previousScene) scene.element.classList.add('room-arrive');
  if (kiosk) root.querySelectorAll("iframe.page-frame").forEach(revealWhenLoaded);
  root.querySelectorAll(scrolled).forEach((el, i) => { el.scrollTop = kept[i] || 0; });
  root.querySelectorAll("details").forEach((el) => { if (open.has(el.className)) el.open = true; });
}

// A new frame stays transparent (the black screen behind it) until its page has loaded, then fades
// in, so a TV never shows a white frame or a half-drawn page. A slow page shows after 5 s anyway.
function revealWhenLoaded(frame) {
  frame.classList.add("loading");
  const show = () => frame.classList.remove("loading");
  frame.addEventListener("load", show, { once: true });
  setTimeout(show, 5000);
}

// Switching what a screen shows fades the new view in from black (not on ordinary repaints).
let shownRoute = null;
function fadeOnSwitch() {
  const route = location.hash;
  if (!kiosk || route === shownRoute) return;
  const first = shownRoute === null;
  shownRoute = route;
  if (first && !document.documentElement.classList.contains("kiosk-boot")) return;
  root.classList.remove("xos-in");
  void root.offsetWidth; // restart the animation
  root.classList.add("xos-in");
}

function parse() {
  const [, view = "", arg = "", sub = ""] = (location.hash || "#/").split("/");
  return { view, arg, sub };
}

const SCREENS = {
  whiteboard: () => '<div class="wb-loading">Opening canvas…</div>',
  tv: (arg) => renderRoomTV(arg),
  tablet: (arg) => renderRoomRemote(arg),
  panel: (arg) => renderPanel(arg),
  signage: (arg) => renderSignage(arg),
  wall: () => renderWall(),
  page: (arg) => renderPage(arg || "welcome"),
  media: (arg) => renderMedia(arg),
  setup: () => renderSetup(),
  remote: (arg) => renderRemote(arg),
  camera: () => renderCamera(deviceId()),
  feed: (arg) => renderFeed(arg),
  share: (arg) => renderShare(arg),
  control: (arg) => renderControl(arg),
};

function syncWhiteboard() {
  const { view, arg } = parse();
  const room = ["tv", "tablet"].includes(view) ? arg : ["remote", "control"].includes(view) ? root.querySelector("[data-whiteboard-room]")?.dataset.whiteboardRoom : ["page", "media"].includes(view) ? S.self()?.room : "";
  watchWhiteboard(view === "whiteboard" ? arg.split("?")[0] : room || "", ["tablet", "remote", "control", "whiteboard"].includes(view), {standalone:view === "whiteboard"});
}

// Previewing as a role or person (Admin → People & roles): a banner with Exit preview on every
// page a person uses. Kiosks never sign in, so they never preview.
const PERSON_VIEWS = new Set(["", "admin", "control", "whiteboard"]);
function previewBanner(view) {
  if (kiosk || !S.isHub() || !PERSON_VIEWS.has(view)) { syncPreviewBanner(null); return; }
  whoAmI().then(syncPreviewBanner);
}

function render() {
  fadeOnSwitch();
  const { view, arg, sub } = parse();
  previewBanner(view);
  if (kiosk || (view && view !== "admin")) {
    const body = SCREENS[view];
    // The share page is an ordinary scrolling page on a phone or laptop, not a full-screen view.
    // So is #/control's list of screens; its remote (#/control/<id>) is full-screen.
    document.body.className = view === "share" || (view === "control" && !arg) ? "sharing-page" : "kiosk";
    // A kiosk with no screen chosen yet shows its pairing code (Admin → Screens → Add a screen).
    paint(`<div class="screen kiosk-screen kiosk-${body ? view : "setup"}">${(body || renderSetup)(arg)}</div>`);
    syncWhiteboard();
    return;
  }
  document.body.className = "admin";
  paint(renderAdmin(arg || "", sub || ""));
  syncWhiteboard();
}

function rerender() {
  const y = window.scrollY;
  render();
  window.scrollTo(0, y);
}

root.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el || el.tagName === "SELECT" || el.tagName === "INPUT") return;
  for (const h of [handleAdmin, handleRemote, handleControl]) {
    const r = h(el.dataset.act, el);
    if (r) { rerender(); return; }
  }
});

root.addEventListener("input", (e) => handleAdminInput(e.target));
// The remote's message box is a form so Enter sends it (the browser clicks its button).
root.addEventListener("submit", (e) => {
  if (e.target.matches("[data-guide-form]")) {
    e.preventDefault();
    handleAdmin("guide-connect", e.submitter || e.target);
  } else if (e.target.closest(".rm")) e.preventDefault();
});

S.subscribe(rerender);
window.addEventListener("hashchange", () => { render(); window.scrollTo(0, 0); });

// Every second: refresh live text in place, and re-render when the minute or a playlist item
// changes.
let tickKey = "";
setInterval(() => {
  root.querySelectorAll("[data-clock]").forEach((el) => { el.textContent = clockStr(); });
  root.querySelectorAll("[data-since]").forEach((el) => { el.textContent = dur(Date.now() - Number(el.dataset.since)); });
  const { view: v, arg: a } = parse();
  const key = `${new Date().getMinutes()}|${v === "page" ? playlistTick(a) : ""}`;
  if (key !== tickKey) { tickKey = key; rerender(); }
}, 1000);

// Kiosk screens check in with the Xos server so Admin → Screens shows what is running where.
// The Fire TV and iOS apps pass ?device=<id>&client=<app>-<version>; browsers get a stored id.
const ROLE_LABELS = { tv: "Room TV", tablet: "Table remote", panel: "Door panel", signage: "Lobby signage", wall: "Dashboard wall", page: "Page", media: "Photo / video", remote: "Room remote", camera: "Live camera", feed: "Live feed" };
function deviceId() {
  if (params.get("device")) return params.get("device");
  let id = localStorage.getItem("xos-device-id");
  if (!id) { id = "web-" + Math.random().toString(36).slice(2, 12); localStorage.setItem("xos-device-id", id); }
  return id;
}
// The Fire TV app exposes window.XosNative (model, LAN address, display on/off, screenshots).
const native = window.XosNative || null;
let deviceName = ""; // set in Admin → Screens, e.g. "Atrium room"; returned by each heartbeat
function nativeInfo() {
  try { return native ? JSON.parse(native.info()) : null; } catch (_) { return null; }
}

function heartbeat() {
  if (!kiosk || !S.isHub()) return;
  const { view, arg, sub } = parse();
  const ua = navigator.userAgent;
  const tv = ua.match(/XosTV\/([\d.]+)/);
  const client = params.get("client") || (tv ? `firetv-${tv[1]}` : "browser");
  const label = `${ROLE_LABELS[view] || view}${arg ? ` · ${arg}` : ""}${sub ? ` (display ${sub})` : ""}`;
  const info = nativeInfo();
  const enrollment = ["tablet", "remote"].includes(view) ? { enrollment_key: prepareDeviceKey(deviceId()), wake_lock: screenWakeState() } : {};
  fetch("/api/devices/heartbeat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...enrollment, id: deviceId(), client, role: view, arg, label, screen: `${screen.width}x${screen.height}`, user_agent: ua.slice(0, 300), airplay: params.get("airplay") || "", ...(info ? { native: info } : {}) }),
  }).then((r) => r.json()).then((d) => { if (d.key) saveDeviceKey(d.id, d.key); deviceName = d.name || ""; S.setMe(d); badge(); nameTag(); checkStream(d.stream); }).catch(() => {});
}

// The share badge: this screen's share link and QR code in the bottom-right corner, when Admin
// turns it on for the screen (Screens → Control → Share). It sits outside #root so repaints
// don't touch it, and under the share overlay. Room TVs already show the link in their footer.
function badge() {
  const me = S.self();
  const on = kiosk && me?.share_badge && me.share_code && !me.share_off && parse().view !== "tv";
  let el = document.getElementById("xos-badge");
  if (!on) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement("div"); el.id = "xos-badge"; document.body.appendChild(el); }
  const html = `<img src="/api/share/${esc(me.share_code)}/qr.svg" alt="" />
    <div><div class="xb-k">Share to this screen</div><div class="xb-link">${esc(location.host)}/s/${esc(me.share_code)}</div>
    ${me.airplay ? `<div class="xb-air">or AirPlay to <b>${esc(me.airplay)}</b></div>` : ""}</div>`;
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
}
window.addEventListener("hashchange", () => { badge(); nameTag(); });

// The screen's name (Admin → Screens) and "Xos", in small faint text at the bottom right, so a technician
// standing in front of it can tell which screen it is without anyone noticing it otherwise.
// Not on the setup screen (it shows its id already), the interactive remotes or the share page.
const NO_TAG = new Set(["setup", "share", "remote", "tablet", ""]);
function nameTag() {
  const { view } = parse();
  const on = kiosk && SCREENS[view] && !NO_TAG.has(view);
  let el = document.getElementById("xos-name");
  if (!on) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement("div"); el.id = "xos-name"; document.body.appendChild(el); }
  const html = `${esc(deviceName || deviceId())}<b>Xos</b>`;
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
}

// ---------- remote control from Admin → Screens ----------

function overlay(html, seconds) {
  let el = document.getElementById("xos-overlay");
  if (!el) { el = document.createElement("div"); el.id = "xos-overlay"; document.body.appendChild(el); }
  el.innerHTML = html;
  el.className = "show";
  clearTimeout(overlay.t);
  overlay.t = setTimeout(() => { el.className = ""; }, seconds * 1000);
}

function ack(cmd, ok, result) {
  fetch(`/api/devices/${encodeURIComponent(deviceId())}/commands/${cmd.id}/ack`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ok, result }),
  }).catch(() => {});
}

const COMMANDS = {
  route({ route }) {
    if (!/^[a-z]+(\/[a-z0-9-]+){0,2}$/.test(route || "")) throw new Error(`bad route ${route}`);
    if (route.startsWith("page/")) loadPages();
    // Xos TV 0.6.0 saves the screen without reloading, so it switches in place (no flash); older
    // builds save it by reloading. Either way the stick reopens on it after a reboot.
    if (native && !native.saveRoute) return native.setRoute(`#/${route}`);
    if (native) {
      const saved = native.saveRoute(`#/${route}`);
      if (String(saved).startsWith("error")) return saved;
    }
    localStorage.setItem("xos-route", route);
    location.hash = `#/${route}`;
    return "ok";
  },
  reload() { setTimeout(() => (native ? native.reload() : location.reload()), 300); return "reloading"; },
  page_scroll({ dir }) { return controlPage({ xos: "scroll", dir }); },
  page_key({ key }) {
    const { arg } = parse();
    if (/Arrow(Right|Left)/.test(key) && stepPlaylist(arg, key === "ArrowRight" ? 1 : -1)) return "next item";
    return controlPage({ xos: "key", key });
  },
  identify() {
    const { view, arg } = parse();
    const info = nativeInfo();
    const showing = `${ROLE_LABELS[view] || view}${arg ? ` · ${arg}` : ""}`;
    overlay(`<b>${esc(deviceName || showing)}</b><span>${deviceName ? `${esc(showing)} · ` : ""}${esc(deviceId())}${info?.ip ? ` · ${esc(info.ip)}` : ""}${info?.model ? ` · ${esc(info.model)}` : ""}</span>`, 10);
    return "shown";
  },
  message({ text = "", seconds = 15 }) { overlay(`<b>${esc(String(text).slice(0, 200))}</b>`, Math.min(300, Number(seconds) || 15)); return "shown"; },
  screenshot({ capture_id = "" } = {}) {
    if (!native) throw new Error("screenshots need the Xos TV app");
    return native.screenshot(`${location.origin}/api/devices/${encodeURIComponent(deviceId())}/screenshot${capture_id ? `?capture_id=${encodeURIComponent(capture_id)}` : ""}`);
  },
  display_off() {
    if (!native) throw new Error("display control needs the Xos TV app");
    // Before 0.6.0 a sleeping stick dropped off Xos, so nothing could turn it back on remotely.
    if (!nativeInfo()?.wakeable) throw new Error("this Xos TV couldn't be turned back on from Xos once off. Update it to 0.6.0 (Admin → Add a screen → by IP) first");
    return native.displayOff();
  },
  display_on() { if (!native) throw new Error("display control needs the Xos TV app"); return native.displayOn(); },
  share(args) { return showShare(args); },
  share_stop({ session }) { return stopShare(session); },
  refresh() { return "ok"; }, // the heartbeat after every command picks up new settings
  return_min({ minutes }) { if (!native) throw new Error("needs the Xos TV app"); return native.setReturnMinutes(Number(minutes) || 0); },
};

let commandStream = null;
function listenForCommands() {
  // Reopen if the stream gave up (EventSource stops for good after an HTTP error, e.g. a 502 mid-deploy).
  if (!kiosk || !S.isHub() || (commandStream && commandStream.readyState !== EventSource.CLOSED)) return;
  commandStream = new EventSource(`/api/devices/${encodeURIComponent(deviceId())}/stream`);
  commandStream.onerror = () => { if (commandStream.readyState === EventSource.CLOSED) setTimeout(listenForCommands, 5000); };
  commandStream.onmessage = (e) => {
    const cmd = JSON.parse(e.data);
    try {
      const result = COMMANDS[cmd.type](cmd.args || {});
      ack(cmd, !String(result).startsWith("error"), result);
    } catch (err) {
      ack(cmd, false, String(err.message || err));
    }
    setTimeout(heartbeat, 1500); // report the new screen / display state promptly
  };
}

// A stream a NAT or proxy dropped silently still looks open here, so it is never reopened and
// commands stop arriving while check-ins carry on. The server says whether it holds our stream;
// two check-ins in a row without it (one could race a reconnect) and we open a fresh one.
let streamMisses = 0;
function checkStream(held) {
  if (held !== false || !commandStream || commandStream.readyState === EventSource.CLOSED) { streamMisses = 0; return; }
  if (++streamMisses < 2) return;
  streamMisses = 0;
  commandStream.close();
  commandStream = null;
  listenForCommands();
}

if (kiosk) {
  setTimeout(() => { heartbeat(); listenForCommands(); }, 2000);
  setInterval(() => { heartbeat(); listenForCommands(); }, 30000);
  window.addEventListener("hashchange", heartbeat);
}

// On a page view, the remote's arrow keys step through the page (slide decks) instead of the frame.
if (kiosk) {
  document.addEventListener("keydown", (e) => {
    if (!["page", "media", "signage"].includes(parse().view) || !/^Arrow(Left|Right|Up|Down)$/.test(e.key)) return;
    e.preventDefault();
    try { COMMANDS.page_key({ key: e.key }); } catch (_) { /* no page frame */ }
  });
}

// ---------- setup screen: how a new screen gets added ----------
let pairCode = "";
let pairAt = 0;
function renderSetup() {
  if (S.isHub() && (!pairCode || Date.now() - pairAt > 30 * 60 * 1000)) {
    pairAt = Date.now();
    fetch("/api/devices/pair", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: deviceId() }) })
      .then((r) => r.json()).then((d) => { pairCode = d.code; S.poke(); }).catch(() => { pairAt = 0; });
  }
  // One thing to do: enter the code in Admin (or scan it). Nothing else competes for attention.
  const d = (pairCode || "······").split("");
  const tile = (c) => `<span>${esc(c)}</span>`;
  return `<div class="pair">
    <header class="pair-top"><img src="../static/img/logo-white.svg" alt="Xos" class="pair-logo" /><span class="pair-xos">Xos</span></header>
    <main class="pair-main">
      <section class="pair-left">
        <div class="pair-k">Add this screen</div>
        <div class="pair-code" aria-label="Pairing code ${esc(pairCode || "")}">${d.slice(0, 3).map(tile).join("")}<i></i>${d.slice(3).map(tile).join("")}</div>
        <p class="pair-how">In <b>Xos Admin → Screens → + Add a screen</b>, enter this code.</p>
        <p class="pair-sub">Name it, pick its room and what it shows. It switches over by itself.</p>
        <p class="pair-url">${esc(location.host)}</p>
      </section>
      ${pairCode ? `<figure class="pair-qr"><img src="/api/pair/qr.svg?code=${esc(pairCode)}" alt="" /><figcaption>Or scan to add it from your phone</figcaption></figure>` : ""}
    </main>
    <footer class="pair-foot"><span class="pair-dot"></span>Waiting to be added<span class="pair-id">${esc(deviceId())}${deviceName ? ` · ${esc(deviceName)}` : ""}</span></footer>
  </div>`;
}

// First paint last, once every module-level value above exists.
render();
