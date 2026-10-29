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
const { homeView, homeSummary, workspaceStatus } = await import("../../src/orch_hub/web/home.js");
const spaces = [{ workspace_id: "a", name: "Product", online: true }, { workspace_id: "b", name: "Legal", online: false }];
test("summary excludes another person's workspace", () => {
  const out = homeSummary(spaces, [{ workspace_id: "a", signable: true }, { workspace_id: "secret" }]);
  assert.equal(out.waiting, 1);
  assert.equal(out.online, 1);
  assert.equal(out.offline, 1);
});
test("home groups rows, names their workspace, and shows health", () => {
  const out = homeView({ workspaces: spaces, items: [
    { workspace_id: "a", title: "First", signable: true },
    { workspace_id: "secret", title: "Secret", signable: true },
    { workspace_id: "a", title: "Second", signable: true },
  ], actions: {} });
  const nodes = out.flatMap((n) => n.all());
  const text = out.map((n) => n.textContent).join(" ");
  assert.match(text, /1 workspaces online, 1 offline, 2 things need you/);
  assert.doesNotMatch(text, /Secret/);
  const groups = nodes.filter((n) => n.className === "workspace-needs");
  assert.equal(groups.length, 1);
  assert.match(groups[0].textContent, /Product.*First.*Product.*Second.*Product/);
  assert.match(text, /offline since an unknown time/);
  assert.match(text, /Hand-offs and approvals wait/);
});
test("offline time is honest and formats available last-seen data", () => {
  assert.equal(workspaceStatus({ online: true }), "online");
  assert.equal(workspaceStatus({ online: false }), "offline since an unknown time");
  assert.equal(workspaceStatus({ last_seen: "bad" }), "offline since an unknown time");
  assert.equal(workspaceStatus({ last_seen: 1700000000 }), "offline since " + new Date(1700000000000).toLocaleString());
});

test("summary and chip share the actionable list, excluding offline and hidden items", () => {
  const items = [
    { workspace_id: "a", signable: true }, { workspace_id: "a", summary_only: true },
    { workspace_id: "a", mode: "selective" }, { workspace_id: "a" },
    { workspace_id: "b", signable: true }, { workspace_id: "a", online: false, signable: true },
    { workspace_id: "secret", signable: true },
  ];
  const out = homeView({ workspaces: spaces, items, actions: {} });
  assert.match(out.map(n => n.textContent).join(" "), /3 things need you/);
  const needs = out.flatMap(n => n.all()).find(n => n.className === "card needs-you");
  assert.equal(needs.all().find(n => n.className === "chip ").textContent, "3");
  assert.equal(homeSummary(spaces, items, ["a"]).waiting, 0);
});

test("with a selective workspace the needs card says quietly that only shared items appear; otherwise it does not", () => {
  const text = (n) => n.flatMap((x) => x.all()).map((x) => x.textContent).join(" ");
  const sel = homeView({ workspaces: [{ workspace_id: "s", name: "S", mode: "selective", online: true }], items: [], actions: {} });
  assert.match(text(sel), /Only items you share appear here/);
  const full = homeView({ workspaces: [{ workspace_id: "f", name: "F", mode: "full", online: true }], items: [], actions: {} });
  assert.doesNotMatch(text(full), /Only items you share appear here/);
});

test("first-run home shows only the connect card", () => {
  const out = homeView({ user: { name: "Anna", is_admin: true }, workspaces: [], items: [], channels: [], thisBrowser: { state: "unregistered" }, actions: { connect() {} } });
  const text = out.map((n) => n.textContent).join(" ");
  assert.match(text, /Connect your first workspace/);
  assert.match(text, /Get a one-time link/);
  assert.doesNotMatch(text, /Where you can sign|Register this browser|Needs you|My workspaces/);
  const withWs = homeView({ user: { name: "Anna" }, workspaces: [{ workspace_id: "a", name: "P", online: true }], items: [], channels: [], thisBrowser: { state: "unregistered" }, actions: {} });
  const t2 = withWs.map((n) => n.textContent).join(" ");
  assert.match(t2, /Where you can sign/);
  assert.doesNotMatch(t2, /Connect your first workspace/);
});
