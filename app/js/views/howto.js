import * as S from "../state.js";
import { userFetch } from "../api.js";
import { esc } from "../ui.js";
import { createSetupGuide } from "../setup-guide.js";

async function request(method, path, body) {
  const response = await userFetch(path, { method, cache: "no-store", signal: AbortSignal.timeout(20000), headers: { "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}) });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Xos could not complete this step. Check the details and try again.");
  return result;
}
const guide = createSetupGuide({ request, changed: S.poke });
let calendar = null;
let connectionsAsked = false;
let waitingBusy = false;
let nextPairCheck = 0;
const active = () => location.hash === "#/admin/howto";
const button = (action, label, primary = false, attrs = "") => `<button type="${action === "connect" ? "submit" : "button"}" class="btn-sm ${primary ? "btn-primary" : ""}" data-act="guide-${action}" ${attrs}>${label}</button>`;
const link = (href, label) => `<a class="btn-sm" href="${esc(href)}">${label}</a>`;
const docs = (_path, label = "Full setup instructions ↗") => `<a href="https://github.com/exowatt-oss/xos#readme" target="_blank" rel="noopener">${label}</a>`;
const field = (key, label, placeholder, extra = "") => `<label>${label}<input data-guide-field="${key}" value="${esc(guide.state.fields[key])}" placeholder="${placeholder}" ${extra} /></label>`;
const roomSelect = () => `<label>Room ${guide.state.kind === "room" ? "(required)" : "(optional)"}<select data-guide-field="room"><option value="">Choose a room</option>${S.rooms().map((r) => `<option value="${esc(r.id)}" ${r.id === guide.state.fields.room ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></label>`;

async function refreshWaiting() {
  if (waitingBusy) return;
  waitingBusy = true;
  try {
    const waiting = await request("GET", "/api/devices/pairing");
    guide.state.waiting = waiting;
  } catch (_) { /* Manual code entry works even if discovery is temporarily unavailable. */ }
  finally { waitingBusy = false; if (!document.activeElement?.closest(".howto-form")) S.poke(); }
}
setInterval(() => {
  if (!active()) return;
  if (guide.state.step === 3) guide.check();
  if (guide.state.kind && guide.state.step < 3 && Date.now() > nextPairCheck) {
    nextPairCheck = Date.now() + 10000;
    refreshWaiting();
  }
}, 3000);

function connections() {
  if (!connectionsAsked) {
    connectionsAsked = true;
    request("GET", "/api/calendar/status").then((c) => { calendar = c; S.poke(); }).catch(() => { connectionsAsked = false; });
  }
  return `<h2>What would you like to do next?</h2><div class="howto-links">
    <article class="ex-card"><h3>Show a video or presentation</h3><p>Open Content, choose a screen under <b>Show on</b>, then choose <b>Show</b>. For a live presentation, open the screen’s share link.</p>${link("#/admin/content", "Open Content")}</article>
    <article class="ex-card"><h3>Connect room calendars</h3><p>Connect the Google account that can read your room calendars. Then assign each room its calendar in Integrations. Bookings appear on the room remote.</p>
      ${calendar?.oauth_configured ? link("/u/api/calendar/oauth/start", calendar.oauth_connected ? "Reconnect Google Calendar" : "Connect Google Calendar") : link("#/admin/integrations", "Set up Google Calendar")}
      ${calendar?.configured ? `<p class="small muted">Calendar connection configured. Check each room in Integrations.</p>` : ""}</article>
    <article class="ex-card"><h3>Join Meet, Zoom, Teams or Webex</h3><p>Use a room computer, connect its camera and speaker, then open the room remote. Choose a calendar meeting or enter its invitation link. The room joins as a guest.</p>${link("#/admin/rooms", "Open room remotes")} <p class="small">${docs("room-computer/", "Meeting setup guide ↗")}</p></article>
    <article class="ex-card"><h3>Add a tablet remote</h3><p>Choose your room in Rooms and open its remote on the tablet. For a fixed tablet installation, follow the tablet setup guide. In Screens, name the tablet and assign it to that room to enable its controls.</p>${link("#/admin/rooms", "Find your room")} ${link("#/admin/screens", "Set up the tablet")} <p class="small">${docs("setup/#add-a-tablet-remote", "Tablet setup guide ↗")}</p></article>
    <article class="ex-card"><h3>Connect a licensed Zoom Room</h3><p>For a dedicated Zoom Rooms installation, configure the Zoom connection in Integrations, link a room, then get its activation code. This requires a Zoom Rooms license.</p>${link("#/admin/integrations", "Open Integrations")} <p class="small">${docs("zoom-setup/", "Zoom Rooms guide ↗")}</p></article>
    <article class="ex-card"><h3>Something is not connecting</h3><p>Check the device has power, the TV is on the correct HDMI input, and both devices are on the office network. In Screens, check its last check-in and recent commands. “Online” alone does not mean it is receiving commands.</p>${link("#/admin/screens", "Check Screens")}</article>
  </div>`;
}
function landing() {
  return `<section class="howto-hero"><span class="howto-eyebrow">START HERE</span><h1>Get a screen working.</h1><p>Choose your device. We’ll walk you through the cables, the connection and the final check.</p><div class="howto-promise"><span>1 · Get ready</span><span>2 · Connect to Xos</span><span>3 · Check it works</span></div></section>
  <div class="howto-choices">
    <button class="ex-card howto-choice" data-act="guide-start" data-kind="tv"><span class="howto-icon" aria-hidden="true">▣</span><span><b>Add a TV or display</b><span>Fire TV, Android TV, or a screen with a browser.<br />Put videos, slides and signage on it.</span><strong>Set up a display →</strong></span></button>
    <button class="ex-card howto-choice" data-act="guide-start" data-kind="room"><span class="howto-icon" aria-hidden="true">▤</span><span><b>Add a room computer</b><span>A mini PC connected to the room’s TV.<br />Run Meet, Zoom, Teams and Webex with the room remote.</span><strong>Set up a room computer →</strong></span></button>
  </div>${connections()}`;
}
function methodPicker() {
  const room = guide.state.kind === "room";
  const methods = room ? [["pair", "I have a code on the TV", "Recommended · the mini PC already runs Xos"], ["install", "I need to install Xos", "Create a connection for a Debian 13 mini PC"]]
    : [["pair", "I have a code on the screen", "Recommended · Xos is already open"], ["ip", "Install Xos on a Fire TV / Android TV", "Use the TV’s network address"]];
  return `<div class="howto-methods">${methods.map(([id, title, hint]) => `<button class="howto-method ${guide.state.method === id ? "on" : ""}" data-act="guide-method" data-method="${id}" aria-pressed="${guide.state.method === id}"><b>${title}</b><span>${hint}</span></button>`).join("")}</div>`;
}
function prepare() {
  const room = guide.state.kind === "room";
  const method = guide.state.method;
  return `<h2>First, get the device ready.</h2><p class="lead">${room ? "A room computer connects the TV, camera and speaker to the room remote." : "Your screen needs an office network connection and Xos open on it."}</p>${methodPicker()}
    <ol class="howto-checklist"><li><b>Connect the hardware.</b> ${room ? "Connect the mini PC to the TV with HDMI. Connect the Rally Bar or camera and speaker by USB. Select that HDMI input on the TV." : "Turn on the TV and select the HDMI input for your Fire TV or computer."}</li>
    <li><b>Join the office network.</b> Use office Ethernet or Wi-Fi with access to Xos. Guest Wi-Fi may not reach the server.</li>
    <li><b>${method === "ip" ? "Enable TV setup." : method === "install" ? "Prepare the computer." : "Open Xos and find the code."}</b> ${method === "ip" ? "On Fire TV: Settings → My Fire TV → About → select the device name seven times. Go back to Developer options and turn on ADB debugging. Find the IP in About → Network. This works with Android-based Fire TVs, not Vega OS models."
      : method === "install" ? "Use a Debian 13 mini PC, or prepare a Debian 13 installer USB. The next step creates this computer’s one-time setup connection. Installing from USB erases the computer’s disk; an existing Debian installation can use the install command instead."
      : room ? "Boot the mini PC from an Xos USB installer. Installing erases its disk. Once installed, Xos opens on the TV with a six-digit code. Already installed? Use the code it is showing now."
      : `Open the Xos TV app. For a computer browser, open <a href="/app/?kiosk=1#/setup" target="_blank" rel="noopener">the Xos setup screen ↗</a> on that computer. Need the Android app? <a href="/firetv/xos-tv.apk">Download Xos TV</a>. Leave the six-digit code on screen.`}</li></ol>
    <p class="small muted">${room ? docs("linux-appliance/", "USB and mini PC installation guide ↗") : docs("setup/", "Screen setup reference ↗")}</p>
    <div class="howto-actions">${button("next", "Ready — connect this device", true)}${button("reset", "Choose a different device")}</div>`;
}
function connectForm() {
  const g = guide.state;
  const eligible = g.waiting.filter((w) => g.kind !== "room" || w.client?.startsWith("xos-display"));
  return `<form data-guide-form><h2>Give it a name. Make the connection.</h2><p class="lead">${g.method === "ip" ? "Xos will install and open the TV app. Keep the TV remote nearby to accept the first connection." : g.method === "install" ? "Xos will create a one-time setup code and an install link for this computer." : "Enter the code on the device. Xos will name it and connect it to your chosen room."}</p>
    <div class="howto-form">${field("name", "Device name", g.kind === "room" ? "e.g. Atrium room computer" : "e.g. Kitchen TV", 'maxlength="60" autocomplete="off"')}${roomSelect()}
    ${g.method === "pair" ? field("code", "Six-digit code on the device", "123 456", 'inputmode="numeric" maxlength="7" autocomplete="off"') : g.method === "ip" ? field("ip", "TV IP address", "e.g. 198.51.100.11", 'inputmode="decimal" maxlength="40"') : ""}</div>
    <p class="small muted">Room missing? ${link("#/admin/rooms", "Add a room first")} Your details stay here when you return.</p>
    ${g.method === "pair" ? `<div class="howto-discovery"><h3>Devices showing a code</h3>${eligible.length ? eligible.map((w) => `<button type="button" class="btn-sm" data-act="guide-pick" data-code="${esc(w.code)}">${esc(w.code)} · ${esc(w.model || w.client || "Screen")}${w.ip ? ` · ${esc(w.ip)}` : ""}</button>`).join(" ") : `<p class="small muted">No devices found yet. You can still enter the code above.</p>`}${button("discover", "Look again")}</div>` : ""}
    ${g.error ? `<p class="howto-error" role="alert">${esc(g.error)}</p>` : ""}
    <div class="howto-actions">${button("connect", g.busy ? "Connecting…" : g.method === "install" ? "Create this computer’s connection" : "Connect device", true, g.busy ? "disabled" : "")}${button("back", "Back", false, g.busy ? "disabled" : "")}</div></form>`;
}
function verification() {
  const g = guide.state;
  const room = S.room(g.fields.room);
  const remote = room ? `#/tablet/${room.id}` : "#/admin/screens";
  if (g.ready) return `<div class="howto-success" role="status"><span class="howto-icon" aria-hidden="true">✓</span><h2>${esc(g.fields.name)} is connected.</h2><p>Xos has received a live check-in from this ${g.kind === "room" ? "room computer" : "display"}${room ? ` in ${esc(room.name)}` : ""}${g.kind === "tv" ? " showing Welcome" : " with its meeting connection running"}.</p></div>
    <div class="howto-actions">${link(g.kind === "room" ? remote : "#/admin/content", g.kind === "room" ? "Open room remote" : "Choose what to show")}${link("#/admin/screens", "See it in Screens")}${button("reset", "Set up another device")}</div><p class="small muted">${g.kind === "room" ? "On the room tablet, open its room remote. Choose a calendar meeting or enter a meeting link to test the camera and speaker." : "Look at the TV to confirm the correct HDMI input and picture. To play a video, open Content, select this screen under Show on, and choose Show."}</p>`;
  return `<h2>${g.appliance ? "Your connection is ready. Install Xos next." : "Now let’s check the device."}</h2>
    <p class="lead">${g.appliance ? "The computer is registered. It will be ready after installation and its first live check-in." : g.kind === "room" ? "The mini PC is installing its meeting software and restarting. This can take a few minutes. Keep it powered on." : "Keep the TV on. This page checks automatically; it only marks setup complete after the device checks in showing Xos."}</p>
    ${g.appliance ? `<div class="howto-install ex-card"><h3>Install on this mini PC</h3><p>On an existing Debian 13 computer, run:</p><code>${esc(g.appliance.install)}</code>${button("copy", "Copy install command")}
      <p>Or boot a Debian 13 USB, choose <b>Advanced options → Automated install</b> and enter:</p><code>${esc(g.appliance.preseed_url)}</code><p class="small">The USB installation erases the disk. The setup code works once and expires ${esc(new Date(g.appliance.expires * 1000).toLocaleDateString())}. Do not use this link for another computer.</p></div>` : ""}
    <div class="howto-status" role="status" aria-live="polite"><span class="${g.checkError ? "" : "rm-spin"}" aria-hidden="true">${g.checkError ? "!" : ""}</span><span>${esc(g.checkError ? "The connection needs attention. See the details below." : g.job?.state === "running" ? g.job.step || "Installing the TV app…" : g.device?.online ? "Device is online; waiting for its setup to finish…" : "Waiting for the device to check in…")}</span></div>
    ${g.method === "ip" ? `<p>When the TV asks <b>Allow USB debugging?</b>, select <b>Always allow</b>, then <b>Allow</b>.</p>` : ""}
    ${g.checkError ? `<p class="howto-error" role="alert">${esc(g.checkError)}</p>` : ""}
    <details class="howto-trouble"><summary>Taking longer than expected?</summary><p>Check power, network and the TV’s HDMI input. A mini PC may restart during installation. For TV setup by IP, confirm ADB debugging is enabled and the TV accepted the connection. If Xos cannot reach the TV’s IP, use the pairing-code method after opening Xos on it.</p><p>Check Screens before starting again to avoid adding the same computer twice. ${docs("linux-appliance/", "Mini PC installation help ↗")}</p></details>
    <div class="howto-actions">${button("check", "Check again")}${link("#/admin/screens", "Check Screens")}${button("reset", "Start another setup")}</div>`;
}
export function renderHowTo() {
  if (!S.isHub()) return `<h1>How to</h1><p>Open Xos from your server address to connect a device.</p>`;
  const g = guide.state;
  if (!g.kind) return `<div class="howto">${landing()}</div>`;
  return `<div class="howto"><button class="link" data-act="guide-reset">← All guides</button><header class="howto-header"><span class="howto-eyebrow">${g.kind === "room" ? "ROOM COMPUTER" : "TV / DISPLAY"}</span><h1>${g.kind === "room" ? "Set up a room computer" : "Set up a display"}</h1></header>
    <ol class="howto-progress" aria-label="Setup progress">${["Get ready", "Connect to Xos", "Check it works"].map((label, i) => `<li class="${g.step === i + 1 ? "current" : g.step > i + 1 ? "complete" : ""}" ${g.step === i + 1 ? 'aria-current="step"' : ""}><span>${g.step > i + 1 ? "✓" : i + 1}</span>${label}</li>`).join("")}</ol>
    <section class="ex-card howto-step">${g.step === 1 ? prepare() : g.step === 2 ? connectForm() : verification()}</section></div>`;
}
export function handleGuideInput(el) {
  if (!el.dataset.guideField || guide.state.busy || guide.state.step === 3) return false;
  guide.state.fields[el.dataset.guideField] = el.value;
  return true;
}
function focusStep() {
  setTimeout(() => {
    if (!active()) return;
    const heading = document.querySelector(".howto-error") || document.querySelector(".howto-step h2, .howto-hero h1");
    if (heading) { heading.tabIndex = -1; heading.focus(); }
  }, 0);
}
export function handleGuide(act, el) {
  if (!act.startsWith("guide-")) return false;
  const g = guide.state;
  if (g.busy) return true;
  switch (act.slice(6)) {
    case "start": guide.start(el.dataset.kind); refreshWaiting(); break;
    case "reset": guide.start(); break;
    case "method": g.method = el.dataset.method; g.error = ""; break;
    case "next": g.step = 2; refreshWaiting(); break;
    case "back": g.step = 1; g.error = ""; break;
    case "pick": g.fields.code = el.dataset.code; break;
    case "discover": refreshWaiting(); break;
    case "connect": guide.connect().then(() => { guide.check(); focusStep(); }); break;
    case "check": guide.check(); break;
    case "copy": navigator.clipboard?.writeText(g.appliance.install).then(() => { el.textContent = "Copied"; }).catch(() => { el.textContent = "Select and copy the command above"; }); break;
  }
  if (["guide-start", "guide-reset", "guide-next", "guide-back"].includes(act)) focusStep();
  return true;
}
