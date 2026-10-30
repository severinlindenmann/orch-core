// M3 secrets S-2, condition 8: the Company secrets page against a tiny DOM stub. The value is locked in the browser, never
// stored, never sent in clear, cleared after every outcome, and never retried on its own.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createECDH } from "node:crypto";
import { openFixed } from "./seal_seam.mjs";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; this.value = ""; }
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
const mod = await import("../../src/orch_hub/web/pages/secrets.js");
const { secretsView, saveSecret, valueProblem, nameProblem, page } = mod;
const find = (n, pred) => n.all().filter(pred);
const button = (view, label) => find(view, (n) => n.tag === "button" && n.textContent === label)[0];
const VALUE = "sk-live-0123456789abcdef";
const WS = ["a".repeat(32), "b".repeat(32)];

// A workspace key pair, so the locked copies can be opened again.
const ecdh = createECDH("prime256v1"); ecdh.generateKeys();
const WXK_SCALAR = ecdh.getPrivateKey("hex").padStart(64, "0");
const b64u = (u) => Buffer.from(u).toString("base64url");
const recipients = WS.map((id, i) => ({ workspace_id: id, wxk_pub: b64u(ecdh.getPublicKey()), wxk_version: i + 1 }));
const REC = { hub_id: "c".repeat(32), secret_id: "d".repeat(32), version: 1, now_ms: 1_800_000_000_000, recipients, without_key: [] };

const data = {
  workspaces: [{ workspace_id: WS[0], name: "Product" }, { workspace_id: WS[1], name: null }],
  secrets: [{ name: "crm-key", version: 2, expires_ms: 1_800_000_000_000 + 40 * 86400000, expired: false, shared: "chosen", workspaces: [WS[0], WS[1]], used_this_week: 12,
    state: { [WS[0]]: { state: "ready", used_this_week: 12 }, [WS[1]]: { state: "needs_value", used_this_week: 0 } } }],
};

// Fills the new-secret form (the one without a secret in the list).
function fill(view, name, value) {
  const nameIn = find(view, (n) => n.tag === "input" && n.attrs["aria-label"] === "Name of the new secret")[0];
  nameIn.value = name;
  const input = find(view, (n) => n.tag === "input" && n.attrs.type === "password")[0];
  input.value = value;
  return input;
}
// Ticks every workspace box in the new-secret form (it starts on "Only chosen workspaces").
function tickAll(view) {
  const form = find(view, (n) => n.className === "share-box")[0];
  for (const b of find(form, (n) => n.tag === "input" && n.attrs["data-ws"])) b.checked = true;
}

test("the page is a registry entry for admins and names a plain tab", () => {
  assert.equal(page.path, "secrets");
  assert.equal(page.nav.admin, true);
  assert.equal(typeof page.render, "function");
});

test("every value box is a password field that browsers do not remember or spell-check", () => {
  const view = secretsView(data, {});
  const boxes = find(view, (n) => n.tag === "input" && n.attrs["aria-label"]?.startsWith("Value"));
  assert.ok(boxes.length >= 2);
  for (const b of boxes) { assert.equal(b.attrs.type, "password"); assert.equal(b.attrs.autocomplete, "off"); assert.equal(b.attrs.spellcheck, "false"); }
});

test("the page says in plain words who can read it and never uses the crypto words", () => {
  const t = secretsView(data, {}).textContent;
  assert.match(t, /The hub could read this value if it were taken over\./);
  assert.match(t, /\u2713 Available/); assert.ok(!/Ready/.test(t)); assert.match(t, /Needs the value/); assert.match(t, /Enter the value/); assert.match(t, /Waiting for you to type the value again for this workspace/);
  assert.match(t, /Valid until .*Used 12 times this week/); assert.match(t, /Shared with: Product, A workspace/);
  assert.match(t, /Product/); assert.match(t, /A workspace/);
  assert.ok(!/WXK|\bseal|\bwrap|encrypt|ciphertext/i.test(t.replace(/\blocked\b/g, "")), t);
  assert.ok(!t.includes(WS[0]), "no ids on screen");
});

