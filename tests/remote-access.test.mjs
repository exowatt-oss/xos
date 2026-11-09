import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../app/js/api.js", import.meta.url), "utf8");
let sequence = 0;
async function api({ blocked = false, kiosk = true } = {}) {
  const values = new Map();
  globalThis.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { if (blocked) throw Error("disabled"); values.set(key, value); },
  };
  globalThis.location = { search: kiosk ? "?kiosk=1" : "", hash: "#/tablet/atrium", pathname: "/app/" };
  globalThis.fetch = async () => { throw Error("unexpected request"); };
  return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}#${sequence++}`);
}

test("enrollment key is saved before any request and survives a lost reply", async () => {
  const a = await api();
  const key = a.prepareDeviceKey("tablet-test");
  assert.match(key, /^[a-f0-9]{48}$/);
  assert.deepEqual(a.deviceKey(), { id: "tablet-test", key });
  globalThis.fetch = async () => { assert.equal(a.deviceKey().key, key); throw Error("lost reply"); };
  await assert.rejects(a.controlFetch("/api/test"), /lost reply/);
  assert.equal(a.prepareDeviceKey("tablet-test"), key);
});

test("disabled storage cannot consume an enrollment key or send an unauthenticated command", async () => {
  const a = await api({ blocked: true });
  assert.equal(a.prepareDeviceKey("tablet-test"), "");
  await assert.rejects(a.controlFetch("/api/test"), /Allow website storage/);
});

test("missing kiosk key gives recovery instructions instead of a Google sign-in request", async () => {
  const a = await api();
  await assert.rejects(a.controlFetch("/api/test"), /Reset remote access/);
});

test("revoked key gives recovery instructions and still uses the device-authenticated API", async () => {
  const a = await api();
  const key = a.prepareDeviceKey("tablet-test");
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/test");
    assert.equal(options.headers["X-Xos-Device"], "tablet-test");
    assert.equal(options.headers["X-Xos-Key"], key);
    return { status: 401 };
  };
  await assert.rejects(a.controlFetch("/api/test"), /Reset remote access/);
});

test("person previews keep their existing sign-in gate", async () => {
  const a = await api({ kiosk: false });
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/u/api/test");
    assert.equal(options.redirect, "manual");
    return { type: "opaqueredirect", status: 0 };
  };
  await assert.rejects(a.controlFetch("/api/test"), (err) => err.signedOut === true);
});
