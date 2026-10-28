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

// --- M2-6: password, authenticator, sign out everywhere ------------------------------------------------------------------
const byId = (out, id) => find(out, n => n.attrs.id === id)[0];
const submit = (form) => form.listeners.submit({ preventDefault() {} });
const ok = (body = { ok: true }) => ({ status: 200, body });
const refused = (error, status = 401) => ({ status, body: { error } });
function secure(security, data = {}) {
  const out = accountView({ user: { name: "Ann" }, channels: [], ...data }, { security });
  const form = (id) => find(out, n => n.tag === "form" && n.all().some(x => x.attrs.id === id))[0];
  return { out, form, t: () => text(out) };
}
function fill(out, values) { for (const [id, v] of Object.entries(values)) byId(out, id).value = v; }

test("three plain-word cards for the signed-in person, only when the page is given the actions", () => {
  const { t, out } = secure({});
  for (const words of ["Change your password", "Use a new authenticator app", "Sign out everywhere", "Current password", "New password (10 or more characters)"]) assert.ok(t().includes(words), words);
  assert.equal(find(out, n => n.tag === "section" && /^sec-/.test(n.attrs.id ?? "")).length, 3);
  for (const id of ["sec-pw-now", "sec-pw-new", "sec-pw-again", "sec-auth-pw"]) assert.equal(byId(out, id).attrs.type, "password", id);
  assert.equal(find(accountView({ channels: [] }, {}), n => /^sec-/.test(n.attrs.id ?? "")).length, 0);
});

test("change password: checks locally, sends the three fields, clears them and says what happened", async () => {
  const calls = [];
  const { out, form, t } = secure({ password: async (b) => { calls.push(b); return ok(); } });
  fill(out, { "sec-pw-now": "old pass phrase", "sec-pw-code": " 123456 ", "sec-pw-new": "short", "sec-pw-again": "short" });
  await submit(form("sec-pw-now"));
  assert.equal(calls.length, 0); assert.match(t(), /at least 10 characters/);
  fill(out, { "sec-pw-new": "a long new phrase", "sec-pw-again": "a different one!!" });
  await submit(form("sec-pw-now"));
  assert.equal(calls.length, 0); assert.match(t(), /two new passwords are not the same/);
  fill(out, { "sec-pw-again": "a long new phrase" });
  await submit(form("sec-pw-now"));
  assert.deepEqual(calls, [{ current_password: "old pass phrase", totp: "123456", new_password: "a long new phrase" }]);
  assert.match(t(), /Password changed\. Other devices were signed out\./);
  for (const id of ["sec-pw-now", "sec-pw-code", "sec-pw-new", "sec-pw-again"]) assert.equal(byId(out, id).value, "", id);
});

test("change password: a refusal is said in plain words and the code is cleared", async () => {
  for (const [error, words] of [["bad_credentials", /password or the code was not right/], ["rate_limited", /Too many tries/], ["same_password", /must be different/], ["reauth_required", /Sign in again/], ["weird_code", /did not work/]]) {
    const { out, form, t } = secure({ password: async () => refused(error) });
    fill(out, { "sec-pw-now": "x".repeat(12), "sec-pw-code": "111111", "sec-pw-new": "y".repeat(12), "sec-pw-again": "y".repeat(12) });
    await submit(form("sec-pw-now"));
    assert.match(t(), words, error);
    assert.doesNotMatch(t(), /Password changed/);
    assert.equal(byId(out, "sec-pw-code").value, "");
  }
});

