// M4 S1 (#1452): the Published page (links tab) against a tiny DOM stub: text only, no buttons that do nothing, hidden workspaces named.
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
const { publishedView, until, page } = await import("../../src/orch_hub/web/pages/published.js");
const find = (n, pred) => n.all().filter(pred);
const NOW = 1_800_000_000_000, DAY = 86400000;
const link = (o = {}) => ({ id: "a".repeat(32), workspace_id: "1".repeat(32), workspace: "Product", kind: "secret", state: "live", created_ms: NOW - 1000, expires_ms: NOW + 7 * DAY, bytes: 5, label: "hello.txt", ticket: null, by_kind: "person", by_name: "Severin", has_password: false, views: 2, ...o });
const data = (o = {}) => ({ links: [link(), link({ id: "b".repeat(32), label: "Contract draft v3.pdf", workspace: "Legal", by_name: "Lea Brunner", has_password: true, expires_ms: NOW + 2 * DAY, views: 1 }), link({ id: "c".repeat(32), label: "Logo pack.zip", state: "expired", views: 24 })], apps: [], hidden: [], stale_ms: null, ...o });

test("the page is registered, admin only, and answers to the path published", () => {
  assert.equal(page.path, "published");
  assert.equal(page.nav.admin, true);
  assert.equal(typeof page.render, "function");
  assert.deepEqual(Object.keys(page).sort(), ["nav", "path", "render"]);
});

test("the links tab shows what, who, until when and how often it was opened; live ones first", () => {
  const view = publishedView(data(), { now: NOW });
  assert.match(view.textContent, /Shared files \(3\)/);
  assert.match(view.textContent, /Apps & pages \(0\)/);
  const rows = find(view, (n) => n.tag === "tr" && n.attrs["data-link"]);
  assert.equal(rows.length, 3);
  assert.deepEqual(rows.map((r) => r.attrs["data-link"][0]), ["a", "b", "c"]);
  assert.match(rows[0].textContent, /hello\.txt.*by Severin.*Product.*in 7 days.*2 times/);
  assert.match(rows[1].textContent, /password.*by Lea Brunner.*Legal.*in 2 days.*1 time$/);
  assert.match(rows[2].textContent, /expired.*24 times/);
});

test("there are no buttons that do nothing: the only buttons are the two tabs", () => {
  const view = publishedView(data(), { now: NOW });
  const buttons = find(view, (n) => n.tag === "button");
  assert.deepEqual(buttons.map((b) => b.attrs["data-pubtab"]), ["apps", "links"]);
  assert.ok(buttons.every((b) => typeof b.listeners.click === "function"));
  assert.doesNotMatch(view.textContent, /Revoke|Extend|Manage/);
});

test("text a workspace wrote is text: markup, bidi and control characters never become elements or stay raw", () => {
  const evil = "<img src=x onerror=1>‮⁦";
  const view = publishedView(data({ links: [link({ label: evil, by_name: "<b>x</b>\x00", workspace: "<script>" })] }), { now: NOW });
  assert.equal(find(view, (n) => ["img", "script"].includes(n.tag) || (n.tag === "b" && n.textContent === "x")).length, 0);
  assert.ok(!/[‮⁦\x00]/.test(view.textContent));
  assert.match(view.textContent, /<img src=x onerror=1>/);
});

