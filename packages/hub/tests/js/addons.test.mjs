// M3-1: the Add-ons page (#1448-#1450) against a tiny DOM stub.
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
const { addonsView, pickable, alreadyAllowed } = await import("../../src/orch_hub/web/addons.js");
const NOW = 1_800_000_000_000;
const find = (n, pred) => n.all().filter(pred);
const data = {
  allow: ["alpha", "beta"], block: ["evil"],
  workspaces: [{ workspace_id: "a", name: "Product", allow: null }, { workspace_id: "b", name: "Legal", allow: ["alpha"] }],
  requests: [{ workspace_id: "b", workspace_name: "Legal", name: "wiki", version: "1.2.0", last_ms: NOW - 60_000, count: 3, actor: "agent" }],
};

test("the picker offers the common list, what the workspace has and what it asked for, never a blocked one", () => {
  assert.deepEqual(pickable(data, data.workspaces[0]), ["alpha", "beta"]);
  assert.deepEqual(pickable(data, data.workspaces[1]), ["alpha", "beta", "wiki"]);   // wiki: asked for by Legal
  assert.deepEqual(pickable({ ...data, block: ["beta", "wiki"] }, data.workspaces[1]), ["alpha"]);
  assert.deepEqual(pickable({ ...data, allow: null }, data.workspaces[1]), ["alpha", "wiki"]); // no common list: only what is known
});

test("an approval changes nothing when there is no common list and no limit, and the page says so", () => {
  const open = { allow: null, workspaces: [{ workspace_id: "a", allow: null }, { workspace_id: "b", allow: ["x"] }] };
  assert.equal(alreadyAllowed(open, "a"), true);
  assert.equal(alreadyAllowed(open, "b"), false);
  assert.equal(alreadyAllowed(data, "a"), false);                                   // a common list exists
});

test("the page says what is allowed everywhere, lists requests with the actor, and limits a workspace without widening", async () => {
  const calls = [];
  const view = addonsView(data, { save: async (w, ids) => calls.push(["save", w.workspace_id, ids]), decide: (r, ok) => calls.push(["decide", r.name, ok]) }, NOW);
  const text = view.textContent;
  assert.match(text, /Allowed everywhere/); assert.match(text, /alpha/); assert.match(text, /Never allowed: evil/);
  assert.match(text, /An agent in Legal asked for wiki 1\.2\.0/); assert.match(text, /3 times/);
  assert.match(text, /A workspace can be limited to fewer add-ons than the common list\. It can never be given more\./);
  assert.match(text, /Installing is always decided by the workspace owner/);
  const buttons = (label) => find(view, (n) => n.tag === "button" && n.textContent === label);
  buttons("Allow for everyone")[0].listeners.click();
  buttons("Turn down")[0].listeners.click();
  assert.deepEqual(calls, [["decide", "wiki", true], ["decide", "wiki", false]]);
  assert.equal(find(view, (n) => n.tag === "input" && n.attrs.type === "text").length, 0);   // nobody types a name
  const picks = (ws) => find(view, (n) => n.tag === "input" && n.attrs["data-addon"]);
  const boxes = find(view, (n) => n.tag === "input" && n.attrs.type === "checkbox" && !n.attrs["data-addon"]);
  await buttons("Save")[0].listeners.click();                                      // Product: not limited, so Save clears the limit
  assert.deepEqual(calls.at(-1), ["save", "a", null]);
  boxes[0].checked = true; boxes[0].listeners.change?.();
  const product = picks().filter((n) => ["alpha", "beta"].includes(n.attrs["data-addon"])).slice(0, 2);
  product[1].checked = true;                                                        // tick beta only
  await buttons("Save")[0].listeners.click();
  assert.deepEqual(calls.at(-1), ["save", "a", ["beta"]]);
  await buttons("Save")[1].listeners.click();                                      // Legal keeps its limit: alpha is ticked
  assert.deepEqual(calls.at(-1), ["save", "b", ["alpha"]]);
});

test("with no hub-wide list everything is allowed and the empty request state reads plainly", () => {
  const view = addonsView({ allow: null, block: [], workspaces: [], requests: [] }, {}, NOW);
  assert.match(view.textContent, /Anything from an allowed marketplace/);
  assert.match(view.textContent, /No requests/);
});
