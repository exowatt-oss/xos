// Live camera (#/camera) and live feed (#/feed/<camera>).
//
// #/camera runs on the computer a Logitech Rally Bar is plugged into (USB mode): it picks the Rally
// Bar's camera, mic and speakers, shows the picture with a mic meter, speaker test and pan/tilt/zoom,
// and serves the feed. #/feed/<camera> on any other Xos screen (TV, tablet, laptop) shows it live.
// Video goes peer to peer over the office network (WebRTC); Xos only relays the setup messages.
//
// These views build their DOM once and are never repainted by main.js (their markup is constant),
// so the <video> keeps playing.

import * as S from "../state.js";

const RALLY = /rally|logi/i;
const post = (channel, msg) => fetch(`/api/rtc/${encodeURIComponent(channel)}/signal`, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(msg),
});

function openPeer(channel, peer, onMessage) {
  const es = new EventSource(`/api/rtc/${encodeURIComponent(channel)}/peer/${encodeURIComponent(peer)}`);
  es.onmessage = (e) => onMessage(JSON.parse(e.data));
  return es;
}

// ======================= camera host (#/camera) =======================

let cam = null; // { stream, peers: Map, es, channel }

export function renderCamera(channel) {
  setTimeout(() => mountCamera(channel), 0);
  return `<div class="cam" id="cam-root"></div>`;
}

// Pan/tilt/zoom needs its own browser permission, so it's only requested when someone taps
// "Enable pan/tilt/zoom" (asking up front would leave the camera waiting on that prompt).
let wantPtz = false;

// ?res=720 for hosts without a hardware video encoder (Raspberry Pi 5); 1080 otherwise.
const RES = new URLSearchParams(location.search).get("res") === "720" ? [1280, 720] : [1920, 1080];

async function pickStream(videoId, audioId) {
  const want = (id) => (id ? { deviceId: { exact: id } } : {});
  return navigator.mediaDevices.getUserMedia({
    video: { ...want(videoId), width: { ideal: RES[0] }, height: { ideal: RES[1] }, ...(wantPtz ? { pan: true, tilt: true, zoom: true } : {}) },
    audio: { ...want(audioId), echoCancellation: true, noiseSuppression: true },
  });
}

