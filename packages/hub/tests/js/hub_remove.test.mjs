// #1463: the Danger zone card ("Remove hub") of the Hub settings page. One sheet, the step-up fields, one red button,
// no typed word; the digest is computed here from the returned context and never taken from the hub.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { body: new Node_("body"), createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { removeCard, removeActions } = await import("../../src/orch_hub/web/pages/hub-settings.js");
const { hubRemoveDigest, signProof, LABEL_PROOF } = await import("../../src/orch_hub/web/channel.js");
const { hex, unb64u, utf8 } = await import("../../src/orch_hub/web/cj.js");

const vectors = JSON.parse(readFileSync(new URL("../../../../contracts/hub/vectors_hub_closing.json", import.meta.url), "utf8"));
const PV = { state: "open", workspaces: 3, relay: true, publish: false, backup_key_ok: true, backup_folder_hint: "orch-hub-backup-...", cancel_allowed: false, refusal: null };
const btn = (root, label) => root.all().find((n) => n.tag === "button" && n.textContent === label);
const inputs = (root) => root.all().filter((n) => n.tag === "input" || n.tag === "textarea" || n.tag === "select");
const FAST = { pause: 1, goneMs: 5 };
const tick = () => new Promise((r) => setTimeout(r, 15));

test("digest_cases of the closing vectors: byte equality with the contract", async () => {
  assert.equal(vectors.digest_cases.length, 5);
  for (const c of vectors.digest_cases) assert.equal(hex(await hubRemoveDigest(c.ctx)), c.digest_hex, c.name);
});

test("the sheet: plain facts, step-up fields only, ONE red button, no typed word, no product name", () => {
  const node = removeCard({ preview: PV }, {});
  btn(node, "Remove hub").listeners.click();
  assert.match(node.textContent, /3 workspaces are connected/);
  assert.match(node.textContent, /24 backup words/);
  assert.match(node.textContent, /relay and publish service on this server are removed too/);
  assert.equal(inputs(node).length, 2);
  assert.deepEqual(inputs(node).map((i) => i.attrs.type ?? "text"), ["password", "text"]);
  assert.equal(node.all().filter((n) => n.tag === "button" && /danger/.test(n.className)).length, 1);
  assert.ok(btn(node, "Remove this hub"));
  assert.doesNotMatch(node.textContent, /NetBird|WireGuard|Tailscale|VPN/i);
});

test("no backup key: the words line and no red button", () => {
  const node = removeCard({ preview: { ...PV, backup_key_ok: false } }, {});
  btn(node, "Remove hub").listeners.click();
  assert.match(node.textContent, /Set up the backup words first/);
  assert.equal(btn(node, "Remove this hub"), undefined);
  assert.equal(inputs(node).length, 0);
});

async function rig({ prepare, confirm, statuses = [] } = {}) {
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  const calls = [];
  const ctx = { contract: 1, exp_ms: 1790000300000, hub_id: "38d8abed52ccfb538da401fd54e64bdc", kind: "orch.hub_remove.v1", nonce: "e57f9658d7cf82ff6094d29922cce814", user_id: "a368bd380588e5dabee62a516031e21f" };
  const good = { ctx, digest: vectors.digest_cases[0].digest_hex, nonce: ctx.nonce, exp_ms: ctx.exp_ms };
  let i = 0, reauth = 0;
  const d = { state: { me: { pair, channelId: "c1", userId: ctx.user_id, hubId: ctx.hub_id } }, removePoll: FAST, reauthView: () => { reauth++; },
    api: async (m, p, b) => {
      calls.push([m, p, b]);
      if (p.endsWith("/prepare")) return prepare ?? { status: 200, body: good };
      if (p.endsWith("/confirm")) return confirm ?? { status: 202, body: {} };
      if (p.endsWith("/status")) { const s = statuses[Math.min(i++, statuses.length - 1)]; if (s === "down") throw new Error("down"); return s ? { status: 200, body: s } : { status: 502, body: {} }; }
      return { status: 200, body: {} };
    } };
  const node = removeCard({ preview: PV }, removeActions(d));
  btn(node, "Remove hub").listeners.click();
  const [pw, code] = inputs(node);
  pw.value = "pw"; code.value = "123456";
  return { node, d, pair, calls, good, ctx, go: () => btn(node, "Remove this hub").listeners.click(), reauthCount: () => reauth };
}

