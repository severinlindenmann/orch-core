// Unit test for "Actions keep your place" in static/app.js, run by tests/test_keep_place.py with plain node and a tiny
// fake DOM: a POST form in main posts in the background, the page the redirect lands on is swapped in, the window
// keeps its place with the form's region at the same height on screen and focused, the server's message shows next to
// it; an armed (prevented) submit, a form outside main and a failed fetch are left alone or said in place; a redirect
// to a page it cannot swap is one GET of it, never a second POST; without fetch the place is kept for the next load.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const SRC = fs.readFileSync(process.argv[2], "utf8");

class El {
  constructor(id, top = 0, kids = [], extra = {}) {
    this.id = id; this.top = top; this.kids = []; this.parentElement = null; this.attrs = {}; this.classes = new Set();
    this.dataset = {}; this.textContent = ""; Object.assign(this, extra); kids.forEach((k) => this.add(k));
  }
  add(k) { k.parentElement = this; this.kids.push(k); return this; }
  closest(sel) {
    for (let n = this; n; n = n.parentElement) if (n.is && n.is(sel)) return n;
    return null;
  }
  is(sel) {
    if (sel === "[id]") return Boolean(this.id);
    if (sel === "main.content") return this.id === "main";
    return false;
  }
  all() { return this.kids.flatMap((k) => [k, ...k.all()]); }
  querySelector(sel) {
    return this.all().find((n) => (sel.startsWith("#") ? n.id === sel.slice(1)
      : n.is(sel) || (n.sels || []).some((s) => sel.split(", ").includes(s)))) || null;
  }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return { top: this.top - win.scrollY }; }
  get classList() { const c = this.classes; return { add: (x) => c.add(x), remove: (x) => c.delete(x), contains: (x) => c.has(x) }; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  replaceWith(other) {
    const p = this.parentElement;
    p.kids = p.kids.map((k) => (k === this ? other : k));
    other.parentElement = p;
    this.parentElement = null;
  }
  before(x) { const p = this.parentElement; p.kids.splice(p.kids.indexOf(this), 0, x); x.parentElement = p; }
  focus() { doc.activeElement = this; }
  append(...xs) { xs.forEach((x) => { if (typeof x === "object") this.add(x); }); }
}

// the page: main > #run-waiting (at 1000) > #pc-R1 (at 1100) > form > button; the window scrolled to 1200
let body;
const page = () => {
  const button = new El("", 1150, [], { name: "", value: "" });
  const form = new El("", 1120, [button], { action: "/permits/R1/grant" });
  Object.assign(form.attrs, { method: "post", action: "/permits/R1/grant" });
  form.matches = (sel) => sel === "main.content form" && Boolean(form.closest("main.content"));
  form.fields = [["sha", "abc"], ["next", "/factory/L-1"]];
  const card = new El("pc-R1", 1100, [form]);
  const main = new El("main", 0, [new El("run-waiting", 1000, [card])]);
  const menu = new El("menu-nav", 0, [], { sels: ["nav.menu"] });
  body = new El("body", 0, [menu, main]);
  return { button, form, card, main };
};
// the server's answer, as DOMParser reads it: a page whose main has a banner 300 px high above the same section
const answer = (flash, keepCard) => {
  const section = new El("run-waiting", 1300, keepCard ? [new El("pc-R1", 1400)] : [new El("pc-R2", 1400)]);
  const kids = [section];
  if (flash) kids.unshift(new El("", 0, [], { sels: [flash.err ? ".flash.err" : ".flash.ok"], textContent: " " + flash.text + " ",
    classes: new Set(["flash", flash.err ? "err" : "ok"]) }));
  const main = new El("main", 0, kids);
  const d = new El("doc", 0, [new El("menu-nav", 0, [], { sels: ["nav.menu"] }), main]);
  d.title = "Run"; d.documentElement = { dataset: {} };
  return d;
};
let nextDoc = null;
class FakeParser { parseFromString() { return nextDoc; } }
class FakeFormData {
  constructor(f) { this.pairs = [...f.fields]; }
  has(k) { return this.pairs.some(([n]) => n === k); }
  append(k, v) { this.pairs.push([k, v]); }
  [Symbol.iterator]() { return this.pairs[Symbol.iterator](); }
}