async function mountCamera(channel) {
  const root = document.getElementById("cam-root");
  if (!root || root.dataset.mounted) return;
  root.dataset.mounted = "1";
  root.innerHTML = `
    <video id="cam-video" autoplay playsinline muted></video>
    <div class="cam-bar">
      <div class="cam-title"><b>Live camera</b> <span id="cam-src">starting…</span></div>
      <div class="cam-meter"><i id="cam-level"></i></div>
      <span id="cam-viewers" class="cam-pill">0 watching</span>
    </div>
    <div class="cam-panel">
      <label>Camera <select id="cam-video-sel"></select></label>
      <label>Microphone <select id="cam-audio-sel"></select></label>
      <label>Speaker <select id="cam-out-sel"></select></label>
      <button id="cam-tone" class="cam-btn">Test speaker</button>
      <span id="cam-ptz" class="cam-ptz"></span>
      <span class="cam-hint">Other screens: Admin → Screens → Control → Show → <b>Live feed</b></span>
    </div>`;
  const $ = (id) => document.getElementById(id);
  if (!navigator.mediaDevices?.getUserMedia) {
    $("cam-src").textContent = "This browser can't use cameras here (needs https or localhost)";
    return;
  }
  cam = { stream: null, peers: new Map(), es: null, channel };

  async function start(videoId, audioId) {
    cam.stream?.getTracks().forEach((t) => t.stop());
    try {
      cam.stream = await pickStream(videoId, audioId);
    } catch (err) {
      $("cam-src").textContent = `Camera blocked or missing: ${err.message}`;
      return;
    }
    $("cam-video").srcObject = cam.stream;
    await fillDevices();
    // Prefer the Rally Bar the first time, once labels are visible.
    if (!videoId) {
      const devs = await navigator.mediaDevices.enumerateDevices();
      const rv = devs.find((d) => d.kind === "videoinput" && RALLY.test(d.label));
      const ra = devs.find((d) => d.kind === "audioinput" && RALLY.test(d.label));
      const cur = cam.stream.getVideoTracks()[0]?.getSettings().deviceId;
      if (rv && rv.deviceId !== cur) return start(rv.deviceId, ra?.deviceId);
    }
    const v = cam.stream.getVideoTracks()[0];
    const s = v?.getSettings() || {};
    $("cam-src").textContent = `${v?.label || "camera"} · ${s.width || "?"}×${s.height || "?"}${RALLY.test(v?.label || "") ? " · Rally Bar" : ""}`;
    meter();
    ptz(v);
    // New tracks for viewers already watching.
    for (const pc of cam.peers.values()) {
      for (const sender of pc.getSenders()) {
        const t = cam.stream.getTracks().find((x) => x.kind === sender.track?.kind);
        if (t) sender.replaceTrack(t);
      }
    }
  }

  async function fillDevices() {
    const devs = await navigator.mediaDevices.enumerateDevices();
    const cur = { videoinput: cam.stream.getVideoTracks()[0]?.getSettings().deviceId, audioinput: cam.stream.getAudioTracks()[0]?.getSettings().deviceId };
    const opts = (kind, sel) => devs.filter((d) => d.kind === kind)
      .map((d) => `<option value="${d.deviceId}" ${d.deviceId === sel ? "selected" : ""}>${d.label || kind}</option>`).join("");
    $("cam-video-sel").innerHTML = opts("videoinput", cur.videoinput);
    $("cam-audio-sel").innerHTML = opts("audioinput", cur.audioinput);
    const outs = devs.filter((d) => d.kind === "audiooutput");
    const rallyOut = outs.find((d) => RALLY.test(d.label));
    $("cam-out-sel").innerHTML = outs.map((d) => `<option value="${d.deviceId}" ${d === rallyOut ? "selected" : ""}>${d.label || "speaker"}</option>`).join("") || `<option>Default</option>`;
  }

  let meterCtx = null;
  function meter() {
    meterCtx?.close();
    const track = cam.stream.getAudioTracks()[0];
    if (!track) return;
    meterCtx = new AudioContext();
    const an = meterCtx.createAnalyser();
    an.fftSize = 512;
    meterCtx.createMediaStreamSource(new MediaStream([track])).connect(an);
    const buf = new Uint8Array(an.fftSize);
    const tick = () => {
      if (!document.getElementById("cam-level")) return meterCtx.close();
      an.getByteTimeDomainData(buf);
      let peak = 0;
      for (const b of buf) peak = Math.max(peak, Math.abs(b - 128));
      $("cam-level").style.width = `${Math.min(100, peak * 1.6)}%`;
      requestAnimationFrame(tick);
    };
    tick();
  }

  function ptz(track) {
    const caps = track?.getCapabilities?.() || {};
    const keys = ["pan", "tilt", "zoom"].filter((k) => caps[k]);
    $("cam-ptz").innerHTML = keys.length ? keys.map((k) =>
      `<label>${k} <input type="range" data-ptz="${k}" min="${caps[k].min}" max="${caps[k].max}" step="${caps[k].step || 1}" value="${track.getSettings()[k] ?? caps[k].min}"></label>`).join("")
      : wantPtz ? `<span class="cam-hint">No pan/tilt/zoom from this camera</span>`
      : `<button id="cam-ptz-on" class="cam-btn">Enable pan/tilt/zoom</button>`;
    const on = document.getElementById("cam-ptz-on");
    if (on) on.onclick = () => { wantPtz = true; start($("cam-video-sel").value, $("cam-audio-sel").value); };
    $("cam-ptz").oninput = (e) => {
      const k = e.target.dataset.ptz;
      if (k) track.applyConstraints({ advanced: [{ [k]: Number(e.target.value) }] }).catch(() => {});
    };
  }

  $("cam-video-sel").onchange = () => start($("cam-video-sel").value, $("cam-audio-sel").value);
  $("cam-audio-sel").onchange = () => start($("cam-video-sel").value, $("cam-audio-sel").value);
  $("cam-tone").onclick = async () => {
    const ctx = new AudioContext();
    if (ctx.setSinkId && $("cam-out-sel").value) await ctx.setSinkId($("cam-out-sel").value).catch(() => {});
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    gain.gain.value = 0.15;
    osc.frequency.value = 660;
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    setTimeout(() => { osc.stop(); ctx.close(); }, 900);
  };

  // Serve the feed: one peer connection per viewer.
  function viewers() { $("cam-viewers").textContent = `${cam.peers.size} watching`; }
  cam.es = openPeer(channel, "camera", async (m) => {
    if (m.type === "join") {
      cam.peers.get(m.from)?.close();
      const pc = new RTCPeerConnection();
      cam.peers.set(m.from, pc);
      viewers();
      cam.stream?.getTracks().forEach((t) => pc.addTrack(t, cam.stream));
      // Office network: keep the picture sharp rather than dropping resolution.
      for (const sender of pc.getSenders().filter((x) => x.track?.kind === "video")) {
        const params = sender.getParameters();
        params.degradationPreference = "maintain-resolution";
        params.encodings = (params.encodings?.length ? params.encodings : [{}]).map((e) => ({ ...e, maxBitrate: 6_000_000 }));
        sender.setParameters(params).catch(() => {});
      }
      pc.onicecandidate = (e) => e.candidate && post(channel, { from: "camera", to: m.from, type: "ice", data: e.candidate.toJSON() });
      pc.onconnectionstatechange = () => {
        if (["failed", "closed"].includes(pc.connectionState)) { pc.close(); cam.peers.delete(m.from); viewers(); }
      };
      await pc.setLocalDescription(await pc.createOffer());
      post(channel, { from: "camera", to: m.from, type: "offer", data: pc.localDescription.toJSON() });
    } else if (m.type === "answer") {
      await cam.peers.get(m.from)?.setRemoteDescription(m.data);
    } else if (m.type === "ice") {
      await cam.peers.get(m.from)?.addIceCandidate(m.data).catch(() => {});
    } else if (m.type === "bye") {
      cam.peers.get(m.from)?.close();
      cam.peers.delete(m.from);
      viewers();
    }
  });

  await start();
}

