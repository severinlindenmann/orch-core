// Unit test for the epic approve form in static/app.js (run by tests/test_inline_confirm_js.py): the fieldset's
// data-start follows the Start radios when the page starts with them restored (reload, Back), on pageshow and on
// change. What the confirm dialog says is built from the fields when pressed (confirm.js, tests/js/confirm_dialog.js).
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const docListeners = {};
const winListeners = {};
const fieldset = { dataset: { start: "" } };
const form = {
  tag: "form",
  dataset: { confirmBuild: "start", confirmEpic: "epic ab12cd34 with 2 children" },
  elements: { start: { value: "dark" } },  // as a browser restores it after a reload or Back
  matches: (sel) => sel === "form[data-confirm-epic]",
  closest(sel) { return this.matches(sel) ? this : null; },
  querySelector: (sel) => (sel === ".charter-factory" ? fieldset : null),
};
const doc = {
  addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
  querySelector() { return null; },
  querySelectorAll(sel) { return sel === "form[data-confirm-epic]" ? [form] : []; },
  createElement() { return {}; },
};
const win = { matchMedia: () => ({ matches: false }), addEventListener(type, fn) { (winListeners[type] ||= []).push(fn); },
              localStorage: null };
const ctx = { window: win, document: doc, Date, setTimeout, clearTimeout, fetch() {}, console, URL };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);

// 1. at start, the restored radio decides
assert.strictEqual(fieldset.dataset.start, "dark");
// 2. on pageshow (Back from the cache), again
form.elements.start.value = "factory";
for (const fn of winListeners.pageshow || []) fn({ persisted: true });
assert.strictEqual(fieldset.dataset.start, "factory");
// 3. on change
form.elements.start.value = "";
for (const fn of docListeners.change || []) fn({ target: form });
assert.strictEqual(fieldset.dataset.start, "");
// no label is kept in the page any more: nothing can go stale
assert.strictEqual(form.dataset.inlineConfirm, undefined);
console.log("charter confirm ok");