test("a bad name or value is caught in the page and nothing is sent", async () => {
  const calls = [];
  const view = secretsView({ ...data, secrets: [] }, { save: async (...a) => { calls.push(a); return { ok: true }; } });
  assert.match(view.textContent, /No company secrets yet/);
  tickAll(view);
  fill(view, "Bad Name", VALUE);
  await button(view, "Save secret").listeners.click();
  fill(view, "crm-key", "short");
  await button(view, "Save secret").listeners.click();
  assert.equal(calls.length, 0);
  assert.equal(nameProblem("crm-key"), null); assert.match(nameProblem("Crm"), /short name/);
  assert.equal(valueProblem(VALUE), null);
  for (const bad of ["short", " leading-space", "trailing-space ", "tab\tinside", "line\nbreak", "café-key-1234", "x".repeat(4097)]) assert.notEqual(valueProblem(bad), null, bad);
});

test("submit: the box is empty before anything is sent, the value is in no request, global or attribute, and the copies open", async () => {
  const requests = [];
  let input;
  const api = async (method, path, body) => {
    requests.push({ method, path, body: JSON.stringify(body) });
    assert.equal(input.value, "", "the box is cleared before the first request");
    if (path.endsWith("/recipients")) return { status: 200, body: REC };
    return { status: 200, body: { name: "crm-key", version: 1 } };
  };
  const actions = { save: (f) => saveSecret({ api, ...f }) };
  const view = secretsView({ ...data, secrets: [] }, actions);
  input = fill(view, "crm-key", VALUE);
  tickAll(view);
  find(view, (n) => n.tag === "select" && n.attrs["aria-label"] === "Valid for")[0].value = "30";
  await button(view, "Save secret").listeners.click();
  assert.equal(requests.length, 2);
  assert.deepEqual(requests.map((r) => r.path), ["/admin/secrets/recipients", "/admin/secrets"]);
  for (const r of requests) assert.ok(!r.body.includes(VALUE), "no value in " + r.path);
  assert.ok(!/"(value|plaintext|secret_value)"/.test(requests[1].body));
  assert.equal(input.value, "");
  // the sealed copies open with the workspace key, to exactly the value, for the right context
  const saved = JSON.parse(requests[1].body);
  assert.equal(saved.version, 1); assert.equal(saved.expires_ms, REC.now_ms + 30 * 86400000);
  assert.equal(saved.wraps.length, 2);
  for (const w of saved.wraps) {
    const a = { hub_id: REC.hub_id, workspace_id: w.workspace_id, secret_id: REC.secret_id, version: 1, wxk_version: w.wxk_version, expires_ms: saved.expires_ms, name: "crm-key", sealed: Buffer.from(w.sealed, "base64url").toString("hex") };
    assert.equal(await openFixed(WXK_SCALAR, a, REC.now_ms, REC.now_ms, 366 * 86400000), VALUE);
  }
  assert.notEqual(saved.wraps[0].sealed, saved.wraps[1].sealed, "a fresh key per copy");
  // nothing anywhere holds the value: globals, the page tree, storage
  const everything = [];
  for (const k of Object.getOwnPropertyNames(globalThis)) {
    try { const v = globalThis[k]; if (typeof v === "string") everything.push(v); else if (v && typeof v === "object" && !(v instanceof Node_) && k !== "document") everything.push(JSON.stringify(v)); } catch { /* host objects */ }
  }
  assert.ok(!everything.some((s) => s?.includes?.(VALUE)), "the value is in no global");
  assert.ok(!view.all().some((n) => n.textContent.includes(VALUE) || Object.values(n.attrs).some((v) => String(v).includes(VALUE)) || n.value === VALUE));
  assert.equal(typeof globalThis.localStorage, "undefined"); assert.equal(typeof globalThis.sessionStorage, "undefined");
});

