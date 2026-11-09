// A persistent canvas outside #root: ordinary room/status repaints never interrupt a stroke.
import { controlFetch, userFetch } from "./api.js";

import { drawingBounds, viewport, fitCamera, svgDrawing, imagePDF } from "./whiteboard-export.js";
import { esc } from "./ui.js";

export const BOARD_WIDTH = 1600, BOARD_HEIGHT = 900;
export const COLORS = ["#17212b", "#2563eb", "#e8590c", "#16845b", "#d92d48"];
export function applyBoardEvent(board, event) {
  if (event.kind === "snapshot") return structuredClone(event);
  if (!board || event.revision <= board.revision) return board;
  const next = { ...board, revision: event.revision, epoch: event.epoch, active: event.active, strokes: [...board.strokes] };
  if (event.kind === "clear") next.strokes = [];
  if (event.kind === "undo") next.strokes.pop();
  if (event.kind === "stroke") {
    const index = next.strokes.findIndex(s => s.id === event.id);
    const stroke = index < 0 ? { id: event.id, color: event.color, width: event.width, points: [] } : next.strokes[index];
    const points = [...stroke.points];
    // Retry acknowledgements can repeat a chunk. Never duplicate its points.
    points.splice(event.offset, event.points.length, ...event.points);
    const updated = { ...stroke, points, done: event.done };
    if (index < 0) next.strokes.push(updated); else next.strokes[index] = updated;
  }
  return next;
}

export function boardPoint(rect, clientX, clientY, view = fitCamera(rect, {x:0,y:0,width:1600,height:900})) {
  const v=viewport(rect,view);
  return [(clientX-rect.left-v.x)/v.scale,(clientY-rect.top-v.y)/v.scale]
    .map(n=>Math.round(Math.max(-100000,Math.min(100000,n))*10)/10);
}
let camera = {x:800,y:450,scale:1}, fitPending = true, hand = false, panning = null;
let drawingContext = null, insertion = null, insertTimer = 0;

let standalone = false, editAllowed = true, boardKey = "", shareLinks = null;
let room = "", controller = false, stream = null, board = null, connected = false;
let overlay = null, canvas = null, observer = null, frame = 0;
let color = COLORS[0], width = 7, eraser = false, current = null;
let drafts = new Map(), queue = [], sending = false, flushTimer = 0, notice = "", confirmingClear = false;
let generation = 0, reconnect = () => {};
const endpoint = () => `/api/rooms/${encodeURIComponent(room)}/whiteboard`;

// In the iPad app (not included), Apple Pencil draws with PencilKit, Apple's own ink, instead of this
// canvas. This page still owns the board: it syncs, retries and talks to the server, and tells
// the app what the board holds; the app draws and edits, then hands strokes back (window.xosInk).
const inkApp = () => controller && !!window.xosNative?.ink && !!window.webkit?.messageHandlers?.xosInk;
let inkOpen = false, inkBoard = null, inkHandled = 0;
function postInk(message) { try { window.webkit.messageHandlers.xosInk.postMessage(message); } catch (_) { /* app gone */ } }
function syncInk() {
  if (!inkApp()) return;
  if (!overlay || !board || (!standalone && !board.active)) {
    if (inkOpen) { inkOpen = false; inkBoard = null; postInk({ type: "close" }); }
    return;
  }
  if (!inkOpen) inkHandled = 0;  // a new native canvas counts its messages from zero
  const message = { type: inkOpen ? "state" : "open", room, title: room.replaceAll("-", " ").replace(/\b\w/g, c => c.toUpperCase()),
    share_url: new URL(`/u/whiteboards/${encodeURIComponent(room)}`, location.origin).href,
    busy: !!current || !!queue.length || sending, handled: inkHandled, connected, editable: editAllowed,
    status: overlay.querySelector(".wb-status")?.textContent || "", warning: !!notice || !connected };
  if (!inkOpen || inkBoard !== board) {
    message.board = { epoch: board.epoch, revision: board.revision, strokes: board.strokes.map(({ id, color, width, points }) => ({ id, color, width, points })) };
    inkBoard = board;
  }
  inkOpen = true;
  postInk(message);
}
window.xosInk = message => {
  inkHandled++;
  if (!board || !editAllowed) { updateStatus(); return; }
  const epoch = board.epoch;
  notice = ""; confirmingClear = false;
  if (message.type === "stroke") {
    const { id, color, width, points } = message;
    for (let i = 0; i < points.length; i += 256) enqueue({ action: "stroke", epoch, id, color, width,
      offset: i, points: points.slice(i, i + 256), done: i + 256 >= points.length });
  } else if (message.type === "replace") enqueue({ action: "replace", epoch, strokes: message.strokes });
  else if (message.type === "exit") enqueue({ action: "exit", epoch });
  updateStatus();
};

