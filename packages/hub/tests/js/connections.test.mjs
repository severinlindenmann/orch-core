import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
class Node {
  constructor(tag) { this.tag = tag; this.nodeType = 1; this.children = []; this.attrs = {}; this.listeners = {}; this._text = ""; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ""; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(key, callback) { this.listeners[key] = callback; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = {
  createElement: (tag) => new Node(tag),
  createElementNS: (namespace, tag) => { assert.equal(namespace, "http://www.w3.org/2000/svg"); return new Node(tag); },
};
const { connectionsView } = await import("../../src/orch_hub/web/connections.js");
const { parseRoute } = await import("../../src/orch_hub/web/route.js");
const data = {
  workspaces: [{ workspace_id: "a", name: "Alpha" }, { workspace_id: "b", name: "Beta" }, { workspace_id: "c", name: "<img onerror=bad>" }],
  edges: [{ from: "a", to: "b" }, { from: "c", to: "a" }],
};
test("directional matrix is text only, with safe names and honest explanation", () => {
  const view = connectionsView(data), all = view.all();
  assert.deepEqual(all.filter((n) => n.tag === "td").map((n) => n.textContent), ["no", "allowed", "no", "no", "no", "no", "allowed", "no", "no"]);
  assert.equal(all.filter((n) => n.tag === "button" || n.tag === "img").length, 0);
  assert.match(view.textContent, /<img onerror=bad>/);
  assert.match(view.textContent, /Changing connections comes later/);
  assert.doesNotMatch(view.textContent.replace(/An arrow from A[^]*later\./, ""), /online|offline/i); // no presence claim; the legend sits behind the i
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
test("empty state, route, admin tab and narrow-screen matrix", () => {
  assert.match(connectionsView({ workspaces: [], edges: [] }).textContent, /No workspaces yet/);
  assert.deepEqual(parseRoute("#/connections"), { kind: "connections" });
  const app = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  assert.match(app, /state.user\?\.is_admin \? tab\("connections"/);
  assert.match(app, /connections: \(\) => connectionsPage/);
  const css = readFileSync(new URL("../../src/orch_hub/web/app.css", import.meta.url), "utf8");
  assert.match(css, /@media \(max-width:639px\) \{ \.connections-map \{ display:none; \}/);
});
