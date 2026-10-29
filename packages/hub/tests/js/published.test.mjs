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

test("a selective workspace is named as one that shares no details", () => {
  const view = publishedView(data({ hidden: [{ workspace_id: "2".repeat(32), name: "Marketing" }] }), { now: NOW });
  assert.match(find(view, (n) => n.attrs["data-hidden"])[0].textContent, /^Marketing shares no details with the hub/);
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
