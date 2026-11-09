import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
const source = readFileSync(new URL("../os/appliance/xos_room_linux.py", import.meta.url), "utf8");
const webexState = source.match(/WEBEX_STATE_JS = r"""([\s\S]*?)"""/)[1];

// Webex's call buttons as the web app draws them (2026-11): a state sentence for the accessible
// name, the short word as the visible text.
function button(aria, text = "", attrs = {}) {
  const set = {};
  return { aria, text, disabled: false, getClientRects: () => [1],
    getAttribute: (a) => (a === "aria-label" ? aria : a === "title" ? null : attrs[a] ?? set[a] ?? null),
    setAttribute: (a, v) => { set[a] = v; }, removeAttribute() {}, get innerText() { return text; } };
}
function read(buttons, text = "") {
  return JSON.parse(runInNewContext(webexState, {
    document: { body: { innerText: text }, querySelectorAll: (sel) => (sel.startsWith("[data-xos-webex]") ? [] : buttons) },
  }));
}

test("Webex's state sentences say the room is muted with its camera off", () => {
  const s = read([
    button("Microphone is currently muted - click to unmute", "Unmute"),
    button("Sending video is currently disabled - click to enable", "Start video"),
    button("Leave meeting"),
  ]);
  assert.deepEqual(s, { muted: true, camera_off: true, leave: true, waiting: false });
});

test("and unmuted with its camera on, from the sentence even before the word", () => {
  const s = read([
    button("Microphone is currently unmuted - click to mute", "Mute"),
    button("Sending video is currently enabled - click to disable", "Stop video"),
    button("Leave meeting"),
  ]);
  assert.equal(s.muted, false);
  assert.equal(s.camera_off, false);
});

test("a button with only the short word still reads", () => {
  const s = read([button("", "Unmute"), button("", "Stop video")]);
  assert.equal(s.muted, true);
  assert.equal(s.camera_off, false);
});

test("Webex's meeting lobby is waiting for the host", () => {
  const s = read([button("Leave meeting")], "You're in the meeting lobby. Waiting for the host to let you in.");
  assert.equal(s.waiting, true);
});

const cookies = source.match(/WEBEX_COOKIES_JS = r"""([\s\S]*?)"""/)[1];
function page(elements) {
  const doc = { querySelectorAll: () => elements };
  return runInNewContext(cookies, { document: doc });
}
const el = (text, extra = {}) => ({ innerText: text, children: [], shadowRoot: null, tagName: "DIV",
  getAttribute: (a) => extra[a] ?? null, getClientRects: () => [1], clicked: false, click() { this.clicked = true; }, ...extra.self });

test("Webex's cookie banner is declined: it covers the call's Mute button", () => {
  const reject = el("Reject", { self: { tagName: "MDC-BUTTON" } });
  const accept = el("Accept", { self: { tagName: "MDC-BUTTON" } });
  assert.equal(page([el("We use cookies to help secure, provide, and improve Webex services."), reject, accept]), "declined");
  assert.equal(reject.clicked, true);
  assert.equal(accept.clicked, false);
});

test("with no cookie banner nothing is clicked, even a Reject elsewhere", () => {
  const reject = el("Reject", { role: "button" });
  assert.equal(page([el("You're in the meeting lobby."), reject]), "none");
  assert.equal(reject.clicked, false);
});
