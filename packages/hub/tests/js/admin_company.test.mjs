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
const bodies = (users) => ({
  users: { users }, groups: { groups: [] }, grants: { grants: [] }, workspaces: { workspaces: [] }, channels: { channels: [] },
  "market-policy": { version: 1, official_url: "https://x.test/o", policy: { markets: [], addons: { allow: null, block: [] }, ceiling: null } }, audit: { records: [] },
});
async function people(users) {
  const calls = [];
  const b = bodies(users);
  const d = { say: (c) => c, api: async (method, path, body) => { calls.push([method, path]); return { status: 200, body: b[path.replace("/admin/", "").split("?")[0]] ?? {} }; } };
  const [, , , panels] = adminView(d);
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  return { panel: panels.children[0], calls };
}

test("a company-invited person shows which account signed in, a name hint, and a plain Remove", async () => {
  const { panel, calls } = await people([
    { user_id: "u1", name: "Julia Meier", company: { email: "julia@acme.example", name: "Mallory Admin" } },
    { user_id: "u2", name: "Pat", company: { email: "pat@acme.example", name: "Pat" } },
    { user_id: "u3", name: "Old Ollie" },
  ]);
  const line = (id) => panel.all().find((n) => n.attrs["data-company"] === id);
  assert.match(line("u1").textContent, /Signed in as julia@acme\.example/);
  assert.match(line("u1").textContent, /Their company account is named Mallory Admin/);
  assert.doesNotMatch(line("u2").textContent, /company account is named/); // same name: no hint
  assert.equal(line("u3"), undefined); // a password person has no company line
  assert.ok(line("u1").all().some((n) => n.tag === "button" && n.textContent === "Remove"));
  assert.equal(calls.filter((c) => c[0] !== "GET").length, 0);
});

test("the company labels are shown as text only: control characters dropped, capped at 200", async () => {
  const { panel } = await people([{ user_id: "u1", name: "Julia", company: { email: "a\u0000b\u0007" + "x".repeat(400), name: "<img src=x onerror=alert(1)>" } }]);
  const line = panel.all().find((n) => n.attrs["data-company"] === "u1");
  const t = line.textContent;
  assert.ok(!t.includes("\u0000") && !t.includes("\u0007"));
  assert.ok(t.length < 520);
  assert.ok(t.includes("<img src=x onerror=alert(1)>")); // shown as text, never parsed (textContent only)
  assert.equal(line.all().filter((n) => n.tag === "img").length, 0);
});
