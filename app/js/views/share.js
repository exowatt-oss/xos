// Share to a screen.
//
// #/share/<code> is the page a share link (/s/<code>) opens on a laptop or phone: share your screen
// (WebRTC, peer to peer over the office network like the camera feed), a link, or a photo to that
// screen. showShare()/stopShare() run on the screen itself when the server sends it a "share" or
// "share_stop" command; the shared content sits over whatever the screen was showing.
//
// #/share with no code (the short link /s) lists the screens you can share to, with a box for the
// code shown on the screen, and remembers the last screen you used.
//
// The share page builds its DOM once and main.js never repaints it (its markup is constant), so a
// running screen share isn't interrupted. WebRTC details (ordering, ICE, codecs) are in ../rtc.js.

import { listen, lowLatency, orderedSender, peerConnection, preferH264, sendStats, signal } from "../rtc.js";

const api = (path, opts = {}) => fetch(`/api/share${path ? `/${path}` : ""}`, { cache: "no-store", ...opts });
const json = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const LAST = "xos-share-last"; // { code, name } of the screen this browser last shared to
const codeOf = (text) => {
  // Accept the bare code, "abc12", "/s/abc12" or the whole link shown on the screen.
  const m = String(text || "").trim().toLowerCase().match(/(?:\/s\/)?([a-z0-9]{3,12})\/?$/);
  return m ? m[1] : "";
};
const escHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
async function errorText(res) {
  try { return (await res.json()).detail || res.statusText; } catch (_) { return res.statusText; }
}

// ======================= the share page (#/share/<code>) =======================

export function renderShare(code) {
  setTimeout(() => (code ? mountShare(code) : mountPicker()), 0);
  return `<div class="share" id="share-root" data-code="${escHtml(code)}"></div>`;
}

let presenting = null; // { session, stream, peers: Map, es, timer }

function stopPresenting() {
  if (!presenting) return;
  presenting.stream?.getTracks().forEach((t) => t.stop());
  presenting.peers?.forEach((pc) => pc.close());
  presenting.es?.close();
  clearInterval(presenting.timer);
  clearTimeout(presenting.slow);
  presenting = null;
}

// ---------- #/share: pick the screen ----------

async function mountPicker() {
  const root = document.getElementById("share-root");
  if (!root || root.dataset.mounted) return;
  root.dataset.mounted = "1";
  const last = (() => { try { return JSON.parse(localStorage.getItem(LAST) || "null"); } catch (_) { return null; } })();
  root.innerHTML = `
    <header class="share-top"><img src="../static/img/logo-white.svg" alt="Xos" /><span>Xos</span></header>
    <h1>Share to a screen</h1>
    <div class="share-actions">
    <form id="pick-code" class="share-card share-code">
      <h2>Type the code on the screen</h2>
      <p>The TV shows a link like <b>${escHtml(location.host)}/s/<i>abc12</i></b>. Type the last part, or scan its QR code with your phone's camera.</p>
      <div class="share-row"><input id="pick-code-in" autocapitalize="none" autocomplete="off" spellcheck="false" maxlength="80" placeholder="abc12" aria-label="Screen code" /><button class="share-btn share-go">Go</button></div>
      <p id="pick-code-msg" class="share-msg bad" role="status"></p>
    </form>
    <div id="pick-last" hidden></div>
    <section class="share-card">
      <h2>Or pick it</h2>
      <input id="pick-filter" class="share-filter" type="search" placeholder="Search screens and rooms" aria-label="Search screens" hidden />
      <div id="pick-list" class="share-pick"><p>Finding screens…</p></div>
    </section>
    </div>`;
  const $ = (id) => document.getElementById(id);
  const go = (code) => { location.hash = `#/share/${code}`; };
  $("pick-code").onsubmit = async (e) => {
    e.preventDefault();
    const code = codeOf($("pick-code-in").value);
    if (!code) { $("pick-code-msg").textContent = "Enter the code shown on the screen."; return; }
    const res = await api(code).catch(() => null);
    if (res?.ok) go(code);
    else $("pick-code-msg").textContent = res?.status === 404 ? "No screen has that code. Check the link on the TV." : "Can't reach Xos. Are you on the office network?";
  };
  let screens = [];
  const row = (s) => `<a class="share-pick-row" href="#/share/${escHtml(s.code)}" data-q="${escHtml(`${s.name} ${s.room}`.toLowerCase())}">
      <span><b>${escHtml(s.name)}</b>${s.room ? `<small>${escHtml(s.room)}</small>` : ""}</span>
      <span class="share-pick-state${s.busy ? " busy" : ""}">${s.busy ? "In use" : "Ready"}</span></a>`;
  const draw = () => {
    const needle = $("pick-filter").value.trim().toLowerCase();
    const shown = screens.filter((s) => !needle || `${s.name} ${s.room}`.toLowerCase().includes(needle));
    $("pick-list").innerHTML = shown.map(row).join("") || `<p>${screens.length ? "No screen matches." : "No screens are on right now."}</p>`;
    $("pick-filter").hidden = screens.length < 6;
  };
  $("pick-filter").oninput = draw;
  const res = await api("").catch(() => null);
  if (!res?.ok) { $("pick-list").innerHTML = "<p>Can't reach Xos. Are you on the office network (not Guest Wi-Fi)?</p>"; return; }
  screens = await res.json();
  const again = last && screens.find((s) => s.code === last.code);
  $("pick-last").hidden = !again;
  if (again) $("pick-last").innerHTML = `<a class="share-btn share-primary share-again" href="#/share/${escHtml(again.code)}">Share to ${escHtml(again.name)} again</a>`;
  draw();
}