test("a failed save: generic words, an empty box, and no second try on its own", async () => {
  for (const failure of [{ status: 500, body: { error: "internal", detail: VALUE } }, { status: 409, body: { error: "conflict" } }, { status: 0, body: {} }]) {
    let calls = 0;
    const api = async (_m, path) => { calls++; if (path.endsWith("/recipients")) return { status: 200, body: REC }; return failure; };
    const view = secretsView({ ...data, secrets: [] }, { save: (f) => saveSecret({ api, ...f }) });
    const input = fill(view, "crm-key", VALUE);
    tickAll(view);
    await button(view, "Save secret").listeners.click();
    assert.equal(calls, 2, "exactly one try");
    assert.equal(input.value, "");
    const note = find(view, (n) => n.attrs.role === "alert")[0].textContent;
    assert.match(note, /Type (it|the secret) again/);
    assert.ok(!note.includes(VALUE) && !note.includes("detail") && !note.includes("internal"));
  }
});

test("an exception while locking or sending is a generic message too, and the box is empty", async () => {
  const boom = async () => { throw new Error("details with " + VALUE); };
  const view = secretsView({ ...data, secrets: [] }, { save: (f) => saveSecret({ api: async () => ({ status: 200, body: REC }), seal: boom, ...f }) });
  const input = fill(view, "crm-key", VALUE);
  tickAll(view);
  await button(view, "Save secret").listeners.click();
  assert.equal(input.value, "");
  const note = find(view, (n) => n.attrs.role === "alert")[0].textContent;
  assert.ok(!note.includes(VALUE) && !note.includes("details"));
  const r = await saveSecret({ api: async () => { throw new Error(VALUE); }, name: "crm-key", value: VALUE, days: 30, shared: "all", workspaces: [] });
  assert.deepEqual(r, { ok: false, code: "failed" });
});

test("Enter the value again: one form per secret that keeps who can read it and saves under the same name", async () => {
  const seen = [];
  const view = secretsView(data, { save: async (f) => { seen.push(f); return { ok: true }; } });
  const card = find(view, (n) => n.attrs["data-secret"] === "crm-key")[0];
  const reenter = find(card, (n) => n.tag === "button" && n.textContent === "Enter the value")[0];
  assert.equal(reenter.className, "primary", "highlighted when a workspace needs it");
  const again = find(card, (n) => n.className === "secret-entry")[0];
  assert.equal(again.hidden, true);
  reenter.listeners.click();
  assert.equal(again.hidden, false);
  const input = find(again, (n) => n.tag === "input")[0];
  input.value = VALUE;
  await find(again, (n) => n.tag === "button" && n.textContent === "Save the value")[0].listeners.click();
  assert.equal(seen.length, 1);
  assert.deepEqual({ ...seen[0], value: "" }, { name: "crm-key", value: "", days: 90, shared: "chosen", workspaces: WS });
  assert.equal(seen[0].value, VALUE); assert.equal(input.value, "");
});

test("who can read it and remove go to the page's actions; remove asks twice", () => {
  const calls = [];
  const view = secretsView(data, { share: (n, s, w) => calls.push(["share", n, s, w]), remove: (s) => calls.push(["remove", s.name]) });
  const boxes = find(view, (n) => n.tag === "input" && n.attrs["data-ws"]);
  boxes[1].checked = false;
  button(view, "Save who can read it").listeners.click();
  assert.deepEqual(calls[0], ["share", "crm-key", "chosen", [WS[0]]]);
  const rm = button(view, "Remove");
  rm.listeners.click(); assert.equal(calls.length, 1); assert.equal(rm.textContent, "Sure? Remove it");
  rm.listeners.click(); assert.deepEqual(calls[1], ["remove", "crm-key"]);
});

