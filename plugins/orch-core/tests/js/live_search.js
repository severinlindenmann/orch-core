// Unit test for the live search (D) in static/app.js, run by tests/test_live_search.py with plain node and a tiny
// fake DOM: typing waits 200 ms after the last key, fetches the form's own GET URL, swaps only the named regions,
// keeps the box (focus and caret) even when it sits inside a swapped region, lets only the newest answer land, skips
// IME composition and gives way to Enter.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const listeners = {};
let timers = [];
const fetches = [];

class El {
  constructor(id, kids = []) { this.id = id; this.kids = kids; this.parent = null; this.attrs = {}; kids.forEach((k) => { k.parent = this; }); }
  contains(x) { for (let n = x; n; n = n.parent) if (n === this) return true; return false; }
  replaceWith(other) {
    const p = this.parent;
    if (other.parent) other.parent.kids = other.parent.kids.filter((k) => k !== other);
    if (p) { p.kids = p.kids.map((k) => (k === this ? other : k)); other.parent = p; }
    this.parent = null;
    if (doc.activeElement && !root.contains(doc.activeElement)) doc.activeElement = null;  // moved out: blurred
  }
  find(sel) {
    const want = sel.startsWith("#") ? sel.slice(1) : sel;
    for (const k of this.kids) { if (k.id === want || k.sel === sel) return k; const f = k.find(sel); if (f) return f; }
    return null;
  }
  querySelector(sel) { return this.find(sel); }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
}

// the page: a form (outside the results) with its box, the tabs and the results; a second form whose box sits inside
// its own swapped region (an addon page)
const input = new El("q");
Object.assign(input, { name: "q", value: "", selectionStart: 0, selectionEnd: 0,
  matches: (s) => s === "form[data-live-search] input[type=search]",
  focus() { doc.activeElement = this; }, setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; } });
const form = new El("form", [input]);
Object.assign(form, { dataset: { liveSearch: "#board-tabs, #board-results" }, getAttribute: () => "/board",
  matches: (s) => s === "form[data-live-search]" });
input.form = form;
let tabs = new El("board-tabs");
let results = new El("board-results");
const inner = new El("find");
Object.assign(inner, { name: "find", value: "", selectionStart: 0, selectionEnd: 0,
  matches: (s) => s === "form[data-live-search] input[type=search]",
  focus() { doc.activeElement = this; }, setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; } });
const innerForm = new El("innerform", [inner]);
Object.assign(innerForm, { dataset: { liveSearch: "#addon-page" }, getAttribute: () => "/addons/wiki/",
  matches: (s) => s === "form[data-live-search]" });
inner.form = innerForm;
const addonPage = new El("addon-page", [innerForm]);
const root = new El("root", [form, tabs, results, addonPage]);
const doc = { documentElement: { dataset: {} }, activeElement: null, addEventListener(t, fn) { (listeners[t] ||= []).push(fn); },
  querySelector: (s) => root.find(s), querySelectorAll: () => [] };

// the server's answer: fresh tabs and results; for the addon page a fresh region holding a fresh copy of the box
let answer = "board";
class FakeParser {
  parseFromString(text) {
    if (text.startsWith("addon")) {
      const copy = new El("copy");
      copy.sel = 'input[type=search][name="find"]';
      return new El("doc", [new El("addon-page", [new El("freshform", [copy]), new El("hits:" + text)])]);
    }
    return new El("doc", [new El("board-tabs", [new El("tabs:" + text)]), new El("board-results", [new El("rows:" + text)])]);
  }
}
const respond = (i, text) => fetches[i].resolve({ ok: true, text: () => Promise.resolve(text) });
const history = [];
const win = { matchMedia: () => ({ matches: false }), addEventListener() {}, DOMParser: FakeParser,
  AbortController: class { constructor() { this.signal = { aborted: false }; } abort() { this.signal.aborted = true; } },
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: (n) => { if (timers[n - 1]) timers[n - 1].fn = null; },
  history: { replaceState: (a, b, url) => history.push(url) }, location: { pathname: "/board", href: "http://h/board" } };
