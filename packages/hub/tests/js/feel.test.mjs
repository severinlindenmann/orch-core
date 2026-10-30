import test from "node:test";
import assert from "node:assert/strict";
import { filterSearch, searchEntries, busyButton, createLoadingController, emptyWords, focusIndex, skeletonPlan, PROGRESS_DELAY, TOAST_DURATION } from "../../src/orch_hub/web/feel.js";

test("skeletons follow the page's column structure", () => {
  assert.deepEqual(skeletonPlan("home"), { grid: "home-grid", sections: [4, 2, 2] });
  assert.equal(skeletonPlan("workspaces").grid, "work-grid");
  assert.deepEqual(skeletonPlan("services").sections, [3]);
  assert.deepEqual(skeletonPlan("workspace"), skeletonPlan("unknown"));
});
test("empty states explain the next step without technical errors", () => {
  assert.equal(emptyWords("home")[0], "Nothing needs you");
  assert.match(emptyWords("workspaces")[1], /Press “Connect a workspace” and follow the steps\./);
  assert.doesNotMatch(emptyWords("workspaces")[1] + emptyWords("workspaces-member")[1], /Settings/);
  assert.doesNotMatch(emptyWords("workspaces")[1] + emptyWords("workspaces-member")[1], /Mission Control/);
  assert.match(emptyWords("services")[1], /check again/);
  assert.deepEqual(emptyWords("search"), ["Nothing found", "Try another name or a shorter search."]);
  assert.doesNotMatch(emptyWords("spaces")[1], /orch-hub|command line|terminal/i);
  assert.match(emptyWords("spaces")[1], /New shared space/);
  assert.equal(emptyWords("unknown").length, 2);
});
test("quick loads cancel the delayed bar; overlapping loads keep it visible", () => {
  let callback, delay, visible = false, cancelled = false;
  const begin = createLoadingController(() => { visible = true; }, () => { visible = false; },
    (fn, ms) => { callback = fn; delay = ms; cancelled = false; return 7; },
    (id) => { assert.equal(id, 7); cancelled = true; });
  const quick = begin();
  assert.equal(delay, 120);
  assert.equal(PROGRESS_DELAY, delay);
  assert.equal(visible, false);
  quick();
  assert.equal(cancelled, true);
  const first = begin(), second = begin();
  callback();
  first(); first();
  assert.equal(visible, true);
  assert.equal(cancelled, false);
  second();
  assert.equal(visible, false);
  assert.equal(cancelled, true);
  begin()();
  assert.equal(visible, false);
});
test("focus wraps in both directions, including empty and single-control dialogs", () => {
  assert.equal(focusIndex(2, 3, false), 0);
  assert.equal(focusIndex(0, 3, true), 2);
  assert.equal(focusIndex(0, 1, false), 0);
  assert.equal(focusIndex(0, 0, true), -1);
  assert.equal(TOAST_DURATION, 4000);
});

test("dialog traps Tab, contains outside focus, closes on Escape and restores its opener", async () => {
  const { trapDialog } = await import("../../src/orch_hub/web/feel.js");
  const listeners = {};
  const previous = globalThis.document;
  const doc = { addEventListener: (key, fn) => { listeners[key] = fn; }, removeEventListener: (key) => { delete listeners[key]; } };
  globalThis.document = doc;
  const control = () => ({ tabIndex: 0, isConnected: true, closest: () => null, getClientRects: () => [1], focus() { doc.activeElement = this; } });
  const opener = control(), first = control(), last = control(), disabled = { ...control(), disabled: true };
  const events = {};
  const dialog = {
    querySelectorAll: (selector) => selector === "button" ? [] : [first, disabled, last], querySelector: () => null,
    contains: (target) => [first, last].includes(target),
    addEventListener: (key, fn) => { events[key] = fn; }, removeEventListener: (key) => { delete events[key]; },
    dispatchEvent: () => true, remove() { this.removed = true; }, focus() { doc.activeElement = this; },
  };
  try {
    const release = trapDialog(dialog, opener);
    assert.equal(doc.activeElement, first);
    const key = (key, shiftKey = false) => events.keydown({ key, shiftKey, preventDefault() {} });
    key("Tab", true); assert.equal(doc.activeElement, last);
    key("Tab"); assert.equal(doc.activeElement, first);
    listeners.focusin({ target: opener }); assert.equal(doc.activeElement, first);
    key("Escape"); assert.equal(dialog.removed, true);
    release(); assert.equal(doc.activeElement, opener);
    assert.equal(listeners.focusin, undefined);
  } finally { globalThis.document = previous; }
});

