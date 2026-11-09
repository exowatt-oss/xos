import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const source = readFileSync(new URL("../app/js/setup-guide.js", import.meta.url), "utf8");
const { createSetupGuide } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
function readyForm(guide, kind = "tv", method = "pair") {
  guide.start(kind);
  guide.state.step = 2;
  guide.state.method = method;
  Object.assign(guide.state.fields, { name: "Kitchen TV", room: "kitchen", code: "123 456", ip: "198.51.100.11" });
}
test("validates before creating anything, retains fields on server rejection", async () => {
  const calls = [];
  const guide = createSetupGuide({ request: async (...args) => { calls.push(args); throw new Error("Code expired. Check the TV."); } });
  readyForm(guide);
  guide.state.fields.code = "123";
  await guide.connect();
  assert.equal(calls.length, 0);
  assert.match(guide.state.error, /six-digit/);
  guide.state.fields.code = "123 456";
  await guide.connect();
  assert.equal(calls[0][2].code, "123456");
  assert.equal(calls[0][2].use, "screen");
  assert.match(guide.state.error, /Code expired/);
  assert.equal(guide.state.fields.name, "Kitchen TV");
  assert.equal(guide.state.step, 2);
});
test("blocks double submits and waits for the exact screen to show its new content", async () => {
  let finish, posts = 0;
  let devices = [];
  const guide = createSetupGuide({ request: async (method) => {
    if (method === "POST") { posts++; return new Promise((resolve) => { finish = resolve; }); }
    return devices;
  } });
  readyForm(guide);
  const pending = guide.connect();
  await guide.connect();
  assert.equal(posts, 1);
  finish({ id: "screen1" });
  await pending;
  await guide.connect();
  assert.equal(posts, 1);
  devices = [{ id: "other", online: true, role: "page", arg: "welcome", room: "kitchen" }];
  await guide.check();
  assert.equal(guide.state.ready, false);
  devices = [{ id: "screen1", online: true, role: "setup", room: "kitchen" }];
  await guide.check();
  assert.equal(guide.state.ready, false);
  devices[0] = { ...devices[0], role: "page", arg: "welcome", connected: false };
  await guide.check();
  assert.equal(guide.state.ready, false, "heartbeat alone is not a command connection");
  devices[0].connected = true;
  await guide.check();
  assert.equal(guide.state.ready, true);
});
test("room computer needs a room and a connected meeting-agent check-in", async () => {
  let payload, devices = [];
  const guide = createSetupGuide({ request: async (method, path, body) => {
    if (method === "POST") { payload = body; return { id: "room1", becoming_room_computer: true }; }
    return devices;
  } });
  readyForm(guide, "room");
  guide.state.fields.room = "";
  await guide.connect();
  assert.equal(payload, undefined);
  guide.state.fields.room = "kitchen";
  await guide.connect();
  assert.equal(payload.use, "room");
  devices = [{ id: "room1", role: "roompc", room: "kitchen", online: true, connected: false }];
  await guide.check();
  assert.equal(guide.state.ready, false);
  devices[0].connected = true;
  await guide.check();
  assert.equal(guide.state.ready, true);
});
test("TV install follows its own job, assigns room once, and waits for heartbeat", async () => {
  let job = { id: "job1", state: "running", step: "Installing" };
  let devices = [], assignments = 0;
  const guide = createSetupGuide({ request: async (method, path, body) => {
    if (method === "POST") return job;
    if (path === "/api/provision") return [{ id: "unrelated", state: "done", device: "wrong" }, job];
    if (method === "PUT") { assignments++; assert.deepEqual(body, { room: "kitchen" }); return {}; }
    return devices;
  } });
  readyForm(guide, "tv", "ip");
  await guide.connect();
  await guide.check();
  assert.equal(guide.state.ready, false);
  assert.equal(assignments, 0);
  job = { ...job, state: "done", device: "tv1" };
  await guide.check();
  await guide.check();
  assert.equal(assignments, 1);
  assert.equal(guide.state.ready, false);
  devices = [{ id: "tv1", role: "page", arg: "welcome", online: true, connected: true, room: "kitchen" }];
  await guide.check();
  assert.equal(guide.state.ready, true);
});
test("a failed install reports its actual error without retrying the mutation", async () => {
  let posts = 0;
  const guide = createSetupGuide({ request: async (method) => {
    if (method === "POST") { posts++; return { id: "failed" }; }
    return [{ id: "failed", state: "failed", error: "TV cannot be reached. Check its network." }];
  } });
  readyForm(guide, "tv", "ip");
  await guide.connect();
  await guide.check();
  await guide.check();
  assert.equal(posts, 1);
  assert.match(guide.state.checkError, /cannot be reached/);
  assert.equal(guide.state.ready, false);
});
test("an install connection is created once and is not ready until the computer joins", async () => {
  let posts = 0;
  const guide = createSetupGuide({ request: async (method, path, body) => {
    if (method === "POST") {
      posts++; assert.equal(path, "/api/appliances"); assert.equal(body.role, "room");
      return { device_id: "newpc", install: "installer command" };
    }
    return [];
  } });
  readyForm(guide, "room", "install");
  await guide.connect();
  await guide.connect();
  await guide.check();
  assert.equal(posts, 1);
  assert.equal(guide.state.appliance.install, "installer command");
  assert.equal(guide.state.deviceId, "newpc");
  assert.equal(guide.state.ready, false);
});
test("starting a different guide ignores an old in-flight response", async () => {
  let finish;
  const guide = createSetupGuide({ request: () => new Promise((resolve) => { finish = resolve; }) });
  readyForm(guide);
  const pending = guide.connect();
  guide.start("room");
  finish({ id: "old" });
  await pending;
  assert.equal(guide.state.kind, "room");
  assert.equal(guide.state.step, 1);
  assert.equal(guide.state.deviceId, "");
});

test("an abandoned readiness check cannot block or unlock checks for the next setup", async () => {
  const reads = [];
  let posts = 0;
  const guide = createSetupGuide({ request: async (method) => {
    if (method === "POST") return { id: `screen${++posts}` };
    return new Promise(resolve => reads.push(resolve));
  } });
  readyForm(guide);
  await guide.connect();
  const oldCheck = guide.check();
  readyForm(guide);
  await guide.connect();
  const currentCheck = guide.check();
  assert.equal(reads.length, 2, "the new setup checks without waiting for the old request");
  const ready = id => ({ id, online: true, connected: true, role: "page", arg: "welcome", room: "kitchen" });
  reads[0]([ready("screen1")]);
  await oldCheck;
  assert.equal(guide.state.ready, false, "an old response cannot complete the current setup");
  await guide.check();
  assert.equal(reads.length, 2, "an old completion cannot unlock a duplicate current check");
  reads[1]([ready("screen2")]);
  await currentCheck;
  assert.equal(guide.state.ready, true);
  assert.equal(posts, 2, "readiness checks do not repeat setup mutations");
});
