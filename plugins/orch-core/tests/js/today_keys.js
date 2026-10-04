// Unit test for Today's keyboard, palette matching and delayed send in static/app.js, run by
// tests/test_today_decide.py with plain node and a tiny fake DOM (no packages).
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const listeners = {};
const html = { dataset: { shortcuts: "on" } };
const doc = {
  documentElement: html,
  activeElement: null,
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tag) { return new El(tag); },
};
// Just enough DOM for the delayed send: closest by a tiny selector table, before/remove/append, focus.
class El {
  constructor(tag, props = {}) { this.tag = tag; this.children = []; this.parent = null; this.dataset = {}; this.hidden = false;
    this.attrs = {}; this.className = ""; this.textContent = ""; this.isConnected = true; Object.assign(this, props); }
  setAttribute(k, v) { this.attrs[k] = v; }
  append(...xs) { xs.forEach((x) => { if (typeof x === "object") { x.parent = this; this.children.push(x); } }); }
  before(x) { x.parent = this.parent; this.parent.children.unshift(x); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.isConnected = false; }
  replaceWith(x) { this.remove(); }
  addEventListener(type, fn) { (this.on ||= {})[type] = fn; }
  focus() { doc.activeElement = this; }
  matches(sel) { return sel === "form[data-delayed-send]" ? this.tag === "form" && "delayedSend" in this.dataset
                     : sel === "[data-decision]" ? "decision" in this.dataset : false; }
  closest(sel) {
    for (let n = this; n; n = n.parent) {
      if (sel === ".question" && /question/.test(n.className)) return n;
      if (sel === ".receipt-pending" && /receipt-pending/.test(n.className)) return n;
      if (sel === "[data-ticket]" && n.dataset.ticket) return n;
      if (sel === "[data-decision]" && "decision" in n.dataset) return n;
      if (sel === "form[data-delayed-send], .receipt-pending" && (n.matches("form[data-delayed-send]") || /receipt-pending/.test(n.className))) return n;
    }
    return null;
  }
  querySelector(sel) { return sel.startsWith("span") ? (this.children.filter((c) => c.tag === "span").slice(-1)[0] || null) : null; }
  querySelectorAll(sel) { return sel === "button" ? this.children.filter((c) => c.tag === "button") : []; }
}
const win = { matchMedia: () => ({ matches: false }), addEventListener() {} };
const timers = [];
const ctx = { window: win, document: doc, Date, setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {},
              setInterval: () => 0, clearInterval() {}, fetch() {}, console, URL,
              FormData: class { constructor() { this.m = new Map(); } has(k) { return this.m.has(k); } append(k, v) { this.m.set(k, v); } },
              URLSearchParams: class { constructor(d) { this.d = d; } } };
win.fetch = ctx.fetch; win.URLSearchParams = ctx.URLSearchParams;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2] || require("path").join(__dirname, "../../src/orch/dashboard/static/app.js"), "utf8"), ctx);

const key = (k, tagName, extra = {}) => {
  const ev = { type: "keydown", key: k, target: { tagName, closest: () => null }, defaultPrevented: false,
               preventDefault() { this.defaultPrevented = true; }, ...extra };
  for (const fn of listeners.keydown || []) fn(ev);
  return ev.defaultPrevented;
};

// "?" opens the key list from anywhere but a field; never while typing; never when the workspace turned them off
assert.strictEqual(key("?", "BUTTON"), true, "? ignored");
assert.strictEqual(key("?", "INPUT"), false, "fired while typing in an input");
assert.strictEqual(key("?", "TEXTAREA"), false, "fired while typing in a textarea");
assert.strictEqual(key("?", "SELECT"), false, "fired in a select");
html.dataset.shortcuts = "off";
assert.strictEqual(key("?", "BUTTON"), false, "fired with shortcuts off");
html.dataset.shortcuts = "on";
// a modifier chord other than Ctrl/⌘ K is the browser's
assert.strictEqual(key("j", "BUTTON", { altKey: true }), false);

// palette matching: synonyms, ticket numbers, every word must match
const { matchCommands, tokens } = win.orchPalette;
const commands = [
  { label: "Approve plan · DEMO-0004", words: "approve plan Coverage in CI" },
  { label: "Verdict · DEMO-0001", words: "verdict accept done Retry with backoff" },
  { label: "Go to Board", words: "board tickets" },
];
assert.deepStrictEqual([...tokens("Sign off")], ["approve"]);
assert.strictEqual(matchCommands("ok plan", commands)[0].label, "Approve plan · DEMO-0004");
assert.strictEqual(matchCommands("sign off", commands)[0].label, "Approve plan · DEMO-0004");
assert.strictEqual(matchCommands("4", commands).length, 1);
assert.strictEqual(matchCommands("kanban", commands)[0].label, "Go to Board");
assert.strictEqual(matchCommands("approve backoff", commands).length, 0);
assert.strictEqual(matchCommands("", commands).length, 3);

// answers and messages wait 5 s for Undo
assert.strictEqual(win.orchDelayedSend.DELAY_MS, 5000);

// holding Enter after picking an option: repeats inside the held form or its receipt never reach a button
const repeatIn = (target) => {
  const ev = { type: "keydown", key: "Enter", repeat: true, target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  for (const fn of listeners.keydown || []) fn(ev);
  return ev.defaultPrevented;
};
const card = new El("article", { dataset: { decision: "", ticket: "DEMO-0005" } });
const question = new El("div", { className: "question" }); card.append(question);
const form = new El("form", { dataset: { delayedSend: "Sending your answer to DEMO-0005" }, action: "/t/DEMO-0005/answer" });
question.append(form);
const option2 = new El("button", { name: "value", value: "B" }); form.append(option2);
const fire = (type, props) => { const ev = { type, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props };
  for (const fn of listeners[type] || []) fn(ev); return ev; };
fire("submit", { target: form, submitter: option2 });
const item = win.orchDelayedSend.held[0];
assert.ok(item, "not held");
assert.strictEqual(question.hidden, true, "the answered question is held");
assert.strictEqual(card.hidden, false, "the whole card was hidden");
const undoButton = item.receipt.children.find((c) => c.tag === "button");
assert.strictEqual(doc.activeElement, undoButton);
assert.strictEqual(repeatIn(undoButton), true, "a held Enter reached Undo");
assert.strictEqual(repeatIn(option2), true, "a held Enter reached the form");
undoButton.on.click();  // the Enter that sent it is still down: no release seen, so Undo does nothing
assert.strictEqual(win.orchDelayedSend.held.length, 1, "Undo acted without a release");
fire("keyup", { target: undoButton, key: "Enter" });
undoButton.on.click();
assert.strictEqual(win.orchDelayedSend.held.length, 0, "Undo after a release did not act");
assert.strictEqual(question.hidden, false);
assert.strictEqual(doc.activeElement, option2, "focus did not go back to the option that sent it");
console.log("today keys ok");