const listeners = {};
const doc = {
  documentElement: { dataset: {} }, activeElement: null, title: "",
  get body() { return body; },
  addEventListener(t, fn) { (listeners[t] ||= []).push(fn); },
  querySelector: (sel) => (sel === "main.content" ? body.kids.find((k) => k.id === "main") : body.querySelector(sel)),
  querySelectorAll: () => [],
  getElementById: (id) => body.querySelector("#" + id),
  adoptNode: (x) => x,
  createElement: () => new El(""),
};
const store = {};
const history = [];
let fetches = [];
const win = {
  scrollY: 0, matchMedia: () => ({ matches: false }), addEventListener() {}, DOMParser: FakeParser,
  scrollTo(x, y) { this.scrollY = y; }, scrollBy(x, y) { this.scrollY += y; },
  location: { href: "http://h/factory/L-1", pathname: "/factory/L-1", origin: "http://h", hash: "", search: "" },
  history: { replaceState: (a, b, url) => history.push(url), pushState() {} },
  sessionStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; } },
  fetch: (url, opts) => new Promise((resolve, reject) => { fetches.push({ url, opts, resolve, reject }); }),
};
const load = () => {
  const ctx = { window: win, document: doc, Date, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    console, URL, URLSearchParams, FormData: FakeFormData, WeakSet, WeakMap, Map, Promise, DOMParser: FakeParser, history: win.history,
    fetch: (...a) => win.fetch(...a) };
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
};
const submit = (target, submitter, defaultPrevented = false) => {
  const ev = { target, submitter, defaultPrevented, prevented: false, preventDefault() { this.prevented = true; } };
  (listeners.submit || []).forEach((fn) => fn(ev));
  return ev;
};
const settle = async () => { for (let i = 0; i < 5; i += 1) await new Promise((r) => setImmediate(r)); };
const html = (text, url, more = {}) => ({ ok: true, status: 200, redirected: true, url, text: () => Promise.resolve(text),
  headers: { get: () => "text/html; charset=utf-8" }, ...more });

