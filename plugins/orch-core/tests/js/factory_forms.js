// Unit test for the AI Factory forms in static/app.js (run by tests/test_inline_confirm_js.py; plain node, a tiny fake
// DOM, no packages): the New ticket form's mode keeps data-mode and the confirm label in step and a factory start is
// sent once; the epic page's Start radios set the confirm label and the fieldset's data-start.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const docListeners = {};
const doc = {
  addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() { return {}; },
};
const win = { matchMedia: () => ({ matches: false }), addEventListener() {}, localStorage: null };
const ctx = { window: win, document: doc, Date, setTimeout, clearTimeout, fetch() {}, console, URL };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const fire = (type, event) => (docListeners[type] || []).forEach((fn) => fn(event));

// -- New ticket ----------------------------------------------------------------------------------------------------
const box = { dataset: { mode: "ticket" } };
const button = { disabled: false };
let mode = "ticket";
const newForm = {
  dataset: { factoryConfirm: "up to 25 children or 72 hours, children of size m or smaller" },
  matches: (sel) => sel === "form[data-new-form]",
  closest(sel) { return sel === "[data-mode]" ? box : sel === "form[data-new-form]" ? this : null; },
  querySelector: (sel) => (sel === "input[name=mode]:checked" ? { value: mode } : null),
  querySelectorAll: (sel) => (sel === "button[type=submit]" ? [button] : []),
};
const radio = { name: "mode", closest: (sel) => (sel === "form[data-new-form]" ? newForm : null) };
const submit = () => {
  const ev = { target: newForm, defaultPrevented: false, prevented: false, preventDefault() { this.prevented = true; } };
  fire("submit", ev);
  return ev;
};

mode = "dark";
fire("change", { target: radio });
assert.strictEqual(box.dataset.mode, "dark");
assert.strictEqual(newForm.dataset.inlineConfirm, "Confirm · start the Dark AI Factory, only commands in the Dark " +
  "profile run: up to 25 children or 72 hours, children of size m or smaller");
mode = "factory";
fire("change", { target: radio });
assert.strictEqual(newForm.dataset.inlineConfirm, "Confirm · start the AI Factory: up to 25 children or 72 hours, " +
  "children of size m or smaller");
mode = "ticket";
fire("change", { target: radio });
assert.strictEqual(box.dataset.mode, "ticket");
assert.strictEqual(newForm.dataset.inlineConfirm, undefined);
assert.strictEqual(submit().prevented, false);  // Ticket mode posts at once, every time
assert.strictEqual(newForm.dataset.sent, undefined);
mode = "factory";
fire("change", { target: radio });
assert.strictEqual(submit().prevented, false);  // the confirmed factory submit goes out ...
assert.strictEqual(newForm.dataset.sent, "1");
assert.strictEqual(submit().prevented, true);  // ... once: a second one is stopped

// -- the epic page's Start radios -------------------------------------------------------------------------------------
const fieldset = { dataset: { start: "" } };
const start = { value: "" };
const charter = {
  dataset: { charterConfirm: "Confirm · epic ab12cd34 with 1 child",
             factoryConfirm: "up to 25 children or 72 hours, children of size m or smaller" },
  elements: { delegate: { checked: false }, start, max_children: { value: "10" }, max_size: { value: "m" } },
  matches: (sel) => sel === "form[data-charter-confirm]",
  closest(sel) { return this.matches(sel) ? this : null; },
  querySelector: (sel) => (sel === ".charter-factory" ? fieldset : null),
};
for (const [value, label] of [
  ["dark", "Confirm · epic ab12cd34 with 1 child · start the Dark AI Factory, only commands in the Dark profile run: " +
           "up to 25 children or 72 hours, children of size m or smaller"],
  ["factory", "Confirm · epic ab12cd34 with 1 child · start the AI Factory: up to 25 children or 72 hours, children " +
              "of size m or smaller"],
  ["", "Confirm · epic ab12cd34 with 1 child · no delegation"]]) {
  start.value = value;
  fire("change", { target: charter });
  assert.strictEqual(charter.dataset.inlineConfirm, label);
  assert.strictEqual(fieldset.dataset.start, value);
}
// "Release up to" (phase 6): with the choice on the form, the Dark confirm says what will run, on both forms
let release = "none";
const withChoice = (sel) => (sel === "[data-release-choice]" ? {} : sel === "input[name=release]:checked"
  ? { value: release } : null);
