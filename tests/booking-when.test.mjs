import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
process.env.TZ = "America/Denver";
const source = readFileSync(new URL("../app/js/ui.js", import.meta.url), "utf8");
const { bookingWhen, untilWhen } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const at = (iso) => Date.parse(iso) / 1000;
const now = at("2026-11-03T12:15:00-06:00"); // Tuesday noon in HQ
const time = (iso) => new Date(Date.parse(iso)).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

test("a booking today is just its times", () => {
  const b = { start: at("2026-11-03T14:00:00-06:00"), end: at("2026-11-03T15:00:00-06:00") };
  assert.equal(bookingWhen(b, now), `${time("2026-11-03T14:00:00-06:00")} – ${time("2026-11-03T15:00:00-06:00")}`);
});

test("tomorrow's booking says Tomorrow", () => {
  const b = { start: at("2026-11-04T09:00:00-06:00"), end: at("2026-11-04T09:30:00-06:00") };
  assert.match(bookingWhen(b, now), /^Tomorrow · /);
});

test("a week-long booking that has started says when it ends, never Tomorrow", () => {
  // The Atrium calendar's "Optics Team in HQ": Sat Oct 31 11 PM to Fri Nov 6 11 PM.
  const b = { start: at("2026-10-31T23:00:00-05:00"), end: at("2026-11-06T23:00:00-06:00") };
  const said = bookingWhen(b, now);
  assert.match(said, /^Until Fri, Nov 6 · /);
  assert.doesNotMatch(said, /Tomorrow/);
  assert.match(untilWhen(b.end, now), /^until Fri, Nov 6 · /);
  assert.equal(untilWhen(at("2026-11-03T15:00:00-06:00"), now), `until ${time("2026-11-03T15:00:00-06:00")}`);
});