export function watchWhiteboard(nextRoom, writable = false, options = {}) {
  const nextKey = options.standalone ? new URLSearchParams(location.hash.split("?")[1] || "").get("board_key") || "" : "";
  if (room === nextRoom && controller === writable && standalone === !!options.standalone && boardKey === nextKey) {
    document.body.classList.toggle("whiteboarding", !!board && (standalone || board.active));
    return;
  }
  finishStroke();
  generation++; clearTimeout(insertTimer); insertion=null; drawingContext=null; fitPending=true;
  stream?.close(); stream = null;
  room = nextRoom || ""; controller = writable; board = null; connected = false;
  standalone = !!options.standalone; editAllowed = !standalone; shareLinks = null;
  boardKey = nextKey;
  drafts.clear(); queue = []; notice = ""; confirmingClear = false;
  updateOverlay();
  if (!room) return;
  const mine = generation;
  const connect = () => {
    if (mine !== generation) return;
    const source = new EventSource(`${endpoint()}/stream`);
    stream = source;
    const currentStream = () => mine === generation && stream === source;
    source.onopen = () => { if (!currentStream()) return; connected = true; updateStatus(); };
    source.onerror = () => {
      if (!currentStream()) return;
      connected = false; finishStroke(); updateStatus();
      if (!board) { const loading = document.querySelector(".wb-loading"); if (loading) loading.textContent = "Canvas unavailable. Check the room link or try again."; }
      if (source.readyState === EventSource.CLOSED) setTimeout(() => { if (currentStream()) connect(); }, 1500);
    };
    source.onmessage = e => {
      if (!currentStream()) return;
      const event = JSON.parse(e.data);
      if (board && event.epoch !== board.epoch) { current = null; drafts.clear(); }
      board = applyBoardEvent(board, event);
      updateOverlay();
    };
  };
  reconnect = connect;
  connect();
  if (standalone) boardFetch(`${endpoint()}/access`, {cache:"no-store"})
    .then(r => r.ok ? r.json() : {editable:false})
    .then(result => { if (mine === generation) { editAllowed = !!result.editable; updateStatus(); } })
    .catch(() => { if (mine === generation) { editAllowed = false; updateStatus(); } });
}

function boardFetch(path, options = {}) {
  if (standalone && boardKey) return fetch(path, {...options, headers:{...options.headers, "X-Xos-Board":boardKey}, credentials:"omit"});
  return (standalone ? userFetch : controlFetch)(path, options);
}
const writeEndpoint = () => endpoint() + (standalone ? "/drawing" : "");

function scheduleDraw() {
  if (!frame) frame = requestAnimationFrame(() => { frame = 0; draw(); });
}

function draw() {
  if (!canvas || !board) return;
  const rect = canvas.getBoundingClientRect(), ratio = Math.min(devicePixelRatio || 1, 2);
  const w = Math.max(1, Math.round(rect.width * ratio)), h = Math.max(1, Math.round(rect.height * ratio));
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext("2d"), strokes=visibleStrokes();
  if (!controller || fitPending) { camera=fitCamera(rect,drawingBounds(strokes)); fitPending=false; }
  const v=viewport(rect,camera);
  ctx.setTransform(ratio,0,0,ratio,0,0);ctx.fillStyle="#fff";ctx.fillRect(0,0,rect.width,rect.height);
  ctx.setTransform(ratio*v.scale,0,0,ratio*v.scale,ratio*v.x,ratio*v.y);
  paintInk(ctx, strokes);
  const zoom=overlay.querySelector(".wb-zoom-label"); if(zoom)zoom.textContent=`${Math.round(camera.scale*100)}%`;
}

function visibleStrokes() {
  const strokes = board.strokes.map(s => drafts.get(s.id) || s);
  for (const s of drafts.values()) if (!board.strokes.some(b => b.id === s.id)) strokes.push(s);
  return strokes;
}

export function paintInk(ctx, strokes) {

  for (const s of strokes) {
    if (!s.points.length) continue;
    ctx.strokeStyle = s.color; ctx.fillStyle = s.color; ctx.lineWidth = s.width;
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.beginPath();
    if (s.points.length === 1) {
      ctx.arc(...s.points[0], s.width / 2, 0, Math.PI * 2); ctx.fill();
    } else {
      ctx.moveTo(...s.points[0]);
      // Midpoint curves keep Pencil and finger strokes smooth on every display and export.
      for (let i = 1; i < s.points.length - 1; i++) {
        const p = s.points[i], next = s.points[i + 1];
        ctx.quadraticCurveTo(...p, (p[0] + next[0]) / 2, (p[1] + next[1]) / 2);
      }
      ctx.lineTo(...s.points.at(-1)); ctx.stroke();
    }
  }
}