const rail = (o = {}) => ({ id: "2".repeat(32), name: "Marketing", role: "owner", online: true, switchable: true, canOpen: true, ...o });
const hiddenOf = (hidden, opts) => find(publishedView(data({ hidden }), { now: NOW, ...opts }), (n) => n.attrs["data-hidden"]);
test("P08: each workspace that keeps its links private gets a line naming it and what to do", () => {
  const mine = { workspace_id: "2".repeat(32), name: "Anna Test's workspace", owner_name: "Anna Test" };
  const ben = { workspace_id: "3".repeat(32), name: "Ben Test's workspace", owner_name: "Ben Test" };
  let opened = null;
  const view = publishedView(data({ hidden: [ben, mine] }), { now: NOW, rows: [rail({ name: mine.name }), rail({ id: ben.workspace_id, role: "view" })], openSettings: (id) => { opened = id; } });
  const rows = find(view, (n) => n.attrs["data-hidden"]);
  assert.equal(rows.length, 2);
  assert.match(rows[0].textContent, /^Ben Test's workspace keeps its links and apps to itself, so they are not listed here\. Stop them on that workspace's Shares page\.Ask Ben Test to share them\.$/);
  assert.equal(find(rows[0], (n) => n.tag === "button").length, 0);
  assert.match(rows[1].textContent, /^Anna Test's workspace keeps its links and apps to itself.*Let the hub see them$/);
  const btn = find(rows[1], (n) => n.tag === "button")[0];
  assert.equal(btn.className, "pri");
  btn.listeners.click();
  assert.equal(opened, mine.workspace_id);
});

test("P08: an own workspace that cannot be opened from here (selective, #1752) gets words, never an Open button", () => {
  const w = { workspace_id: "2".repeat(32), name: "Marketing", owner_name: "Anna" };
  for (const row of [rail({ switchable: false, canOpen: false }), rail({ online: false })]) {
    const r = hiddenOf([w], { rows: [row], openSettings() { throw new Error("must not open"); } })[0];
    assert.equal(find(r, (n) => n.tag === "button").length, 0);
    assert.match(r.textContent, /Open it on its own computer, then choose Let the hub read everything in Settings > Hub\./);
  }
  const noName = hiddenOf([{ workspace_id: "9".repeat(32), name: "X", owner_name: null }], { rows: [] })[0];
  assert.match(noName.textContent, /Ask its owner to share them\./);
});

test("P08: names and owners a workspace wrote stay text", () => {
  const evil = "<img src=x onerror=1>";
  const r = hiddenOf([{ workspace_id: "2".repeat(32), name: evil, owner_name: "<b>x</b>" }], { rows: [] });
  assert.equal(find(r[0], (n) => n.tag === "img").length, 0);
  assert.deepEqual(find(r[0], (n) => n.tag === "b").map((n) => n.textContent), [evil]); // only our own name wrapper, holding text
  assert.match(r[0].textContent, /<img src=x onerror=1>/);
  assert.match(r[0].textContent, /Ask <b>x<\/b> to share them/);
});

test("no links and no apps shows plain empty words; the apps tab lists apps read only", () => {
  const empty = publishedView(data({ links: [] }), { now: NOW });
  assert.match(empty.textContent, /No shared files yet/);
  const app = { id: "d".repeat(32), workspace: "Marketing", kind: "page", state: "running", app_name: "Release notes 2.4", by_name: "Anna Keller", by_kind: "person", access: "public", version: 3, visits_30d: 412, updated_ms: NOW };
  const view = publishedView(data({ apps: [app] }), { now: NOW });
  assert.match(view.textContent, /Release notes 2\.4.*Page.*running.*Marketing.*Public.*v3.*412/);
  find(view, (n) => n.attrs["data-pubtab"] === "links")[0].listeners.click();
  assert.match(view.textContent, /hello\.txt/);
});

test("a stale answer says so; the times are plain words", () => {
  assert.match(publishedView(data({ stale_ms: 5 * 60000 }), { now: NOW }).textContent, /Publish does not answer right now.*5 min ago/);
  assert.equal(until(NOW + 3 * 3600000, NOW), "in 3 hours");
  assert.equal(until(NOW + DAY, NOW), "in 1 day");
  assert.equal(until(NOW - 1, NOW), "ended");
  assert.equal(until(null, NOW), "no end date");
});

test("the mockup's demo data renders as the mockup's four rows", async () => {
  const { demo } = await import("./published_demo.mjs");
  const view = publishedView(demo(NOW), { now: NOW });
  assert.match(view.textContent, /Shared files \(4\)/);
  assert.deepEqual(find(view, (n) => n.attrs["data-link"]).map((r) => r.children[0].children[0].textContent),
    ["hello.txt", "Release notes 2.4 (page)", "Contract draft v3.pdf", "Logo pack.zip"]);
});

test("S2: with an act function the hub offers Revoke link and Stop app only, one click each; widening is words, not a button", () => {
  const calls = [];
  const act = (path, done) => calls.push([path, done]);
  const app = { id: "d".repeat(32), workspace: "Marketing", kind: "page", state: "running", app_name: "Release notes", by_name: "Anna", by_kind: "person", access: "company", version: 3, visits_30d: 4, updated_ms: NOW };
  const stopped = { ...app, id: "e".repeat(32), state: "stopped" };
  const view = publishedView(data({ apps: [app, stopped] }), { now: NOW, act });
  assert.deepEqual(find(view, (n) => n.tag === "button").map((b) => b.attrs["data-pubtab"] ?? b.attrs["data-stop"]), ["apps", "links", "d".repeat(32)]);
  find(view, (n) => n.attrs["data-stop"])[0].listeners.click();
  assert.equal(calls[0][0], "apps/" + "d".repeat(32) + "/stop");
  assert.match(view.textContent, /approve in Marketing/);
  find(view, (n) => n.attrs["data-pubtab"] === "links")[0].listeners.click();
  const revoke = find(view, (n) => n.tag === "button" && n.attrs["data-revoke"]);
  assert.deepEqual(revoke.map((b) => b.attrs["data-revoke"][0]), ["a", "b"]); // live links only, not the expired one
  revoke[0].listeners.click();
  assert.equal(calls[1][0], "links/" + "a".repeat(32) + "/revoke");
  assert.match(view.textContent, /Extend or add a password: approve in Product/);
  const labels = find(view, (n) => n.tag === "button").map((b) => b.textContent);
  assert.ok(labels.every((l) => !/extend|password|start|roll back|change/i.test(l.replace(/Shared files|Apps & pages/g, ""))), labels.join("|"));
});

test("an agent's share is shown as the agent of the person, never as the person", () => {
  const view = publishedView(data({ links: [link({ by_kind: "agent", by_name: "Severin" }), link({ id: "b".repeat(32), by_kind: "agent", by_name: null })] }), { now: NOW });
  const rows = find(view, (n) => n.tag === "tr" && n.attrs["data-link"]);
  assert.match(rows[0].textContent, /by Severin's agent/);
  assert.match(rows[1].textContent, /by an agent/);
  assert.doesNotMatch(rows[0].textContent, /by Severin(?!')/);
});
