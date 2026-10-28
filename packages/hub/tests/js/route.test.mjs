import test from "node:test";
import assert from "node:assert/strict";
import { parseRoute } from "../../src/orch_hub/web/route.js";
const ws = "ab".repeat(16);
test("sections and workspace deep links", () => {
  for (const kind of ["home", "workspaces", "services", "admin"]) assert.deepEqual(parseRoute("#/" + kind), { kind });
  assert.deepEqual(parseRoute("#/w/" + ws), { kind: "workspace", workspace: ws });
  for (const kind of ["item", "addon", "settings"]) {
    assert.deepEqual(parseRoute(`#/w/${ws}/${kind}/PR-12`), { kind, workspace: ws, key: "PR-12" });
  }
  assert.equal(parseRoute(`#/w/${ws}/item/a%20b`).key, "a b");
});
test("setup and switch tickets keep their existing shapes", () => {
  assert.deepEqual(parseRoute("#setup=abc-def"), { kind: "setup", token: "abc-def" });
  assert.deepEqual(parseRoute("#t=abcdefgh"), { kind: "ticket", token: "abcdefgh" });
});
test("unknown and malformed fragments fall back safely", () => {
  for (const hash of [null, "", "#/nope", "#/w/nope", `#/w/${ws}/item/%`, `#/w/${ws}/item/%00`, `#/w/${ws}/item/a/b`]) assert.deepEqual(parseRoute(hash), { kind: "home" });
});
