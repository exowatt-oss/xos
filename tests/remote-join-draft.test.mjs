import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../app/js/views/remote.js", import.meta.url), "utf8")
  .replace(/^import .*;$/gm, "")
  .replace(/^export /gm, "");

function remote() {
  const listeners = {};
  const context = vm.createContext({
    document: { addEventListener: (type, fn) => { listeners[type] = fn; } },
    window: { addEventListener() {} },
    navigator: {},
    S: { rooms: () => [] },
    members: () => [],
    setInterval() {},
    esc: value => String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
  });
  vm.runInContext(source, context);
  return {
    input(id, value) { listeners.input({ target: { id, value } }); },
    render() { return vm.runInContext('joinSheet("test-room")', context); },
    action(act, sheet = "") {
      context.el = { dataset: { sheet } };
      return vm.runInContext(`handleRemote(${JSON.stringify(act)}, el)`, context);
    },
  };
}

test("typed passcode survives redraws and join-option toggles, but resets for a new join", () => {
  const r = remote();
  r.action("rm-sheet", "join");
  r.input("rm-join", "123456789");
  r.input("rm-pass", 'a&b"<');
  for (let i = 0; i < 3; i++) {
    r.action("rm-join-options");
    assert.match(r.render(), /id="rm-join"[^>]*value="123456789"/);
    assert.match(r.render(), /id="rm-pass"[^>]*value="a&amp;b&quot;&lt;"/);
  }
  r.action("rm-sheet", "");
  r.action("rm-sheet", "join");
  assert.match(r.render(), /id="rm-pass"[^>]*value=""/);
});
