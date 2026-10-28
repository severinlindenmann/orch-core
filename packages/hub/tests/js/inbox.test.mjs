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
const { inboxView } = await import("../../src/orch_hub/web/inbox.js");
const data = { workspaces: [{ workspace_id: "a", name: "Alpha", online: true }, { workspace_id: "b", name: "Beta", online: false }], items: [
  { workspace_id: "a", kind: "gate", gate: "design", key: "A-1", title: "A title", signable: true, deadline: 1000 },
  { workspace_id: "a", kind: "verdict", key: "A-2", title: "Another title" },
  { workspace_id: "b", kind: "question", key: "B-1", title: "Choose", summary_only: true, signable: false }
], offline: ["b"], queued: [{ workspace_id: "b", type: "gate.approved" }, { workspace_id: "a", type: "question.answered" }] };
const render = (d = data, a = {}) => inboxView(d, { filter() {}, review() {}, open() {}, ...a }, 1000);
const text = (out) => out.map((n) => n.textContent).join("\n");
const find = (out, pred) => out.flatMap((n) => n.all()).filter(pred);
test("groups, counts, sentences, ticket and deadline; review reuses the original item", () => {
  let reviewed;
  const out = render(data, { review: (it) => reviewed = it });
  assert.deepEqual(find(out, (n) => n.tag === "h2").map((n) => n.textContent), ["Alpha (2)", "Beta (1)", "Sent, waiting for the workspace"]);
  assert.match(text(out), /Approve the design gateA-1 · A title/);
  assert.match(text(out), /Due Today/);
  find(out, (n) => n.tag === "button" && n.textContent === "Review")[0].listeners.click();
  assert.equal(reviewed, data.items[0]);
});
test("workspace and kind filters and selection callback", () => {
  let filter;
  const out = render({ ...data, filter: { workspace: "a", kind: "verdict" } }, { filter: (f) => filter = f });
  assert.match(text(out), /Alpha \(1\)/);
  assert.doesNotMatch(text(out), /Approve the|Beta \(1\)|Beta is offline/);
  find(out, (n) => n.tag === "select")[1].listeners.change({ target: { value: "question" } });
  assert.deepEqual(filter, { workspace: "a", kind: "question" });
  assert.match(text(render({ ...data, filter: { workspace: "a", kind: "question" } })), /Nothing is waiting for you\./);
});
test("summary opens workspace, offline and queued messages", () => {
  let opened;
  const out = render(data, { open: (w) => opened = w });
  assert.match(text(out), /Summary only: answer it in Beta/);
  find(out, (n) => n.tag === "button" && n.textContent === "Open")[0].listeners.click();
  assert.deepEqual(opened, { id: "b", name: "Beta" });
  assert.match(text(out), /Beta is offline\. Items for it are delivered when it is back\.Waiting/);
  assert.match(text(out), /Gate approval for Beta: delivered when Beta is back/);
  assert.match(text(out), /Answer for Alpha: delivering/);
});
test("phone only advertises an active iphone; unavailable reads are visible", () => {
  assert.match(text(render()), /pair it in Home > Where you can sign/);
  const out = render({ ...data, channels: [{ kind: "iphone", status: "revoked", label: "Old" }, { kind: "iphone", status: "active", label: "Pocket" }], unavailable: ["a"] });
  assert.match(text(out), /Phone: Pocket can approve with Face ID/);
  assert.doesNotMatch(text(out), /Old/);
  assert.match(text(out), /Items for Alpha cannot be read right now/);
});