// ======================= viewer (#/feed/<camera>) =======================

export function renderFeed(channel) {
  setTimeout(() => mountFeed(channel), 0);
  return `<div class="cam cam-feed" id="feed-root"></div>`;
}

function mountFeed(channel) {
  const root = document.getElementById("feed-root");
  if (!root || root.dataset.mounted) return;
  root.dataset.mounted = "1";
  root.innerHTML = `
    <video id="feed-video" autoplay playsinline muted></video>
    <div class="cam-wait" id="feed-wait">Waiting for the camera…</div>
    <button id="feed-sound" class="cam-btn cam-sound">🔈 Sound on</button>`;
  const me = `v-${Math.random().toString(36).slice(2, 10)}`;
  const video = document.getElementById("feed-video");
  const wait = document.getElementById("feed-wait");
  let pc = null;
  let gotOffer = 0;

  document.getElementById("feed-sound").onclick = (e) => {
    video.muted = !video.muted;
    e.target.textContent = video.muted ? "🔈 Sound on" : "🔇 Mute";
  };

  function join() {
    if (Date.now() - gotOffer < 8000) return;
    post(channel, { from: me, to: "camera", type: "join" }).then((r) => {
      wait.textContent = r.ok ? "Connecting…" : "Waiting for the camera to come online…";
    }).catch(() => {});
  }

  openPeer(channel, me, async (m) => {
    if (m.type === "offer") {
      gotOffer = Date.now();
      pc?.close();
      pc = new RTCPeerConnection();
      pc.ontrack = (e) => {
        video.srcObject = e.streams[0];
        wait.style.display = "none";
        video.play().catch(() => {});
      };
      pc.onicecandidate = (e) => e.candidate && post(channel, { from: me, to: "camera", type: "ice", data: e.candidate.toJSON() });
      pc.onconnectionstatechange = () => {
        if (["failed", "disconnected", "closed"].includes(pc.connectionState)) {
          wait.style.display = ""; wait.textContent = "Reconnecting…"; gotOffer = 0;
        }
      };
      await pc.setRemoteDescription(m.data);
      await pc.setLocalDescription(await pc.createAnswer());
      post(channel, { from: me, to: "camera", type: "answer", data: pc.localDescription.toJSON() });
    } else if (m.type === "ice") {
      await pc?.addIceCandidate(m.data).catch(() => {});
    } else if (m.type === "bye" && m.from === "camera") {
      wait.style.display = ""; wait.textContent = "Camera went offline. Waiting…"; gotOffer = 0;
    }
  });
  setTimeout(join, 800);
  setInterval(join, 5000);
}

/** For heartbeats and Admin: is this page the camera host? */
export const isCameraHost = () => Boolean(cam?.stream) && S.isHub();