test("flow: prepare with channel_id, digest computed locally, confirm body keys exactly {nonce, proof, password, totp}, proof verifies", async () => {
  const r = await rig({ statuses: ["down"] });
  r.good.digest = hex(await hubRemoveDigest(r.ctx));
  await r.go();
  const [prep, conf] = r.calls;
  assert.deepEqual(prep, ["POST", "/admin/hub/remove/prepare", { channel_id: "c1" }]);
  assert.equal(conf[1], "/admin/hub/remove/confirm");
  assert.deepEqual(Object.keys(conf[2]).sort(), ["nonce", "password", "proof", "totp"]);
  assert.equal(conf[2].password, "pw"); assert.equal(conf[2].totp, "123456");
  // the proof is a signature over LABEL_PROOF || the locally computed digest
  const dg = await hubRemoveDigest(r.ctx);
  const msg = new Uint8Array([...utf8(LABEL_PROOF), ...dg]);
  // verify with the public half: re-sign check via a second key is impossible, so verify using the exported pair.publicKey
  assert.ok(await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, r.pair.publicKey, unb64u(conf[2].proof), msg));
});

test("a digest from the hub that does not match the context is refused: nothing is signed or confirmed", async () => {
  const r = await rig();
  r.good.digest = "00".repeat(32);
  await r.go();
  assert.equal(r.calls.length, 1);
  assert.match(r.node.textContent, /nothing was signed/);
});

test("a nonce that differs from the one in the context is refused", async () => {
  const r = await rig();
  r.good.digest = hex(await hubRemoveDigest(r.ctx));
  r.good.nonce = "ff".repeat(16);
  await r.go();
  assert.equal(r.calls.length, 1);
});

test("reauth_required sends the admin to the sign-in again", async () => {
  const r = await rig({ prepare: { status: 403, body: { error: "reauth_required" } } });
  await r.go();
  assert.equal(r.reauthCount(), 1);
});

test("refusals are said in words", async () => {
  for (const [code, re] of [["hub_closing", /already being removed/], ["bad_credentials", /password or the code/], ["no_backup_key", /backup words/], ["nonce_used", /already used/], ["factor_too_weak", /Sign in again/]]) {
    const r = await rig({ confirm: { status: 409, body: { error: code } } });
    r.good.digest = hex(await hubRemoveDigest(r.ctx));
    await r.go();
    assert.match(r.node.textContent, re, code);
  }
  const r = await rig({ confirm: { status: 409, body: { error: "weird_code", plain: "Disk is full." } } });
  r.good.digest = hex(await hubRemoveDigest(r.ctx));
  await r.go();
  assert.match(r.node.textContent, /Disk is full/);
});

test("202: 'Removing' with step words, then 'The hub is gone' when the page stops answering", async () => {
  const r = await rig({ statuses: [{ state: "closing", plain: "Stopping changes", told: 0, total: 3, cancel_allowed: true, result: null }, "down"] });
  r.good.digest = hex(await hubRemoveDigest(r.ctx));
  const seen = [];
  const t = setInterval(() => seen.push(r.node.textContent), 1);
  await r.go();
  clearInterval(t);
  assert.ok(seen.some((s) => /Removing\. This page will stop answering|Stopping changes/.test(s)));
  assert.match(r.node.textContent, /The hub is gone\./);
});

test("Cancel only while cancel_allowed; stalled offers 'Finish removing'", async () => {
  const st = { state: "backed_up", plain: "Last backup made", told: 0, total: 3, cancel_allowed: true, result: null };
  let n = 0;
  const node = removeCard({ preview: { ...PV, state: "backed_up" } }, { poll: FAST, status: async () => (n++ < 40 ? st : null), cancel: async () => ({ ok: true }) });
  await new Promise((r) => setTimeout(r, 4));
  assert.ok(node.all().find((n) => n.attrs["data-act"] === "cancel"));
  const stalled = removeCard({ preview: { ...PV, state: "stalled" } }, { poll: FAST, status: async () => ({ ...st, state: "stalled", cancel_allowed: false, plain: "Stopped" }) });
  await tick();
  assert.equal(stalled.all().find((n) => n.attrs["data-act"] === "cancel"), undefined);
  assert.ok(btn(stalled, "Finish removing"));
  assert.equal(inputs(stalled).length, 2);
});

test("F6: a context for another hub or another person is never signed", async () => {
  for (const over of [{ hub_id: "00".repeat(16) }, { user_id: "11".repeat(16) }]) {
    const base = await rig();
    const ctx = { ...base.ctx, ...over };
    const r = await rig({ prepare: { status: 200, body: { ctx, digest: hex(await hubRemoveDigest(ctx)), nonce: ctx.nonce, exp_ms: ctx.exp_ms } } });
    await r.go();
    assert.match(r.node.textContent, /nothing was signed/);
    assert.ok(!r.calls.some((c) => c[1].endsWith("/confirm")));
  }
});
