// M2-8a: the owner's list of access changes (changes.js) and the admin's waiting list (admin.js Access tab).
// Both build real DOM, so this runs them against a tiny DOM stub (no browser needed).
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  focus() {} select() {}
  get tagName() { return this.tag.toUpperCase(); }
  querySelectorAll() { return this.all().slice(1).filter(n => ["input", "select", "textarea", "button", "summary", "details"].includes(n.tag)); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { changesView, checkChange, sentence, LABEL, KIND } = await import("../../src/orch_hub/web/changes.js");
const { adminView } = await import("../../src/orch_hub/web/admin.js");

const NOW = 1_800_000_000_000;
const WS = "a1".repeat(16), WS2 = "b2".repeat(16);
const grant = {
  id: "01MCC5SH9G1SNTXZSDWC7Q3PF3", kind: "grant", state: "pending", body: { group: "devs", role: "edit", workspace_id: WS },
  group: "devs", role: "edit", person_name: null, proposer: { user_id: "x", name: "Ada" }, created_ms: NOW - 3600_000, expires_ms: NOW + 86400_000, decided_ms: null, decided_by: null, reason: null,
  approvals: [{ workspace_id: WS, workspace_name: "Orchard", owner_name: "Olive", state: "pending", stale: false, delta: [{ role: "edit", user_id: "u1" }], people: [{ role: "edit", user_id: "u1", name: "Dana" }] }],
};
const member = { ...grant, id: "01MCC5SH9GQAHYGG43BXAZ5XYE", kind: "member_add", body: { group: "devs", user_id: "u1" }, role: null, person_name: "Newbie" };
const text = (nodes) => nodes.map((n) => n.textContent).join("\n");
const find = (nodes, pred) => nodes.flatMap((n) => n.all()).filter(pred);
const button = (nodes, label) => find(nodes, (n) => n.tag === "button" && n.textContent === label);

test("the sentences say who wants what in plain words", () => {
  assert.equal(sentence(grant, "Orchard"), "Ada wants to give devs edit access to Orchard");
  assert.equal(sentence(member, "Orchard"), "Ada wants to add Newbie to devs, which can open Orchard");
  assert.equal(sentence({ ...grant, proposer: { name: "A\u202eB" } }, "O"), "A<U+202E>B wants to give devs edit access to O");
});

test("a pending change shows Approve and Reject, which call the actions with the change and the workspace", () => {
  const calls = [];
  const out = changesView({ pending: [grant, member], notices: [] }, { canSign: true, approve: (c, a) => calls.push(["approve", c.id, a.workspace_id]), reject: (c, a) => calls.push(["reject", c.id, a.workspace_id]) }, NOW);
  assert.match(text(out), /Access changes waiting for you \(2\)/);
  assert.match(text(out), /Ada wants to give devs edit access to Orchard/);
  assert.match(text(out), /Ada wants to add Newbie to devs, which can open Orchard/);
  button(out, "Approve")[0].listeners.click();
  button(out, "Reject")[1].listeners.click();
  assert.deepEqual(calls, [["approve", grant.id, WS], ["reject", member.id, WS]]);
});

test("an out-of-date or lapsed change cannot be approved; no browser key disables Approve", () => {
  const stale = { ...grant, approvals: [{ ...grant.approvals[0], stale: true }] };
  let out = changesView({ pending: [stale] }, { canSign: true }, NOW);
  assert.equal(button(out, "Approve").length, 0);
  assert.match(text(out), /can no longer be approved/);
  out = changesView({ pending: [grant] }, { canSign: true }, NOW + 2 * 86400_000);
  assert.equal(button(out, "Approve").length, 0);
  assert.match(text(out), /has lapsed/);
  out = changesView({ pending: [grant] }, { canSign: false }, NOW);
  assert.equal(button(out, "Approve")[0].attrs.disabled, "");
  assert.match(text(out), /Set up this browser/);
  assert.deepEqual(changesView({ pending: [], notices: [] }, {}, NOW), []);
  assert.match(text(changesView({ pending: [grant] }, { canSign: true }, NOW)), /This gives access to: Dana \(edit\)/);
});

test("an override shows up as a notice with the reason", () => {
  const over = { ...grant, state: "overridden", decided_by: "Root", decided_ms: NOW - 1000, reason: "Release tonight" };
  const out = changesView({ pending: [], notices: [over] }, {}, NOW);
  assert.match(text(out), /Applied without your approval/);
  assert.match(text(out), /Root applied this/);
  assert.match(text(out), /Ada wants to give devs edit access to Orchard/);
  assert.match(text(out), /Reason given: Release tonight/);
});

// The digest, built here by hand: H("orch/v2/hub-access-change|" || canonical JSON of the context).
const ctxOf = (c, ws = WS) => ({ contract: 1, kind: KIND, change_id: c.id, workspace_id: ws, body: c.body, base_snapshot_version: 7, expires_ms: c.expires_ms, delta: (c.approvals.find((a) => a.workspace_id === ws) ?? c.approvals[0]).delta });
const canonical = `{"base_snapshot_version":7,"body":{"group":"devs","role":"edit","workspace_id":"${WS}"},"change_id":"${grant.id}","contract":1,"delta":[{"role":"edit","user_id":"u1"}],"expires_ms":${grant.expires_ms},"kind":"orch.access_change.v1","workspace_id":"${WS}"}`;
const digestHex = (s) => createHash("sha256").update(LABEL + s).digest("hex");

test("checkChange recomputes the digest and refuses anything that is not the listed change", async () => {
  assert.equal(LABEL, "orch/v2/hub-access-change|");
  const ctx = ctxOf(grant), good = { ctx, digest: digestHex(canonical) };
  const d = await checkChange(good, grant, WS);
  assert.equal(Buffer.from(d).toString("hex"), good.digest);
  const refused = async (prepared, change, ws, code) => assert.rejects(checkChange(prepared, change, ws), { message: code });
  await refused({ ctx, digest: "00".repeat(32) }, grant, WS, "event_changed"); // the hub's digest is not ours
  await refused(good, { ...grant, body: { ...grant.body, role: "admin" } }, WS, "event_changed"); // not the change shown
  await refused(good, grant, WS2, "event_changed"); // another workspace
  await refused(good, { ...grant, id: member.id }, WS, "event_changed");
  await refused(good, { ...grant, expires_ms: grant.expires_ms + 1 }, WS, "event_changed");
  await refused({ ctx: { ...ctx, kind: "orch.other.v1" }, digest: good.digest }, grant, WS, "event_changed");
  await refused({ ctx: { ...ctx, extra: 1 }, digest: good.digest }, grant, WS, "malformed");
  await refused({ ctx: null, digest: good.digest }, grant, WS, "malformed");
  // the people shown are the people signed for: a different delta in the context is refused
  await refused({ ctx: { ...ctx, delta: [{ role: "edit", user_id: "u2" }] }, digest: good.digest }, grant, WS, "event_changed");
  await refused({ ctx: { ...ctx, base_snapshot_version: 1.5 }, digest: "x" }, grant, WS, "malformed");
});

// --- the admin's Access tab ---------------------------------------------------------------------------------------------

const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
function admin(handler) {
  const calls = [];
  const state = { changes: [grant] };
  const bodies = () => ({
    users: { users: [{ user_id: "u1", name: "Newbie" }] }, groups: { groups: [{ name: "devs", members: [] }, { name: "hub-admin", members: [] }] }, grants: { grants: [] },
    workspaces: { workspaces: [{ workspace_id: WS, name: "Orchard", mode: "full", status: "bound" }] }, channels: { channels: [] },
    "market-policy": { version: 1, official_url: "https://x.test/o", policy: { markets: [], addons: { allow: null, block: [] }, ceiling: null } }, audit: { records: [] },
    changes: { changes: state.changes },
  });
  const d = { say: (c) => "say:" + c, refresh: async () => {}, api: async (method, path, body) => {
    calls.push([method, path, body]);
    if (method === "GET") return { status: 200, body: bodies()[path.replace("/admin/", "").split("?")[0]] };
    return handler(method, path, body, state);
  } };
  const view = adminView(d);
  return { view, calls, state, message: view[1], panel: (i = 1) => view[3].children[i] };
}

test("a 202 says it was sent to the owner, and the change waits with Cancel and Override", async () => {
  const a = admin((m, p, b, s) => ({ status: 202, body: { change_id: grant.id, waiting_for: [{ workspace_id: WS, owner_name: "Olive" }] } }));
  await settle();
  const waiting = () => a.panel().all().find((n) => n.attrs.id === "waiting-changes");
  assert.ok(waiting());
  assert.match(waiting().textContent, /Waiting for approval/);
  assert.match(waiting().textContent, /Ada wants to give devs edit access to Orchard/);
  assert.match(waiting().textContent, /Waiting for Olive/);
  assert.ok(button([waiting()], "Cancel").length === 1 && button([waiting()], "Override").length === 1);
  const form = a.panel().all().find((n) => n.tag === "form" && n.textContent.includes("Grant access"));
  form.listeners.submit({ preventDefault() {} });
  await settle();
  assert.match(a.message.textContent, /Sent to Olive for approval/);
  assert.doesNotMatch(a.message.textContent, /Saved/);
});

test("Override needs a reason, tells who will be informed, and sends the reason", async () => {
  const a = admin((m, p, b, s) => { s.changes = []; return { status: 200, body: { ok: true } }; });
  await settle();
  const waiting = () => a.panel().all().find((n) => n.attrs.id === "waiting-changes");
  assert.equal(button([waiting()], "Apply without approval").length, 0);
  button([waiting()], "Override")[0].listeners.click();
  assert.match(waiting().textContent, /Olive will see that this was applied without their approval\./);
  const apply = button([waiting()], "Apply without approval")[0];
  assert.equal(apply.attrs.disabled, "");
  const reason = waiting().all().find((n) => n.attrs.id === "override-reason");
  reason.listeners.input({ target: { value: "  Release tonight " } });
  assert.equal(apply.attrs.disabled, "", "the stub keeps the attribute; the property below is what the browser uses");
  assert.equal(apply.disabled, false);
  apply.listeners.click();
  await settle();
  assert.deepEqual(a.calls.filter(([m]) => m === "POST"), [["POST", "/admin/changes/" + grant.id + "/override", { reason: "Release tonight" }]]);
  assert.match(a.message.textContent, /Applied without approval/);
  assert.equal(a.panel().all().find((n) => n.attrs.id === "waiting-changes"), undefined);
});

test("Cancel withdraws the waiting change", async () => {
  const a = admin((m, p, b, s) => { s.changes = []; return { status: 200, body: { ok: true } }; });
  await settle();
  button([a.panel().all().find((n) => n.attrs.id === "waiting-changes")], "Cancel")[0].listeners.click();
  await settle();
  assert.deepEqual(a.calls.filter(([m]) => m === "POST"), [["POST", "/admin/changes/" + grant.id + "/cancel", {}]]);
});

test("last_admin is a plain warning and only a confirm sends the change again; owner_protected is only refused", async () => {
  let n = 0;
  const a = admin((m, p, b) => (++n === 1 ? { status: 409, body: { error: "last_admin" } } : { status: 200, body: { ok: true } }));
  await settle();
  const revoke = () => a.panel(0).all().find((x) => x.tag === "button" && x.textContent === "Remove person");
  const btn = revoke();
  btn.listeners.click(); // arms
  btn.listeners.click(); // sends
  await settle();
  assert.match(a.message.textContent, /nobody who can administer it/);
  assert.equal(a.calls.filter(([m]) => m !== "GET").length, 1);
  button([a.message], "Do it anyway")[0].listeners.click();
  await settle();
  assert.deepEqual(a.calls.filter(([m]) => m !== "GET").map(([m, p, b]) => [m, p, b]), [["DELETE", "/admin/users/u1", undefined], ["DELETE", "/admin/users/u1", { confirm: true }]]);

  const b = admin(() => ({ status: 409, body: { error: "owner_protected" } }));
  await settle();
  const rm = b.panel(0).all().find((x) => x.tag === "button" && x.textContent === "Remove person");
  rm.listeners.click(); rm.listeners.click();
  await settle();
  assert.equal(b.message.textContent, "say:owner_protected");
  assert.equal(button([b.message], "Do it anyway").length, 0);
});