test("new authenticator: the secret is shown once with Copy, then turned on with a code from it", async () => {
  const calls = [], copied = [];
  const { out, form, t } = secure({
    begin: async (b) => { calls.push(["begin", b]); return ok({ secret: "JBSWY3DPEHPK3PXP", uri: "otpauth://x" }); },
    confirm: async (c) => { calls.push(["confirm", c]); return ok(); },
    copy: (s) => copied.push(s),
  });
  assert.equal(byId(out, "sec-auth-secret"), undefined);
  fill(out, { "sec-auth-pw": "my password", "sec-auth-code": "123456" });
  await submit(form("sec-auth-pw"));
  assert.deepEqual(calls, [["begin", { password: "my password", totp: "123456" }]]);
  assert.equal(byId(out, "sec-auth-pw"), undefined);  // the first step gives way to the second
  assert.equal(byId(out, "sec-auth-secret").textContent, "JBSWY3DPEHPK3PXP");
  assert.match(t(), /shown only now/); assert.match(t(), /old authenticator keeps working/);
  find(out, n => n.tag === "button" && n.textContent === "Copy")[0].listeners.click();
  assert.deepEqual(copied, ["JBSWY3DPEHPK3PXP"]);
  assert.doesNotMatch(t(), /otpauth/);
  byId(out, "sec-auth-new").value = " 654321 ";
  await submit(form("sec-auth-new"));
  assert.deepEqual(calls[1], ["confirm", "654321"]);
  assert.match(t(), /new authenticator is on\. The old one no longer works\. Other devices were signed out\./);
  assert.equal(byId(out, "sec-auth-secret"), undefined);  // the secret leaves the screen
  assert.ok(byId(out, "sec-auth-pw"));                      // and the first step is back
});

test("new authenticator: a failed begin shows no secret; a wrong code keeps the secret on screen; an expired one starts over", async () => {
  let begun = 0, answer = refused("bad_code", 400);
  const { out, form, t } = secure({
    begin: async () => { begun++; return begun === 1 ? refused("bad_credentials") : ok({ secret: "ABCDEFGH" }); },
    confirm: async () => answer,
  });
  fill(out, { "sec-auth-pw": "pw", "sec-auth-code": "000000" });
  await submit(form("sec-auth-pw"));
  assert.equal(byId(out, "sec-auth-secret"), undefined); assert.match(t(), /password or the code was not right/);
  await submit(form("sec-auth-pw"));
  assert.equal(byId(out, "sec-auth-secret").textContent, "ABCDEFGH");
  byId(out, "sec-auth-new").value = "111111";
  await submit(form("sec-auth-new"));
  assert.match(t(), /did not match the new authenticator/);
  assert.equal(byId(out, "sec-auth-secret").textContent, "ABCDEFGH");
  assert.equal(byId(out, "sec-auth-new").value, "");
  answer = refused("pending_expired", 410);
  await submit(form("sec-auth-new"));
  assert.match(t(), /ran out of time/);
  assert.equal(byId(out, "sec-auth-secret"), undefined); assert.ok(byId(out, "sec-auth-pw"));
  assert.doesNotMatch(t(), /is on\./);
});

test("sign out everywhere: one click, once; a refusal is said in words", async () => {
  let calls = 0, answer = ok();
  const { out, t } = secure({ endAll: async () => { calls++; return answer; } });
  const button = find(out, n => n.tag === "button" && n.textContent === "Sign out everywhere")[0];
  await button.listeners.click();
  assert.equal(calls, 1);
  answer = refused("unavailable", 503);
  await button.listeners.click();
  assert.equal(calls, 2); assert.match(t(), /did not work/);
  assert.match(t(), /Your phones and browsers stay registered/);
});

test("a session that came from a workspace sees only a sign-in prompt instead of the three cards", async () => {
  let again = 0;
  const { out, t } = secure({ signInAgain: () => again++ }, { needsSignIn: true });
  assert.match(t(), /sign in again with your password and code/);
  assert.equal(find(out, n => n.tag === "form").length, 0);
  assert.equal(byId(out, "sec-pw-now"), undefined);
  find(out, n => n.tag === "button" && n.textContent === "Sign in again")[0].listeners.click();
  assert.equal(again, 1);
});

test("the Account page wires the three actions to the hub's own routes", () => {
  const body = source.slice(source.indexOf("async function account()"), source.indexOf("async function approvals("));
  for (const route of ["/me/password", "/me/authenticator/begin", "/me/authenticator/confirm", "/me/sessions/end-all"]) assert.ok(body.includes('"' + route + '"'), route);
  assert.match(body, /needsSignIn: !!state\.user\.needs_sign_in/);
});
