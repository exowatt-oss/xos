// #/control: anyone in your organization takes one screen that has sharing turned on, while no one else is
// using it, and drives it with the room remote (remote.js, one-screen mode). A screen operator
// also sees screens with sharing off and opens a free one without the button. The hold is the
// server's (src/app/hub.py → controls): it lapses after a while with no commands, or on Release.

import * as S from "../state.js";
import { esc } from "../ui.js";
import { userFetch, whoAmI, signInUrl, SignedOut } from "../api.js";
import { renderSolo } from "./remote.js";
import { loadPages } from "./page.js";

let list = null; // {me, screens, idle_s} from /u/api/control
let err = "";
let busy = "";
let timer = 0;
let me = null;

const onControl = () => location.hash.startsWith("#/control");
const can = (perm) => Boolean(list?.me?.perms?.includes(perm));
// A preview started or ended (people.js): show this page as the new perspective.
window.addEventListener("xos-me", (e) => { me = e.detail; list = null; if (me.email && onControl()) { refresh(); keepFresh(); } });

async function refresh() {
  try {
    const res = await userFetch("/api/control", { cache: "no-store" });
    if (res.status === 401) { me = { email: "" }; list = null; }
    else if (res.ok) { list = await res.json(); err = ""; }
  } catch (e) {
    if (e instanceof SignedOut) me = { email: "" };
    else err = "Can't reach Xos right now.";
  }
  S.poke();
}

function keepFresh() {
  clearTimeout(timer);
  if (!onControl()) return;
  timer = setTimeout(async () => { await refresh(); keepFresh(); }, 5000);
}
window.addEventListener("hashchange", () => { if (onControl()) { refresh(); keepFresh(); } });

const page = (inner) => `<div class="share ctl"><header class="share-top"><img src="../static/img/logo-white.svg" alt="Xos" /><span>Xos</span></header>${inner}</div>`;

export function renderControl(id) {
  if (!S.isHub()) return page(`<h1>Control a screen</h1><p>Open Xos from your server address.</p>`);
  if (!me) {
    whoAmI().then((m) => { me = m; if (m.email) { refresh(); keepFresh(); loadPages(); } S.poke(); });
    return page(`<p>Checking who you are…</p>`);
  }
  if (!me.email) {
    return page(`<h1>Control a screen</h1>
      <p>Sign in with your organization account to take control of a screen.</p>
      <a class="share-btn share-primary" href="${signInUrl()}">Sign in with Google</a>`);
  }
  if (!list) return page(`<p>${err ? esc(err) : "Loading screens…"}</p>`);
  const screen = id && list.screens.find((s) => s.id === id);
  if (screen && (screen.mine || (can("screens.operate") && !screen.controlled_by && !list.me.preview))) {
    if (!screen.mine) take(screen.id); // operators skip the button
    const bar = `<div class="ctl-bar" role="status"><span>You're controlling <b>${esc(screen.name)}</b>. It frees itself after ${Math.round(list.idle_s / 60)} minutes without a tap.</span>
      <a class="rm-btn ctl-back" href="#/control">All screens</a>
      <button class="rm-btn rm-off" data-act="ctl-release" data-dev="${esc(screen.id)}">${busy === screen.id ? "Releasing…" : "Release"}</button></div>`;
    return renderSolo(screen.id, bar, can("screens.operate") && !list.me.preview);
  }
  if (id && !screen) err = "That screen isn't available to control.";
  if (screen && screen.controlled_by && !screen.mine) err = `${screen.controlled_by.name} is controlling ${screen.name}.`;
  return page(`<h1>Control a screen</h1>
    <p>Screens with sharing turned on. Take one that no one else is using; it's yours until you release it or stop using it for ${Math.round(list.idle_s / 60)} minutes.${can("admin.view") ? ` The rest is in <a href="#/admin">Admin</a>.` : ""}</p>
    ${err ? `<p class="share-msg bad">${esc(err)}</p>` : ""}
    <div class="share-actions">${list.screens.map(card).join("") || `<div class="share-card"><p>No screen has sharing turned on yet.</p></div>`}</div>`);
}

function card(s) {
  const held = s.controlled_by;
  const since = held ? Math.max(1, Math.round((Date.now() / 1000 - held.since) / 60)) : 0;
  const state = !s.online ? `<span class="ctl-st off">Offline</span>`
    : s.mine ? `<span class="ctl-st mine">You're controlling it</span>`
    : held ? `<span class="ctl-st held">${esc(held.name)} · ${since} min</span>`
    : `<span class="ctl-st free">Free${s.sharing_off ? " · sharing off" : ""}</span>`;
  const action = s.mine ? `<a class="share-btn share-primary" href="#/control/${esc(s.id)}">Open remote</a>`
    : held ? `<button class="share-btn" disabled>In use</button>`
    : `<button class="share-btn share-primary" data-act="ctl-take" data-dev="${esc(s.id)}" ${busy === s.id ? "disabled" : ""}>${busy === s.id ? "Taking…" : "Take control"}</button>`;
  return `<div class="share-card ctl-card">
    ${s.screenshot ? `<img class="ctl-shot" src="/api/devices/${encodeURIComponent(s.id)}/screenshot?t=${Math.round(s.screenshot.at)}" alt="" />` : ""}
    <div class="ctl-row"><div><h2>${esc(s.name)}</h2>${state}</div>${action}</div></div>`;
}

async function take(id) {
  if (busy) return;
  busy = id;
  S.poke();
  try {
    const res = await userFetch(`/api/control/${encodeURIComponent(id)}`, { method: "POST" });
    const out = await res.json().catch(() => ({}));
    if (res.ok) { err = ""; await refresh(); location.hash = `#/control/${id}`; }
    else { err = out.detail || "Couldn't take that screen."; await refresh(); }
  } catch (e) {
    err = e.message;
  } finally { busy = ""; S.poke(); }
}

async function release(id) {
  busy = id;
  S.poke();
  try { await userFetch(`/api/control/${encodeURIComponent(id)}`, { method: "DELETE" }); } catch (_) { /* refresh shows it */ }
  busy = "";
  await refresh();
  location.hash = "#/control";
}

export function handleControl(act, el) {
  if (act === "ctl-take") { take(el.dataset.dev); return true; }
  if (act === "ctl-release") { release(el.dataset.dev); return true; }
  return false;
}
