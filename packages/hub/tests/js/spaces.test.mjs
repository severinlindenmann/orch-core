// M3-2: the Shared spaces page (#1443 #1444 #1447) against a tiny DOM stub.
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { spacesView } = await import("../../src/orch_hub/web/pages/spaces.js");
const NOW = 1_800_000_000_000;
const find = (n, pred) => n.all().filter(pred);
const data = { spaces: [
  { space_id: "s1", name: "Finance", epoch: 2, items: 3, revoked: 0, archived_ms: null, members: [{ workspace_id: "a", name: "Product", mode: "edits" }, { workspace_id: "b", name: "Legal", mode: "reads" }] },
  { space_id: "s2", name: "Old <b>wiki</b>", epoch: 1, items: 1, revoked: 0, archived_ms: NOW - 86400_000, members: [{ workspace_id: "a", name: "Product", mode: "edits" }] },
] };

test("the page says plainly that the hub can read, lists members with Reads/Edits, and hides archived spaces", () => {
  const calls = [];
  const view = spacesView(data, { setMode: (s, m, mode) => calls.push(["mode", s.space_id, m.workspace_id, mode]), archive: (s, a) => calls.push(["archive", s.space_id, a]) }, NOW);
  assert.match(view.textContent, /The hub and its admins can read what is in a shared space\./);
  assert.match(view.textContent, /Finance/); assert.doesNotMatch(view.textContent, /Old/);   // archived: hidden by default
  assert.match(view.textContent, /Show archived \(1\)/);
  const selects = find(view, (n) => n.tag === "select");
  assert.deepEqual(selects.map((s) => s.value), ["edits", "reads"]);
  selects[0].value = "reads"; selects[0].listeners.change();
  assert.deepEqual(calls[0], ["mode", "s1", "a", "reads"]);
  const archive = find(view, (n) => n.tag === "button" && n.textContent === "Archive")[0];
  archive.listeners.click();                                  // the first click only asks "Sure?"
  assert.equal(calls.length, 1); assert.equal(archive.textContent, "Sure? Archive it");
  archive.listeners.click();
  assert.deepEqual(calls[1], ["archive", "s1", true]);
  find(view, (n) => n.attrs.id === "toggle-archived")[0].listeners.click();
  assert.match(view.textContent, /Old <b>wiki<\/b>/);          // text, never markup
  assert.match(view.textContent, /Nobody can add to it; it can still be read\./);
  assert.equal(find(view, (n) => n.tag === "b" && n.textContent === "wiki").length, 0);
  find(view, (n) => n.tag === "button" && n.textContent === "Bring back")[0].listeners.click();
  assert.deepEqual(calls[2], ["archive", "s2", false]);
});

test("no spaces: a plain empty state", () => {
  assert.match(spacesView({ spaces: [] }, {}, NOW).textContent, /No shared spaces yet/);
});