test("toast announces text, replaces previous messages, supports Undo and expires", async () => {
  const { toast } = await import("../../src/orch_hub/web/feel.js");
  const saved = { document: globalThis.document, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const nodes = [], timers = new Map();
  let sequence = 0, undone = 0;
  class Node {
    constructor() { this.nodeType = 1; this.children = []; this.attrs = {}; this.events = {}; }
    setAttribute(key, value) { this.attrs[key] = value; }
    addEventListener(key, fn) { this.events[key] = fn; }
    append(...children) { this.children.push(...children); }
    remove() { this.removed = true; }
  }
  globalThis.document = {
    body: new Node(), getElementById: (id) => nodes.find((node) => node.attrs.id === id),
    createElement: () => { const node = new Node(); nodes.push(node); return node; },
    createTextNode: (text) => ({ nodeType: 3, textContent: text }),
  };
  globalThis.setTimeout = (fn, ms) => { timers.set(++sequence, { fn, ms }); return sequence; };
  globalThis.clearTimeout = (id) => timers.delete(id);
  try {
    toast("<b>Copied</b>");
    const region = document.getElementById("toast-region"), first = region.children[0];
    assert.equal(region.attrs["aria-live"], "polite");
    assert.equal(first.children[0].textContent, "<b>Copied</b>");
    toast("Removed", { undo: () => { undone++; } });
    assert.equal(first.removed, true);
    assert.equal(timers.size, 1);
    const second = region.children[1];
    second.children[1].events.click();
    assert.equal(undone, 1);
    assert.equal(second.removed, true);
    assert.equal(timers.size, 0);
    toast("Done");
    const timer = [...timers.values()][0];
    assert.equal(timer.ms, 4000);
    timer.fn();
    assert.equal(region.children[2].removed, true);
  } finally { Object.assign(globalThis, saved); }
});

test("Escape uses Cancel to settle a prompt, respects cancel prevention, and closes native dialogs", async () => {
  const { trapDialog } = await import("../../src/orch_hub/web/feel.js");
  const previous = globalThis.document;
  globalThis.document = { addEventListener() {}, removeEventListener() {} };
  try {
    for (const mode of ["button", "prevent", "close", "remove"]) {
      let key, settled = false, dispatched = false, closed = false;
      const promise = new Promise(resolve => {
        const button = { textContent: "Cancel", hasAttribute: () => false, click: () => { settled = true; resolve(); } };
        const dialog = {
          querySelectorAll: s => s === "button" && mode === "button" ? [button] : [], querySelector: () => null,
          addEventListener: (s, fn) => { key = fn; }, removeEventListener() {}, focus() {},
          dispatchEvent(event) { dispatched = true; assert.equal(event.type, "cancel"); assert.equal(event.cancelable, true); if (mode === "prevent") event.preventDefault(); return !event.defaultPrevented; },
          remove() { closed = true; }, ...(mode === "close" ? { close() { closed = true; } } : {}),
        };
        const release = trapDialog(dialog);
        key({ key: "Escape", preventDefault() {} });
        release();
        if (mode !== "button") resolve();
      });
      await promise;
      assert.equal(settled, mode === "button");
      assert.equal(dispatched, mode !== "button");
      assert.equal(closed, mode === "close" || mode === "remove");
    }
  } finally { globalThis.document = previous; }
});

test("background replacement preserves fields, expanded forms, focus and selection", async () => {
  const { replaceContent } = await import("../../src/orch_hub/web/feel.js");
  const previous = globalThis.document;
  const doc = globalThis.document = {};
  const field = (tagName, id, value) => ({ tagName, id, value, checked: true, open: true,
    getAttribute: () => null, focus() { doc.activeElement = this; },
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; } });
  const old = [field("INPUT", "answer", "draft"), field("SELECT", "workspace", "legal"), field("DETAILS", "manual")];
  old[0].selectionStart = 1; old[0].selectionEnd = 3;
  const fresh = [field("INPUT", "answer", ""), field("SELECT", "workspace", "product"), field("DETAILS", "manual")];
  fresh[2].open = false;
  const container = { nodes: old, querySelectorAll() { return this.nodes; }, replaceChildren(...nodes) { this.nodes = nodes; } };
  try {
    doc.activeElement = old[0];
    replaceContent(container, fresh);
    assert.equal(fresh[0].value, "draft"); assert.equal(fresh[0].checked, true);
    assert.equal(fresh[1].value, "legal"); assert.equal(fresh[2].open, true);
    assert.equal(doc.activeElement, fresh[0]); assert.equal(fresh[0].selectionStart, 1); assert.equal(fresh[0].selectionEnd, 3);
    const routeField = field("INPUT", "answer", "new route");
    replaceContent(container, [routeField], false);
    assert.equal(routeField.value, "new route");
  } finally { globalThis.document = previous; }
});