test("the page source never logs, stores, navigates or schedules, and never reads a value from anywhere but the box", () => {
  const src = readFileSync(new URL("../../src/orch_hub/web/pages/secrets.js", import.meta.url), "utf8");
  const code = src.replace(/^\s*\/\/.*$/gm, "");
  for (const [rx, what] of [
    [/\bconsole\b/, "console"], [/\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b/, "storage"], [/\bdocument\s*\.\s*cookie\b/, "cookie"],
    [/\blocation\b|\bhistory\b|\bURLSearchParams\b|\?[a-z]+=/, "address"], [/\bwindow\b|\bglobalThis\b|\bself\b/, "global"],
    [/\bsetTimeout\b|\bsetInterval\b|\bqueueMicrotask\b/, "timer or retry"], [/\bfetch\s*\(|\bXMLHttpRequest\b|\bsendBeacon\b/, "own network call"],
    [/\bpostMessage\b|\bBroadcastChannel\b|\bnavigator\b|\bclipboard\b/, "side channel"], [/\bretry\b/i, "retry"],
  ]) assert.ok(!rx.test(code), what);
  assert.ok(/type: "password"/.test(code) && /autocomplete: "off"/.test(code) && /spellcheck: "false"/.test(code));
  const mut = code.match(/^(let|var)\s+\w+/gm);
  assert.equal(mut, null, "no module-level mutable state");
});

test("review fix 1: a long pasted value is refused with a length message, never cut by the box", async () => {
  const calls = [];
  const view = secretsView({ ...data, secrets: [] }, { save: async (f) => { calls.push(f); return { ok: true }; } });
  for (const b of find(view, (n) => n.tag === "input" && n.attrs.type === "password")) assert.equal(b.attrs.maxlength, undefined, "no maxlength: it would cut a paste silently");
  const input = fill(view, "crm-key", "x".repeat(5000));
  await button(view, "Save secret").listeners.click();
  assert.equal(calls.length, 0);
  assert.match(find(view, (n) => n.attrs.role === "alert")[0].textContent, /between 8 and 4096/);
  assert.equal(input.value.length, 5000);
});

test("P10: valid for offers 7/30/90/365 with 90 selected, and a recommended hint", () => {
  const view = secretsView({ ...data, secrets: [] }, {});
  const sel = find(view, (n) => n.tag === "select" && n.attrs["aria-label"] === "Valid for")[0];
  assert.deepEqual(find(sel, (n) => n.tag === "option").map((o) => o.attrs.value), ["7", "30", "90", "365"]);
  assert.equal(sel.value, "90");
  assert.match(view.textContent, /Recommended: 90 days\. Pick 7 days for something temporary\./);
});

test("P10: a workspace with only an owner name reads as the owner's workspace, never an id", () => {
  const d = { ...data, workspaces: [{ workspace_id: WS[0], name: "Anna's workspace" }, { workspace_id: WS[1], name: "Ben's workspace" }] };
  const t = secretsView(d, {}).textContent;
  assert.match(t, /Ben's workspace/); assert.ok(!t.includes(WS[1]));
});

test("P10: no Enter-the-value button, hint or warning when every row is ready", () => {
  const ready = { ...data, secrets: [{ ...data.secrets[0], state: { [WS[0]]: { state: "ready" }, [WS[1]]: { state: "ready" } } }] };
  const view = secretsView(ready, {});
  assert.equal(button(view, "Enter the value"), undefined);
  assert.ok(!/Waiting for you|Needs the value|Enter the value/.test(view.textContent));
  assert.match(view.textContent, /\u2713 Available/);
});

test("P07: the new-secret form starts on chosen workspaces with a reason, and the share box sits above Save", () => {
  const view = secretsView({ ...data, secrets: [] }, {});
  const box = find(view, (n) => n.className === "share-box")[0];
  const sel = find(box, (n) => n.tag === "select")[0];
  assert.equal(sel.value, "chosen");
  assert.match(box.textContent, /Recommended: only the workspaces that need it\./);
  assert.equal(find(box, (n) => n.attrs.role === "note")[0].hidden, true, "the hub warning shows only for All workspaces");
  sel.value = "all"; sel.listeners.change();
  assert.equal(find(box, (n) => n.attrs.role === "note")[0].hidden, false);
  const all = find(view, (n) => n.className === "secret-entry")[0].all();
  assert.ok(all.indexOf(box) < all.indexOf(button(view, "Save secret")), "above Save");
  assert.equal(find(view, (n) => n.tag === "select" && n.attrs["aria-label"] === "Who can read it").length, 1, "no duplicate block");
  assert.ok(!view.children.some((n) => n.className === "banner warn"), "no page-wide warning");
});

test("P07: a sole workspace is already ticked; with two, none is", () => {
  const one = secretsView({ workspaces: [data.workspaces[0]], secrets: [] }, {});
  assert.equal(find(one, (n) => n.attrs["data-ws"])[0].checked, true);
  const two = secretsView({ ...data, secrets: [] }, {});
  assert.deepEqual(find(two, (n) => n.attrs["data-ws"]).map((b) => b.checked), [false, false]);
});

test("P07: Save with nothing ticked says so and sends nothing; after a good save the name is empty again", async () => {
  const calls = [];
  const view = secretsView({ ...data, secrets: [] }, { save: async (f) => { calls.push(f); return { ok: true }; } });
  const input = fill(view, "crm-key", VALUE);
  await button(view, "Save secret").listeners.click();
  assert.equal(calls.length, 0);
  assert.equal(find(view, (n) => n.attrs.role === "alert")[0].textContent, "Pick at least one workspace.");
  assert.equal(input.value, VALUE, "the value stays so nothing is lost");
  tickAll(view);
  await button(view, "Save secret").listeners.click();
  assert.equal(calls.length, 1); assert.equal(calls[0].shared, "chosen"); assert.deepEqual(calls[0].workspaces, WS);
  assert.equal(find(view, (n) => n.attrs["aria-label"] === "Name of the new secret")[0].value, "", "name cleared");
});

test("P07: the card says Not used yet, 'as reported' lives only behind the i, and Available has one explanation", () => {
  const view = secretsView({ ...data, secrets: [{ ...data.secrets[0], used_this_week: 0 }] }, {});
  const card = find(view, (n) => n.attrs["data-secret"] === "crm-key")[0];
  assert.match(card.textContent, /Valid until .* \u00b7 Not used yet/);
  const details = find(card, (n) => n.tag === "details").map((d) => d.textContent);
  const inside = details.join("|");
  const outside = details.reduce((t, d) => t.replace(d, ""), card.textContent);
  assert.ok(!/as reported/.test(outside)); assert.match(inside, /as reported by the workspaces/);
  assert.match(inside, /Available means the workspace has received the secret\./);
  assert.match(card.textContent, /Needs the value/);
});

test("P07: the card's share box shows the hub warning above its Save only while All workspaces is picked", () => {
  const view = secretsView(data, {});
  const card = find(view, (n) => n.attrs["data-secret"] === "crm-key")[0];
  const sel = find(card, (n) => n.tag === "select" && n.attrs["aria-label"] === "Who can read it")[0];
  const warn = () => find(card, (n) => n.attrs.role === "note" && /hub could read/.test(n.textContent))[0];
  assert.equal(sel.value, "chosen"); assert.equal(warn().hidden, true);
  sel.value = "all"; sel.listeners.change();
  assert.equal(warn().hidden, false);
  const all = card.all();
  assert.ok(all.indexOf(warn()) < all.indexOf(button(card, "Save who can read it")), "above Save");
});

test("#1431: per-workspace use is behind a small click, in plain words, with the expiry shown", () => {
  const view = secretsView(data, {});
  const note = find(view, (n) => n.attrs["data-use"] === "crm-key")[0];
  assert.equal(note.hidden, true);
  assert.equal(note.textContent, "Used this week: Product 12, A workspace 0");
  const btn = find(view, (n) => n.attrs["data-use-toggle"] === "crm-key")[0];
  btn.listeners.click();
  assert.equal(note.hidden, false);
  assert.match(view.textContent, /Valid until /); // run15c P07 wording
});
