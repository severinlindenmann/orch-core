// Unit test for the AI Factory forms (run by tests/test_inline_confirm_js.py; plain node, a tiny fake DOM, no
// packages). static/app.js: the New ticket form's mode keeps data-mode in step, a factory start is sent once, a choice
// that hides a field clears it. static/confirm.js: the dialog's checklist says what the chosen start signs.
"use strict";
const fs = require("fs");
const vm = require("vm");
const path = require("path");
const assert = require("assert");

const docListeners = {};
const doc = {
  documentElement: { classList: { add() {} } },
  addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() { return {}; },
};
const win = { matchMedia: () => ({ matches: false }), addEventListener() {}, localStorage: null };
const ctx = { window: win, document: doc, Date, setTimeout, clearTimeout, fetch() {}, console, URL, JSON };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const confirmJs = process.argv[3] || path.join(path.dirname(process.argv[2]), "confirm.js");
const cctx = { window: { addEventListener() {} }, document: { documentElement: null, addEventListener() {} }, console, JSON };
vm.createContext(cctx);
vm.runInContext(fs.readFileSync(confirmJs, "utf8"), cctx);
const { startConfig } = cctx.window.orchConfirm;
const fire = (type, event) => (docListeners[type] || []).forEach((fn) => fn(event));

// -- New ticket: data-mode and sent once ------------------------------------------------------------------------------
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
assert.strictEqual(newForm.dataset.inlineConfirm, undefined, "no paragraph label any more: the dialog asks");
mode = "ticket";
fire("change", { target: radio });
assert.strictEqual(box.dataset.mode, "ticket");
assert.strictEqual(submit().prevented, false);  // Ticket mode posts at once, every time
assert.strictEqual(newForm.dataset.sent, undefined);
mode = "factory";
fire("change", { target: radio });
assert.strictEqual(submit().prevented, false);  // the confirmed factory submit goes out ...
assert.strictEqual(newForm.dataset.sent, "1");
assert.strictEqual(submit().prevented, true);  // ... once: a second one is stopped

// -- the dialog's checklist, from the chosen options (both forms) -----------------------------------------------------
const LIMITS = "up to 25 children or 72 hours, children of size m or smaller";
const form = (start, fields, isNew) => ({
  dataset: isNew ? { newForm: "", confirmBuild: "start", factoryConfirm: LIMITS }
                 : { confirmBuild: "start", confirmEpic: "epic ab12cd34 with 1 child", factoryConfirm: LIMITS },
  elements: { [isNew ? "mode" : "start"]: { value: start }, title: { value: "Backup" }, ...fields },
  querySelector: (sel) => (sel === "[data-release-choice]" && "release" in fields ? {} : null),
});
const texts = (cfg) => cfg.items.map((i) => i.text);
for (const isNew of [true, false]) {
  let cfg = startConfig(form("factory", {}, isNew));
  assert.strictEqual(cfg.ok, "Start AI Factory");
  assert.ok(texts(cfg).includes("Up to 25 children or 72 hours, children of size m or smaller"));
  assert.ok(texts(cfg).some((t) => t.startsWith("A command outside your grants")));
  for (const [rel, part] of [["none", "Nothing is released"], ["merge", "Merges each child by itself"],
                             ["dev", "deploys dev by itself"]]) {
    cfg = startConfig(form("dark", { release: { value: rel } }, isNew));
    assert.strictEqual(cfg.ok, "Start Dark run");
    assert.strictEqual(cfg.tone, "");
    assert.ok(texts(cfg).some((t) => t.includes(part)), rel);
    assert.ok(texts(cfg).some((t) => t.startsWith("Sessions run without permission prompts")));
    if (rel !== "none") assert.ok(texts(cfg).some((t) => t.includes("nothing goes to production")));
    assert.ok(!texts(cfg).some((t) => t.includes("rollback")));
  }
  // Production names the window and whether the signed rollback runs; it is the care dialog
  for (const rb of [false, true]) {
    cfg = startConfig(form("dark", { release: { value: "prod" }, rollback: { checked: rb } }, isNew));
    assert.strictEqual(cfg.tone, "care");
    assert.strictEqual(cfg.ok, "Start and release to production");
    const prod = cfg.items.find((i) => i.text.startsWith("Releases to production by itself"));
    assert.ok(prod && prod.tone === "care" && prod.text.includes("never before its release window opens"));
    assert.ok(texts(cfg).includes(rb ? "Runs the recipe's rollback by itself if the production check fails"
                                     : "No rollback by itself if the production check fails"));
  }
  // the opt-in auto-close: it says it replaces your verdict, and only while the box is ticked
  cfg = startConfig(form("dark", { release: { value: "none" }, close: { checked: true } }, isNew));
  const close = cfg.items.find((i) => i.text.startsWith("Closes the epic by itself"));
  assert.ok(close && close.tone === "care" && cfg.tone === "care");
  assert.ok(close.text.includes("in place of your verdict") && close.text.includes("nothing is executed or verified by the factory")
            && close.text.includes("coverage is text mentions only") && close.text.includes("an epic that names no file is not closed by itself")
            && close.text.includes("with no release nothing is deployed or run"), close.text);
  assert.ok(!cfg.stays.includes("the verdict"));
  cfg = startConfig(form("dark", { release: { value: "prod" }, close: { checked: true } }, isNew));
  assert.ok(!cfg.items.find((i) => i.text.startsWith("Closes")).text.includes("nothing is deployed"));
  cfg = startConfig(form("dark", { release: { value: "dev" }, close: { checked: false } }, isNew));
  assert.ok(texts(cfg).includes("You give the verdict") && cfg.stays.includes("the verdict"));
}
assert.strictEqual(startConfig(form("ticket", {}, true)), null, "Ticket mode asks nothing");

// -- a choice that hides the rollback or the close clears it, so a hidden box is never sent ticked ----------------------
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
console.log("factory forms ok");
