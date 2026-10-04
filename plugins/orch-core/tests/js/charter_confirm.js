// Unit test for the epic approve form's confirm label in static/app.js (run by tests/test_inline_confirm_js.py):
// the label follows the delegation fields when the page starts with them restored, on pageshow and on change.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const docListeners = {};
const winListeners = {};
const form = {
  tag: "form",
  dataset: { charterConfirm: "Confirm · epic ab12cd34 with 2 children", inlineConfirm: "Confirm · epic ab12cd34 with 2 children · no delegation" },
  // as a browser restores them after a reload or Back: the box ticked, the limits edited
  elements: { delegate: { checked: true }, max_children: { value: "4" }, max_size: { value: "s" } },
  matches: (sel) => sel === "form[data-charter-confirm]",
  closest(sel) { return this.matches(sel) ? this : null; },
};
const doc = {
  addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
  querySelector() { return null; },
  querySelectorAll(sel) { return sel === "form[data-charter-confirm]" ? [form] : []; },
  createElement() { return {}; },
};
const win = { matchMedia: () => ({ matches: false }), addEventListener(type, fn) { (winListeners[type] ||= []).push(fn); },
              localStorage: null };
const ctx = { window: win, document: doc, Date, setTimeout, clearTimeout, fetch() {}, console, URL };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);

const label = () => form.dataset.inlineConfirm;
// 1. at start, the restored fields decide the label
assert.strictEqual(label(), "Confirm · epic ab12cd34 with 2 children · delegation on: up to 4 children, size ≤ s");
// 2. on pageshow (Back from the cache), again
form.elements.delegate.checked = false;
for (const fn of winListeners.pageshow || []) fn({ persisted: true });
assert.strictEqual(label(), "Confirm · epic ab12cd34 with 2 children · no delegation");
// 3. on change
form.elements.delegate.checked = true;
form.elements.max_size.value = "l";
for (const fn of docListeners.change || []) fn({ target: form });
assert.ok(label().endsWith("size ≤ l"), label());
console.log("charter confirm ok");
