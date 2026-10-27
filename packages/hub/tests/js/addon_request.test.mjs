// FB15 (protocol 8.8b): the add-on request prompt. Signable only for exactly the six fields, on the workspace log, against the
// action this page sent; words come from this page, never from the host.
import test from "node:test";
import assert from "node:assert/strict";
import { describe, checkBindings, addonTermsOk } from "../../src/orch_hub/web/render.js";

const D = new Uint8Array(32);
const TERMS = { name: "wiki", version: "1.2.0", package_sha256: "sha256:" + "a".repeat(64), capabilities: ["network", "serve_http"], handoff: "hr_01J9ZP0000000000000000HND1" };
const ev = (over = {}) => ({ auth: "hub", type: "addon.requested", ...TERMS, ...over });
const d = (e, log = "workspace") => describe({ log, event: e }, D, { workspaceId: "w", action: { type: "addon.requested", log: "workspace", ...TERMS } });

test("the add-on request is signable on the workspace log and says what it allows in this page's words", () => {
  const r = d(ev());
  assert.equal(r.signable, true);
  assert.match(r.sentence, /wiki 1\.2\.0/);
  const text = JSON.stringify(r.facts);
  assert.match(text, /Use the network/);
  assert.match(text, /sha256:a{64}/);
});

test("not signable: a ticket log, an unknown capability, unsorted capabilities, an extra field, a bad runtime", () => {
  assert.equal(d(ev(), "01J9ZP0000000000000000TKT1").signable, false);
  assert.equal(d(ev({ capabilities: ["root"] })).signable, false);
  assert.equal(d(ev({ capabilities: ["serve_http", "network"] })).signable, false);
  assert.equal(d(ev({ fields: {} })).signable, false);
  assert.equal(d(ev({ runtime: { kind: "python-requirements", lock: "r.txt", packages: 0 } })).signable, false);
  assert.equal(d(ev({ runtime: { kind: "python-requirements", lock: "r.txt", packages: 2 } })).signable, true);
});

test("the terms parser refuses anything but the six fields", () => {
  assert.equal(addonTermsOk(TERMS), true);
  assert.equal(addonTermsOk({ ...TERMS, extra: 1 }), false);
  assert.equal(addonTermsOk({ ...TERMS, name: "Wiki" }), false);
  assert.equal(addonTermsOk({ ...TERMS, version: "1.2" }), false);
  assert.equal(addonTermsOk({ ...TERMS, handoff: "x" }), false);
});

test("bindings: the signed terms must equal the action this page sent", () => {
  const me = { channelId: "c".repeat(32), keyPub: "k", userId: "u".repeat(32), hubId: "h".repeat(32) };
  const full = (e) => ({ workspace_id: "w", log: "workspace", event: { ...e, channel: me.channelId, channel_factor: "password+totp", hub_id: me.hubId, actor: { id: "u_" + me.userId, via: me.channelId }, channel_cert: { o: { channel_id: me.channelId, key_pub: "k", user_id: me.userId, factor: "password+totp" } } } });
  const sent = { workspaceId: "w", action: { type: "addon.requested", log: "workspace", ...TERMS } };
  checkBindings(full(ev()), me, sent);
  assert.throws(() => checkBindings(full(ev({ capabilities: ["network"] })), me, sent));
  assert.throws(() => checkBindings(full(ev({ package_sha256: "sha256:" + "b".repeat(64) })), me, sent));
});

test("the sign prompt heading says what is being approved", async () => {
  const src = await (await import("node:fs/promises")).readFile(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  assert.match(src, /d\.type === "addon\.requested" \? "Approve an add-on" : d\.type === "settings\.changed" \? "Approve a settings change" : "Review your answer"/);
});
