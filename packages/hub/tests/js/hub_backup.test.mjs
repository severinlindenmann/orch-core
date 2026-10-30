// M4 S5 (#1462): the Backup card of the Hub settings page against a tiny DOM stub. Text only; the step-up form sends
// the password and the code with each action; the 24 words show once and only where the page may show them.
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
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
const inputs = (root) => root.all().filter((n) => n.tag === "input" && n.attrs.type !== "file");
const fileInput = (root) => root.all().find((n) => n.tag === "input" && n.attrs.type === "file");

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
  assert.match(c.textContent, /Download the backup of/);
  const go = button(c, "Download this backup");
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
  button(c, "Type them instead").listeners.click();
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
  assert.match(c.textContent, /Restored from the backup of/); assert.match(c.textContent, /Check that your workspaces show Online/); assert.match(c.textContent, /2 people are switched off/);
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

test("after a reload a pending key shows 'I have saved it' without the words, and a refused confirm is said", async () => {
  let saved = 0;
  const c = card(hubSettingsView({ ...BASE, backup: { ...SET, key_pending: true } }, { wordsMayShow: true, wordsSaved: () => saved++ }));
  assert.ok(c.all().some((n) => n.attrs["data-pending"]));
  assert.match(c.textContent, /kept until you confirm/);
  button(c, "I have saved it").listeners.click();
  assert.equal(saved, 1);
  assert.ok(!card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true })).all().some((n) => n.attrs["data-pending"]));
  const { hubSettingsPage } = await import("../../src/orch_hub/web/pages/hub-settings.js");
  const notes = []; const shown = [];
  const d = { pageLoading() {}, refresh: async () => true, state: { user: { is_admin: true } }, loadRail: async () => {}, say: (e) => "Why: " + e,
    banner: (kind, title, detail) => kind + ":" + title + ":" + detail, home: () => "home", loginView: () => "login", reauthView: () => "reauth",
    shell: (id, notice, view) => shown.push([notice, view]),
    api: async (m, p) => {
      if (p === "/admin/settings") return { status: 200, body: { ...BASE } };
      if (p === "/admin/backup") return { status: 200, body: { ...SET, key_pending: true } };
      notes.push(p); return { status: 403, body: { error: "confirm_denied" } };
    } };
  await hubSettingsPage(d);
  const view = shown.at(-1)[1];
  await button(card(view), "I have saved it").listeners.click();
  assert.deepEqual(notes, ["/admin/backup/key/confirm"]);
  assert.match(d.state.notice ?? shown.at(-1)[0] ?? "", /Only the session that made the new words/);
});

test("a hub without the backup answer shows no Backup card", () => {
  assert.equal(card(hubSettingsView(BASE, {})), undefined);
});

test("P07: the result of Back up now shows inside the Backup card in plain words", () => {
  const ok = card(hubSettingsView({ ...BASE, backup: { ...SET, note: { kind: "ok", title: "Backup made" } } }, { wordsMayShow: true }));
  const n = ok.all().find((x) => x.attrs["data-backup-note"]);
  assert.equal(n.attrs["data-backup-note"], "ok"); assert.match(n.textContent, /Backup made/);
  const bad = card(hubSettingsView({ ...BASE, backup: { ...SET, note: { kind: "bad", title: "Not done", detail: "A backup was made a moment ago. Try again in a few minutes." } } }, { wordsMayShow: true }));
  assert.match(bad.all().find((x) => x.attrs["data-backup-note"]).textContent, /Try again in a few minutes/);
  assert.ok(!card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true })).all().some((x) => x.attrs["data-backup-note"]));
});

test("P07: Back up now is greyed out with the reason while the next time is in the future", () => {
  const wait = card(hubSettingsView({ ...BASE, backup: { ...SET, next_manual_ms: Date.now() + 4 * 60000 } }, { wordsMayShow: true }));
  assert.equal(button(wait, "Back up now").attrs.disabled, "disabled");
  assert.match(wait.all().find((x) => x.attrs["data-backup-wait"]).textContent, /You just made one\. You can make another in [45] minutes\./);
  const free = card(hubSettingsView({ ...BASE, backup: { ...SET, next_manual_ms: Date.now() - 1000 } }, { wordsMayShow: true }));
  assert.equal(button(free, "Back up now").attrs.disabled, undefined);
  assert.ok(!free.all().some((x) => x.attrs["data-backup-wait"]));
});

test("P07: Download opens a headed form with 'Download this backup' and Cancel; the row says Downloaded afterwards", () => {
  const c = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true }));
  button(c, "Download").listeners.click();
  assert.match(c.textContent, /Download the backup of/); assert.ok(button(c, "Download this backup"));
  button(c, "Cancel").listeners.click();
  assert.equal(inputs(c).length, 0);
  const d = card(hubSettingsView({ ...BASE, backup: { ...SET, note: { kind: "ok", title: "Backup downloaded", downloaded: NAME } } }, { wordsMayShow: true }));
  assert.match(d.all().find((x) => x.attrs["data-downloaded"]).textContent, /^Downloaded$/);
});