class FakeFormData { constructor(f) { this.f = f; } *[Symbol.iterator]() { for (const k of this.f.kids) if (k.name) yield [k.name, k.value]; } }
const ctx = { window: win, document: doc, Date, setTimeout: win.setTimeout, clearTimeout: win.clearTimeout, setInterval: () => 0,
  clearInterval() {}, console, URL, URLSearchParams, FormData: FakeFormData, WeakSet, Promise,
  fetch: (url, opts) => new Promise((resolve) => { fetches.push({ url, opts, resolve }); }) };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const fire = (type, props) => { for (const fn of listeners[type] || []) fn({ type, preventDefault() {}, ...props }); };
const runTimers = () => { const due = timers.filter((t) => t.fn); timers = []; due.forEach((t) => t.fn()); };
const settle = () => new Promise((r) => setImmediate(r));

(async () => {
  // typing: nothing is fetched until 200 ms after the last key
  input.focus();
  input.value = "ex"; fire("input", { target: input });
  input.value = "exp"; fire("input", { target: input });
  assert.strictEqual(fetches.length, 0, "fetched before the pause");
  assert.ok(timers.filter((t) => t.fn).length === 1 && timers.find((t) => t.fn).ms === 200, "not one 200 ms timer");
  runTimers();
  assert.strictEqual(fetches.length, 1);
  assert.strictEqual(fetches[0].url, "http://h/board?q=exp");
  assert.strictEqual(form.attrs["aria-busy"], "true");

  // a newer search aborts the older one; the late older answer never lands
  input.value = "export"; input.selectionStart = input.selectionEnd = 6; fire("input", { target: input });
  runTimers();
  assert.strictEqual(fetches.length, 2);
  assert.ok(fetches[0].opts.signal.aborted, "the older request was not aborted");
  respond(1, "export"); await settle(); await settle();
  respond(0, "exp"); await settle(); await settle();
  assert.ok(root.find("rows:export") && !root.find("rows:exp"), "the stale answer replaced the newer one");
  assert.ok(root.find("tabs:export"), "the tabs were not swapped");
  assert.strictEqual(doc.activeElement, input, "the box lost focus");
  assert.deepStrictEqual(history, ["http://h/board?q=export"]);
  assert.strictEqual(form.attrs["aria-busy"], undefined);

  // IME: nothing while composing, a search once the composition ends
  fire("input", { target: input, isComposing: true });
  assert.strictEqual(timers.filter((t) => t.fn).length, 0, "searched while an IME was composing");
  fire("compositionend", { target: input });
  assert.strictEqual(timers.filter((t) => t.fn).length, 1);

  // Enter: the pending live search is dropped, the form submits as a page load
  fire("submit", { target: form });
  runTimers();  // the cleared timer does nothing
  assert.strictEqual(fetches.length, 2, "a live search ran after Enter");

  // a box inside the swapped region stays the same element, focused, with its caret
  inner.focus(); inner.value = "back"; inner.selectionStart = inner.selectionEnd = 4;
  fire("input", { target: inner });
  runTimers();
  assert.strictEqual(fetches[2].url, "http://h/addons/wiki/?find=back");
  respond(2, "addon:back"); await settle(); await settle();
  assert.ok(root.find("hits:addon:back"), "the addon region was not swapped");
  assert.ok(root.find("freshform").kids.includes(inner), "the live box was replaced by its fresh copy");
  assert.strictEqual(doc.activeElement, inner, "the box inside the region lost focus");
  assert.strictEqual(inner.selectionStart, 4);

  // other text fields are left alone
  const other = new El("note");
  other.matches = () => false;
  fire("input", { target: other });
  assert.strictEqual(timers.filter((t) => t.fn).length, 0);
  console.log("live search ok");
})().catch((e) => { console.error(e); process.exit(1); });
