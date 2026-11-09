import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const source = readFileSync(new URL("../app/js/dashboard.js", import.meta.url), "utf8");
const { createDashboardCapture } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const tv = { id: "tv", online: true, connected: true, client: "firetv-1.0", native: { displayOn: true } };

test("captures connected awake Fire TVs only, and pauses outside the visible dashboard", async () => {
  const ids = [];
  const capture = createDashboardCapture({ request: async (id) => { ids.push(id); return { ok: true }; }, changed() {} });
  const devices = [tv, { ...tv, id: "offline", online: false }, { ...tv, id: "sleep", native: { displayOn: false } },
    { ...tv, id: "disconnected", connected: false }, { ...tv, id: "browser", client: "browser" }];
  await capture.tick(devices, false);
  assert.deepEqual(ids, []);
  await capture.tick(devices, true);
  assert.deepEqual(ids, ["tv"]);
});

test("limits each TV to one request per ten seconds and never overlaps requests", async () => {
  let clock = 1000, calls = 0, finish;
  const capture = createDashboardCapture({ now: () => clock, changed() {}, request: () => {
    calls++;
    return new Promise((resolve) => { finish = resolve; });
  } });
  const pending = capture.tick([tv], true);
  clock += 20000;
  await capture.tick([tv], true);
  assert.equal(calls, 1);
  finish({ ok: true });
  await pending;
  clock += 10000;
  const next = capture.tick([tv], true);
  assert.equal(calls, 2);
  finish({ ok: true });
  await next;
  clock += 9999;
  await capture.tick([tv], true);
  assert.equal(calls, 2);
});

test("reports HTTP and network failures and retries at the next capture interval", async () => {
  let clock = 0, mode = "http", changes = 0;
  const capture = createDashboardCapture({ now: () => clock, changed() { changes++; }, request: async () => {
    if (mode === "network") throw new Error("offline");
    return { ok: mode === "ok" };
  } });
  await capture.tick([tv], true);
  assert.match(capture.error(tv.id), /Couldn't request/);
  mode = "network";
  clock += 10000;
  await capture.tick([tv], true);
  assert.match(capture.error(tv.id), /Couldn't request/);
  mode = "ok";
  clock += 10000;
  await capture.tick([tv], true);
  assert.equal(capture.error(tv.id), "");
  assert.equal(changes, 3);
});

test("waits for an uploaded image, or a bounded upload deadline, before recapturing", async () => {
  let clock = 0, calls = 0;
  const device = { ...tv, screenshot: { at: 100 } };
  const capture = createDashboardCapture({ now: () => clock, changed() {}, request: async () => {
    calls++;
    return { ok: true };
  } });
  await capture.tick([device], true);
  clock = 10000;
  await capture.tick([device], true);
  assert.equal(calls, 1, "queued is not uploaded");
  device.screenshot.at = 101;
  await capture.tick([device], true);
  assert.equal(calls, 2, "new image completes the capture");
  clock = 39999;
  await capture.tick([device], true);
  assert.equal(calls, 2);
  clock = 40000;
  await capture.tick([device], true);
  assert.equal(calls, 3, "missing upload does not stall forever");
});