const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${({
  hand:'<path d="M8 13V7a2 2 0 0 1 4 0v5-8a2 2 0 0 1 4 0v8-5a2 2 0 0 1 4 0v8c0 5-3 7-7 7-3 0-5-2-7-5l-3-4a2 2 0 0 1 3-2l2 2"/>',
  pen:'<path d="m16 3 5 5-12 12-6 1 1-6Z M13 6l5 5"/>',
  eraser:'<path d="m15 3 6 6-11 12H7l-5-5Z M7 11l6 6 M10 21h12"/>',
  undo:'<path d="m9 4-6 6 6 6 M3 10h12a6 6 0 0 1 0 12"/>',
  clear:'<path d="M4 7h16 M9 3h6 M6 7l1 14h10l1-14 M10 11v6 M14 11v6"/>',
  share:'<path d="M12 16V3m-4 4 4-4 4 4 M5 12v8h14v-8"/>',
  close:'<path d="m6 6 12 12 M18 6 6 18"/>'
})[name] || ""}</svg>`;

function updateOverlay() {
  if (!board || (!standalone && !board.active)) {
    observer?.disconnect(); observer = null;
    overlay?.remove(); overlay = canvas = null;
    const root = document.getElementById("root"); if (root) root.inert = false;
    document.body.classList.remove("whiteboarding");
    syncInk();
    return;
  }
  if (!overlay) {
    overlay = document.createElement("section");
    overlay.className = `wb ${controller ? "wb-controller" : "wb-tv"}`;
    overlay.setAttribute("aria-label", controller ? "Whiteboard drawing mode" : "Room whiteboard");
    overlay.innerHTML = controller ? `<header class="wb-head"><div class="wb-heading"><span class="wb-mark" aria-hidden="true">${icon("pen")}</span><div><b>${esc(room.replaceAll("-", " ").replace(/\b\w/g, c => c.toUpperCase()))}<span class="wb-heading-label"> / Canvas</span></b><span class="wb-status" role="status"></span></div></div><div class="wb-head-actions"><a class="wb-signin" href="/u/whiteboards/${encodeURIComponent(room)}" target="_blank" rel="noopener" hidden>Sign in to draw</a><button data-wb="share" class="wb-share">${icon("share")} Share</button>${standalone ? "" : '<button data-wb="exit" class="wb-exit">Done</button>'}</div></header>
      <div class="wb-tools" role="toolbar" aria-label="Drawing tools">
        <button data-wb="pen" aria-pressed="true" title="Pen (P)">${icon("pen")}<span>Pen</span></button><button data-wb="eraser" aria-pressed="false" title="Eraser (E)">${icon("eraser")}<span>Eraser</span></button>
        <button data-wb="hand" aria-pressed="false" title="Move canvas (H)">${icon("hand")}<span>Move</span></button><span class="wb-divider"></span>
        ${COLORS.map((c,i) => `<button data-wb="color" data-color="${c}" class="wb-color" style="--ink:${c}" aria-label="${["Black","Blue","Orange","Green","Red"][i]} pen" aria-pressed="${i === 0}"></button>`).join("")}
        <span class="wb-divider"></span>
        ${[3,7,14].map((n,i) => `<button data-wb="width" data-width="${n}" aria-label="${["Fine","Medium","Broad"][i]} line" aria-pressed="${n === 7}"><i style="width:${n+3}px;height:${n+3}px"></i></button>`).join("")}
        <span class="wb-tool-space"></span><button data-wb="undo" title="Undo (⌘/Ctrl Z)">${icon("undo")}<span>Undo</span></button><button data-wb="clear">${icon("clear")}<span>Clear</span></button>
      </div><div class="wb-stage"><div class="wb-paper"><canvas aria-label="Draw here with your finger or Apple Pencil"></canvas></div></div>
      <button data-wb="focus" class="wb-focus-toggle" aria-pressed="false">Hide tools</button><div class="wb-navigation" aria-label="Canvas view"><button data-wb="zoom-out" aria-label="Zoom out">−</button><span class="wb-zoom-label">100%</span><button data-wb="zoom-in" aria-label="Zoom in">+</button><button data-wb="fit">Fit drawing</button></div><aside class="wb-share-panel" aria-label="Share canvas" hidden></aside><footer class="wb-foot"><span>${standalone ? "A shared space to think together" : "Draw here · Live on the room TV"}</span><span class="wb-shortcuts">P Pen · E Eraser · H Move · ⌘/Ctrl Z Undo</span><span class="wb-clear-confirm" hidden>Clear the whole board? <button data-wb="clear-yes">Clear board</button><button data-wb="clear-no">Cancel</button></span></footer>` : '<canvas aria-label="Whiteboard drawing"></canvas>';
    document.body.appendChild(overlay);
    const root = document.getElementById("root"); if (root) root.inert = true;
    document.body.classList.add("whiteboarding");
    canvas = overlay.querySelector("canvas");
    observer = new ResizeObserver(() => {if(!current&&!panning)fitPending=true;scheduleDraw();}); observer.observe(canvas);
    if (controller) bindDrawing();
  }
  updateStatus(); scheduleDraw();
}

function updateStatus() {
  if (!overlay || !controller) return;
  const status = overlay.querySelector(".wb-status");
  status.textContent = notice || (standalone && !editAllowed ? "View only · Sign in to draw" : !connected ? "Reconnecting — drawing paused" : queue.length || sending || current ? "Syncing drawing…" : standalone ? "All changes saved" : "Live on TV");
  status.classList.toggle("wb-warning", !!notice || !connected);
  overlay.querySelector('[data-wb="pen"]').setAttribute("aria-pressed", String(!eraser && !hand));
  overlay.querySelector('[data-wb="eraser"]').setAttribute("aria-pressed", String(eraser && !hand));
  overlay.querySelector('[data-wb="hand"]').setAttribute("aria-pressed",String(hand));
  overlay.querySelectorAll('[data-wb="color"]').forEach(el => el.setAttribute("aria-pressed", String(!eraser && el.dataset.color === color)));
  overlay.querySelectorAll('[data-wb="width"]').forEach(el => el.setAttribute("aria-pressed", String(Number(el.dataset.width) === width)));
  overlay.querySelector(".wb-signin").hidden = !standalone || editAllowed;
  overlay.querySelectorAll('[data-wb="pen"], [data-wb="eraser"], [data-wb="color"], [data-wb="width"]').forEach(el => el.disabled = !editAllowed);
  overlay.querySelector(".wb-clear-confirm").hidden = !confirmingClear;
  overlay.querySelector('[data-wb="undo"]').disabled = !connected || !editAllowed || !board?.strokes.length || !!current || queue.length > 0 || sending;
  overlay.querySelector('[data-wb="clear"]').disabled = !connected || !editAllowed || !!current || queue.length > 0 || sending;
  const pending=!!current||!!queue.length||sending;
  overlay.querySelectorAll("[data-export]").forEach(el=>el.disabled=pending||!!insertion?.preparing);
  const insert=overlay.querySelector("[data-wb=insert-copper]");if(insert)insert.disabled=pending||!!insertion?.preparing||!editAllowed;
  const access=overlay.querySelector(".wb-insert-access");if(access)access.textContent=editAllowed?"":"Sign in to Xos to insert this drawing.";
  const exportPending=overlay.querySelector(".wb-export-pending");if(exportPending)exportPending.textContent=pending?"Waiting for drawing to finish saving…":"";
  canvas.style.cursor = hand ? (panning ? "grabbing" : "grab") : !editAllowed ? "default" : eraser ? "cell" : "crosshair";
  syncInk();
}

async function post(op, path = writeEndpoint()) {
  const response = await boardFetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(op) });
  const result = await response.json();
  if (!response.ok) { const error = new Error(result.detail || "Whiteboard unavailable"); error.status = response.status; throw error; }
  return result;
}

function enqueue(op) { queue.push(op); drain(); }
async function drain() {
  if (sending) return;
  sending = true;
  const mine = generation, path = writeEndpoint();
  try {
    while (queue.length && mine === generation) {
      const op = queue[0];
      try {
        const event = await post(op, path);
        if (mine !== generation) break;
        queue.shift(); notice = "";
        board = applyBoardEvent(board, event);
        if (op.done) drafts.delete(op.id);
        if (event.epoch !== op.epoch) { current = null; drafts.clear(); }
        updateOverlay();
      } catch (error) {
        if (mine !== generation) break;
        if (standalone && error.status === 401) editAllowed = false;
        notice = error.status ? error.message : "Connection interrupted — retrying drawing…";
        if (error.status && error.status < 500) {
          queue = []; current = null; drafts.clear();
          try {
            const fresh = await (await fetch(endpoint(), { cache: "no-store" })).json();
            if (mine !== generation) break;
            board = fresh;
          } catch (_) { /* stream will reconnect */ }
          if (mine !== generation) break;
          updateOverlay(); break;
        }
        updateStatus();
        await new Promise(resolve => setTimeout(resolve, 1200));
      }
    }
  } finally { sending = false; updateStatus(); if (queue.length) drain(); }
}

function flushStroke(done = false) {
  clearTimeout(flushTimer); flushTimer = 0;
  if (!current) return;
  const s = current, points = s.points.slice(s.sent);
  // Each request is bounded. A coalesced Pencil event may contain many samples.
  for (let i = 0; i < points.length; i += 256) {
    const chunk = points.slice(i, i + 256);
    enqueue({ action: "stroke", epoch: s.epoch, id: s.id, color: s.color, width: s.width,
      offset: s.sent + i, points: chunk, done: done && i + 256 >= points.length });
  }
  if (done && !points.length) enqueue({ action: "stroke", epoch: s.epoch, id: s.id,
    color: s.color, width: s.width, offset: s.sent, points: [], done: true });
  s.sent = s.points.length;
}
function finishStroke() {
  if (!current) return;
  flushStroke(true); current = null; updateStatus();
}

function zoomView(factor) {finishStroke();camera.scale=Math.max(.02,Math.min(8,camera.scale*factor));scheduleDraw();}
function bindDrawing() {
  canvas.addEventListener("wheel",e=>{e.preventDefault();finishStroke();if(e.ctrlKey||e.metaKey)zoomView(Math.exp(-e.deltaY*.005));else{camera.x+=e.deltaX/camera.scale;camera.y+=e.deltaY/camera.scale;scheduleDraw();}},{passive:false});
  canvas.addEventListener("pointerdown", e => {
    if(hand && !current) { e.preventDefault(); panning={pointer:e.pointerId,x:e.clientX,y:e.clientY,cx:camera.x,cy:camera.y}; canvas.setPointerCapture(e.pointerId); updateStatus(); return; }
    if (current || !connected || !editAllowed || (!standalone && !board?.active) || !board || (e.pointerType === "mouse" && e.button !== 0)) return;
    e.preventDefault(); notice = ""; confirmingClear = false;
    current = { id: crypto.randomUUID(), epoch: board.epoch, color: eraser ? "#ffffff" : color,
      width: eraser ? 32 : width, points: [boardPoint(canvas.getBoundingClientRect(), e.clientX, e.clientY, camera)], sent: 0, pointer: e.pointerId };
    drafts.set(current.id, current);
    canvas.setPointerCapture(e.pointerId);
    flushStroke(); scheduleDraw(); updateStatus();
  });
  canvas.addEventListener("pointermove", e => {
    if(panning?.pointer===e.pointerId) {camera.x=panning.cx-(e.clientX-panning.x)/camera.scale;camera.y=panning.cy-(e.clientY-panning.y)/camera.scale;scheduleDraw();return;}
    if (!current || e.pointerId !== current.pointer) return;
    e.preventDefault();
    const samples = e.getCoalescedEvents?.();
    for (const sample of samples?.length ? samples : [e]) {
      const p = boardPoint(canvas.getBoundingClientRect(), sample.clientX, sample.clientY, camera), last = current.points.at(-1);
      if (Math.hypot(p[0] - last[0], p[1] - last[1]) >= .8) current.points.push(p);
      if (current.points.length >= 8192) { finishStroke(); break; }
    }
    scheduleDraw();
    if (!flushTimer) flushTimer = setTimeout(() => { flushTimer = 0; flushStroke(); }, 60);
  });
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) canvas.addEventListener(type, e => {
    if(panning?.pointer===e.pointerId){panning=null;updateStatus();}
    if (current?.pointer === e.pointerId) { finishStroke(); scheduleDraw(); }
  });
}

async function showSharePanel() {
  const panel = overlay.querySelector(".wb-share-panel");
  panel.hidden = false;
  const mine = generation;
  panel.innerHTML = '<p role="status">Getting sharing links…</p>';
  try {
    const res = await boardFetch(`${endpoint()}/share`, {cache:"no-store"});
    if (!res.ok) throw new Error("Sign in to see Copper sharing");
    shareLinks = await res.json();
    const context=await boardFetch(`${endpoint()}/context`,{cache:"no-store"});
    drawingContext=context.ok?await context.json():null;
  } catch (_) { shareLinks = {org_url: new URL(`/u/whiteboards/${encodeURIComponent(room)}`, location.origin).href}; }
  if (mine !== generation || !overlay) return;
  const currentMeeting=drawingContext?.meetings?.find(m=>m.start<=Date.now()/1000&&m.end>Date.now()/1000);
  const defaultMode=currentMeeting?"meeting":drawingContext?.person?.email?"personal":"choose";
  panel.innerHTML = `<div class="wb-panel-heading"><h2>Share this canvas</h2><button data-wb="share-close" aria-label="Close sharing">${icon("close")}</button></div>
    <p>Save a native snapshot to a Copper canvas, or share the live drawing with your team.</p>
    <div class="wb-native-insert"><b>Insert into Copper</b><small>Choose the destination using the account signed in to Copper. The drawing becomes a movable image.</small>
    <label for="wb-copper-mode">Destination</label><select id="wb-copper-mode">
    ${drawingContext?.meetings?.length?`<option value="meeting" ${defaultMode==="meeting"?"selected":""}>Meeting canvas</option>`:""}<option value="personal" ${defaultMode==="personal"?"selected":""}>My Personal canvas</option>${shareLinks.copper_url?'<option value="room">Connected room canvas</option>':""}<option value="choose" ${defaultMode==="choose"?"selected":""}>Choose a canvas in Copper</option></select>
    ${drawingContext?.meetings?.length?`<div class="wb-meeting-choice" ${defaultMode!=="meeting"?"hidden":""}><label for="wb-meeting">Meeting</label><select id="wb-meeting">${drawingContext.meetings.map(m=>`<option value="${esc(m.id)}" ${currentMeeting?.id===m.id?"selected":""}>${esc(m.title)} · ${new Date(m.start*1000).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}</option>`).join("")}</select></div>`:""}
    ${drawingContext?.warning?`<small>${esc(drawingContext.warning)}</small>`:""}<button data-wb="insert-copper" class="wb-insert-button">Insert drawing into Copper</button><small class="wb-insert-access"></small><a class="wb-native-open" hidden>Open Copper destination picker ↗</a><small>Requires the current Copper version. PNG, SVG and PDF work in any browser.</small></div>
    <label for="wb-org-link">Organization link</label><div class="wb-link-row"><input id="wb-org-link" readonly value="${esc(shareLinks.org_url)}"><button data-wb="copy-org">Copy link</button></div><small>Signed-in people in your organization can view and draw.</small>
    ${shareLinks.copper_url ? `<div class="wb-copper-card"><b>Continue in Copper Canvas</b><p>The same live drawing, with room for notes, links and your team's ideas.</p><a class="wb-primary-link" href="${esc(shareLinks.copper_url)}">Open in Copper ↗</a>${(shareLinks.copper_invite_url || shareLinks.copper_web_url) ? `<label for="wb-copper-link">Copper invitation</label><div class="wb-link-row"><input id="wb-copper-link" readonly value="${esc(shareLinks.copper_invite_url || shareLinks.copper_web_url)}"><button data-wb="copy-copper">Copy invite</button></div><small>Recipients join through Copper Cloud. Canvas members can edit this drawing.</small>` : ""}</div>` : '<p class="wb-muted">A Copper canvas has not been connected to this room yet. The organization link works in any browser.</p>'}
    <div class="wb-export"><b>Other export options</b><div class="wb-export-options"><button data-export="png">PNG image</button><button data-export="svg">SVG vector</button><button data-export="pdf">PDF document</button><button data-export="copy">Copy image</button></div><small>Includes all drawing, even outside the current view. SVG preserves vector lines.</small></div><span class="wb-export-pending" role="status"></span><span class="wb-export-status" role="status"></span>`;
  panel.querySelector('[data-wb="share-close"]').focus();
  updateStatus();
}

function panelNotice(message) {const status=overlay?.querySelector(".wb-export-status");if(status)status.textContent=message;}
function rasterSnapshot(strokes=board.strokes) {
  const b=drawingBounds(strokes),scale=Math.min(1,4096/Math.max(b.width,b.height));
  const out=document.createElement("canvas");out.width=Math.max(1,Math.ceil(b.width*scale));out.height=Math.max(1,Math.ceil(b.height*scale));
  const ctx=out.getContext("2d");ctx.fillStyle="#fff";ctx.fillRect(0,0,out.width,out.height);
  ctx.setTransform(scale,0,0,scale,-b.x*scale,-b.y*scale);paintInk(ctx,strokes);return out;
}
const canvasBlob=(out,type="image/png")=>new Promise((resolve,reject)=>out.toBlob(blob=>blob?resolve(blob):reject(new Error("The image could not be exported")),type,.95));
function saveBlob(blob,extension,sourceRoom=room) {
  const link=document.createElement("a"),url=URL.createObjectURL(blob);
  link.href=url;link.download=`${sourceRoom}-canvas-${new Date().toISOString().slice(0,10)}.${extension}`;
  document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
}
async function exportDrawing(format) {
  const strokes=structuredClone(board.strokes);
  const sourceRoom=room,mine=generation;
  const notice=message=>{if(mine===generation)panelNotice(message);};
  try {
    if(format==="svg")saveBlob(new Blob([svgDrawing(strokes,`${sourceRoom} whiteboard`)],{type:"image/svg+xml"}),"svg",sourceRoom);
    else {
      const out=rasterSnapshot(strokes);
      if(format==="copy") {
        if(!navigator.clipboard?.write||typeof ClipboardItem==="undefined")throw new Error("Image copy is unavailable here. Download a PNG instead.");
        await navigator.clipboard.write([new ClipboardItem({"image/png":canvasBlob(out)})]);notice("Image copied. Paste it into Copper or another app.");return;
      }
      if(format==="pdf") {
        const jpeg=new Uint8Array(await(await canvasBlob(out,"image/jpeg")).arrayBuffer());
        saveBlob(new Blob([imagePDF(jpeg,out.width,out.height)],{type:"application/pdf"}),"pdf",sourceRoom);
      } else saveBlob(await canvasBlob(out),"png",sourceRoom);
    }
    notice(`${format.toUpperCase()} download started.`);
  } catch(error){notice(format==="copy"?"Could not copy the image in this browser. Use PNG image instead.":error.message||"Export failed. Try another format.");}
}
// A stalled fetch or JSON body must not permanently stop receipt polling.
async function readCopperReceipt(url) {
  const controller=new AbortController();let timer;
  const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{
    controller.abort();reject(new Error("Copper confirmation timed out"));
  },15000);});
  const read=async()=>{
    const response=await fetch(url,{cache:"no-store",credentials:"omit",signal:controller.signal});
    return {status:response.status,ok:response.ok,state:response.ok?await response.json():null};
  };
  try{return await Promise.race([read(),deadline]);}
  finally{clearTimeout(timer);}
}
async function insertCopper() {
  const mode=overlay.querySelector("#wb-copper-mode").value;
  const meeting=mode==="meeting"?overlay.querySelector("#wb-meeting")?.value||"":"";
  const mine=generation;
  try {
    if(!insertion || insertion.revision!==board.revision || insertion.mode!==mode || insertion.meeting!==meeting) {
      insertion={preparing:true};updateStatus();panelNotice("Preparing a native drawing snapshot…");
      const snapshot=structuredClone(board),out=rasterSnapshot(snapshot.strokes);
      const response=await boardFetch(`${endpoint()}/exports`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({png:out.toDataURL("image/png"),width:out.width,height:out.height,epoch:snapshot.epoch,revision:snapshot.revision,meeting_id:meeting})});
      const result=await response.json();if(!response.ok)throw new Error(result.detail||"Could not prepare the drawing");
      if(mine!==generation)return;
      const url=new URL("copper://canvas/insert");url.searchParams.set("source",result.source);url.searchParams.set("mode",mode);
      insertion={...result,url:url.href,mode,meeting,revision:snapshot.revision,preparing:false};
    }
    const link=overlay.querySelector(".wb-native-open");link.href=insertion.url;link.hidden=false;
    panelNotice("Choose a canvas in Copper. This page confirms when the image has been inserted.");
    const pending=insertion;let attempts=0;clearTimeout(insertTimer);
    const poll=async()=>{
      if(mine!==generation||insertion!==pending)return;
      try {
        const res=await readCopperReceipt(pending.status_url);
        // Room changes and replacement snapshots invalidate replies, including a delayed body.
        if(mine!==generation||insertion!==pending)return;
        if(res.status===410){panelNotice("Snapshot expired. Click Insert again to make a fresh one.");insertion=null;updateStatus();return;}
        if(res.ok){const state=res.state;if(state.status==="inserted"){
          panelNotice(`Inserted into ${state.receipt.canvas_name}. The saved image is independent of future board edits.`);
          link.href=`copper://canvas/${state.receipt.canvas_id}`;link.textContent="Open the saved image in Copper ↗";return;
        }}
      }catch(_){/* A brief outage cannot turn a launch into a successful insert. */}
      if(mine!==generation||insertion!==pending)return;
      if(++attempts===8)panelNotice("Choose a canvas in Copper. If no picker appeared, update Copper or use an export below.");
      if(attempts<180)insertTimer=setTimeout(poll,2000);
    };
    insertTimer=setTimeout(poll,1000);location.href=insertion.url;
  }catch(error){if(mine!==generation)return;insertion=null;panelNotice(error.message||"Could not open Copper. Use an export below.");}
  updateStatus();
}

