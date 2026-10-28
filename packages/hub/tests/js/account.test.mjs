import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { accountView } = await import("../../src/orch_hub/web/account.js");
const text = (nodes) => nodes.map((n) => n.textContent).join("\n");
const find = (nodes, pred) => nodes.flatMap((n) => n.all()).filter(pred);

test("Account lists phones and browsers, status, dates and this browser", () => {
  const out = accountView({ user: { name: "Ann <Admin>" }, thisBrowserId: "b", channels: [
    { channel_id: "p", kind: "phone", label: "My phone", status: "active", created_ms: 1, last_used_ms: 2 },
    { channel_id: "b", kind: "browser", label: "Office", status: "active" },
    { channel_id: "r", kind: "browser", label: "Old", status: "revoked" },
    { channel_id: "e", kind: "phone", label: "Spare", status: "expired" },
  ] }, { remove() {} });
  const t = text(out);
  for (const words of ["Signed in as Ann <Admin>", "My phones and browsers", "My phone", "Phone · Active", "Browser · Active", "Removed", "Expired", "Added", "Last used", "This browser", "can no longer approve for you", "Pair a phone"]) assert.ok(t.includes(words), words);
  assert.equal(find(out, n => n.tag === "button").length, 2);
  assert.equal(find(out, n => n.textContent === "This browser").length, 1);
});

test("empty state explains how to add a phone and missing dates stay absent", () => {
  const t = text(accountView({ user: { name: "Ann" }, channels: [] }, {}));
  assert.match(t, /No phones or browsers yet/);
  assert.match(t, /phone app.*Pair a phone/);
  assert.doesNotMatch(t, /Added|Last used|password|authenticator/i);
});

test("Remove passes the device id and prevents duplicate clicks", async () => {
  let resolve, calls = [];
  const pending = new Promise(r => { resolve = r; });
  const out = accountView({ channels: [{ kind: "browser", channel_id: "b", label: "<img>", status: "active" }] },
    { remove: id => { calls.push(id); return pending; } });
  const button = find(out, n => n.tag === "button")[0];
  const first = button.listeners.click();
  await button.listeners.click();
  assert.deepEqual(calls, ["b"]);
  assert.equal(button.disabled, true);
  resolve(); await first;
  assert.equal(button.disabled, false);
  assert.equal(button.attrs["aria-label"], "Remove <img>");
});

const source = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
test("removal uses the existing endpoint and deletes only this browser's local key after success", async () => {
  for (const [status, id, deleted] of [[200, "mine", 1], [200, "phone", 0], [403, "mine", 0]]) {
    const calls = [];
    const context = vm.createContext({ state: { me: { channelId: "mine" } }, location: { hash: "#/account" },
      parseRoute: () => ({ kind: "account" }), api: async (...args) => { calls.push(args); return { status, body: { error: "denied" } }; },
      deleteChannel: async () => { calls.push("delete"); }, banner: (...args) => args, say: String,
      account: async () => calls.push("account"), home: async () => calls.push("home") });
    vm.runInContext(source.slice(source.indexOf("async function removeChannel("), source.indexOf("function clearSession(")), context);
    await context.removeChannel(id);
    assert.deepEqual(calls[0].slice(0, 2), ["POST", "/channels/" + id + "/revoke"]);
    assert.equal(calls.filter(c => c === "delete").length, deleted);
    assert.equal(calls.at(-1), "account");
  }
});
