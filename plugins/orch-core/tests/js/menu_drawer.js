// Unit test for the phone menu drawer in static/early.js, run by tests/test_navigation.py with plain node: a tiny
// fake DOM (details > summary + .menu-body, a <dialog> drawer) records the listeners early.js registers.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const listeners = {};
class El {
  constructor(tag, cls = "") { this.tag = tag; this.cls = cls; this.children = []; this.parentElement = null; this.attrs = {}; this.on = {};
    this.classList = { s: new Set(), add: (c) => this.classList.s.add(c), toggle: (c) => (this.classList.s.has(c) ? this.classList.s.delete(c) : this.classList.s.add(c)),
                       contains: (c) => this.classList.s.has(c) }; this.offsetParent = {}; }
  append(el) { if (el.parentElement) el.parentElement.children = el.parentElement.children.filter((c) => c !== el); el.parentElement = this; this.children.push(el); }
  get isConnected() { return true; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(type, fn) { (this.on[type] ||= []).push(fn); }
  dispatch(type) { for (const fn of this.on[type] || []) fn({ type }); this.on[type] = []; }
  focus() { doc.activeElement = this; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  contains(el) { return el === this || this.all().includes(el); }
  matches(sel) {
    if (sel === "details.menu-more > summary") return this.tag === "summary" && this.parentElement && this.parentElement.cls === "menu-more";
    if (sel === "dialog.menu-drawer") return this.tag === "dialog";
    if (sel === ".drawer-close") return this.cls === "drawer-close";
    return false;
  }
  closest(sel) { let n = this; while (n) { if (n.matches(sel)) return n; n = n.parentElement; } return null; }
  querySelector(sel) {
    if (sel === ":scope > summary") return this.children.find((c) => c.tag === "summary") || null;
    if (sel === ":scope > .menu-body") return this.children.find((c) => c.cls === "menu-body") || null;
    if (sel === "dialog.menu-drawer") return this.all().find((c) => c.tag === "dialog") || null;
    if (sel === "[aria-current=page]") return this.all().find((c) => c.attrs["aria-current"] === "page") || null;
    return null;
  }
  querySelectorAll() { return this.all().filter((c) => ["a", "button", "summary"].includes(c.tag)); }
}
const nav = new El("nav", "menu");
const details = new El("details", "menu-more"); nav.append(details);
const summary = new El("summary"); details.append(summary);
const body = new El("div", "menu-body"); details.append(body);
const today = new El("a"); body.append(today);
const board = new El("a"); board.setAttribute("aria-current", "page"); body.append(board);
const theme = new El("button"); body.append(theme);
const drawer = new El("dialog", "menu-drawer"); nav.append(drawer);
const close = new El("button", "drawer-close"); drawer.append(close);
drawer.open = false;
drawer.getBoundingClientRect = () => ({ left: 0, right: 300, top: 0, bottom: 800 });
drawer.showModal = () => { drawer.open = true; };
drawer.close = () => { drawer.open = false; drawer.dispatch("close"); };

const doc = {
  activeElement: null,
  documentElement: { classList: { add() {} } },
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  querySelector(sel) { return sel === "dialog.menu-drawer[open]" && drawer.open ? drawer : null; },
  querySelectorAll() { return [details]; },
};
let narrow = true;
const mql = { get matches() { return narrow; }, addEventListener() {} };
const ctx = { window: { matchMedia: () => mql }, document: doc, Array, String, Boolean, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const fire = (type, props) => { const ev = { type, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props };
  for (const fn of listeners[type] || []) fn(ev); return ev; };

// 1. Menu on a phone opens the drawer with the items in it, focus on the current page's item
summary.focus();
let ev = fire("click", { target: summary });
assert.ok(ev.defaultPrevented, "the <details> must not toggle");
assert.ok(drawer.open && body.parentElement === drawer, "items moved into the open drawer");
assert.strictEqual(doc.activeElement, board);
assert.strictEqual(summary.attrs["aria-expanded"], "true");

// 2. Tab wraps inside the drawer (both directions)
theme.focus(); ev = fire("keydown", { key: "Tab", shiftKey: false });
assert.ok(ev.defaultPrevented); assert.strictEqual(doc.activeElement, close);
ev = fire("keydown", { key: "Tab", shiftKey: true });
assert.ok(ev.defaultPrevented); assert.strictEqual(doc.activeElement, theme);

// 3. Close (Esc fires the same close event natively) puts the items back and returns focus to Menu
fire("click", { target: close });
assert.ok(!drawer.open && body.parentElement === details);
assert.strictEqual(doc.activeElement, summary);
assert.strictEqual(summary.attrs["aria-expanded"], "false");

// 4. A click on the dialog's own padding keeps it open; a click on the backdrop (outside its box) closes it
fire("click", { target: summary }); assert.ok(drawer.open);
fire("click", { target: drawer, clientX: 10, clientY: 400 }); assert.ok(drawer.open, "padding click closed the drawer");
fire("click", { target: drawer, clientX: 350, clientY: 400 }); assert.ok(!drawer.open);

// 5. Wide screens leave the <details> alone (it is the sidebar there)
narrow = false;
ev = fire("click", { target: summary });
assert.ok(!ev.defaultPrevented && !drawer.open);

// 6. No <dialog> support: Menu expands the items inline
narrow = true; drawer.showModal = undefined;
fire("click", { target: summary });
assert.ok(details.classList.contains("expanded"));
console.log("menu drawer ok");