document.addEventListener("click", async e => {
  const button = e.target.closest("[data-wb], [data-export], [data-act='wb-start']");
  if (!button || button.disabled) return;
  if(button.dataset.export){finishStroke();await exportDrawing(button.dataset.export);return;}
  const action = button.dataset.wb || "start";
  if (action === "start") {
    button.disabled = true;
    try {
      const path = `/api/rooms/${encodeURIComponent(button.dataset.room)}/whiteboard`;
      const snapshot = await (await fetch(path, { cache: "no-store" })).json();
      await post({ action: "start", epoch: snapshot.epoch }, path);
    } catch (error) {
      let status = button.parentElement.querySelector(".wb-start-error");
      if (!status) { status = document.createElement("span"); status.className = "wb-start-error"; status.setAttribute("role", "alert"); button.after(status); }
      status.textContent = error.message;
    } finally { button.disabled = false; }
    return;
  }
  finishStroke();
  if (action === "share") { await showSharePanel(); return; }
  if (action === "share-close") { overlay.querySelector(".wb-share-panel").hidden = true; overlay.querySelector('[data-wb="share"]').focus(); return; }
  if (action === "copy-org" || action === "copy-copper") {
    const input = overlay.querySelector(action === "copy-org" ? "#wb-org-link" : "#wb-copper-link");
    try { await navigator.clipboard.writeText(input.value); button.textContent = "Copied"; }
    catch (_) { input.focus(); input.select(); button.textContent = "Select and copy link"; }
    return;
  }
  if (action === "insert-copper") {await insertCopper();return;}
  if(action === "focus"){const focused=overlay.classList.toggle("wb-focused");button.textContent=focused?"Show tools":"Hide tools";button.setAttribute("aria-pressed",String(focused));return;}
  if(action === "zoom-in" || action === "zoom-out"){zoomView(action==="zoom-in"?1.25:.8);return;}
  if(action === "fit"){fitPending=true;scheduleDraw();return;}
  if (action === "pen") {eraser = false; hand=false;}
  else if(action === "hand") hand=true;
  else if (action === "eraser") {eraser = true;hand=false;}
  else if (action === "color") { color = button.dataset.color; eraser = false; hand=false; }
  else if (action === "width") width = Number(button.dataset.width);
  else if (action === "clear") confirmingClear = true;
  else if (action === "clear-no") confirmingClear = false;
  else if (["undo", "clear-yes", "exit"].includes(action)) {
    confirmingClear = false;
    enqueue({ action: action === "clear-yes" ? "clear" : action, epoch: board.epoch });
  }
  updateStatus();
});
window.addEventListener("pagehide", finishStroke);
window.addEventListener("offline", () => {
  connected = false; stream?.close(); finishStroke(); updateStatus();
});
window.addEventListener("online", () => { if (room) { stream?.close(); reconnect(); } });

