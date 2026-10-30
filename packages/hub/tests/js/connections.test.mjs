import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
class Node {
  constructor(tag) { this.tag = tag; this.nodeType = 1; this.children = []; this.attrs = {}; this.listeners = {}; this._text = ""; }
  remove() {}
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ""; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(key, callback) { this.listeners[key] = callback; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
const toastNodes = [];
globalThis.document = {
  body: new Node("body"), getElementById: (id) => toastNodes.find((n) => n.attrs.id === id),
  createElement: (tag) => { const n = new Node(tag); toastNodes.push(n); return n; },
  createTextNode: (t) => ({ nodeType: 3, textContent: t, all: () => [] }),
  createElementNS: (namespace, tag) => { assert.equal(namespace, "http://www.w3.org/2000/svg"); return new Node(tag); },
};
const { connectionsView } = await import("../../src/orch_hub/web/pages/connections.js");
const { parseRoute, registerPages } = await import("../../src/orch_hub/web/route.js");
const data = {
  workspaces: [{ workspace_id: "a", name: "Alpha" }, { workspace_id: "b", name: "Beta" }, { workspace_id: "c", name: "<img onerror=bad>" }],
  edges: [{ from: "a", to: "b" }, { from: "c", to: "a" }],
};
test("directional matrix is text only, with safe names and honest explanation", () => {
  const view = connectionsView(data), all = view.all();
  assert.deepEqual(all.filter((n) => n.tag === "td").map((n) => n.textContent), ["–", "allowed", "no", "no", "–", "no", "allowed", "no", "–"]);
  assert.equal(all.filter((n) => n.tag === "button" || n.tag === "img").length, 0);
  assert.match(view.textContent, /<img onerror=bad>/);
  assert.match(view.textContent, /Ask to allow waits for an owner or admin/);
  assert.doesNotMatch(view.textContent.replace(/An arrow from A[^]*approve\./, ""), /online|offline/i); // no presence claim; the legend sits behind the i
  assert.ok(all.some((n) => n.tag === "summary" && n.textContent === "i"));
  assert.equal(all.filter((n) => n.tag === "path").length, 2);
});
test("click and keyboard selection highlight incoming and outgoing arrows", () => {
  let selected;
  const view = connectionsView(data, { select: (id) => { selected = id; } });
  const nodes = view.all().filter((n) => n.attrs.role === "button");
  nodes[0].listeners.click();
  assert.equal(selected, "a");
  assert.match(view.textContent, /Can reach: Beta/);
  assert.match(view.textContent, /Reached by: <img onerror=bad>/);
  assert.equal(view.all().filter((n) => n.attrs.class === "connection-edge outgoing").length, 1);
  assert.equal(view.all().filter((n) => n.attrs.class === "connection-edge incoming").length, 1);
  nodes[1].listeners.keydown({ key: "Enter", preventDefault() {} });
  assert.equal(selected, "b");
  assert.match(view.textContent, /Can reach: Nobody/);
  assert.match(view.textContent, /Reached by: Alpha/);
  assert.equal(nodes[0].attrs["aria-pressed"], "false");
  assert.equal(nodes[1].attrs["aria-pressed"], "true");
});
test("reciprocal arrows and self permissions have finite geometry", () => {
  const view = connectionsView({ ...data, edges: [{ from: "a", to: "a" }, { from: "a", to: "b" }, { from: "b", to: "a" }] });
  const lines = view.all().filter((n) => n.tag === "line");
  assert.equal(lines.length, 2);
  assert.notEqual(lines[0].attrs.x1, lines[1].attrs.x2);
  assert.doesNotMatch(JSON.stringify(view.all().map((n) => n.attrs)), /NaN|Infinity/);
});
test("empty state, route, admin tab and narrow-screen matrix", async () => {
  assert.match(connectionsView({ workspaces: [], edges: [] }).textContent, /No workspaces yet/);
  const { page } = await import("../../src/orch_hub/web/pages/connections.js");   // the registry entry: route, tab, renderer
  assert.deepEqual([page.path, page.nav.admin, page.render.name], ["connections", true, "connectionsPage"]);
  assert.deepEqual(parseRoute("#/connections"), { kind: "home" });                  // unknown until app.js registers the page
  registerPages([page.path]);
  assert.deepEqual(parseRoute("#/connections"), { kind: "connections" });
  const css = readFileSync(new URL("../../src/orch_hub/web/app.css", import.meta.url), "utf8");
  assert.match(css, /@media \(max-width:639px\) \{ \.connections-map \{ display:none; \}/);
});

test("cells offer Ask to allow and a confirmed Close; pending, stored-only and counts are said in words", () => {
  const calls = [];
  const d = { ...data, connections: [{ from: "a", to: "b", via: "approved" }, { from: "b", to: "c", via: "override" }],
    pending: [{ id: "x", from: "c", to: "b", proposer: "Root" }], traffic: [{ from: "a", to: "b", n: 7 }] };
  const view = connectionsView(d, { set: (...args) => calls.push(args) }), all = view.all();
  const cells = all.filter((n) => n.tag === "td").map((n) => n.textContent);
  assert.match(cells[1], /allowed7 in 30 daysClose/);       // a -> b: allowed, counted, can be closed
  assert.match(cells[5], /connected, no accessClose/);        // b -> c: connected but access says no
  assert.match(cells[7], /waiting for approval/);             // c -> b: asked, nothing to click
  assert.doesNotMatch(cells[7], /Ask to allow|Close/);
  const button = (label) => all.find((n) => n.tag === "button" && n.attrs["aria-label"] === label);
  const timers = [], saved = [globalThis.setTimeout, globalThis.clearTimeout];
  globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  globalThis.clearTimeout = () => {};
  try {
    const ask = button("Ask to allow Alpha to <img onerror=bad>");
    ask.listeners.click();
    assert.deepEqual(calls, []);                                // nothing is sent during the Undo window
    assert.equal(ask.disabled, true);
    const undo = toastNodes.filter((n) => n.tag === "button" && n._text === "Undo").at(-1);
    undo.listeners.click();                                     // Undo: nothing was sent, the button is back
    assert.equal(ask.disabled, false);
    timers.at(-1).fn();
    assert.deepEqual(calls, []);
    ask.listeners.click();
    assert.equal(timers.at(-1).ms, 5000);
    timers.at(-1).fn();                                         // the window ends: now it is sent
    assert.deepEqual(calls, [["a", "c", true]]);
  } finally { [globalThis.setTimeout, globalThis.clearTimeout] = saved; }
  const close = button("Close Alpha to Beta");
  close.listeners.click();                                      // the first click only asks "Sure?"
  assert.equal(calls.length, 1);
  assert.equal(close.textContent, "Sure? Close it");
  close.listeners.click();
  assert.deepEqual(calls[1], ["a", "b", false]);
});
