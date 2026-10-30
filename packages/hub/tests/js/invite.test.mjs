// M2-5: the invitation page (invite.js), its route, the People tab's links, and the recovery banner of the shell.
// The pages build real DOM, so they run against a tiny DOM stub (no browser needed).
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import vm from "node:vm";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; this.removed = false; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  remove() { this.removed = true; }
  focus() {} select() {}
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
  querySelectorAll() { return this.all().slice(1).filter((n) => ["input", "select", "textarea", "button", "summary", "details"].includes(n.tag)); }
}
globalThis.document = { createElement: (tag) => new Node_(tag), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { inviteView, proofDigest, makeProof, PROOF_LABEL, NEED_PROOF, DEAD_LINK } = await import("../../src/orch_hub/web/invite.js");
const { parseRoute } = await import("../../src/orch_hub/web/route.js");
const { adminView } = await import("../../src/orch_hub/web/admin.js");

const TOKEN = "T".repeat(43);
const PROOF_VECTOR = "63813eea3b1695c1229a2e8c3e0d8bb1931d3dd515851e62369057c4dbbac73b"; // challenge bytes 0..31, token "T" * 43
const ME = "ab".repeat(16);
const flush = async () => { for (let i = 0; i < 25; i++) await new Promise((r) => setImmediate(r)); };
const find = (root, pred) => root.all().find(pred);
const byId = (root, id) => find(root, (n) => n.attrs.id === id);
const button = (root, label) => find(root, (n) => n.tag === "button" && n.textContent === label);
const submit = (root) => find(root, (n) => n.tag === "form").listeners.submit({ preventDefault() {} });

function deps(answers, extra = {}) {
  const calls = [];
  const d = {
    calls, signedIn: 0, opened: 0, finishedCount: 0,
    api: async (method, path, body) => { calls.push([method, path, JSON.parse(JSON.stringify(body ?? null))]); const a = answers[path]; return typeof a === "function" ? a(body, calls) : a; },
    say: (c) => "say:" + c, signIn() { d.signedIn++; }, openHub() { d.opened++; }, finished() { d.finishedCount++; }, ...extra,
  };
  return d;
}

test("the invite route carries only the token, from the fragment", () => {
  assert.deepEqual(parseRoute("#invite=" + TOKEN), { kind: "invite", token: TOKEN });
  for (const bad of ["#invite=", "#invite=short", "#invite=" + "x".repeat(200), "#invite=" + TOKEN + "&x=1", "#invite=a b" + "c".repeat(40)]) assert.deepEqual(parseRoute(bad), { kind: "home" }, bad);
  assert.deepEqual(parseRoute("#setup=abc-def"), { kind: "setup", token: "abc-def" }); // the old shapes are unchanged
});

test("the proof digest is SHA-256(label || challenge || SHA-256(token)), as the hub computes it", async () => {
  const raw = Uint8Array.from({ length: 32 }, (_, i) => i);
  const challenge = Buffer.from(raw).toString("base64url");
  const tokenHash = createHash("sha256").update(TOKEN).digest();
  const want = createHash("sha256").update(Buffer.concat([Buffer.from(PROOF_LABEL), Buffer.from(raw), tokenHash])).digest("hex");
  const got = Buffer.from(await proofDigest(challenge, TOKEN)).toString("hex");
  assert.equal(got, want);
  assert.equal(got, PROOF_VECTOR, "the same vector as tests/test_invites.py: both sides compute the same bytes");
});

test("join: two passwords, one request, the secret on the person's own screen, then sign in", async (t) => {
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async () => {} } } });
  t.after(() => { delete globalThis.navigator; });
  const d = deps({
    "/invite/peek": { status: 200, body: { kind: "join", name: "Dora\u202e", needs_proof: false } },
    "/invite/redeem": { status: 200, body: { kind: "join", recovery: false, user_id: ME, name: "Dora", totp_secret: "JBSWY3DPEHPK3PXP", totp_uri: "otpauth://x" } },
  });
  const [box] = (() => { const v = inviteView(TOKEN, d); v.ready.then(() => {}); return v; })();
  await flush();
  assert.deepEqual(d.calls, [["POST", "/invite/peek", { token: TOKEN }]]);
  assert.match(box.textContent, /Welcome, Dora<U\+202E>/); // the name goes through renderText
  assert.doesNotMatch(box.textContent, /terminal|VPN|command line/i);
  const pw = byId(box, "invite-pw"), again = byId(box, "invite-pw2");
  pw.value = "a long enough password"; again.value = "something else entirely";
  await submit(box);
  assert.equal(d.calls.length, 1, "mismatch: nothing is sent");
  assert.match(box.textContent, /not the same/);
  again.value = "a long enough password";
  await submit(box);
  await flush();
  assert.deepEqual(d.calls[1], ["POST", "/invite/redeem", { token: TOKEN, password: "a long enough password" }]);
  assert.equal(d.finishedCount, 1);
  assert.equal(byId(box, "invite-secret").textContent, "JBSWY3DPEHPK3PXP");
  assert.match(box.textContent, /6-digit code each time you sign in/);
  assert.equal(box.textContent.match(/will not see this key again/g)?.length, 1, "the key-gone note is said one time");
  assert.ok(!box.all().some((n) => n.tag === "details"), "the key is not folded");
  assert.equal(box.all().filter((n) => n.tag === "button" && /^Copy/.test(n.textContent)).length, 1, "one Copy button");
  assert.match(box.textContent, /groups afterwards/);
  assert.equal(pw.value, "", "the typed password is wiped");
  assert.match(box.textContent, /already signed in/);
  assert.ok(!button(box, "Continue to sign in"), "a new person is not asked to sign in again (r10-P09)");
  await button(box, "I've added it, open the hub").listeners.click();
  assert.equal(d.opened, 1);
  assert.equal(d.signedIn, 0);
});