test("a second press on a busy button does nothing and returns undefined, so a caller can tell it from a success (null)", async () => {
  const attrs = {};
  const button = { disabled: false, getAttribute: (k) => attrs[k] ?? null, setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: (k) => { delete attrs[k]; } };
  let release;
  const first = busyButton(button, () => new Promise((ok) => { release = () => ok(null); }));
  assert.equal(await busyButton(button, async () => null), undefined);
  release();
  assert.equal(await first, null);
});

test("a shared ticket is found by search for the person it was shared with, and only from what the hub returned", () => {
  const shared = [{ workspace_id: "w".repeat(32), key: "DEMO-7", title: "Invoice export for Ben", shared_by: "Anna Test" }];
  const hit = filterSearch(searchEntries({ shared }), "invoice export");
  assert.deepEqual(hit.map((e) => e.group), ["Shared tickets"]);
  assert.match(hit[0].label, /shared by Anna Test/);
  assert.equal(hit[0].hash, "#/w/" + "w".repeat(32));
  assert.deepEqual(filterSearch(searchEntries({ shared: [] }), "invoice export"), []); // nothing returned for another user
});

test("delayedCommit sends only after the Undo window, Undo cancels it, leaving the page sends it at once", async () => {
  const { delayedCommit } = await import("../../src/orch_hub/web/feel.js");
  const saved = { document: globalThis.document, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const timers = new Map(); let n = 0;
  class Node {
    constructor() { this.nodeType = 1; this.children = []; this.attrs = {}; this.events = {}; }
    setAttribute(k, v) { this.attrs[k] = v; }
    addEventListener(k, f) { this.events[k] = f; }
    append(...c) { this.children.push(...c); }
    remove() {}
  }
  const made = [];
  globalThis.document = { body: new Node(), getElementById: () => made.find((x) => x.attrs.id === "toast-region"),
    createElement: () => { const x = new Node(); made.push(x); return x; }, createTextNode: (t) => ({ nodeType: 3, textContent: t }) };
  globalThis.setTimeout = (fn, ms) => { timers.set(++n, { fn, ms }); return n; };
  globalThis.clearTimeout = (id) => timers.delete(id);
  try {
    const sent = [];
    const a = delayedCommit(() => sent.push("a"), "Asking");
    assert.deepEqual(sent, []);
    [...timers.values()].at(-1).fn();
    assert.deepEqual(sent, ["a"]);
    a.flush();                                   // second flush is a no-op
    assert.deepEqual(sent, ["a"]);
    let undone = 0;
    const b = delayedCommit(() => sent.push("b"), "Asking", { onUndo: () => undone++ });
    b.cancel();
    b.flush();
    assert.deepEqual([sent, undone], [["a"], 1]);
    const c = delayedCommit(() => sent.push("c"), "Asking");
    c.flush();                                   // page leaves
    assert.deepEqual(sent, ["a", "c"]);
  } finally { Object.assign(globalThis, saved); }
});