test("P07: the page puts a refused Back up now (429) in the card with the Backup wording", async () => {
  const { hubSettingsPage } = await import("../../src/orch_hub/web/pages/hub-settings.js");
  const shown = [];
  const d = { pageLoading() {}, refresh: async () => true, state: { user: { is_admin: true } }, loadRail: async () => {}, say: (e) => "Generic " + e,
    banner: (kind, title, detail) => kind + ":" + title + ":" + detail, home: () => "home", loginView: () => "login", reauthView: () => "reauth",
    shell: (id, notice, view) => shown.push([notice, view]),
    api: async (m, p) => {
      if (p === "/admin/settings") return { status: 200, body: { ...BASE } };
      if (p === "/admin/backup") return { status: 200, body: { ...SET } };
      return { status: 429, body: { error: "rate_limited" } };
    } };
  await hubSettingsPage(d);
  await button(card(shown.at(-1)[1]), "Back up now").listeners.click();
  const [notice, view] = shown.at(-1);
  assert.ok(notice == null);                                                // nothing at the top of the page
  const n = card(view).all().find((x) => x.attrs["data-backup-note"]);
  assert.equal(n.attrs["data-backup-note"], "bad");
  assert.match(n.textContent, /A backup was made a moment ago\. Try again in a few minutes\./);
});


const W24 = Array.from({ length: 24 }, (_, i) => "word" + i).join(" ");

test("P08: the words panel offers 'Save as a file' (text only, never sent) and says to keep the file away from the server", () => {
  const made = [];
  globalThis.Blob = class { constructor(parts, o) { made.push([parts.join(""), o]); } };
  globalThis.URL = { createObjectURL: () => "blob:x", revokeObjectURL() {} };
  const c = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true, words: W24, wordsSaved() {} }));
  const b = button(c, "Save as a file");
  assert.ok(b);
  b.listeners.click();
  assert.equal(made.length, 1);
  assert.equal(made[0][0].trim(), W24);
  assert.match(made[0][1].type, /^text\/plain/);
  assert.match(c.textContent, /copy of the backup key/); assert.match(c.textContent, /away from this server/);
});

test("P08: Restore offers 'Choose the file' and 'Paste' instead of a words box; the request body still carries the words", async () => {
  const sent = [];
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { readText: async () => "  " + W24.replace(/ /g, "\n") + "\n" } } });
  const c = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true, restore: async (n, b) => sent.push([n, b]) }));
  button(c, "Restore").listeners.click();
  assert.ok(button(c, "Choose the file")); assert.ok(button(c, "Paste")); assert.ok(button(c, "Type them instead"));
  assert.equal(c.all().filter((n) => n.tag === "textarea" && !("hidden" in n.attrs)).length, 0);   // no words box until asked for
  const f = fileInput(c);
  assert.match(f.attrs.accept, /text/);
  await f.listeners.change({ target: { files: [{ name: "w.txt", size: 200, text: async () => W24 + "\n" }] } });
  assert.match(c.textContent, /w\.txt/);
  const [pw, code] = inputs(c);
  pw.value = "pw"; code.value = "111222";
  await button(c, "Restore this backup").listeners.click();
  assert.deepEqual(sent, [[NAME, { password: "pw", totp: "111222", words: W24 }]]);
  // Paste path
  button(c, "Paste") && await button(c, "Paste").listeners.click();
  pw.value = "pw"; code.value = "333444";
  await button(c, "Restore this backup").listeners.click();
  assert.equal(sent[1][1].words, W24);
});

test("P08: a file or paste that is not 24 words is refused in plain words and nothing is sent as words", async () => {
  const sent = [];
  const c = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: true, restore: async (n, b) => sent.push([n, b]) }));
  button(c, "Restore").listeners.click();
  await fileInput(c).listeners.change({ target: { files: [{ name: "x.txt", size: 20, text: async () => "only three words" }] } });
  assert.match(c.textContent, /not the 24 words/);
  await fileInput(c).listeners.change({ target: { files: [{ name: "big.bin", size: 10_000_000, text: async () => W24 }] } });
  assert.match(c.textContent, /not the 24 words/);
  const [pw, code] = inputs(c);
  pw.value = "pw"; code.value = "111222";
  await button(c, "Restore this backup").listeners.click();
  assert.equal(sent[0][1].words, "");                      // the hub refuses empty words; the password and code are still required
});

test("P08: on an address that is not private neither file nor paste is offered", () => {
  const c = card(hubSettingsView({ ...BASE, backup: SET }, { wordsMayShow: false, restore: async () => {} }));
  button(c, "Restore").listeners.click();
  assert.equal(fileInput(c), undefined); assert.equal(button(c, "Paste"), undefined); assert.equal(button(c, "Choose the file"), undefined);
});
