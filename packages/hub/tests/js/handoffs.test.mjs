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
const { handoffsView, sendHandoffView, statusOf } = await import("../../src/orch_hub/web/handoffs.js");
const { actorLabel } = await import("../../src/orch_hub/web/ui.js");
const { parseRoute } = await import("../../src/orch_hub/web/route.js");
const now = new Date(2026, 9, 27, 12).getTime();
const A = { workspace_id: "a", name: "Product", online: true };
const B = { workspace_id: "b", name: "Legal", online: false, can_start: true };
const C = { workspace_id: "c", name: "Archive", online: false };
const data = { workspaces: [A, B, C], handoffs: [
  { from: A, to: B, sent_ms: now - 86400000, status: "waiting" },
  { from: B, to: C, sent_ms: now, status: "delivered" },
  { from: A, to: {}, sent_ms: now, status: "delivered" },
] };
const find = (out, pred) => out.flatMap((n) => n.all()).filter(pred);
const words = (out) => out.map((n) => n.textContent).join("\n");
const click = (out, label) => find(out, (n) => n.tag === "button" && n.textContent === label)[0].listeners.click();
const rows = (out) => find(out, (n) => n.attrs["data-status"]);

test("route, sealed explanation, status reasons and sent times", () => {
  assert.deepEqual(parseRoute("#/handoffs"), { kind: "handoffs" });
  const out = handoffsView(data, {}, now);
  assert.equal(rows(out).length, 3);
  assert.match(words(out), /Product→Legal/);
  assert.match(words(out), /Waiting for Legal to come online/);
  assert.match(words(out), /Yesterday 12:00/); // the Sent column only: no second "since" line
  assert.doesNotMatch(words(out), /since Yesterday/);
  assert.match(words(out), /Product→Another workspace/);
  assert.match(words(out), /Which workspace sent something to which, and whether it arrived\./);
  assert.match(words(out), /What is inside stays private\. Delivered means it reached the workspace, not that someone accepted it\./); // behind the i
  assert.ok(find(out, (n) => n.tag === "summary" && n.textContent === "i").length >= 1);
  assert.doesNotMatch(words(out), /sealed|ticket/i);
  assert.deepEqual(find(out, (n) => n.tag === "th").map((n) => n.textContent), ["From → to", "Sent", "State"]);
  assert.equal(statusOf({ ...data.handoffs[0], to: A }, now).label, "Waiting for Product to take it");
  assert.equal(statusOf({ status: "delivered" }, now).label, "Delivered");
});

test("workspace matches either end and combines with state; reset and empty results", () => {
  const out = handoffsView(data, {}, now);
  const select = find(out, (n) => n.tag === "select")[0];
  select.value = "b"; select.listeners.change();
  assert.equal(rows(out).length, 2);
  click(out, "Delivered");
  assert.equal(rows(out).length, 1);
  assert.match(rows(out)[0].textContent, /Legal→Archive/);
  select.value = "c"; select.listeners.change(); click(out, "Waiting");
  assert.equal(rows(out).length, 0);
  assert.match(words(out), /No hand-offs match these filters\./);
  select.value = ""; select.listeners.change(); click(out, "All");
  assert.equal(rows(out).length, 3);
  assert.deepEqual(find(out, (n) => n.attrs["aria-pressed"]).map((n) => n.attrs["aria-pressed"]), ["true", "false", "false"]);
  assert.match(words(handoffsView({ workspaces: [], handoffs: [] })), /No hand-offs yet/);
});

test("send dialog uses eligible sources and opens the exact workspace", async () => {
  let dialog, opened, closed = 0;
  const out = handoffsView(data, { dialog: (...nodes) => { dialog = nodes; }, open: (w) => { opened = w; }, close: () => closed++ });
  click(out, "Send a hand-off");
  const [from] = find(dialog, (n) => n.tag === "select");
  assert.deepEqual(from.children.map((n) => n.attrs.value), ["a", "b"]);
  assert.equal(find(dialog, (n) => n.tag === "select").length, 1); // no target picker: the workspace owns the hand-off
  assert.match(words(dialog), /Choose where it comes from, then use Hand off there\./);
  assert.doesNotMatch(words(dialog), /ticket|sealed/i);
  from.value = "b"; from.listeners.change();
  await click(dialog, "Open Legal");
  assert.equal(opened, B);
  click(dialog, "Cancel"); assert.equal(closed, 1);
});

test("send empty choices and open failure remain actionable", async () => {
  for (const workspaces of [[], [C]]) {
    const out = sendHandoffView(workspaces);
    assert.equal(find(out, (n) => n.tag === "button").at(-1).disabled, true);
  }
  const out = sendHandoffView([A, B], { open: async () => { throw Error(); } });
  await click(out, "Open Product");
  assert.match(words(out), /Could not open the workspace. Please try again./);
  assert.equal(find(out, (n) => n.tag === "button").at(-1).disabled, false);
});

test("workspace names and actor labels are safe text; agents never use person names", () => {
  const name = '<img src=x>\u202e';
  const out = handoffsView({ workspaces: [], handoffs: [{ from: { name }, to: B, status: "waiting" }] });
  assert.match(words(out), /<img src=x><U\+202E>/);
  assert.equal(find(out, (n) => n.tag === "img").length, 0);
  assert.equal(actorLabel({ kind: "agent", session_id: "s1", name: "Anna" }), "agent:s1");
  assert.equal(actorLabel({ kind: "agent", session: "s2" }), "agent:s2");
  assert.equal(actorLabel({ kind: "agent", name: "Anna" }), "agent");
  assert.equal(actorLabel({ kind: "agent", session_id: {}, name: "Anna" }), "agent");
  assert.equal(actorLabel({ kind: "person", name: "Anna" }), "Anna");
  assert.equal(actorLabel({ kind: "person", name }), "<img src=x><U+202E>");
  assert.equal(actorLabel({ kind: "agent", session_id: "s\u202e" }), "agent:s<U+202E>");
  assert.equal(actorLabel({ kind: "host", name: "Anna" }), "host");
  for (const actor of [null, undefined, [], "Anna", {}, { kind: "other", name: "Anna" }]) assert.equal(actorLabel(actor), "unknown");
  assert.equal(actorLabel({ kind: "person", name: {} }), "person");
});

test("filter row offers workspace and state only: no person filter (the hub never learns the sender)", () => {
  const out = handoffsView(data, {}, now);
  assert.deepEqual(find(out, (n) => n.tag === "select").map((n) => n.attrs["aria-label"]), ["Workspace"]);
  assert.deepEqual(find(out, (n) => n.attrs["aria-pressed"]).map((n) => n.textContent), ["All", "Waiting", "Delivered"]);
  assert.doesNotMatch(words(out), /person|people/i);
});