charter.querySelector = (sel) => (sel === ".charter-factory" ? fieldset : withChoice(sel));
start.value = "dark";
for (const [value, tail] of [["none", " · releases nothing"],
  ["merge", " · releases up to merge using the recipe on this machine; nothing releases to production"],
  ["dev", " · releases up to dev (merge, then dev) using the recipe on this machine; nothing releases to production"]]) {
  release = value;
  fire("change", { target: charter });
  assert.strictEqual(charter.dataset.inlineConfirm, "Confirm · epic ab12cd34 with 1 child · start the Dark AI Factory, " +
    "only commands in the Dark profile run: up to 25 children or 72 hours, children of size m or smaller" + tail);
  newForm.querySelector = (sel) => (sel === "input[name=mode]:checked" ? { value: "dark" } : withChoice(sel));
  fire("change", { target: { name: "release", closest: (sel) => (sel === "form[data-new-form]" ? newForm : null) } });
  assert.ok(newForm.dataset.inlineConfirm.endsWith(tail));
}
// Production names the window and whether the signed rollback runs
let rollback = false;
const withProd = (sel) => (sel === "input[name=rollback]" ? { checked: rollback } : withChoice(sel));
charter.querySelector = (sel) => (sel === ".charter-factory" ? fieldset : withProd(sel));
release = "prod";
for (const [rb, tail] of [[false, "; no rollback by itself"],
  [true, "; runs the recipe's rollback if the production check fails"]]) {
  rollback = rb;
  fire("change", { target: charter });
  assert.ok(charter.dataset.inlineConfirm.endsWith(" · releases to production by itself (merge, dev, then production) " +
    "using the recipe on this machine, after its release window" + tail), charter.dataset.inlineConfirm);
}
// the opt-in auto-close: the confirm says it replaces your verdict, and only while the box is ticked
let close = true;
charter.querySelector = (sel) => (sel === ".charter-factory" ? fieldset
  : sel === "input[name=close]" ? { checked: close } : withProd(sel));
fire("change", { target: charter });
assert.ok(charter.dataset.inlineConfirm.endsWith(" · closes the epic by itself when everything is proven, on what the " +
  "agents wrote under the close rules: nothing is executed or verified by the factory; coverage is checked as text mentions only, and an epic that names no file is not closed by itself; this replaces your verdict for " +
  "this run; Reopen stays yours"), charter.dataset.inlineConfirm);  // production is chosen above: no "nothing deployed"
release = "none";
fire("change", { target: charter });
assert.ok(charter.dataset.inlineConfirm.includes("and with no release nothing is deployed or run"));
release = "prod";
close = false;
fire("change", { target: charter });
assert.ok(!charter.dataset.inlineConfirm.includes("closes the epic"));
// a choice that hides the rollback or the close clears it, so a hidden box is never sent ticked
const rbBox = { name: "rollback", checked: true };
const closeBox = { name: "close", checked: true };
const plainForm = { querySelectorAll: (sel) => (sel === "input[name=rollback], input[name=close]" ? [rbBox, closeBox] : []) };
const pick = (name, value) => fire("change", { target: { name, value, closest: (sel) => (sel === "form" ? plainForm : null) } });
pick("release", "dev");
assert.strictEqual(rbBox.checked, false); assert.strictEqual(closeBox.checked, true);
rbBox.checked = true;
pick("release", "prod");
assert.strictEqual(rbBox.checked, true);
pick("mode", "dark");
assert.strictEqual(rbBox.checked, true); assert.strictEqual(closeBox.checked, true);
pick("start", "factory");
assert.strictEqual(rbBox.checked, false); assert.strictEqual(closeBox.checked, false);
assert.ok(!/[A-Z]{4,}/.test(fs.readFileSync(process.argv[2], "utf8").match(/DARK_START = "([^"]*)"/)[1]));
console.log("factory forms ok");
