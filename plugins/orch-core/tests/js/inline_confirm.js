// Unit test for the inline confirm in static/app.js, run by tests/test_inline_confirm_js.py with plain node: a tiny
// fake DOM records the listeners app.js registers, then events are replayed in the order a browser sends them.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

let now = 1000;
const listeners = {};
const timers = [];
class ClassList { constructor() { this.s = new Set(); } add(c) { this.s.add(c); } remove(c) { this.s.delete(c); } contains(c) { return this.s.has(c); } }
class El {
  constructor(tag, parent) { this.tag = tag; this.parent = parent; this.children = []; this.dataset = {}; this.classList = new ClassList();
    this.style = {}; this.textContent = ""; this.attrs = {}; this.offsetWidth = 120; }
  setAttribute(k, v) { this.attrs[k] = v; }
  after(el) { el.parent = this.parent; this.parent.children.push(el); }
  append(el) { el.parent = this; this.children.push(el); }
  remove() { this.parent.children = this.parent.children.filter((c) => c !== this); }
  addEventListener(type, fn) { (this.on ||= {})[type] = fn; }
  focus() { doc.activeElement = this; }
  closest(sel) { let n = this; while (n) { if (n.matches && n.matches(sel)) return n; n = n.parent; } return null; }
  matches(sel) {
    if (sel === "form[data-inline-confirm]") return this.tag === "form" && "inlineConfirm" in this.dataset;
    if (sel === "form[data-armed]") return this.tag === "form" && "armed" in this.dataset;
    return false;
  }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelector(sel) {
    if (sel === "button[data-armed-label]") return this.all().find((c) => c.tag === "button" && "armedLabel" in c.dataset) || null;
    if (sel === "button[type=submit]") return this.all().find((c) => c.tag === "button" && c.type === "submit") || null;
    return null;
  }
  querySelectorAll(sel) {
    if (sel === ".ic-cancel, .ic-live") return this.all().filter((c) => /ic-cancel|ic-live/.test(c.className || ""));
    if (sel === "form[data-armed]") return this.all().filter((c) => c.matches("form[data-armed]"));
    return [];
  }
}
const body = new El("body", null);
const doc = {
  activeElement: null,
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  querySelector() { return null; },
  querySelectorAll(sel) { return body.querySelectorAll(sel); },
  createElement(tag) { return new El(tag, null); },
};
const win = { matchMedia: () => ({ matches: false }), addEventListener() {}, localStorage: null };
const ctx = { window: win, document: doc, Date: { now: () => now }, setTimeout: (fn, ms) => { timers.push({ fn, at: now + ms }); return timers.length; },
              clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = () => {}; }, fetch() {}, console, URL };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2] || require("path").join(__dirname, "../../src/orch/dashboard/static/app.js"), "utf8"), ctx);

const form = new El("form", body); body.children.push(form);
form.dataset.inlineConfirm = "Confirm · plan ab1e…7f";
const button = new El("button", form); button.type = "submit"; button.textContent = "Approve plan"; form.children.push(button);

let posted = 0;
const fire = (type, props = {}) => {
  const ev = { type, target: props.target || button, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props };
  for (const fn of listeners[type] || []) fn(ev);
  return ev;
};
// A press on the button: what the browser sends for a click, and a submit unless the keydown/click was cancelled.
const submit = () => { const ev = fire("submit", { target: form, submitter: button }); if (!ev.defaultPrevented) posted += 1; return ev; };
const click = () => { fire("pointerup"); submit(); };
const tick = (ms) => { now += ms; for (const t of timers.splice(0)) { if (t.at <= now) t.fn(); else timers.push(t); } };

// 1. first press arms, names the hash, keeps focus
click();
assert.strictEqual(posted, 0); assert.strictEqual(button.textContent, "Confirm · plan ab1e…7f"); assert.strictEqual(doc.activeElement, button);

// 2. a double click (second press 150 ms later) does not confirm
tick(150); click(); assert.strictEqual(posted, 0, "double click confirmed");

// 3. Escape reverts and refocuses
fire("keydown", { key: "Escape" }); assert.strictEqual(button.textContent, "Approve plan"); assert.ok(!("armed" in form.dataset));

// 4. holding Enter: the keydown arms; repeats (each ~30 ms, well past 400 ms in total) are cancelled before they
//    reach the button, so nothing posts while the key stays down
const enterDown = (repeat) => { const ev = fire("keydown", { key: "Enter", repeat }); if (!ev.defaultPrevented) submit(); };
enterDown(false); assert.ok("armed" in form.dataset);
for (let i = 0; i < 40; i += 1) { tick(30); enterDown(true); }
assert.strictEqual(posted, 0, "held Enter confirmed");
// releasing the key, then a second, separate press confirms
fire("keyup", { key: "Enter" }); enterDown(false);
assert.strictEqual(posted, 1);

// 5. pressing again only after the 6 s timeout arms again instead of confirming
fire("keydown", { key: "Escape" });  // (a real browser would have left the page after the post)
click(); tick(6100); assert.strictEqual(button.textContent, "Approve plan"); click(); assert.strictEqual(posted, 1);

// 6. the pure rule
const { canConfirm, DOUBLE_CLICK_MS } = win.orchInlineConfirm;
assert.strictEqual(canConfirm({ at: 0, released: false }, 10000), false);
assert.strictEqual(canConfirm({ at: 0, released: true }, DOUBLE_CLICK_MS - 1), false);
assert.strictEqual(canConfirm({ at: 0, released: true }, DOUBLE_CLICK_MS), true);
console.log("inline confirm ok");
