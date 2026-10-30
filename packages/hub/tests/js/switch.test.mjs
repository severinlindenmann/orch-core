import test from "node:test";
import assert from "node:assert/strict";
import { colour, initials, landing, landingPlan, openTarget, railRows, safeReturn, safeUrl, ticketFromHash } from "../../src/orch_hub/web/switch.js";

test("ticketFromHash takes only a well formed #t= fragment", () => {
  const ok = "a".repeat(32) + "." + "b".repeat(32);
  assert.equal(ticketFromHash("#t=" + ok), ok);
  for (const bad of ["", "#setup=abc", "#t=", "#t=short", "#t=" + ok + "&x=1", "#t=a b c d e f g h", "t=" + ok, "#t=<script>aaaaaaaa"]) assert.equal(ticketFromHash(bad), null, bad);
});

test("safeReturn allows only the hub's own paths", () => {
  assert.equal(safeReturn("/"), "/");
  assert.equal(safeReturn("/c"), "/c");
  assert.equal(safeReturn("/phones"), "/phones");
  for (const bad of ["//evil.example", "https://evil.example", "/\\evil", "/../x", "/admin", "", null, 5, "/a\nb"]) assert.equal(safeReturn(bad), null, String(bad));
});

test("landing: home, path, signout, expired", () => {
  assert.deepEqual(landing(200, { user_id: "u", return_to: "/", kind: "open" }), { kind: "home" });
  assert.deepEqual(landing(200, { user_id: "u", return_to: "/c", kind: "open" }), { kind: "path", path: "/c" });
  assert.deepEqual(landing(200, { user_id: "u", return_to: "/phones", kind: "open" }), { kind: "path", path: "/#/phones" });
  assert.deepEqual(landing(200, { user_id: "u", return_to: "//evil", kind: "open" }), { kind: "home" });
  assert.deepEqual(landing(200, { user_id: null, return_to: "/", kind: "signout" }), { kind: "signout" });
  assert.deepEqual(landing(400, { error: "bad_ticket" }), { kind: "expired" });
  assert.deepEqual(landing(0, { error: "unavailable" }), { kind: "expired" });
});

test("railRows keeps the closed shape and marks workspaces without an origin", () => {
  const rows = railRows({ workspaces: [
    { workspace_id: "a".repeat(32), name: "Product", role: "owner", online: true, badge: 3, origin: "http://x:1" },
    { workspace_id: "b".repeat(32), name: null, online: false, badge: -2, origin: null },
    { nope: 1 },
  ] });
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0], { id: "a".repeat(32), name: "Product", role: "owner", online: true, badge: 3, canOpen: true });
  assert.equal(rows[1].name, "A workspace");
  assert.equal(railRows({ workspaces: [{ workspace_id: "c".repeat(32), name: null, owner_name: "Ben Test" }] })[0].name, "Ben Test's workspace");
  assert.equal(rows[1].badge, 0);
  assert.equal(rows[1].canOpen, false);
  assert.deepEqual(railRows(null), []);
});

test("initials, colour and safeUrl", () => {
  assert.equal(initials("Acme hub"), "AH");
  assert.equal(initials("Product"), "PR");
  assert.equal(initials("", "ff00"), "W"); // never a piece of an id
  assert.equal(initials("Ben Test's workspace"), "BT");
  assert.match(colour("x"), /^c[0-3]$/);
  assert.equal(colour("same"), colour("same"));
  assert.equal(safeUrl("http://127.0.0.1:5/switch#t=abc"), "http://127.0.0.1:5/switch#t=abc");
  assert.equal(safeUrl("javascript:alert(1)"), null);
  assert.equal(safeUrl("nonsense"), null);
});

test("landingPlan never signs in silently", () => {
  assert.equal(landingPlan({ kind: "signout", user_id: "u", signed_in_as: null }), "redeem");
  assert.equal(landingPlan({ kind: "open", user_id: "u", signed_in_as: { user_id: "u" } }), "redeem");
  assert.equal(landingPlan({ kind: "open", user_id: "u", signed_in_as: null }), "confirm");
  assert.equal(landingPlan({ kind: "open", user_id: "u", signed_in_as: { user_id: "v" } }), "conflict");
  assert.equal(landingPlan(null), "confirm");
  assert.equal(landingPlan({ kind: "open" }), "confirm");
});

test("openTarget matches only /open/<32 hex> and landing forwards it, never as a path", () => {
  const id = "ab".repeat(16);
  assert.equal(openTarget("/open/" + id), id);
  for (const bad of ["/open/" + id + "/x", "/open/ABC", "/open/" + id.slice(1), "//open/" + id, "/x/open/" + id, null]) assert.equal(openTarget(bad), null, String(bad));
  assert.deepEqual(landing(200, { user_id: "u", kind: "open", return_to: "/open/" + id }), { kind: "forward", workspace_id: id });
  assert.equal(safeReturn("/open/" + id), null);
});

test("openingState names the workspace, with a plain fallback", async () => {
  const { openingState } = await import("../../src/orch_hub/web/switch.js");
  assert.equal(openingState("Product").title, "Opening Product…");
  assert.equal(openingState(null).title, "Opening your workspace…");
});

test("the hub shows the Opening state before it asks for a ticket or navigates", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  const body = src.slice(src.indexOf("async function openWorkspace"));
  const fn = body.slice(0, body.indexOf("\n}\n"));
  const shown = fn.indexOf("openingState(");
  assert.ok(shown >= 0);
  assert.ok(shown < fn.indexOf('"/switch/ticket"'));
  assert.ok(shown < fn.indexOf("location.assign"));
  const css = readFileSync(new URL("../../src/orch_hub/web/app.css", import.meta.url), "utf8");
  assert.match(css, /prefers-reduced-motion: reduce\) \{ \.opening span \{ animation:none/);
});
