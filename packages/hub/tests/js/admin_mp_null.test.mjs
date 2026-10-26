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
  focus() {}
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

const policy = (extra = {}) => ({ version: 3, official_url: "https://example.test/official", policy: { markets: [], addons: { allow: null, block: [] }, ceiling: null }, ...extra });
const bodies = (pol) => ({
  users: { users: [] }, groups: { groups: [] }, grants: { grants: [] }, workspaces: { workspaces: [] }, channels: { channels: [] },
  "market-policy": pol, audit: { records: [] },
});
async function mount(pol, { api } = {}) {
  const b = bodies(pol);
  const d = {
    say: (c) => c,
    api: api ?? (async (method, path) => ({ status: 200, body: b[path.replace("/admin/", "").split("?")[0]] })),
  };
  const [, , tabs, panels] = adminView(d);
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  return { tabs, panels, d };
}
const mpPanel = (panels) => panels.children[3];

test("no 'null' text when there is no note, stale banner or overwrite warning", async () => {
  const { panels } = await mount(policy());
  const panel = mpPanel(panels);
  assert.ok(panel, "marketplaces panel drawn");
  assert.doesNotMatch(panel.textContent, /null/);
  const texts = panel.all().filter((n) => n.tag === "#text").map((n) => n.textContent);
  assert.ok(!texts.includes("null"));
  assert.equal(panel.all().filter((n) => n.className.includes("banner")).length, 0);
});

test("the note renders when present (after a refused save)", async () => {
  const { panels } = await mount(policy(), {
    api: async (method, path) => {
      if (method === "PUT") return { status: 400, body: {} };
      const b = bodies(policy());
      return { status: 200, body: b[path.replace("/admin/", "").split("?")[0]] };
    },
  });
  const panel = mpPanel(panels);
  const addOfficial = panel.all().find((n) => n.attrs.id === "mp-add-official");
  assert.ok(addOfficial, "official button present");
  addOfficial.listeners.click();
  const save = mpPanel(panels).all().find((n) => n.tag === "button" && n._text === "Save rules") ?? mpPanel(panels).all().find((n) => n.tag === "button" && /^Save/.test(n._text));
  assert.ok(save, "save button present");
  await save.listeners.click();
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  const now = mpPanel(panels);
  const note = now.all().find((n) => n.attrs.id === "mp-note");
  assert.ok(note, "note rendered");
  assert.match(note.textContent, /did not accept/);
  assert.doesNotMatch(now.textContent, /null/);
});
