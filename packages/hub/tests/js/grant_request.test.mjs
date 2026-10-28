import test from "node:test";
import assert from "node:assert/strict";
import { describe, checkBindings, grantTermsOk, grantSentence } from "../../src/orch_hub/web/render.js";
import { parseRoute } from "../../src/orch_hub/web/route.js";
const TERMS = { scope: "workable", verbs: "agent", hours: 8, handoff: "01J9ZP00000000000000000001" };
const ev = (over = {}) => ({ auth: "hub", type: "grant.requested", ...TERMS, ...over });
const d = (e, log = "workspace") => describe({ log, event: e }, new Uint8Array(32));

test("access route and strict terms, including real strings", () => {
  const workspace = "a".repeat(32);
  assert.deepEqual(parseRoute(`#/w/${workspace}/access/abc`), { kind: "access", workspace, key: "abc" });
  assert.equal(grantTermsOk(TERMS), true);
  assert.equal(grantTermsOk({ ...TERMS, label: "My agent" }), true);
  for (const over of [{ scope: ["all"] }, { verbs: ["agent"] }, { hours: "8" }, { hours: 0 }, { hours: 169 }, { hours: 1.5 }, { label: ["label"] }, { label: "a\nb" }, { label: "x".repeat(201) }, { handoff: [TERMS.handoff] }, { handoff: TERMS.handoff + "\n" }, { handoff: "hr_" + TERMS.handoff }, { secret_hash: "x" }]) {
    assert.equal(grantTermsOk({ ...TERMS, ...over }), false, JSON.stringify(over));
  }
  for (const key of ["scope", "verbs", "hours", "handoff"]) {
    const t = { ...TERMS }; delete t[key]; assert.equal(grantTermsOk(t), false);
  }
});

test("grant review is signable only with known fields on the workspace log", () => {
  assert.equal(d(ev()).signable, true);
  assert.match(grantSentence(TERMS, "Work"), /Let an agent work for you in Work: only tickets it can work on, for 8 hours/);
  assert.match(d(ev({ scope: "all" })).sentence, /all tickets/);
  assert.equal(d(ev(), "ticket").signable, false);
  assert.equal(d(ev({ extra: true })).signable, false);
  assert.equal(d(ev({ label: {} })).signable, false);
  assert.ok(d(ev()).fields.some(([k, v]) => k === "handoff" && v === TERMS.handoff));
});

test("every signed term must match the page, including label presence", () => {
  const me = { channelId: "c".repeat(32), keyPub: "k", userId: "u".repeat(32), hubId: "h".repeat(32) };
  const full = (e) => ({ workspace_id: "w", log: "workspace", event: { ...e, channel: me.channelId, channel_factor: "password+totp", hub_id: me.hubId, actor: { id: "u_" + me.userId, via: me.channelId }, channel_cert: { o: { channel_id: me.channelId, key_pub: "k", user_id: me.userId, factor: "password+totp" } } } });
  const sent = { workspaceId: "w", action: { type: "grant.requested", log: "workspace", ...TERMS } };
  checkBindings(full(ev()), me, sent);
  for (const over of [{ scope: "all" }, { verbs: ["log"] }, { hours: 9 }, { label: "added" }, { handoff: "01J9ZP00000000000000000002" }]) assert.throws(() => checkBindings(full(ev(over)), me, sent));
  assert.throws(() => checkBindings(full(ev()), me, { ...sent, action: { ...sent.action, label: "removed" } }));
});

test("screen requires membership and edit role and uses the review flow", async () => {
  const src = await (await import("node:fs/promises")).readFile(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  const screen = src.slice(src.indexOf("async function accessView"), src.indexOf("async function addonView"));
  assert.match(screen, /parseStrict/);
  assert.match(screen, /!mine \|\| !\["edit", "admin"\]\.includes\(mine\.role\)/);
  assert.match(screen, /startSign\(wsId, action\)/);
  assert.match(screen, /Approve agent access/);
  assert.match(src, /d\.type === "grant\.requested" \? "Approve agent access"/);
});