async function mountShare(code) {
  const root = document.getElementById("share-root");
  if (!root || root.dataset.mounted) return;
  root.dataset.mounted = "1";
  const canShareScreen = Boolean(navigator.mediaDevices?.getDisplayMedia);
  root.innerHTML = `
    <header class="share-top"><img src="../static/img/logo-white.svg" alt="Xos" /><span>Xos</span><a class="share-other" href="#/share">Other screen</a></header>
    <h1 id="share-title">Share to a screen</h1>
    <p id="share-state" class="share-state">Checking the screen…</p>
    <div id="share-now" class="share-now" hidden>
      <div><b id="share-now-what"></b><span id="share-now-sub"></span></div>
      <button class="share-btn share-stop" id="share-stop">Stop sharing</button>
    </div>
    <div id="share-actions" class="share-actions">
      <label class="share-name">Your name <input id="share-name" maxlength="60" placeholder="Shown on the screen (optional)" autocomplete="name" /></label>
      <section class="share-card">
        <h2>Your screen</h2>
        ${canShareScreen
          ? `<p>Pick a window, tab or your whole screen. Keep this tab open while you share.</p>
             <button class="share-btn share-primary" id="share-screen">Share my screen</button>
             <label class="share-check"><input type="checkbox" id="share-motion" /> Playing a video? Favour smooth motion over sharp text</label>
             <p id="share-live" class="share-live" hidden></p>`
          : `<p>Screen sharing needs a laptop browser (Chrome, Edge, Safari or Firefox). On a phone or tablet, share a link or a photo below<span id="share-airplay-hint"></span>.</p>`}
      </section>
      <section class="share-card">
        <h2>A link</h2>
        <p>A web page, dashboard or published slide deck. Some sites (Google Docs, for example) block being shown inside another page; share your screen for those.</p>
        <form id="share-url-form" class="share-row"><input id="share-url" type="url" inputmode="url" placeholder="https://…" required /><button class="share-btn">Show</button></form>
      </section>
      <section class="share-card">
        <h2>A photo</h2>
        <label class="share-btn share-file">Choose a photo<input id="share-file" type="file" accept="image/jpeg,image/png,image/gif,image/webp" /></label>
      </section>
      <p id="share-airplay" class="share-airplay" hidden></p>
    </div>
    <p id="share-msg" class="share-msg" role="status"></p>`;
  const $ = (id) => document.getElementById(id);
  const name = $("share-name");
  name.value = localStorage.getItem("xos-share-name") || "";
  name.onchange = () => localStorage.setItem("xos-share-name", name.value.trim());
  const msg = (text, bad = false) => { $("share-msg").textContent = text; $("share-msg").className = `share-msg${bad ? " bad" : ""}`; };
  let status = null;

  async function refresh() {
    const res = await api(code).catch(() => null);
    if (!res || !res.ok) {
      $("share-title").textContent = "Share link not found";
      $("share-state").textContent = res?.status === 404 ? "This link was replaced or never existed. Use the link shown on the screen." : "Can't reach Xos. Are you on the office network?";
      $("share-actions").hidden = true;
      return;
    }
    status = await res.json();
    localStorage.setItem(LAST, JSON.stringify({ code: status.code, name: status.name }));
    $("share-title").textContent = `Share to ${status.name}`;
    $("share-state").textContent = !status.enabled ? "Sharing to this screen is turned off."
      : status.online ? "The screen is on and ready." : "The screen is offline right now.";
    $("share-state").className = `share-state ${status.enabled && status.online ? "ok" : "bad"}`;
    $("share-actions").hidden = !status.enabled;
    if (status.airplay) {
      $("share-airplay").hidden = false;
      $("share-airplay").innerHTML = `On an iPhone, iPad or Mac? You can also AirPlay to <b>${escHtml(status.airplay)}</b> from Control Center → Screen Mirroring.`;
      const hint = $("share-airplay-hint");
      if (hint) hint.innerHTML = `, or AirPlay to <b>${escHtml(status.airplay)}</b>`;
    }
    const s = status.sharing;
    const mine = s && presenting?.session === s.session;
    $("share-now").hidden = !s;
    if (s) {
      const what = { screen: "a screen", url: "a link", image: "a photo" }[s.kind] || "something";
      $("share-now-what").textContent = mine ? `You're sharing ${what}` : `${s.from || "Someone"} is sharing ${what}`;
      $("share-now-sub").textContent = mine ? "" : "Sharing something new replaces it.";
    }
  }

  async function start(body) {
    const res = await api(code, json({ ...body, name: name.value.trim() }));
    if (!res.ok) { msg(await errorText(res), true); return null; }
    msg("Showing on the screen.");
    return res.json();
  }

  async function shareScreen() {
    stopPresenting();
    const motion = Boolean($("share-motion")?.checked);
    let stream;
    try {
      // 1080p at up to 30 fps: sharp on a TV, and light enough for a Fire TV stick to decode
      // without lag. A Retina or 4K laptop would otherwise send its full resolution.
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { width: { max: 1920 }, height: { max: 1080 }, frameRate: { ideal: 30, max: 30 } },
        audio: true, selfBrowserSurface: "exclude", surfaceSwitching: "include", systemAudio: "include",
      });
    } catch (err) {
      msg(err.name === "NotAllowedError" ? "Screen sharing was cancelled." : `Can't share the screen: ${err.message}`, true);
      return;
    }
    const video = stream.getVideoTracks()[0];
    if (video && "contentHint" in video) video.contentHint = motion ? "motion" : "detail";
    msg("Connecting to the screen…");
    const session = await start({ kind: "screen" });
    if (!session) { stream.getTracks().forEach((t) => t.stop()); return; }
    msg("Connecting to the screen…");
    const channel = `share-${session.session}`;
    const send = orderedSender(channel, "presenter");
    const p = presenting = { session: session.session, stream, peers: new Map(), es: null, timer: 0, slow: 0 };
    video.onended = () => stop(); // "Stop sharing" in the browser's own bar
    // If the screen got the request but can't reach this computer, say what to try.
    p.slow = setTimeout(() => {
      if (presenting !== p || [...p.peers.values()].some((pc) => pc.connectionState === "connected")) return;
      const air = status?.airplay ? ` or AirPlay to ${status.airplay}` : "";
      msg(`Still connecting. The screen can't reach this computer directly: check you're on the office Wi-Fi (not Guest). You can also share a link or a photo${air}.`, true);
    }, 12000);
    p.es = listen(channel, "presenter", async (m) => {
      if (presenting !== p) return;
      if (m.type === "join") {
        p.peers.get(m.from)?.close();
        const pc = await peerConnection();
        p.peers.set(m.from, pc);
        stream.getTracks().forEach((t) => pc.addTrack(t, stream));
        preferH264(pc);
        for (const sender of pc.getSenders().filter((x) => x.track?.kind === "video")) {
          const params = sender.getParameters();
          // Slides: keep text sharp and drop frames if the network is busy. Video: the opposite.
          params.degradationPreference = motion ? "maintain-framerate" : "maintain-resolution";
          params.encodings = (params.encodings?.length ? params.encodings : [{}])
            .map((e) => ({ ...e, maxBitrate: 8_000_000, maxFramerate: 30 }));
          sender.setParameters(params).catch(() => {});
        }
        pc.onicecandidate = (e) => e.candidate && send(m.from, "ice", e.candidate.toJSON());
        pc.onconnectionstatechange = () => {
          if (presenting !== p) return;
          if (pc.connectionState === "connected") { clearTimeout(p.slow); msg("Live on the screen. Keep this tab open; close it or press Stop sharing to end."); }
        };
        await pc.setLocalDescription(await pc.createOffer());
        send(m.from, "offer", pc.localDescription.toJSON());
      } else if (m.type === "answer") {
        await p.peers.get(m.from)?.setRemote(m.data);
      } else if (m.type === "ice") {
        await p.peers.get(m.from)?.addRemoteCandidate(m.data);
      } else if (m.type === "bye") {
        p.peers.get(m.from)?.close();
        p.peers.delete(m.from);
      }
    });
    // Live numbers while sharing, so you can see it's working and how well.
    let prev = {};
    p.timer = setInterval(async () => {
      const pc = [...p.peers.values()].find((x) => x.connectionState === "connected");
      const el = $("share-live");
      if (!el) return;
      if (!pc) { el.hidden = true; return; }
      const st = await sendStats(pc, prev).catch(() => null);
      if (!st || presenting !== p) return;
      prev = st;
      el.hidden = false;
      el.textContent = [st.width && `${st.width}×${st.height}`, `${st.fps} fps`, st.mbps && `${st.mbps.toFixed(1)} Mbps`,
        st.rttMs != null && `${st.rttMs} ms`, st.codec].filter(Boolean).join(" · ");
    }, 2000);
    refresh();
  }

  async function stop() {
    const session = presenting?.session || "";
    stopPresenting();
    await api(`${code}/stop`, json({ session })).catch(() => {});
    msg("Stopped.");
    refresh();
  }

  $("share-screen")?.addEventListener("click", shareScreen);
  $("share-stop").onclick = stop;
  $("share-url-form").onsubmit = async (e) => {
    e.preventDefault();
    stopPresenting();
    if (await start({ kind: "url", url: $("share-url").value.trim() })) refresh();
  };
  $("share-file").onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    stopPresenting();
    msg("Sending the photo…");
    const res = await api(`${code}/image?name=${encodeURIComponent(name.value.trim())}`, {
      method: "POST", headers: { "Content-Type": file.type }, body: file,
    }).catch(() => null);
    if (!res || !res.ok) { msg(res ? await errorText(res) : "Couldn't send the photo.", true); return; }
    msg("Showing on the screen.");
    refresh();
  };
  // Closing the tab ends a screen share straight away rather than leaving it frozen on the TV.
  window.addEventListener("pagehide", () => {
    if (presenting) navigator.sendBeacon(`/api/share/${code}/stop`, new Blob([JSON.stringify({ session: presenting.session })], { type: "application/json" }));
  });
  await refresh();
  setInterval(refresh, 5000);
}

