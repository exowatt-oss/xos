// WebRTC helpers shared by screen sharing (views/share.js) and camera feeds (views/camera.js).
//
// Video goes peer to peer; Xos only relays the setup messages (POST /api/rtc/<channel>/signal,
// Server-Sent Events from /api/rtc/<channel>/peer/<me>). These helpers make that fast and
// reliable:
// - ICE servers come from the server (XOS_ICE_SERVERS), so a STUN or TURN server can be added
//   for screens on another subnet without changing the app.
// - Messages from one peer are sent strictly in order. Separate POSTs can otherwise overtake each
//   other, and an ICE candidate that arrives before its offer is lost.
// - Candidates that arrive before the remote description is set are held, then added.
// - H.264 is preferred: laptops encode it in hardware and Fire TV sticks decode it in hardware,
//   which keeps latency low and the stick's CPU free.

let config = null;
/** RTCPeerConnection configuration, fetched once per page. */
export function rtcConfig() {
  config ||= fetch("/api/rtc/config", { cache: "no-store" }).then((r) => (r.ok ? r.json() : {}))
    .then((c) => ({ iceServers: c.iceServers || [] }))
    .catch(() => ({ iceServers: [] }));
  return config;
}

export const signal = (channel, msg) => fetch(`/api/rtc/${encodeURIComponent(channel)}/signal`, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(msg),
});

/** A send function for one peer that delivers its messages in order. */
export function orderedSender(channel, from) {
  let chain = Promise.resolve();
  return (to, type, data) => {
    chain = chain.then(() => signal(channel, { from, to, type, data })).catch(() => {});
    return chain;
  };
}

/** Listen for this peer's signalling messages. Handlers run one at a time, in arrival order. */
export function listen(channel, me, onMessage) {
  const es = new EventSource(`/api/rtc/${encodeURIComponent(channel)}/peer/${encodeURIComponent(me)}`);
  let chain = Promise.resolve();
  es.onmessage = (e) => {
    const m = JSON.parse(e.data);
    chain = chain.then(() => onMessage(m)).catch((err) => console.warn("rtc", err));
  };
  return es;
}

/** A peer connection that holds ICE candidates until it has a remote description. */
export async function peerConnection() {
  const pc = new RTCPeerConnection(await rtcConfig());
  const held = [];
  pc.addRemoteCandidate = (c) => {
    if (pc.remoteDescription) return pc.addIceCandidate(c).catch(() => {});
    held.push(c);
    return Promise.resolve();
  };
  pc.setRemote = async (desc) => {
    await pc.setRemoteDescription(desc);
    for (const c of held.splice(0)) await pc.addIceCandidate(c).catch(() => {});
  };
  return pc;
}

/** Order the video codecs H.264 (constrained baseline first), VP8, then the rest. */
export function preferH264(pc) {
  const caps = window.RTCRtpSender?.getCapabilities?.("video");
  if (!caps) return;
  const rank = (c) => {
    if (/h264/i.test(c.mimeType)) return /42e01f/i.test(c.sdpFmtpLine || "") ? 0 : 1;
    if (/vp8/i.test(c.mimeType)) return 2;
    if (/rtx|red|ulpfec|flexfec/i.test(c.mimeType)) return 4;
    return 3;
  };
  const codecs = [...caps.codecs].sort((a, b) => rank(a) - rank(b));
  for (const t of pc.getTransceivers()) {
    if (t.sender.track?.kind === "video" && t.setCodecPreferences) {
      try { t.setCodecPreferences(codecs); } catch (_) { /* keep the browser's order */ }
    }
  }
}

/** Ask the receiver to play frames as soon as they arrive (no extra buffering). */
export function lowLatency(receiver) {
  try { if ("playoutDelayHint" in receiver) receiver.playoutDelayHint = 0; } catch (_) { /* unsupported */ }
}

/** Live numbers for an outgoing video: resolution, frame rate, bitrate, round trip and codec. */
export async function sendStats(pc, prev = {}) {
  const report = await pc.getStats();
  const out = { width: 0, height: 0, fps: 0, mbps: 0, rttMs: null, codec: "", bytes: 0, at: performance.now() };
  let codecId = "";
  report.forEach((s) => {
    if (s.type === "outbound-rtp" && s.kind === "video") {
      out.width = s.frameWidth || 0; out.height = s.frameHeight || 0; out.fps = Math.round(s.framesPerSecond || 0);
      out.bytes += s.bytesSent || 0; codecId = s.codecId || codecId;
    } else if (s.type === "candidate-pair" && s.nominated && s.state === "succeeded" && s.currentRoundTripTime != null) {
      out.rttMs = Math.round(s.currentRoundTripTime * 1000);
    }
  });
  if (codecId) out.codec = (report.get(codecId)?.mimeType || "").replace(/^video\//, "");
  if (prev.at && out.bytes >= prev.bytes) out.mbps = ((out.bytes - prev.bytes) * 8) / ((out.at - prev.at) * 1000);
  return out;
}
