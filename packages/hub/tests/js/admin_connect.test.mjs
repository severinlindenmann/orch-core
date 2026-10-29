// #82: the Marketplaces tab must not print "null" for the optional note / stale banner / overwrite warning.
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
  get tagName() { return this.tag.toUpperCase(); }
  querySelectorAll() { return this.all().slice(1).filter(n => ["input", "select", "textarea", "button", "summary", "details"].includes(n.tag)); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = {
  createElement: (tag) => new Node_(tag),
  createTextNode: (t) => new Node_("#text", t),
  getElementById: () => null,
};
const { adminView } = await import("../../src/orch_hub/web/admin.js");

const LINK = "https://hub.test/c#h1." + "ab".repeat(16) + ".CODE123.10.0.0.5:8105";
const bodies = {
  users: { users: [] }, groups: { groups: [] }, grants: { grants: [] }, workspaces: { workspaces: [] }, channels: { channels: [] },
  "market-policy": { version: 1, official_url: "https://x.test/o", policy: { markets: [], addons: { allow: null, block: [] }, ceiling: null } }, audit: { records: [] },
};
test("Connect a workspace mints one link and shows it", async (t) => {
  const calls = [];
  const d = { say: (c) => c, api: async (method, path, body) => {
    calls.push([method, path]);
    if (method === "POST" && path === "/admin/connect") return { status: 200, body: { link: LINK, code: "CODE123", hub_id: "ab".repeat(16), endpoint: "10.0.0.5:8105", expires_ms: Date.now() + 1800000 } };
    return { status: 200, body: bodies[path.replace("/admin/", "").split("?")[0]] };
  } };
  const [, , , panels] = adminView(d);
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  const ws = panels.children[2];
  const btn = ws.all().find((n) => n.attrs.id === "connect-workspace");
  assert.ok(btn, "button present");
  await btn.listeners.click({ target: btn });
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
  assert.deepEqual(calls.filter((c) => c[0] === "POST"), [["POST", "/admin/connect"]]);
  const input = panels.children[2].all().find((n) => n.attrs.id === "connect-link");
  assert.ok(input, "link shown");
  assert.equal(input.attrs.value, LINK);
  assert.match(panels.children[2].textContent, /works once/);
  assert.ok(ws.all().some((n) => n.tag === "button" && n.textContent === "Make a new link"));

  const clipboardWrites = [];
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { clipboard: { writeText: async (text) => { clipboardWrites.push(text); } } },
  });
  t.after(() => {
    if (navigatorDescriptor) Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    else delete globalThis.navigator;
  });
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const copy = ws.all().find((n) => n.tag === "button" && n.textContent === "Copy link");
  assert.ok(copy);
  await copy.listeners.click();
  assert.deepEqual(clipboardWrites, [LINK]);
  assert.equal(copy.textContent, "Copied ✓");
  t.mock.timers.tick(1999);
  assert.equal(copy.textContent, "Copied ✓");
  t.mock.timers.tick(1);
  assert.equal(copy.textContent, "Copy link");
});

test("Workspaces cards use plain words (no Binding, Selective, bound)", async () => {
  const b = { ...bodies,
    users: { users: [{ user_id: "u1", name: "Anna Test" }] },
    workspaces: { workspaces: [
      { workspace_id: "w1", name: "Alpha", mode: "selective", status: "bound", online: true, owner_alias: "u1" },
      { workspace_id: "w2", name: "Beta", mode: "full", status: "bound", online: false },
      { workspace_id: "w3", name: "Gamma", mode: "selective", status: "bound", online: true, owner_alias: "u2" },
    ] } };
  const d = { self: "u1", say: (c) => c, api: async (m, path) => ({ status: 200, body: b[path.replace("/admin/", "").split("?")[0]] }) };
  const [, , , panels] = adminView(d);
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  const t = panels.children[2].textContent;
  assert.doesNotMatch(t, /Binding|Selective|bound|Mission Control|owner only/);
  assert.match(t, /Shares only what you choose/);
  assert.match(t, /Shares everything/);
  assert.match(t, /Connected · online/);
  assert.match(t, /Connected · offline/);
  assert.match(t, /Personal · only you · Anna Test/);
});

test("Access grants overview says 'only you' just for the viewer's own personal workspace", async () => {
  const b = { ...bodies, users: { users: [{ user_id: "u1", name: "Anna" }, { user_id: "u2", name: "Ben" }] },
    groups: { groups: [{ name: "Design", members: ["u1"] }] },
    workspaces: { workspaces: [
      { workspace_id: "w1", name: "Mine", mode: "selective", status: "bound", owner_alias: "u1" },
      { workspace_id: "w3", name: "Bens", mode: "selective", status: "bound", owner_alias: "u2" }] } };
  const d = { self: "u1", say: (c) => c, api: async (m, path) => ({ status: 200, body: b[path.replace("/admin/", "").split("?")[0]] }) };
  const [, , , panels] = adminView(d);
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  const t = panels.textContent;
  assert.match(t, /Personal · only you/);
  assert.match(t, /Personal · only the owner/);
});
