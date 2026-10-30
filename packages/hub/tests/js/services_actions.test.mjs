// HUB-SV-02: the console's check of a prepared service action (digest vector made by the Python helper) and the buttons.
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { checkServiceAction } = await import("../../src/orch_hub/web/service_actions.js");
const { servicesView } = await import("../../src/orch_hub/web/services.js");

const V = {"ctx": {"kind": "orch.hub.service.action/v1", "hub_id": "abababababababababababababababab", "host_id": "vps1", "part": "relay", "action": "restart", "target_version": null, "nonce": "cccccccccccccccccccccccccccccccc", "iat": 1800000000123, "channel_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "user_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "text_sha256": "c79797be49f69eff6aca358363a5cb80694cdf34d336ae13af76045c3a98a8c2"}, "digest": "0ddc87b5259c4d77982f73bebc8a4f0391f3b29592bf5bbacb6905d7f79cca22", "text": "Restart Relay on vps1. It is unavailable for a few seconds."};
const want = { part: "relay", action: "restart", userId: "a".repeat(32), channelId: "b".repeat(32) };
const code = async (p, w = want) => { try { await checkServiceAction(p, w); return "ok"; } catch (e) { return e.message; } };

test("the digest the helper made equals the one this page recomputes", async () => {
  assert.equal(await code(V), "ok");
});
test("another part, action, person or key is refused", async () => {
  assert.equal(await code(V, { ...want, part: "publish" }), "event_changed");
  assert.equal(await code(V, { ...want, action: "stop" }), "event_changed");
  assert.equal(await code(V, { ...want, userId: "c".repeat(32) }), "event_changed");
  assert.equal(await code(V, { ...want, channelId: "c".repeat(32) }), "event_changed");
});
test("a text that is not the bound one, a changed field or an extra key is refused", async () => {
  assert.equal(await code({ ...V, text: "Stop Relay on vps1." }), "event_changed");
  assert.equal(await code({ ...V, ctx: { ...V.ctx, iat: V.ctx.iat + 1 } }), "event_changed");
  assert.equal(await code({ ...V, ctx: { ...V.ctx, extra: 1 } }), "malformed");
  assert.equal(await code({ ...V, digest: "00".repeat(32) }), "event_changed");
  assert.equal(await code({ ...V, ctx: { ...V.ctx, target_version: "9.9" } }), "event_changed");
  assert.equal(await code({}), "malformed");
  assert.equal(await code({ ...V, text: 5 }), "malformed");
});

const NOW = Date.now();
const data = (helper) => ({
  hub: { state: "up", version: "0.1.0", started_ms: NOW - 1000 },
  relay: { name: "relay", state: "down", url: "http://r", last_ok_ms: null },
  publish: { name: "publish", state: "up", url: "http://p", checked_ms: NOW },
  workspaces: { count: 0, online: 0 },
  helper,
});
const find = (nodes, f) => nodes.flatMap((n) => n.all()).filter(f);

test("buttons appear only for parts the helper manages, and call the action", () => {
  const calls = [];
  const view = servicesView(data({ state: "up", host_id: "vps1", parts: [{ name: "relay", label: "Relay" }, { name: "hub", label: "Hub" }], hub_busy: false }), { service: (n, v) => calls.push([n, v]), copy: () => {} }, NOW);
  const btns = find(view, (n) => n.attrs["data-svc"]);
  assert.deepEqual(btns.map((b) => b.attrs["data-svc"]).sort(), ["hub|restart", "hub|stop", "relay|restart", "relay|start", "relay|stop"]);
  btns.find((b) => b.attrs["data-svc"] === "relay|start").listeners.click?.();
  assert.equal(find(view, (n) => n.attrs["data-svc"] === "publish|restart").length, 0);
  assert.ok(find(view, (n) => n.attrs.id === "how-publish").length === 1); // publish keeps the copy-the-command help
  assert.ok(find(view, (n) => n.attrs.id === "how-relay").length === 0);
});
test("no helper: no buttons, the command help stays; helper down: a note; busy hub: buttons off", () => {
  const none = servicesView(data({ state: "not_set_up", parts: [] }), { service: () => {}, copy: () => {} }, NOW);
  assert.equal(find(none, (n) => n.attrs["data-svc"]).length, 0);
  assert.equal(find(none, (n) => n.attrs.id === "how-relay").length, 1);
  const down = servicesView(data({ state: "down", parts: [] }), { service: () => {}, copy: () => {} }, NOW);
  assert.match(find(down, (n) => n.attrs.id === "helper-note")[0].textContent, /not running/);
  const busy = servicesView(data({ state: "up", parts: [{ name: "relay" }], hub_busy: true }), { service: () => {}, copy: () => {} }, NOW);
  assert.ok(find(busy, (n) => n.attrs["data-svc"]).every((b) => b.getAttribute("disabled") !== null));
});
