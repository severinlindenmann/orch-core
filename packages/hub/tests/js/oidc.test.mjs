import test from "node:test";
import assert from "node:assert/strict";
class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; this.parentElement = null; this.hidden = false; }
  append(...kids) { for (const k of kids) { const n = k && k.nodeType ? k : new Node_("#text", String(k)); n.parentElement = this; this.children.push(n); } }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const where = [];
globalThis.location = { assign: (u) => where.push(u) };
const O = await import("../../src/orch_hub/web/oidc.js");
const find = (n, pred) => n.all().filter(pred);

test("only the hub's own https or localhost address is followed", () => {
  assert.equal(O.safeLoginUrl("https://login.microsoftonline.com/x"), "https://login.microsoftonline.com/x");
  assert.equal(O.safeLoginUrl("http://localhost:8100/x"), "http://localhost:8100/x");
  for (const bad of ["javascript:alert(1)", "http://evil.example/x", "data:text/html,x", "", null, "//evil.example"]) assert.equal(O.safeLoginUrl(bad), null, String(bad));
});

test("callback parameters need both a code and a state", () => {
  assert.deepEqual(O.callbackParams("?code=abc&state=def"), { code: "abc", state: "def" });
  for (const bad of ["", "?code=abc", "?state=def", "?error=access_denied&state=x"]) assert.equal(O.callbackParams(bad), null, bad);
  assert.equal(O.callbackParams("?code=" + "a".repeat(3000) + "&state=x"), null);
});

test("the finish goes to the route of the purpose the hub reports for the login cookie", async () => {
  const calls = [];
  let purpose = null;
  const api = async (m, p, b) => { calls.push([m, p, b]); return p === "/oidc/pending" ? { status: 200, body: { purpose } } : { status: 200, body: { ok: true } }; };
  for (const [want, path] of [["signin", "/oidc/finish"], ["link", "/oidc/link/finish"], ["test", "/admin/oidc/test/finish"]]) {
    purpose = want;
    const r = await O.finishCallback(api, "?code=c&state=s");
    assert.ok(r.ok);
    assert.deepEqual(calls.at(-1), ["POST", path, { code: "c", state: "s" }]);
  }
  const n = calls.length;
  assert.equal((await O.finishCallback(api, "?error=access_denied&state=s")).ok, false); // the person cancelled
  assert.deepEqual(calls.slice(n), [["POST", "/oidc/cancel", {}]]); // only the cookie is dropped, nothing is finished
  purpose = null; // no (or a foreign) cookie: refused, nothing finished
  assert.equal((await O.finishCallback(api, "?code=c&state=s")).ok, false);
  assert.equal(calls.at(-1)[1], "/oidc/pending");
});

test("begin navigates only to a safe address and remembers the purpose", async () => {
  where.length = 0;
  const ok = await O.begin(async () => ({ status: 200, body: { url: "https://login.microsoftonline.com/a?x=1" } }), "signin");
  assert.equal(ok.ok, true);
  assert.deepEqual(where, ["https://login.microsoftonline.com/a?x=1"]);
  const bad = await O.begin(async () => ({ status: 200, body: { url: "javascript:alert(1)" } }), "link", { password: "x", totp: "1" });
  assert.equal(bad.ok, false);
  assert.equal(where.length, 1);
  const refused = await O.begin(async () => ({ status: 403, body: { error: "oidc_not_allowed" } }), "signin");
  assert.deepEqual(refused, { ok: false, error: "oidc_not_allowed" });
});

test("the sign-in button exists only when company login is on", () => {
  assert.equal(O.signInButton({ enabled: false }, () => {}), null);
  const b = O.signInButton({ enabled: true, provider: "entra" }, () => {});
  const btn = find(b, (n) => n.tag === "button")[0];
  assert.equal(btn.textContent, "Sign in with Microsoft");
  assert.equal(O.signInButton({ enabled: true, provider: "google" }, () => {}).textContent.includes("Google"), true);
});

test("the admin card never shows a secret and says what is needed", () => {
  const card = O.companyCard({ configured: true, provider: "entra", client_id: "cid", tenant: "t", domain: "", mode: "invited", idp_mfa: false, enabled: false, tested: false, has_secret: true, redirect_uri: "https://hub.example/oidc/callback", secure_address: true }, { save: async () => ({ status: 200 }), test: async () => ({ ok: true }) });
  const t = card.textContent;
  for (const words of ["Company login", "Redirect address", "https://hub.example/oidc/callback", "Client secret", "second step", "Test the connection", "guests", "test the connection first", "auth_time", "amr"]) assert.ok(t.includes(words), words);
  const secret = find(card, (n) => n.attrs.id === "oidc-secret")[0];
  assert.equal(secret.attrs.type, "password");
  assert.equal(secret.attrs.value, undefined); // the saved secret is never put back into the page
  const on = find(card, (n) => n.attrs.id === "oidc-on")[0];
  assert.ok("disabled" in on.attrs); // cannot be turned on before a passing test
});

test("an address that Microsoft and Google refuse is explained", () => {
  const card = O.companyCard({ configured: false, has_secret: false, redirect_uri: "http://10.0.0.5:8100/oidc/callback", secure_address: false }, {});
  assert.ok(card.textContent.includes("needs a secure address"));
});

test("the account card links with a password and a code, or removes the link", async () => {
  assert.equal(O.accountCard({ enabled: false, linked: null }, {}), null);
  let sent = null;
  const c = O.accountCard({ enabled: true, provider: "google", linked: null }, { link: async (b) => { sent = b; return { ok: true }; } });
  find(c, (n) => n.attrs.id === "oidc-link-pw")[0];
  const go = find(c, (n) => n.tag === "button")[0];
  assert.ok(go.textContent.includes("Google"));
  const linked = O.accountCard({ enabled: true, provider: "entra", linked: { provider: "entra" } }, { unlink: async () => ({ status: 200 }) });
  assert.ok(linked.textContent.includes("Linked to your Microsoft account"));
  assert.equal(sent, null);
});

test("a company-login person is told how to get a key, in plain words", () => {
  assert.ok(O.KEY_WORDS.includes("ask your hub admin for a set-up link"));
});

test("an invitation login finishes on its own route and says plainly what failed", async () => {
  const calls = [];
  const api = async (m, p, b) => { calls.push([m, p, b]); return p === "/oidc/pending" ? { status: 200, body: { purpose: "invite" } } : { status: 403, body: { error: "bad_invite" } }; };
  const r = await O.finishCallback(api, "?code=c&state=s");
  assert.equal(r.purpose, "invite");
  assert.deepEqual(calls.at(-1), ["POST", "/oidc/invite/finish", { code: "c", state: "s" }]);
  assert.equal(r.ok, false);
  assert.ok(O.INVITE_WORDS.bad_invite.includes("Ask your admin for a new one"));
  assert.ok(O.INVITE_WORDS.oidc_already_linked.includes("already used"));
  assert.ok(O.WELCOME.includes("ask your hub admin for a set-up link"));
});
