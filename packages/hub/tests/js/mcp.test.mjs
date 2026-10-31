// M3-4: the Tool servers page (#1435 #1436) against a tiny DOM stub.
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
const { mcpView, serverProblem } = await import("../../src/orch_hub/web/pages/mcp.js");
const find = (n, pred) => n.all().filter(pred);
const A = "a".repeat(32), B = "b".repeat(32);
const data = {
  workspaces: [{ workspace_id: A, name: "Product" }, { workspace_id: B, name: null }],
  servers: [{ name: "crm", url: "https://tools.example.com/mcp", auth: "hub_secret", secret: "crm-key", description: "Our <b>CRM</b>", shared: "chosen", offered_to: [A] }],
};
const clickText = (view, label) => find(view, (n) => n.tag === "button" && n.textContent === label)[0].listeners.click();

test("a server shows as text, offer boxes reflect who has it, and nothing is on until the owner accepts", () => {
  const calls = [];
  const view = mcpView(data, { save: (n, b) => calls.push(["save", n, b]), offer: (n, sh, ws) => calls.push(["offer", n, sh, ws]), remove: (s) => calls.push(["remove", s.name]) });
  assert.match(view.textContent, /Nothing is switched on for anyone until their workspace owner accepts it\./);
  assert.match(view.textContent, /Agents never see the sign-in secret/);
  assert.match(view.textContent, /Tools that change things start off; each workspace owner turns them on\./);
  const boxes = find(view, (n) => n.tag === "input" && n.attrs["data-ws"]);
  assert.deepEqual(boxes.map((b) => b.checked), [true, false]);
  boxes[1].checked = true;
  clickText(view, "Save who gets it");
  assert.deepEqual(calls[0], ["offer", "crm", "chosen", [A, B]]);
  clickText(view, "Save");
  assert.deepEqual(calls[1], ["save", "crm", { url: "https://tools.example.com/mcp", auth: "hub_secret", secret: "crm-key", description: "Our <b>CRM</b>" }]);
  const remove = find(view, (n) => n.tag === "button" && n.textContent === "Remove")[0];
  remove.listeners.click(); assert.equal(calls.length, 2); assert.equal(remove.textContent, "Sure? Remove it");
  remove.listeners.click(); assert.deepEqual(calls[2], ["remove", "crm"]);
});

test("a bad new server is caught in the page and nothing is sent", () => {
  const calls = [];
  const view = mcpView({ ...data, servers: [] }, { save: (...a) => calls.push(a) });
  assert.match(view.textContent, /No tool servers yet/);
  clickText(view, "Add server");
  assert.equal(calls.length, 0);
  assert.match(find(view, (n) => n.attrs.role === "alert")[0].textContent, /short name/);
});

test("the page check mirrors the hub rules: https only, no key in the address, a secret by name", () => {
  assert.equal(serverProblem("crm", "https://tools.example.com/mcp", "none", null), null);
  assert.match(serverProblem("Crm", "https://x.example.com", "none", null), /short name/);
  assert.match(serverProblem("orch-x", "https://x.example.com", "none", null), /reserved/);
  assert.match(serverProblem("crm", "http://x.example.com", "none", null), /secure address/);
  assert.match(serverProblem("crm", "npx -y server", "none", null), /secure address/);
  assert.match(serverProblem("crm", "https://x.example.com/?key=1", "none", null), /key never goes/);
  assert.match(serverProblem("crm", "https://x.example.com", "hub_secret", ""), /company secret/);
});

test("each server shows one plain status word and a Check now button (#1439)", async () => {
  const { statusWord } = await import("../../src/orch_hub/web/pages/mcp.js");
  assert.equal(statusWord({ status: "reachable" }), "Reachable");
  assert.equal(statusWord({ status: "needs_sign_in" }), "Needs sign-in");
  assert.equal(statusWord({ status: "failing", detail: "<b>upstream</b>" }), "Not reachable");
  assert.equal(statusWord({ status: "weird" }), "Not checked yet");
  assert.equal(statusWord(undefined), "Not checked yet");
  const calls = [];
  const view = mcpView({ ...data, servers: [{ ...data.servers[0], status: { status: "needs_sign_in", checked_ms: 5, detail: "sign_in" } }] }, { check: (n) => calls.push(n) });
  const st = find(view, (n) => n.attrs["data-status"])[0];
  assert.equal(st.textContent, "Needs sign-in");
  clickText(view, "Check now");
  assert.deepEqual(calls, ["crm"]);
  assert.equal(find(mcpView(data), (n) => n.attrs["data-status"])[0].textContent, "Not checked yet");
});

test("who gets a server defaults to all workspaces and saves no list", () => {
  const calls = [];
  const view = mcpView({ ...data, servers: [{ ...data.servers[0], shared: undefined, offered_to: [] }] }, { offer: (n, sh, ws) => calls.push([n, sh, ws]) });
  assert.equal(find(view, (n) => n.tag === "select" && n.attrs["aria-label"] === "Who gets it")[0].value, "all");
  clickText(view, "Save who gets it");
  assert.deepEqual(calls[0], ["crm", "all", []]);
});
