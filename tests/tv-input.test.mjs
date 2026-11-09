import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const source = readFileSync(new URL("../app/js/tv-input.js", import.meta.url), "utf8");
const { switchInputProblem, tvAppVersion } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const stick = (app, displayControl = true) => ({ client: `firetv-${app}`, native: { app, displayControl } });

test("sticks from Xos TV 0.6.0 with display control can switch the TV to Xos", () => {
  for (const v of ["0.6.0", "0.6.1", "0.10.0", "1.0"]) assert.equal(switchInputProblem(stick(v)), "", v);
});

test("older sticks, no display control, and screens without the app say why not", () => {
  for (const v of ["0.5.0", "0.5.9", "0.1"]) assert.match(switchInputProblem(stick(v)), /0\.6\.0 or later/, v);
  assert.match(switchInputProblem(stick("0.6.0", false)), /display control/);
  assert.match(switchInputProblem({ client: "browser" }), /Xos TV app/);
  assert.match(switchInputProblem({ client: "firetv-0.6.0" }), /Xos TV app/); // never checked in natively
  assert.match(switchInputProblem({ client: "ios-1.2", native: { app: "1.2", displayControl: true } }), /Xos TV app/);
});

test("the version comes from the app, else from the client", () => {
  assert.deepEqual(tvAppVersion({ client: "firetv-0.5.0", native: { app: "0.6.2" } }), [0, 6, 2]);
  assert.deepEqual(tvAppVersion({ client: "firetv-0.6.0", native: {} }), [0, 6, 0]);
  assert.equal(tvAppVersion({ client: "browser" }), null);
});
