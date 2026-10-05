// Unit test for the in-place page swap and the live refresh in static/app.js, run by
// tests/test_dashboard_host_adapter.py with plain node and a fake DOM, fetch and EventSource:
//  - a page prefetched before a change is never served after it (the cache is cleared on "change");
//  - a page fetched before a change keeps the queued refresh alive, so the refresh corrects it;
//  - a stale in-flight live refresh is dropped once a newer document is shown, but a user's click or Back is not.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const listeners = {};
const winListeners = {};
let timers = [];
const fetches = [];
const pushed = [];
let swaps = 0;

const mainEl = () => ({ replaceWith() { swaps += 1; }, querySelectorAll: () => [], querySelector: () => null });
const menuEl = () => ({ replaceWith() {} });
const doc = {
  documentElement: { dataset: { path: "/board", version: "v1" } }, title: "board", hidden: false, activeElement: null,
  body: { querySelectorAll: () => [], classList: { add() {}, remove() {} } },
  addEventListener(t, fn) { (listeners[t] ||= []).push(fn); },
  querySelector: (s) => (s === "main.content" ? mainEl() : s === "nav.menu" ? menuEl() : null),
  querySelectorAll: () => [], getElementById: () => null, adoptNode: (n) => n,
};
class FakeParser {
  parseFromString(text) {
    return { title: text, documentElement: { dataset: {} }, querySelector: (s) => (s === "main.content" ? mainEl() : s === "nav.menu" ? menuEl() : null) };
  }
}
let source = null;
class FakeSource {
  constructor() { this.l = {}; source = this; }
  addEventListener(t, fn) { this.l[t] = fn; }
  close() {}
}
const win = {
  matchMedia: () => ({ matches: false }), addEventListener(t, fn) { (winListeners[t] ||= []).push(fn); }, DOMParser: FakeParser,
  EventSource: FakeSource, scrollTo() {}, history: { pushState: (s, t, u) => pushed.push(u) },
  location: { pathname: "/board", search: "", hash: "", href: "http://h/board", origin: "http://h" },
};
const setTimeoutFake = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
const clearTimeoutFake = (n) => { if (timers[n - 1]) timers[n - 1].fn = null; };
const ctx = { window: win, document: doc, Date, setTimeout: setTimeoutFake, clearTimeout: clearTimeoutFake, setInterval: () => 0,
  clearInterval() {}, console, URL, URLSearchParams, WeakSet, WeakMap, Promise, EventSource: FakeSource, DOMParser: FakeParser,
  fetch: (url) => new Promise((resolve) => { fetches.push({ url, resolve }); }) };
win.fetch = ctx.fetch;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);

const answer = (i, title) => fetches[i].resolve({ ok: true, url: "http://h" + fetches[i].url, headers: { get: () => "text/html" }, text: () => Promise.resolve(title) });
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r)); };
const runTimers = (ms) => { const due = timers.filter((t) => t.fn && (ms === undefined || t.ms === ms)); due.forEach((t) => { const f = t.fn; t.fn = null; f(); }); };
const fire = (type, props) => { for (const fn of listeners[type] || []) fn({ type, button: 0, preventDefault() {}, ...props }); };
const link = (href) => { const a = { getAttribute: () => href, hasAttribute: () => false, target: "", closest: (sel) => (sel === "a[href]" ? a : null) }; return a; };
const click = (href) => { const a = link(href); fire("click", { target: a }); };
const change = () => source.l.change();

(async () => {
  // 1. prefetched on hover, then a change, then a click: the click fetches again instead of using the old page
  fire("mouseover", { target: link("/activity") });
  runTimers(65);
  assert.strictEqual(fetches.length, 1);
  answer(0, "old activity"); await settle();
  change();
  click("/activity");
  assert.strictEqual(fetches.length, 2, "the page fetched before the change was served after it");
  answer(1, "new activity"); await settle();
  assert.strictEqual(doc.title, "new activity");
  assert.ok(!timers.some((t) => t.fn && t.ms === 1500), "a fresh page left a refresh queued");

  // 2. a click already in flight when a change arrives: the page may be stale, so the refresh stays and corrects it
  click("/reports");
  assert.strictEqual(fetches.length, 3);
  change();
  answer(2, "reports before the change"); await settle();
  assert.strictEqual(doc.title, "reports before the change");
  assert.ok(timers.some((t) => t.fn && t.ms === 1500), "the queued refresh was cancelled by a stale page");
  runTimers(1500);
  assert.strictEqual(fetches.length, 4, "the refresh did not fetch");
  answer(3, "reports after the change"); await settle();
  assert.strictEqual(doc.title, "reports after the change");

  // 3. a stale in-flight refresh is dropped once a click showed a newer document; the click itself is applied
  change();
  runTimers(1500);
  assert.strictEqual(fetches.length, 5);  // refresh in flight
  click("/workspace");
  assert.strictEqual(fetches.length, 6);
  answer(5, "board by click"); await settle();
  answer(4, "stale refresh"); await settle();
  assert.strictEqual(doc.title, "board by click", "a stale refresh landed on a newer document");

  // 4. a click that finishes after a refresh is still applied (never dropped)
  change();
  runTimers(1500);
  click("/activity");
  const refreshAt = fetches.length - 2;
  answer(refreshAt, "refresh first"); await settle();
  assert.strictEqual(doc.title, "refresh first");
  answer(fetches.length - 1, "click second"); await settle();
  assert.strictEqual(doc.title, "click second", "a user's click was dropped");

  // 5. Back (popstate) is also never dropped by a refresh that finished first
  win.location.pathname = "/groom"; win.location.href = "http://h/groom";
  change();
  runTimers(1500);
  for (const fn of winListeners.popstate || []) fn({});
  const r = fetches.length - 2;
  answer(r, "refresh before back"); await settle();
  answer(fetches.length - 1, "back"); await settle();
  assert.strictEqual(doc.title, "back", "Back was dropped");
  console.log("page swap ok");
})().catch((e) => { console.error(e); process.exit(1); });
