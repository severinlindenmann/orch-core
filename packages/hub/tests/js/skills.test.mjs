// M3-3: the Company skills page (#1430) against a tiny DOM stub.
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
const { skillsView, skillProblem } = await import("../../src/orch_hub/web/pages/skills.js");
const { wsLabel, withNames } = await import("../../src/orch_hub/web/ui.js");
const find = (n, pred) => n.all().filter(pred);
const data = {
  max_chars: 2400,
  workspaces: [{ workspace_id: "a".repeat(32), name: "Product" }, { workspace_id: "b".repeat(32), name: null }],
  skills: [{ name: "invoices", text: "Write <b>INV</b> numbers.", digest: "sha256:x", shared: "chosen", offered_to: ["a".repeat(32)] }],
};

test("a skill shows its text as text, offer boxes reflect who has it, and the page says nothing is on until accepted", () => {
  const calls = [];
  const view = skillsView(data, { save: (n, t) => calls.push(["save", n, t]), offer: (n, sh, ws) => calls.push(["offer", n, sh, ws]), remove: (s) => calls.push(["remove", s.name]) });
  assert.match(view.textContent, /Nothing is switched on for anyone until their workspace owner accepts it\./);
  assert.equal(find(view, (n) => n.tag === "b" && n.textContent === "INV").length, 0); // text, never markup
  const boxes = find(view, (n) => n.tag === "input" && n.attrs["data-ws"]);
  assert.deepEqual(boxes.map((b) => b.checked), [true, false]);
  assert.match(view.textContent, /A workspace/);
  boxes[1].checked = true;
  find(view, (n) => n.tag === "button" && n.textContent === "Save who gets it")[0].listeners.click();
  assert.deepEqual(calls[0], ["offer", "invoices", "chosen", ["a".repeat(32), "b".repeat(32)]]);
  find(view, (n) => n.tag === "button" && n.textContent === "Save text")[0].listeners.click();
  assert.deepEqual(calls[1], ["save", "invoices", "Write <b>INV</b> numbers."]);
  const remove = find(view, (n) => n.tag === "button" && n.textContent === "Remove")[0];
  remove.listeners.click();                                       // the first click only asks "Sure?"
  assert.equal(calls.length, 2); assert.equal(remove.textContent, "Sure? Remove it");
  remove.listeners.click();
  assert.deepEqual(calls[2], ["remove", "invoices"]);
});

test("a bad new skill is caught in the page and nothing is sent", () => {
  const calls = [];
  const view = skillsView({ ...data, skills: [] }, { save: (...a) => calls.push(a) });
  assert.match(view.textContent, /No company skills yet/);
  find(view, (n) => n.tag === "button" && n.textContent === "Add skill")[0].listeners.click();
  assert.equal(calls.length, 0);
  assert.match(find(view, (n) => n.attrs.role === "alert")[0].textContent, /short name/);
});

test("the page check mirrors the hub rules", () => {
  assert.equal(skillProblem("invoices", "ok"), null);
  assert.match(skillProblem("Invoices", "ok"), /short name/);
  assert.match(skillProblem("orch-tickets", "ok"), /orch's own/);
  assert.match(skillProblem("invoices", "  "), /Write what/);
  assert.match(skillProblem("invoices", "x".repeat(2401)), /under 2400/);
  assert.match(skillProblem("invoices", "---\nx"), /plain text/);
});

test("an offer target is named like the rail names it, with a friendly fallback and never an id", () => {
  const id = "dcac275f" + "0".repeat(24);
  assert.equal(wsLabel({ workspace_id: id }, []), "A workspace");
  assert.equal(wsLabel({ workspace_id: id, name: null }, [{ id, name: "dcac275f" }]), "A workspace");
  assert.equal(wsLabel({ workspace_id: id }, [{ id, name: "Personal Workspace" }]), "Personal Workspace");
  assert.equal(wsLabel({ workspace_id: id, name: "Own" }, [{ id, name: "Rail" }]), "Own");
  assert.equal(wsLabel({ workspace_id: id, owner_name: "Ben Test" }, []), "Ben Test's workspace");
  assert.equal(withNames([{ workspace_id: id }], [])[0].name, "A workspace");
});

test("who gets a skill defaults to all workspaces, hides the boxes, and saves no list", () => {
  const calls = [];
  const all = { ...data, skills: [{ name: "invoices", text: "x", digest: "sha256:x", offered_to: [] }] };
  const view = skillsView(all, { offer: (n, sh, ws) => calls.push([n, sh, ws]) });
  const pick = find(view, (n) => n.tag === "select" && n.attrs["aria-label"] === "Who gets it")[0];
  assert.equal(pick.value, "all");
  assert.match(view.textContent, /All workspaces \(also new ones\)/);
  assert.equal(find(view, (n) => n.className === "share-list")[0].hidden, true);
  find(view, (n) => n.tag === "button" && n.textContent === "Save who gets it")[0].listeners.click();
  assert.deepEqual(calls[0], ["invoices", "all", []]);
  pick.value = "chosen"; pick.listeners.change();
  assert.equal(find(view, (n) => n.className === "share-list")[0].hidden, false);
});

test("needs attention sits at the top, one line and one action link each, and is absent when empty", () => {
  const items = [{ kind: "secret_expiring", text: "crm-key expires in 5 days.", page: "secrets", action: "Enter a new value" }];
  const view = skillsView({ ...data, attention: items });
  const kids = view.children.map((c) => c.textContent);
  assert.match(kids[1], /^Needs attention/);
  assert.match(view.textContent, /crm-key expires in 5 days\./);
  const link = find(view, (n) => n.tag === "a")[0];
  assert.equal(link.attrs.href, "#/secrets");
  assert.equal(link.textContent, "Enter a new value");
  assert.doesNotMatch(skillsView({ ...data, attention: [] }).textContent, /Needs attention/);
});