test("a reset or recovery still ends on the sign-in step", async () => {
  const d = deps({
    "/invite/peek": { status: 200, body: { kind: "reset", name: "Dora", needs_proof: false } },
    "/invite/redeem": { status: 200, body: { kind: "reset", recovery: true, user_id: ME, name: "Dora", totp_secret: "SECRET234", totp_uri: "" } },
  });
  const [box] = inviteView(TOKEN, d);
  await flush();
  byId(box, "invite-pw").value = byId(box, "invite-pw2").value = "a long enough password";
  await submit(box);
  await flush();
  await button(box, "Continue to sign in").listeners.click();
  assert.equal(d.signedIn, 1);
  assert.equal(d.opened, 0);
});

test("copy button copies the secret and says so", async (t) => {
  const d = deps({
    "/invite/peek": { status: 200, body: { kind: "join", name: "Dora", needs_proof: false } },
    "/invite/redeem": { status: 200, body: { kind: "join", recovery: false, user_id: ME, name: "Dora", totp_secret: "SECRET234", totp_uri: "" } },
  });
  const writes = [];
  const was = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async (x) => { writes.push(x); } } } });
  t.after(() => { if (was) Object.defineProperty(globalThis, "navigator", was); else delete globalThis.navigator; });
  const [box] = inviteView(TOKEN, d);
  await flush();
  byId(box, "invite-pw").value = byId(box, "invite-pw2").value = "a long enough password";
  await submit(box);
  await flush();
  await button(box, "Copy").listeners.click();
  assert.deepEqual(writes, ["SECRET234"]);
  assert.equal(button(box, "Copied ✓")?.textContent, "Copied ✓");
});

test("a link that does not work says so in one sentence and offers no form", async () => {
  for (const status of [403, 429]) {
    const d = deps({ "/invite/peek": { status, body: { error: status === 403 ? "bad_invite" : "rate_limited" } } });
    const [box] = inviteView(TOKEN, d);
    await flush();
    assert.equal(find(box, (n) => n.tag === "form"), undefined);
    assert.match(box.textContent, status === 403 ? new RegExp(DEAD_LINK.slice(0, 30)) : /Too many tries/);
  }
});

