// M4 S5 (#1462): the Backup card of the Hub settings page against a tiny DOM stub. Text only; the step-up form sends
// the password and the code with each action; the 24 words show once and only where the page may show them.
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
globalThis.document = { body: new Node_("body"), createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { hubSettingsView, wordsMayShow } = await import("../../src/orch_hub/web/pages/hub-settings.js");

const BASE = { configured: true, name: "Acme Hub", company: "", phone_kind: "private", can_change_addresses: false, restarts: false, addresses: [{ kind: "private", web: "http://100.64.1.2:8100" }], sign_in: [] };
const NONE = { key_set: false, key_set_ms: null, backups: [], keep: 7, schedule: "daily", cli_line: "orch-hub backup-key" };
const NAME = "hub-0123abcd-20261029T030000Z.orchbak";
const SET = { key_set: true, key_set_ms: 1, backups: [{ name: NAME, size: 20480, made_ms: Date.now() - 3600000 }, { name: "../../etc/passwd", size: 1, made_ms: 1 }], keep: 7, schedule: "daily", cli_line: "orch-hub backup-key" };
const card = (v) => v.all().find((n) => n.attrs["data-card"] === "backup");
const button = (root, label) => root.all().find((n) => n.tag === "button" && n.textContent === label);
const inputs = (root) => root.all().filter((n) => n.tag === "input");

test("no key yet: one 'Set up backups' step with password and code, plain words, no product name", () => {
  const c = card(hubSettingsView({ ...BASE, backup: NONE }, { wordsMayShow: true }));
  assert.match(c.textContent, /Backup/); assert.match(c.textContent, /24 backup words/);
  assert.doesNotMatch(c.textContent, /NetBird|WireGuard|Tailscale|VPN/i);
  assert.equal(inputs(c).length, 2);
  assert.ok(button(c, "Set up backups"));
});

test("Set up backups sends the password and the code, and clears the fields", async () => {
  const sent = [];
  const c = card(hubSettingsView({ ...BASE, backup: NONE }, { wordsMayShow: true, setKey: async (b) => sent.push(b) }));
  const [pw, code] = inputs(c);
  pw.value = "correct horse"; code.value = " 123456 ";
  await button(c, "Set up backups").listeners.click();
  assert.deepEqual(sent, [{ password: "correct horse", totp: "123456" }]);
  assert.equal(pw.value, ""); assert.equal(code.value, "");
});

test("on an address that is not private the words are not offered: the server command is shown instead", () => {
  const c = card(hubSettingsView({ ...BASE, backup: NONE }, { wordsMayShow: false }));
  assert.equal(inputs(c).length, 0); assert.equal(button(c, "Set up backups"), undefined);
  assert.match(c.textContent, /not private/); assert.match(c.textContent, /orch-hub backup-key/);
  const k = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: false }));
  assert.equal(button(k, "Replace the backup key"), undefined);
  assert.match(k.textContent, /orch-hub backup-key --replace/);
});

test("the words are shown once as a numbered list; 'I have saved it' hands control back", () => {
  const words = Array.from({ length: 24 }, (_, i) => "word" + i).join(" ");
  let saved = 0;
  const c = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true, words, wordsSaved: () => saved++ }));
  const li = c.all().filter((n) => n.tag === "li" && /^word\d+$/.test(n.textContent));
  assert.equal(li.length, 24);
  assert.match(c.textContent, /shown only now/);
  button(c, "I have saved it").listeners.click();
  assert.equal(saved, 1);
  const again = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true }));   // the next render has no words
  assert.doesNotMatch(again.textContent, /word0/);
  assert.ok(!again.all().some((n) => n.attrs["data-words"]));
});

test("with a key: the kept backups, Back up now, and a download that asks for the password and a code", async () => {
  const calls = [], dl = [];
  const c = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true, makeBackup: () => calls.push("make"), download: async (n, b) => dl.push([n, b]) }));
  assert.match(c.textContent, /every day/); assert.match(c.textContent, /7 newest/); assert.match(c.textContent, /20 KB/);
  assert.equal(c.all().filter((n) => n.attrs["data-backup"]).length, 1);          // a hostile name from the answer is not shown
  assert.ok(!c.textContent.includes("etc/passwd"));
  button(c, "Back up now").listeners.click();
  assert.deepEqual(calls, ["make"]);
  assert.equal(inputs(c).length, 0);                                              // no step-up form until Download is clicked
  button(c, "Download").listeners.click();
  const [pw, code] = inputs(c);
  pw.value = "pw"; code.value = "654321";
  const go = c.all().filter((n) => n.tag === "button" && n.textContent === "Download")[1];
  await go.listeners.click();
  assert.deepEqual(dl, [[NAME, { password: "pw", totp: "654321" }]]);
});

