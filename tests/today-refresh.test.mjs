import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {runInNewContext} from "node:vm";

// Today calls refreshCalendars on every render and a poke renders again, so a poke with nothing
// to read would loop forever and freeze Admin.
test("Today repaints only after reading a calendar, never when nothing is due", async () => {
  let reads = 0, pokes = 0;
  const context = {
    S: {poke() { pokes++; }},
    userFetch: async () => { reads++; return {json: async () => ({configured: true, calendar: "room", bookings: []})}; },
    localStorage: {getItem: () => null},
    document: {addEventListener() {}},
  };
  const source = readFileSync(new URL("../app/js/views/today.js", import.meta.url), "utf8")
    .replace(/^import .*;$/gm, "").replace(/export function /g, "function ");
  const refresh = runInNewContext(source + "\nrefreshCalendars", context);
  const rooms = [{id: "atrium"}];
  await refresh(rooms);
  assert.equal(reads, 1);
  assert.equal(pokes, 1);
  await refresh(rooms);
  await refresh([]);
  assert.equal(reads, 1);
  assert.equal(pokes, 1);
});
