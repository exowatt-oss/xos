import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { setImmediate as tick } from "node:timers/promises";
const source = readFileSync(new URL("../app/js/keep-awake.js", import.meta.url), "utf8");
let seq = 0;
async function setup({ visible = true, search = "?kiosk=1", hash = "#/tablet/atrium", request } = {}) {
  const locks = [];
  let calls = 0;
  globalThis.document = Object.assign(new EventTarget(), { visibilityState: visible ? "visible" : "hidden" });
  globalThis.window = new EventTarget();
  globalThis.location = { search, hash };
  const makeLock = () => {
    const lock = Object.assign(new EventTarget(), { releases: 0 });
    lock.release = async () => { lock.releases++; lock.dispatchEvent(new Event("release")); };
    locks.push(lock);
    return lock;
  };
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: {
    wakeLock: { request: async (kind) => { calls++; assert.equal(kind, "screen"); return request ? request(makeLock, calls) : makeLock(); } },
  } });
  const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}#${seq++}`);
  const stop = module.keepRoomRemoteAwake();
  await tick();
  return { locks, stop, state: module.screenWakeState, calls: () => calls, visibility: async (value) => {
    document.visibilityState = value;
    document.dispatchEvent(new Event("visibilitychange"));
    await tick();
  } };
}

test("visible tablet acquires one lock and releases it on shutdown", async () => {
  const s = await setup();
  document.dispatchEvent(new Event("pointerdown"));
  await tick();
  assert.equal(s.calls(), 1);
  assert.equal(s.state(), "held");
  s.stop();
  assert.equal(s.locks[0].releases, 1);
});

test("wake lock is reacquired after returning from the background", async () => {
  const s = await setup({ visible: false });
  assert.equal(s.calls(), 0);
  assert.equal(s.state(), "background");
  await s.visibility("visible");
  assert.equal(s.calls(), 1);
  await s.visibility("hidden");
  assert.equal(s.locks[0].releases, 1);
  await s.visibility("visible");
  assert.equal(s.calls(), 2);
  location.hash = "#/admin/screens";
  window.dispatchEvent(new Event("hashchange"));
  assert.equal(s.locks[1].releases, 1);
  s.stop();
});

test("a lock granted after the tablet is hidden is released immediately", async () => {
  let grant;
  const s = await setup({ request: (make) => new Promise((resolve) => { grant = () => resolve(make()); }) });
  await s.visibility("hidden");
  grant();
  await tick();
  assert.equal(s.locks[0].releases, 1);
  s.stop();
});

test("OS refusal does not loop; the next touch may retry", async () => {
  const s = await setup({ request: (make, calls) => { if (calls === 1) throw Error("low power"); return make(); } });
  await tick();
  assert.equal(s.calls(), 1);
  assert.equal(s.state(), "denied");
  document.dispatchEvent(new Event("pointerdown"));
  await tick();
  assert.equal(s.calls(), 2);
  s.stop();
});

test("ordinary previews and display pages do not request a wake lock", async () => {
  for (const options of [{ search: "" }, { hash: "#/page/atrium-room" }]) {
    const s = await setup(options);
    assert.equal(s.calls(), 0);
    s.stop();
  }
});