document.addEventListener("keydown", e => {
  if (!overlay || !controller || e.target.closest("input, textarea, [contenteditable]")) return;
  if (e.key === "Escape") { const panel = overlay.querySelector(".wb-share-panel"); if (panel && !panel.hidden) { panel.hidden = true; overlay.querySelector('[data-wb="share"]').focus(); } return; }
  if (!editAllowed || e.altKey) return;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") { const undo = overlay.querySelector('[data-wb="undo"]'); if (!e.shiftKey && !undo.disabled) { e.preventDefault(); undo.click(); } }
  else if (!e.metaKey && !e.ctrlKey && ["p", "e", "h"].includes(e.key.toLowerCase())) { finishStroke(); eraser = e.key.toLowerCase() === "e"; hand=e.key.toLowerCase()==="h"; updateStatus(); }
});

document.addEventListener("change",e=>{
  if(e.target.id!=="wb-copper-mode"&&e.target.id!=="wb-meeting")return;
  clearTimeout(insertTimer);insertion=null;
  const meeting=overlay?.querySelector(".wb-meeting-choice");if(meeting)meeting.hidden=overlay.querySelector("#wb-copper-mode").value!=="meeting";
  const link=overlay?.querySelector(".wb-native-open");if(link)link.hidden=true;
  panelNotice("");updateStatus();
});