// ======================= on the screen (share / share_stop commands) =======================

let showing = null; // { session, code, pc, es, timer }

function layer() {
  let el = document.getElementById("xos-share");
  if (!el) { el = document.createElement("div"); el.id = "xos-share"; document.body.appendChild(el); }
  return el;
}

function teardown() {
  if (!showing) return;
  showing.pc?.close();
  showing.es?.close();
  clearInterval(showing.timer);
  clearTimeout(showing.gone);
  showing = null;
  const el = document.getElementById("xos-share");
  if (el) { el.className = ""; el.innerHTML = ""; }
  pauseBehind(false);
}

// While something is shared, stop drawing the view underneath (a page's animation,
// video): it's covered anyway, and a Fire TV stick needs all its power to decode the share.
function pauseBehind(on) {
  const root = document.getElementById("root");
  if (root) root.style.display = on ? "none" : "";
}

/** Show a share session over whatever the screen shows. Returns the command result. */
export function showShare(s) {
  teardown();
  if (!s?.session || !["screen", "url", "image"].includes(s.kind)) throw new Error("bad share");
  showing = { session: s.session, code: s.code };
  const el = layer();
  const who = s.from ? `Shared by ${escHtml(s.from)}` : "Shared to this screen";
  const chip = `<div class="xs-chip">${who}</div>`;
  if (s.kind === "url") {
    if (!/^https?:\/\//.test(s.url || "")) throw new Error("bad link");
    el.innerHTML = `<iframe src="${escHtml(s.url)}" allow="autoplay; fullscreen" referrerpolicy="no-referrer"></iframe>${chip}`;
  } else if (s.kind === "image") {
    if (!/^\/api\/share\//.test(s.url || "")) throw new Error("bad image");
    el.innerHTML = `<img src="${escHtml(s.url)}" alt="" />${chip}`;
  } else {
    el.innerHTML = `<video autoplay playsinline muted></video><div class="xs-wait">Connecting to ${escHtml(s.from || "the presenter")}…</div>${chip}`;
    watchScreen(s.session, el.querySelector("video"), el.querySelector(".xs-wait"));
  }
  el.className = "show";
  pauseBehind(true);
  return "showing";
}

/** Stop showing a share (a specific session, or whatever is showing). */
export function stopShare(session) {
  if (showing && (!session || session === showing.session)) teardown();
  return "stopped";
}

function watchScreen(session, video, wait) {
  const channel = `share-${session}`;
  const me = `tv-${Math.random().toString(36).slice(2, 10)}`;
  const send = orderedSender(channel, me);
  const state = showing;
  const t0 = Date.now();
  let gotOffer = 0;
  const live = () => state.pc?.connectionState === "connected";
  // Ask the presenter for a stream. Its page may still be connecting to Xos, so retry quickly at
  // first instead of waiting for the next 4-second tick.
  const join = async () => {
    if (showing !== state || live() || Date.now() - gotOffer < 8000) return;
    const res = await signal(channel, { from: me, to: "presenter", type: "join" }).catch(() => null);
    if (!res?.ok && showing === state && Date.now() - t0 < 15000) setTimeout(join, 400);
  };
  // The presenter closed the tab or lost the network: give it a moment, then clear the screen.
  const presenterGone = () => {
    if (showing !== state) return;
    wait.style.display = ""; wait.textContent = "The presenter disconnected…"; gotOffer = 0;
    clearTimeout(state.gone);
    state.gone = setTimeout(() => {
      if (showing !== state || live()) return;
      fetch(`/api/share/${state.code}/stop`, json({ session })).catch(() => {});
      teardown();
    }, 12000);
  };
  state.es = listen(channel, me, async (m) => {
    if (showing !== state) return;
    if (m.type === "offer") {
      gotOffer = Date.now();
      clearTimeout(state.gone);
      state.pc?.close();
      const pc = await peerConnection();
      state.pc = pc;
      pc.ontrack = (e) => {
        lowLatency(e.receiver);
        if (video.srcObject !== e.streams[0]) video.srcObject = e.streams[0];
        if (e.track.kind === "video") wait.style.display = "none";
        video.muted = false; // the Xos TV app allows sound without a tap; browsers may not
        video.play().catch(() => { video.muted = true; video.play().catch(() => {}); });
      };
      pc.onicecandidate = (e) => e.candidate && send("presenter", "ice", e.candidate.toJSON());
      pc.onconnectionstatechange = () => {
        if (state.pc !== pc) return;
        if (pc.connectionState === "connected") { clearTimeout(state.gone); if (video.srcObject) wait.style.display = "none"; }
        else if (pc.connectionState === "failed") presenterGone();
        // A Wi-Fi blip often recovers by itself within a few seconds.
        else if (pc.connectionState === "disconnected") setTimeout(() => { if (state.pc === pc && !live()) presenterGone(); }, 3000);
      };
      await pc.setRemote(m.data);
      await pc.setLocalDescription(await pc.createAnswer());
      send("presenter", "answer", pc.localDescription.toJSON());
    } else if (m.type === "ice") {
      await state.pc?.addRemoteCandidate(m.data);
    } else if (m.type === "bye" && m.from === "presenter") {
      presenterGone();
    }
  });
  setTimeout(join, 150);
  state.timer = setInterval(join, 4000);
  state.gone = setTimeout(() => { if (!live()) presenterGone(); }, 30000); // never connected
}