test("a short password is refused here and by the hub's answer, without losing the page", async () => {
  const d = deps({
    "/invite/peek": { status: 200, body: { kind: "join", name: "Dora", needs_proof: false } },
    "/invite/redeem": { status: 400, body: { error: "weak_password" } },
  });
  const [box] = inviteView(TOKEN, d);
  await flush();
  byId(box, "invite-pw").value = byId(box, "invite-pw2").value = "short";
  await submit(box);
  assert.equal(d.calls.length, 1);
  byId(box, "invite-pw").value = byId(box, "invite-pw2").value = "ten chars!!";
  await submit(box);
  await flush();
  assert.match(box.textContent, /at least 10 characters/);
  assert.ok(byId(box, "invite-pw"), "the form is still there");
});

const RESET = (extra = {}) => ({ status: 200, body: { kind: "reset", name: "Ann", user_id: ME, needs_proof: true, recovery: false, challenge: Buffer.alloc(32, 7).toString("base64url"), ...extra } });

test("reset with a device here: the page signs the challenge with this browser's key and sends the proof", async () => {
  const signed = [];
  const key = { pair: { privateKey: "KEY" }, channelId: "cd".repeat(16), userId: ME };
  const d = deps(
    { "/invite/peek": RESET(), "/invite/redeem": { status: 200, body: { kind: "reset", recovery: false, user_id: ME, name: "Ann", totp_secret: "NEWCODE234", totp_uri: "" } } },
    { loadChannel: async () => key, signProof: async (k, digest) => { signed.push([k, Buffer.from(digest).toString("hex")]); return "SIG"; } },
  );
  const [box] = inviteView(TOKEN, d);
  await flush();
  byId(box, "invite-pw").value = byId(box, "invite-pw2").value = "a long enough password";
  await submit(box);
  await flush();
  const want = Buffer.from(await proofDigest(RESET().body.challenge, TOKEN)).toString("hex");
  assert.deepEqual(signed, [["KEY", want]]);
  assert.deepEqual(d.calls[1][2].proof, { channel_id: key.channelId, challenge: RESET().body.challenge, sig: "SIG" });
  assert.equal(byId(box, "invite-secret").textContent, "NEWCODE234");
});

test("reset with no device of this person here: plain words, no form to submit, nothing sent", async () => {
  for (const loaded of [null, { pair: { privateKey: "K" }, channelId: "cd".repeat(16), userId: "ef".repeat(16) }]) {
    const d = deps({ "/invite/peek": RESET() }, { loadChannel: async () => loaded });
    const [box] = inviteView(TOKEN, d);
    await flush();
    assert.match(box.textContent, new RegExp(NEED_PROOF.slice(0, 40)));
    assert.equal(byId(box, "invite-pw").tag, "input");
    assert.equal(button(box, "Save new password").attrs.disabled, "");
    assert.equal(await makeProof(RESET().body, TOKEN, loaded), null);
    assert.equal(d.calls.length, 1);
  }
});

test("when the hub wants a proof the page asks again with a fresh challenge and says what to do", async () => {
  let n = 0;
  const key = { pair: { privateKey: "KEY" }, channelId: "cd".repeat(16), userId: ME };
  const d = deps(
    { "/invite/peek": () => RESET({ challenge: Buffer.alloc(32, ++n).toString("base64url") }), "/invite/redeem": { status: 403, body: { error: "proof_required" } } },
    { loadChannel: async () => key, signProof: async () => "SIG" },
  );
  const [box] = inviteView(TOKEN, d);
  await flush();
  byId(box, "invite-pw").value = byId(box, "invite-pw2").value = "a long enough password";
  await submit(box);
  await flush();
  assert.deepEqual(d.calls.map((c) => c[1]), ["/invite/peek", "/invite/redeem", "/invite/peek"]);
  assert.match(box.textContent, new RegExp(NEED_PROOF.slice(0, 40)));
});

test("recovery shows the warning that every device is removed", async () => {
  const d = deps({ "/invite/peek": { status: 200, body: { kind: "recovery", name: "Ann", user_id: ME, needs_proof: false, recovery: true } } });
  const [box] = inviteView(TOKEN, d);
  await flush();
  assert.match(box.textContent, /Every device you approved on will be removed/);
  assert.equal(button(box, "Save new password").attrs.disabled ?? null, null);
});

