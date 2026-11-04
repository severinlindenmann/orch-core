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
document.body = new Node_("body");
const { personReachView } = await import("../../src/orch_hub/web/admin_reach.js");
const { adminView } = await import("../../src/orch_hub/web/admin.js");
const people = [{ user_id: "ann", name: "Ann" }, { user_id: "bob", name: "Bob" }];
const answer = { user: people[0], workspaces: [
  { name: "Team", mode: "full", role: "edit", via: [{ kind: "group", group: "Sales", role: "edit" }, { kind: "hub-admin" }] },
  { name: "Private", mode: "selective", role: "owner", owner: { name: "Ann" }, via: [{ kind: "owner" }] },
  { name: "Other", mode: "full", role: null, via: [] },
] };
const pick = (view) => view.all().find((n) => n.tag === "select");

test("person selector shows levels, every reason and personal owner", async () => {
  const calls = [];
  const view = personReachView({ say: String, api: async (...args) => { calls.push(args); return { status: 200, body: answer }; } }, people);
  const select = pick(view);
  select.value = "ann";
  await select.listeners.change();
  assert.deepEqual(calls, [["GET", "/admin/people/ann/reach"]]);
  for (const s of ["Check a person", "Workspace", "Level", "Because of", "member of group Sales (Edit)", "hub administrator", "owner", "Personal: only Ann", "No access"]) assert.ok(view.textContent.includes(s), s);
});

test("late responses cannot replace the current person's result", async () => {
  const pending = [];
  const view = personReachView({ say: String, api: () => new Promise((resolve) => pending.push(resolve)) }, people);
  const select = pick(view);
  select.value = "ann";
  const first = select.listeners.change();
  select.value = "bob";
  const second = select.listeners.change();
  pending[1]({ status: 200, body: { ...answer, user: people[1] } });
  await second;
  pending[0]({ status: 200, body: answer });
  await first;
  assert.match(view.textContent, /Workspace access for Bob/);
  assert.doesNotMatch(view.textContent, /Workspace access for Ann/);
});

test("failure can be retried and empty people need no request", async () => {
  let count = 0;
  const view = personReachView({ say: () => "Could not check access", api: async () => ++count === 1 ? { status: 503 } : { status: 200, body: answer } }, people);
  pick(view).value = "ann";
  await pick(view).listeners.change();
  assert.match(view.textContent, /Could not check access/);
  await view.all().find((n) => n.tag === "button").listeners.click();
  assert.match(view.textContent, /Workspace access for Ann/);
  const empty = personReachView({}, []);
  assert.match(empty.textContent, /No people yet/);
});

test("Check a person is the first section of the Admin Access tab", async () => {
  const bodies = {
    users: { users: people }, groups: { groups: [] }, grants: { grants: [] }, workspaces: { workspaces: [] }, channels: { channels: [] },
    "market-policy": { version: 1, official_url: "official", policy: { markets: [], addons: { allow: null, block: [] }, ceiling: null } }, audit: { records: [] },
  };
  const view = adminView({ say: String, api: async (method, path) => ({ status: 200, body: bodies[path.slice(7)] }) });
  await new Promise((r) => setImmediate(r));
  const access = view[3].children[1];
  assert.equal(access.children[1].attrs["aria-label"], "Check a person");
});