test("Replace the backup key explains that old backups go and asks for the password and a code", () => {
  const sent = [];
  const c = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true, setKey: (b) => sent.push(b) }));
  button(c, "Replace the backup key").listeners.click();
  assert.match(c.textContent, /deleted/);
  assert.ok(button(c, "Make new words"));
  assert.equal(inputs(c).length, 2);
});

test("wordsMayShow: this computer, https and this hub's OWN private-network address only", () => {
  const mine = ["http://100.64.1.2:8100"];
  const at = (protocol, hostname, port = "8100") => wordsMayShow({ protocol, hostname, origin: `${protocol}//${hostname}:${port}` }, mine);
  for (const [p, h_] of [["http:", "127.0.0.1"], ["http:", "localhost"], ["http:", "[::1]"], ["http:", "100.64.1.2"], ["https:", "hub.example"]]) assert.equal(at(p, h_), true, h_);
  for (const [p, h_] of [["http:", "100.64.9.9"], ["http:", "100.127.1.1"], ["http:", "192.168.1.5"], ["http:", "10.0.0.5"], ["http:", "hub.example"], ["http:", "203.0.113.9"], ["file:", "x"]]) assert.equal(at(p, h_), false, h_);
  assert.equal(at("http:", "100.64.1.2", "9999"), false);   // same host, another port: not the address in the settings
  assert.equal(wordsMayShow({ protocol: "http:", hostname: "100.64.1.2", origin: "http://100.64.1.2:8100" }, []), false);
  assert.equal(wordsMayShow(null), false);
});

test("Restore asks for the words, the password and a code, in plain words, and sends all three", async () => {
  const sent = [];
  const c = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true, restore: async (n, b) => sent.push([n, b]) }));
  assert.equal(inputs(c).length, 0);                                              // nothing to type until Restore is clicked
  button(c, "Restore").listeners.click();
  assert.match(c.textContent, /Everything changed since then is lost/); assert.match(c.textContent, /switched off/);
  assert.doesNotMatch(c.textContent, /NetBird|WireGuard|Tailscale|VPN/i);
  const area = c.all().find((n) => n.tag === "textarea");
  const [pw, code] = inputs(c);
  area.value = "a b c"; pw.value = "pw"; code.value = " 111222 ";
  await button(c, "Restore this backup").listeners.click();
  assert.deepEqual(sent, [[NAME, { password: "pw", totp: "111222", words: "a b c" }]]);
  assert.equal(area.value, ""); assert.equal(pw.value, "");                        // nothing is kept in the page
});

test("on an address that is not private the restore shows the server command instead of a words box", () => {
  const c = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: false, restore: async () => {} }));
  button(c, "Restore").listeners.click();
  assert.equal(c.all().filter((n) => n.tag === "textarea").length, 0);
  assert.match(c.textContent, /orch-hub restore FILE/);
});

test("after a restore the card says what happened and what to check, with a way to dismiss it", () => {
  let n = 0;
  const c = card(hubSettingsView({ ...BASE, backup: { ...SET, restored: { made_ms: 1, restored_ms: 2, suspended: 2 } } }, { wordsMayShow: true, dismissRestored: () => n++ }));
  assert.match(c.textContent, /Restored from the backup of/); assert.match(c.textContent, /Check People and Workspaces/); assert.match(c.textContent, /2 people are switched off/);
  button(c, "Got it").listeners.click();
  assert.equal(n, 1);
});

test("items a restore switched off are listed and each has a Turn on click", () => {
  const got = [];
  const c = card(hubSettingsView({ ...BASE, backup: { ...SET, pending: [{ id: 3, kind: "secret_share", label: "api key", workspace_id: "ab" }] } }, { wordsMayShow: true, confirmPending: (i) => got.push(i) }));
  assert.match(c.textContent, /Switched off by the restore/); assert.match(c.textContent, /api key/);
  button(c, "Turn on").listeners.click();
  assert.deepEqual(got, [3]);
});

test("a hub without the backup answer shows no Backup card", () => {
  assert.equal(card(hubSettingsView(BASE, {})), undefined);
});