// --- People tab ---------------------------------------------------------------------------------------------------------

const ANN = "11".repeat(16), BOB = "22".repeat(16), ADMIN = "33".repeat(16);
const bodies = () => ({
  users: { users: [{ user_id: ANN, name: "Ann", groups: [], disabled: false }, { user_id: BOB, name: "Bob", groups: [], disabled: true }, { user_id: ADMIN, name: "Root", groups: ["hub-admin"], disabled: false }] },
  groups: { groups: [{ name: "hub-admin", members: [ADMIN], grants: [] }] }, grants: { grants: [] }, workspaces: { workspaces: [] },
  channels: { channels: [{ channel_id: "c1", user_id: ANN, kind: "browser", label: "Laptop", status: "active" }, { channel_id: "c2", user_id: ANN, kind: "phone", label: "", status: "revoked" }] },
  "market-policy": { version: 1, official_url: "https://x.test/o", policy: { markets: [], addons: { allow: null, block: [] }, ceiling: null } }, audit: { records: [] },
});
async function admin(extra = {}) {
  const calls = [], b = bodies();
  const d = { self: ADMIN, say: (c) => "say:" + c, refresh: async () => {}, api: async (method, path, body) => {
    calls.push([method, path, body]);
    if (method === "GET") return { status: 200, body: b[path.replace("/admin/", "").split("?")[0]] };
    if (path === "/admin/invites") return { status: 200, body: { link: "https://hub.test/#invite=" + TOKEN, expires_ms: Date.now() + 7 * 86400000 } };
    if (path.endsWith("/reset")) return { status: 200, body: { link: "https://hub.test/#invite=" + "R".repeat(43), expires_ms: Date.now() + 7 * 86400000, kind: body.recovery ? "recovery" : "reset" } };
    if (path.endsWith("/enable")) return { status: 200, body: { link: "https://hub.test/#invite=" + "E".repeat(43), expires_ms: Date.now() + 7 * 86400000, kind: "recovery" } };
    return { status: 200, body: { ok: true } };
  }, ...extra };
  const [, , , panels] = adminView(d);
  await flush();
  return { calls, panel: panels.children[0], panels };
}

test("People: Invite a person shows the link, the promise and the group hint; the old add form is gone", async () => {
  const { calls, panel } = await admin();
  assert.ok(button(panel, "Invite a person"));
  assert.match(panel.textContent, /With a workspace\? Use Workspaces › Connect a workspace\./);
  assert.ok(!button(panel, "Add person"));
  byId(panel, "invite-name").value = "  Dora ";
  await find(panel, (n) => n.tag === "form" && byId(n, "invite-name")).listeners.submit({ preventDefault() {} });
  await flush();
  assert.deepEqual(calls.filter((c) => c[0] === "POST"), [["POST", "/admin/invites", { name: "Dora" }]]);
  assert.equal(byId(panel, "person-link").attrs.value, "https://hub.test/#invite=" + TOKEN);
  assert.match(panel.textContent, /Send this link to the person\. They choose their own password and scan their own code; you will not see them\./);
  assert.match(panel.textContent, /Add them to groups afterwards/);
  assert.match(panel.textContent, /\(7 days\)/);
  assert.ok(button(panel, "Copy link"));
});

