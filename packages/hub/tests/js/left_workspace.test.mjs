// Left workspaces are hidden by default and can be removed with confirmation.
// admin.js builds real DOM, so this runs it against a tiny DOM stub (no browser needed).
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); } // like the DOM: non-nodes become text, so null -> "null"
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  focus() {} select() {}
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = {
  createElement: (tag) => new Node_(tag),
  createTextNode: (t) => new Node_("#text", t),
  getElementById: () => null,
};
const { adminView } = await import("../../src/orch_hub/web/admin.js");

const WS = "ab".repeat(16);
const leftMs = 1730000000000;
const bodies = {
  users: { users: [] }, groups: { groups: [] }, grants: { grants: [] },
  workspaces: { workspaces: [{ workspace_id: WS, name: null, mode: "full", status: "unbound", left_ms: leftMs }] },
  channels: { channels: [] },
  "market-policy": { version: 1, official_url: "https://x.test/o", policy: { markets: [], addons: { allow: null, block: [] }, ceiling: null } }, audit: { records: [] },
};
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
test("left workspace is hidden until toggled and Remove confirms then deletes", async () => {
  const calls = [];
  const d = { say: (c) => c, refresh: async () => {}, api: async (method, path) => {
    calls.push([method, path]);
    if (method === "DELETE") {
      bodies.workspaces.workspaces = [];
      return { status: 200, body: { ok: true } };
    }
    return { status: 200, body: bodies[path.replace("/admin/", "").split("?")[0]] };
  } };
  const [, , , , panels] = adminView(d);
  await settle();
  const panel = () => panels.children[2];
  const toggle = () => panel().all().find((n) => n.attrs.type === "checkbox");
  assert.ok(toggle());
  assert.equal(toggle().attrs.checked, undefined);
  assert.match(panel().textContent, /Show left workspaces/);
  assert.equal(panel().all().filter((n) => n.className === "card left").length, 0);
  toggle().listeners.change({ target: { checked: true } });
  const card = panel().all().find((n) => n.className === "card left");
  assert.ok(card);
  assert.ok(card.all().some((n) => n.className.startsWith("chip") && n.textContent === "Left"));
  assert.ok(card.textContent.includes("Left on " + new Date(leftMs).toLocaleDateString()));
  assert.doesNotMatch(card.textContent, /Full|Connection|Binding|unbound/);
  assert.ok(!panel().textContent.includes(WS.slice(0, 8)));
  const remove = card.all().find((n) => n.tag === "button" && n.textContent === "Remove");
  remove.listeners.click();
  assert.equal(remove.textContent, "Really? Remove");
  assert.equal(calls.filter(([method]) => method === "DELETE").length, 0);
  remove.listeners.click();
  await settle();
  assert.deepEqual(calls.filter(([method]) => method === "DELETE"), [["DELETE", "/admin/workspaces/" + WS]]);
  assert.equal(toggle(), undefined);
  assert.equal(panel().all().filter((n) => n.className === "card left").length, 0);
});
