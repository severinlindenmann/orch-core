// Unit test for the Board's data-autosubmit filters and the data-busy Refresh in static/app.js, run by
// tests/test_board_polish.py with plain node: a select changed by pointer applies at once, one stepped through with
// the keyboard waits for the Filter button (WCAG 3.2.2); the busy timer stops once its form left the page.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const listeners = {};
const timers = [];
const doc = { documentElement: { dataset: {} }, activeElement: null, addEventListener(t, fn) { (listeners[t] ||= []).push(fn); },
              querySelector() { return null; }, querySelectorAll() { return []; } };
let submits = 0;
const form = { isConnected: true, dataset: { busy: "Refreshing…" }, requestSubmit() { submits += 1; },
               matches: (s) => s === "form[data-autosubmit]" || s === "form[data-busy]", querySelector: () => button };
const select = { tagName: "SELECT", matches: (s) => s === "select" || s === "form[data-autosubmit] select",
                 closest: (s) => (s === "form[data-autosubmit]" ? form : s === "form[data-autosubmit] select" ? select : null) };
const button = { attrs: {}, textContent: "Refresh", setAttribute(k, v) { this.attrs[k] = v; } };
const ctx = { window: { matchMedia: () => ({ matches: false }), addEventListener() {} }, document: doc, Date,
              setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {}, setInterval: () => 0,
              clearInterval() {}, fetch() {}, console, URL, WeakSet };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const fire = (type, props) => { const ev = { type, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props };
  for (const fn of listeners[type] || []) fn(ev); return ev; };

// keyboard: arrow keys change the value step by step, nothing is submitted
fire("keydown", { key: "ArrowDown", target: select });
fire("change", { target: select });
fire("keydown", { key: "ArrowDown", target: select });
fire("change", { target: select });
assert.strictEqual(submits, 0, "a keyboard change submitted the filters");
// pointer: picking an option applies it
fire("pointerdown", { target: select });
fire("change", { target: select });
assert.strictEqual(submits, 1);
// back on the keyboard afterwards: waits again
fire("keydown", { key: "ArrowUp", target: select });
fire("change", { target: select });
assert.strictEqual(submits, 1);

// busy: after the first tick the button says so; once the form is gone the timer stops re-arming
fire("submit", { target: form });
timers.shift()();
assert.strictEqual(button.textContent, "Refreshing…");
form.isConnected = false;
const before = timers.length;
timers.shift()();
assert.strictEqual(timers.length, before - 1, "the busy timer kept running after its form left the page");
console.log("board filters ok");