test("People: devices count, reset, recovery with its warning, disable in two steps, enable returns a recovery link", async () => {
  const { calls, panel } = await admin();
  const rows = panel.all().filter((n) => n.tag === "article");
  const row = (name) => rows.find((r) => r.textContent.startsWith(name));
  assert.match(row("Ann").textContent, /1 device ·/);
  assert.match(row("Bob").textContent, /0 devices/);
  assert.match(row("Bob").textContent, /Disabled/);
  assert.match(panel.textContent, /A recovery link gives whoever opens it this person's account\. Hand it over only to them\./);
  assert.match(panel.textContent, /They lost their devices \(recovery\)/);
  // a plain reset
  await button(row("Ann"), "Reset password and code").listeners.click({ target: button(row("Ann"), "Reset password and code") });
  await flush();
  assert.deepEqual(calls.at(-1), ["POST", `/admin/users/${ANN}/reset`, { recovery: false }]);
  assert.doesNotMatch(panel.textContent, /Recovery link for Ann/);
  // recovery: tick the box
  const tick = byId(row("Ann"), "recovery-" + ANN);
  tick.checked = true;
  const reset = button(row("Ann"), "Reset password and code");
  await reset.listeners.click({ target: reset });
  await flush();
  assert.deepEqual(calls.at(-1), ["POST", `/admin/users/${ANN}/reset`, { recovery: true }]);
  assert.match(byId(panel, "person-link-panel").textContent, /Recovery link for Ann/);
  assert.match(byId(panel, "person-link-panel").textContent, /whoever opens it this person's account/);
  // disable takes two clicks; yourself gets no button
  const dis = button(row("Ann"), "Disable");
  await dis.listeners.click();
  assert.equal(dis.textContent, "Really? Disable");
  assert.ok(!calls.some((c) => c[1].endsWith("/disable")));
  await dis.listeners.click();
  await flush();
  assert.deepEqual(calls.find((c) => c[1].endsWith("/disable")), ["POST", `/admin/users/${ANN}/disable`, undefined]);
  assert.ok(!button(row("Root"), "Disable"));
  assert.match(row("Root").textContent, /You/);
  // a disabled person can be enabled, which hands over a recovery link; no reset button for them
  assert.ok(!button(row("Bob"), "Reset password and code"));
  const en = button(row("Bob"), "Enable");
  await en.listeners.click({ target: en });
  await flush();
  assert.deepEqual(calls.find((c) => c[1].endsWith("/enable")), ["POST", `/admin/users/${BOB}/enable`, {}]);
  assert.match(byId(panel, "person-link-panel").textContent, /Recovery link for Bob/);
  assert.equal(byId(panel, "person-link").attrs.value, "https://hub.test/#invite=" + "E".repeat(43));
});

// --- the recovery banner in the shell -----------------------------------------------------------------------------------

class El {
  constructor(tag) { this.tag = tag; this.nodeType = 1; this.childNodes = []; this.attrs = {}; this.className = ""; this.listeners = {}; this.removed = false; this._text = ""; }
  append(...nodes) { for (const n of nodes) { n.parent = this; this.childNodes.push(n); } }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
  replaceWith(node) { const p = this.parent; p.childNodes[p.childNodes.indexOf(this)] = node; node.parent = p; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(k, fn) { this.listeners[k] = fn; }
  remove() { this.removed = true; }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text + this.childNodes.map((c) => c.textContent).join(""); }
  get classList() { return { add: (c) => this.classList.toggle(c, true), remove: (c) => this.classList.toggle(c, false), toggle: (c, on) => { const s = new Set(this.className.split(" ")); on ? s.add(c) : s.delete(c); this.className = [...s].join(" "); } }; }
  all() { return [this, ...this.childNodes.flatMap((c) => c.all())]; }
  querySelectorAll(sel) { return this.all().slice(1).filter((n) => sel.startsWith(".") ? n.className.split(" ").includes(sel.slice(1)) : n.getAttribute(sel.slice(1, -1)) !== null); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] ?? null; }
}
const appSource = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
const at = (needle) => appSource.indexOf(needle);

function shellContext(user) {
  const root = new El("main"), calls = [];
  const state = { user, rail: { hubName: "Company", rows: [] }, menu: false };
  const context = vm.createContext({ clearInterval, root, state, PAGES: [], location: { hash: "#/home" }, parseRoute, enter() {}, replaceContent: (el, nodes) => el.replaceChildren(...nodes), renderText: String,
    initials: (s) => s.slice(0, 2), colour: () => "c0", navigate() {}, signOutMenu: () => new El("button"), openSearch() {}, openShortcuts() {},
    api: async (method, path) => { calls.push([method, path]); return { status: 200, body: { ok: true } }; }, skeleton: () => new El("p"),
    document: { createElement: (t) => new El(t), createTextNode: (text) => Object.assign(new El("#text"), { _text: text }) } });
  vm.runInContext(appSource.slice(at("function h("), at("const field =")) + appSource.slice(at("function shell("), at("async function loadRail(")), context);
  return { root, state, context, calls };
}

test("the shell shows the recovery notice first, in plain words, and Dismiss clears it", async () => {
  const at_ms = Date.UTC(2031, 2, 4, 12);
  const { root, state, context, calls } = shellContext({ name: "Ann", is_admin: false, recovery: { by_name: "Root\u202e", at_ms } });
  context.shell("home", new El("h1"));
  const note = root.querySelector(".recovery-notice");
  assert.ok(note, "banner present");
  const content = root.querySelector(".content");
  assert.equal(content.childNodes[0], note, "above the page");
  assert.match(note.textContent, /^Your account was reset by Root\u202e on .*\d.*\. If you did not expect this, tell your IT person\.Dismiss$/);
  const dismiss = note.childNodes.find((n) => n.tag === "button");
  await dismiss.listeners.click();
  assert.deepEqual(calls, [["POST", "/me/recovery/dismiss"]]);
  assert.equal(state.user.recovery, null);
  assert.equal(note.removed, true);
  context.shell("home", new El("h1")); // the next paint has no banner
  assert.equal(root.querySelector(".recovery-notice"), null);
});

test("no notice when nothing was recovered, and a removed admin is named plainly", () => {
  const none = shellContext({ name: "Ann", is_admin: false, recovery: null });
  none.context.shell("home", new El("h1"));
  assert.equal(none.root.querySelector(".recovery-notice"), null);
  const gone = shellContext({ name: "Ann", is_admin: false, recovery: { by_name: null, at_ms: 1 } });
  gone.context.shell("home", new El("h1"));
  assert.match(gone.root.querySelector(".recovery-notice").textContent, /reset by an administrator on/);
});

test("app.js dispatches #invite= with one line and keeps the link out of the address bar after use", () => {
  assert.match(appSource, /route\.kind === "invite"\) show\(inviteView\(route\.token, \{ api, say, signIn: \(\) => loginView\(\), openHub: \(\) => afterLogin\(\), finished: \(\) => history\.replaceState\(null, "", location\.pathname\) \}\)\)/);
});

