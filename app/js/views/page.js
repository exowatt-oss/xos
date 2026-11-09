// Page view (#/page/<id>): a page shown full-screen on a TV. Pages are managed in Admin → Content.
// Slideshows play in the Xos player (static/player/), announcements and uploaded HTML are served
// by Xos in a sandbox and can be scrolled / stepped through from Admin, links are shown as-is.
// Media view (#/media/<id>): one photo or video from the library, full-screen.

import * as S from "../state.js";
import { esc } from "../ui.js";

let pages = null;
let loading = false;

export async function loadPages() {
  if (loading) return;
  loading = true;
  try {
    pages = await (await fetch("/api/pages", { cache: "no-store" })).json();
  } catch (_) {
    pages = pages || [];
  }
  loading = false;
  S.poke();
}

export const getPages = () => pages || [];

// Re-uploading a page reaches every TV showing it within a minute.
setInterval(() => { if (pages !== null) loadPages(); }, 60000);

const PLAYER = "/static/player/index.html";

export function pageSrc(p) {
  const v = Math.round(p.updated || 0);
  if (p.kind === "slides") return `${PLAYER}?page=${encodeURIComponent(p.id)}&v=${v}`;
  if (p.kind === "html" || p.kind === "announcement") return `/pages/${encodeURIComponent(p.id)}/?v=${v}`;
  return p.url;
}

export const mediaSrc = (id) => `${PLAYER}?media=${encodeURIComponent(id)}`;

// Pages are laid out as on a 1080p screen and scaled to fit. The Fire TV's WebView is only
// 960×540 CSS pixels, where sites switch to their narrow layout; this keeps them looking as they do
// in a desktop browser. CSS zoom, not transform: scale(): Android WebView doesn't draw a video
// that sits under a CSS mask inside a transformed frame (the Sun in Sun Room stayed black).
const ZOOM = typeof CSS !== "undefined" && CSS.supports?.("zoom", "0.5");
function fit() {
  const s = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
  if (!(s < 0.98 && window.innerWidth > window.innerHeight)) return "";
  return ZOOM ? `style="width:1920px;height:1080px;zoom:${s.toFixed(4)}"`
    : `style="width:1920px;height:1080px;transform:scale(${s.toFixed(4)});transform-origin:0 0"`;
}

function frame(p) {
  const sandbox = p.kind === "html" || p.kind === "announcement" ? `sandbox="allow-scripts allow-forms allow-popups"` : "";
  // The player and built-in welcome page fit any screen size; other pages get the 1080p fit.
  const own = p.kind === "slides" || p.kind === "announcement" || p.builtin;
  return `<iframe class="page-frame" title="${esc(p.name)}" src="${esc(pageSrc(p))}" ${sandbox} ${own ? "" : fit()} allow="autoplay; fullscreen" referrerpolicy="no-referrer"></iframe>`;
}

export function renderMedia(id) {
  return `<iframe class="page-frame" title="Photo or video" src="${esc(mediaSrc(id))}" allow="autoplay; fullscreen"></iframe>`;
}

// Playlists rotate on wall-clock time, so every screen showing the same playlist stays in step.
let skew = 0; // seconds added by Next / Previous on this screen
function playlistNow(p) {
  const items = (p.items || []).filter((i) => pages.some((x) => x.id === i.page && x.kind !== "playlist"));
  const total = items.reduce((n, i) => n + i.seconds, 0);
  if (!total) return null;
  let t = ((Date.now() / 1000 + skew) % total + total) % total;
  for (let k = 0; k < items.length; k++) {
    if (t < items[k].seconds) return { items, k, left: items[k].seconds - t };
    t -= items[k].seconds;
  }
  return { items, k: 0, left: items[0].seconds };
}

/** Changes whenever a playlist on screen moves to its next item (main.js re-renders on change). */
export function playlistTick(id) {
  const p = pages?.find((x) => x.id === id);
  return p?.kind === "playlist" ? String(playlistNow(p)?.k) : "";
}

/** Next / Previous on a playlist: jump to the neighbouring item. */
export function stepPlaylist(id, dir) {
  const p = pages?.find((x) => x.id === id);
  const now = p?.kind === "playlist" && playlistNow(p);
  if (!now) return false;
  const prev = now.items[(now.k - 1 + now.items.length) % now.items.length].seconds;
  const cur = now.items[now.k].seconds;
  skew += dir > 0 ? now.left + 0.01 : -(cur - now.left) - prev + 0.01;
  S.poke();
  return true;
}

export function renderPage(id) {
  if (pages === null) { loadPages(); return `<div class="page-msg">Loading…</div>`; }
  const p = pages.find((x) => x.id === id);
  if (!p) return `<div class="page-msg">No page “${esc(id)}”. Add it in Xos Admin → Content.</div>`;
  if (p.kind !== "playlist") return frame(p);
  const now = playlistNow(p);
  if (!now) return `<div class="page-msg">Playlist “${esc(p.name)}” is empty.</div>`;
  return frame(pages.find((x) => x.id === now.items[now.k].page));
}

/** Scroll / key commands from Admin, passed into the page (works for uploaded HTML). */
export function controlPage(message) {
  const frame = document.querySelector(".page-frame");
  if (!frame) throw new Error("this screen is not showing a page");
  const win = frame.contentWindow;
  try {
    // Pages on this Xos server (e.g. "/" as a link) are same-origin: drive them directly.
    const doc = win.document;
    if (message.xos === "scroll") {
      const h = win.innerHeight * 0.8;
      const top = { up: win.scrollY - h, down: win.scrollY + h, top: 0, bottom: doc.body.scrollHeight }[message.dir];
      win.scrollTo({ top, behavior: "smooth" });
    } else if (message.xos === "key") {
      const opts = { key: message.key, code: message.key, bubbles: true, cancelable: true };
      (doc.activeElement || doc.body).dispatchEvent(new win.KeyboardEvent("keydown", opts));
    }
    return "ok";
  } catch (_) {
    // Sandboxed upload (control script listens) or another site (ignored by it).
    win.postMessage({ ...message }, "*");
    return "sent";
  }
}