(async () => {
  page();
  load();
  const before = listeners.submit.length;

  // a Grant: posted in the background, the page swapped in place, the place kept
  let p = page();
  win.scrollY = 1200;
  let ev = submit(p.form, p.button);
  assert.ok(ev.prevented, "the post was not taken");
  assert.strictEqual(fetches.length, 1);
  const f = fetches[0];
  assert.strictEqual(f.url, "http://h/permits/R1/grant");
  assert.strictEqual(f.opts.method, "POST");
  assert.strictEqual(f.opts.credentials, "same-origin");
  assert.strictEqual(f.opts.headers["Content-Type"], "application/x-www-form-urlencoded");
  assert.strictEqual(String(f.opts.body), "sha=abc&next=%2Ffactory%2FL-1");
  assert.strictEqual(p.button.attrs["aria-busy"], "true");
  nextDoc = answer({ text: "granted once" }, false);
  f.resolve(html("<html>", "http://h/factory/L-1?msg=granted+once"));
  await settle();
  const region = doc.getElementById("run-waiting");
  assert.ok(doc.querySelector("main.content").all().includes(region), "main was not swapped");
  assert.strictEqual(win.scrollY, 1500, "the section did not stay at its height on screen (" + win.scrollY + ")");
  assert.strictEqual(doc.activeElement, region, "focus did not go to the form's region");
  assert.strictEqual(region.attrs.tabindex, "-1");
  const receipt = region.parentElement.kids[region.parentElement.kids.indexOf(region) - 1];
  assert.ok(receipt.classes.size === 0 && receipt.className === "receipt", "no receipt next to the region");
  assert.ok(JSON.stringify(receipt.kids.map((k) => k.textContent)).includes("granted once"), "the server's message is missing");
  assert.deepStrictEqual(history, ["http://h/factory/L-1?msg=granted+once"]);
  assert.strictEqual(listeners.submit.length, before, "a swap added listeners");

  // a refusal: the card is still there; it keeps its height on screen and the error is said next to it, as an alert
  p = page();
  win.scrollY = 1200;
  submit(p.form, p.button);
  nextDoc = answer({ text: "the command changed", err: true }, true);
  fetches[1].resolve(html("<html>", "http://h/factory/L-1?err=the+command+changed"));
  await settle();
  const card = doc.getElementById("pc-R1");
  assert.strictEqual(win.scrollY, 1500);
  assert.strictEqual(doc.activeElement, card);
  const err = card.parentElement.kids[card.parentElement.kids.indexOf(card) - 1];
  assert.strictEqual(err.attrs.role, "alert");
  assert.ok(err.className.includes("receipt-err"));

  // the first press of an inline confirm (prevented) and a form outside main are left alone
  p = page();
  submit(p.form, p.button, true);
  const outside = new El("", 0, [], { matches: () => false });
  submit(outside, null);
  assert.strictEqual(fetches.length, 2, "a prevented submit or a form outside main was posted");

  // a redirect to a page it cannot swap: one GET of it (a navigation), never a second POST
  p = page();
  submit(p.form, p.button);
  fetches[2].resolve({ ok: true, status: 200, redirected: true, url: "http://h/terminals?msg=x", text: () => Promise.resolve("{}"),
    headers: { get: () => "application/json" } });
  await settle();
  assert.strictEqual(win.location.href, "http://h/terminals?msg=x");
  assert.strictEqual(fetches.length, 3);
  win.location.href = "http://h/factory/L-1";

  // the dashboard unreachable: said in place, nothing sent again
  p = page();
  submit(p.form, p.button);
  fetches[3].reject(new Error("offline"));
  await settle();
  const said = p.form.parentElement.kids[0];
  assert.ok(said.className.includes("receipt-err") && JSON.stringify(said.kids.map((k) => k.textContent)).includes("offline"));
  assert.strictEqual(p.button.attrs["aria-busy"], undefined);
  assert.strictEqual(fetches.length, 4);

  // an addon's action (it may answer with a one-time download a background fetch would use up) posts as before
  p = page();
  p.form.attrs.action = "/addons/wiki/actions/export";
  ev = submit(p.form, p.button);
  assert.ok(!ev.prevented && fetches.length === 4, "an addon action was posted in the background");

  // without fetch: the form posts as before, and the place is kept for the next load of this page, restored once
  const realFetch = win.fetch;
  win.fetch = undefined;
  p = page();
  win.scrollY = 1200;
  ev = submit(p.form, p.button);
  assert.ok(!ev.prevented, "the plain post was stopped");
  const kept = JSON.parse(store["orch-place"]);
  assert.strictEqual(kept.y, 1200);
  assert.deepStrictEqual(kept.ids.map((r) => r.id), ["pc-R1", "run-waiting"]);
  assert.strictEqual(kept.path, "/factory/L-1");
  win.fetch = realFetch;
  page();  // the next load: the section moved down by 300 px
  doc.getElementById("run-waiting").top = 1300;
  body.querySelector("#pc-R1").top = 1400;
  win.scrollY = 0;
  load();
  assert.strictEqual(win.scrollY, 1500, "the stored place was not restored (" + win.scrollY + ")");
  assert.strictEqual(store["orch-place"], undefined, "the place was not used once");
  win.scrollY = 0;
  load();
  assert.strictEqual(win.scrollY, 0, "the place was restored twice");
  console.log("keep place ok");
})().catch((e) => { console.error(e); process.exit(1); });