test("a company invitation shows one button, no password form, and sends the token only in the start POST body", async () => {
  const calls = []; const went = [];
  const d = { api: async (m, p, b) => { calls.push([m, p, b]); return p === "/invite/peek" ? { status: 200, body: { kind: "join", name: "Julia", needs_proof: false, company: true, provider: "google" } } : { status: 200, body: { url: "https://accounts.google.com/o/oauth2/v2/auth?state=x" } }; }, assign: (u) => went.push(u) };
  const view = inviteView(TOKEN, d);
  await view.ready;
  const all = view[0].all();
  assert.equal(all.filter((n) => n.tag === "input").length, 0); // no password field
  const btn = all.find((n) => n.attrs.id === "invite-company");
  assert.equal(btn.textContent, "Continue with Google");
  await btn.listeners.click();
  assert.deepEqual(calls.at(-1), ["POST", "/oidc/invite/start", { token: TOKEN }]);
  assert.deepEqual(went, ["https://accounts.google.com/o/oauth2/v2/auth?state=x"]);
  assert.ok(!went[0].includes(TOKEN));
});

test("a company invitation does not follow an unsafe address", async () => {
  const went = [];
  const d = { api: async (m, p) => (p === "/invite/peek" ? { status: 200, body: { kind: "join", name: "J", needs_proof: false, company: true, provider: "entra" } } : { status: 200, body: { url: "javascript:alert(1)" } }), assign: (u) => went.push(u) };
  const view = inviteView(TOKEN, d);
  await view.ready;
  await view[0].all().find((n) => n.attrs.id === "invite-company").listeners.click();
  assert.deepEqual(went, []);
});
