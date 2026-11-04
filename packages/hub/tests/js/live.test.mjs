import test from "node:test";
import assert from "node:assert/strict";
import { retryDelay, searchEntries, filterSearch, shortcut, itemKey, busyButton, shouldPoll, UNDO_DURATION, focusIndex } from "../../src/orch_hub/web/feel.js";

test("retry backs off 2/4/8/15 seconds and remains capped", () => {
  assert.deepEqual(Array.from({ length: 7 }, (_, i) => retryDelay(i)), [2000, 4000, 8000, 15000, 15000, 15000, 15000]);
  assert.equal(retryDelay(0), 2000); // reset after recovery
});
test("polling waits for the deadline, a visible session, and no overlapping request", () => {
  const ready = { now: 15000, due: 15000, visible: true, active: true, busy: false };
  assert.equal(shouldPoll(ready), true);
  for (const patch of [{ now: 14999 }, { visible: false }, { active: false }, { busy: true }]) assert.equal(shouldPoll({ ...ready, ...patch }), false);
  assert.equal(shouldPoll({ ...ready, now: 60000 }), true);
});
test("search groups live entities, encodes item keys, and hides privileged entries", () => {
  const data = { workspaces: [{ id: "abc", name: "Design" }], items: [{ workspace_id: "abc", key: "T/1", title: "Review layout" }], people: [{ name: "Alice", user_id: "u" }] };
  const member = searchEntries(data);
  assert.deepEqual([...new Set(member.map((e) => e.group))], ["Pages", "Workspaces", "Tickets and items"]);
  assert.equal(member.find((e) => e.group === "Tickets and items").hash, "#/w/abc/item/T%2F1");
  assert.equal(member.some((e) => ["Admin", "Services", "Alice"].includes(e.label)), false);
  const admin = searchEntries({ ...data, admin: true });
  assert.deepEqual([...new Set(admin.map((e) => e.group))], ["Pages", "Workspaces", "Tickets and items", "People", "Actions"]);
  assert.equal(filterSearch(admin, "  LAYOUT review ")[0].label, "T/1 · Review layout");
  assert.equal(filterSearch(admin, "no matches").length, 0);
  assert.equal(filterSearch(admin, "").length, admin.length);
  assert.equal(filterSearch(admin, "connect")[0].action, "connect");
});
test("shortcuts honor typing, modifiers, prefixes and admin permissions", () => {
  for (const key of ["/", "?", "g", "h", "k"]) assert.equal(shortcut(key, { typing: true, meta: true, prefix: true }), null);
  assert.equal(shortcut("K", { meta: true }), "search");
  assert.equal(shortcut("k", { ctrl: true }), "search");
  assert.equal(shortcut("/"), "search");
  assert.equal(shortcut("?"), "shortcuts");
  assert.equal(shortcut("g"), "prefix");
  assert.equal(shortcut("h"), null);
  for (const [key, page] of [["h", "home"], ["w", "workspaces"], ["s", "services"], ["a", "admin"]]) assert.equal(shortcut(key, { prefix: true, admin: true }), page);
  assert.equal(shortcut("a", { prefix: true }), null);
  assert.equal(shortcut("s", { prefix: true }), null);
  assert.equal(shortcut("h", { prefix: true, ctrl: true }), null);
  assert.equal(shortcut("/", { alt: true }), null);
  assert.equal(focusIndex(0, 2, true), 1);
  assert.equal(focusIndex(1, 2, false), 0);
  assert.equal(focusIndex(0, 0, false), -1);
});
test("row identities distinguish workspace and item, without depending on mutable titles", () => {
  assert.notEqual(itemKey({ workspace_id: "a", item_id: "1" }), itemKey({ workspace_id: "b", item_id: "1" }));
  assert.equal(itemKey({ workspace_id: "a", item_id: "1", title: "old" }), itemKey({ workspace_id: "a", item_id: "1", title: "new" }));
  assert.equal(UNDO_DURATION, 5000);
});
test("busy buttons lock synchronously, suppress double clicks, and release on error", async () => {
  const attrs = new Map();
  const button = { disabled: false, getAttribute: (k) => attrs.get(k), setAttribute: (k, v) => attrs.set(k, v), removeAttribute: (k) => attrs.delete(k) };
  let resolve, calls = 0;
  const pending = busyButton(button, () => { calls++; return new Promise((r) => { resolve = r; }); });
  assert.equal(button.disabled, true);
  assert.equal(attrs.get("aria-busy"), "true");
  await busyButton(button, () => { calls++; });
  assert.equal(calls, 1);
  resolve(); await pending;
  assert.equal(button.disabled, false);
  assert.equal(attrs.has("aria-busy"), false);
  await assert.rejects(busyButton(button, () => { throw new Error("failed"); }), /failed/);
  assert.equal(button.disabled, false);
  button.disabled = true;
  await busyButton(button, () => { calls++; });
  assert.equal(calls, 1);
});

test("live row reconciliation retains focus, highlights arrivals, and slides completed rows out", async () => {
  const { reconcileRows } = await import("../../src/orch_hub/web/feel.js");
  const previous = { document: globalThis.document, setTimeout: globalThis.setTimeout, window: globalThis.window };
  const timers = [];
  class Node {
    constructor(tag, key, text = "") { this.tagName = tag.toUpperCase(); this.nodeType = 1; this.dataset = key ? { liveKey: key } : {}; this.childNodes = []; this.attributes = []; this.textContent = text; this.classes = new Set(); this.listeners = {}; }
    get children() { return this.childNodes; }
    get classList() { return { add: (c) => this.classes.add(c), contains: (c) => this.classes.has(c), remove: (c) => this.classes.delete(c) }; }
    set className(value) { this.classes = new Set(value.split(" ")); }
    append(node) { this.childNodes.push(node); node.parent = this; }
    remove() { this.parent.childNodes.splice(this.parent.childNodes.indexOf(this), 1); }
    matches(selector) { return selector.startsWith("article") && this.tagName === "ARTICLE"; }
    querySelectorAll() { return []; }
    querySelector(selector) { return this.children.find((n) => n.classes.has(selector.slice(1))); }
    contains(node) { return node === this || this.children.some((n) => n.contains(node)); }
    addEventListener(key, fn) { this.listeners[key] = fn; }
    getBoundingClientRect() { return { bottom: 100, height: 20 }; }
    setAttribute() {}
  }
  globalThis.document = { createElement: (tag) => new Node(tag), activeElement: null };
  globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); };
  globalThis.window = { scrollBy() {} };
  try {
    const target = new Node("section"), source = new Node("section");
    const retained = new Node("article", "a", "Before"), completed = new Node("article", "b", "Done");
    target.append(retained); target.append(completed);
    source.append(new Node("article", "a", "After")); source.append(new Node("article", "c", "Arrived"));
    document.activeElement = retained;
    reconcileRows(target, source);
    assert.equal(target.children[0], retained);
    assert.equal(document.activeElement, retained);
    assert.equal(retained.textContent, "After");
    const arrived = target.children.find((n) => n.dataset.liveKey === "c");
    assert.equal(arrived.classList.contains("row-new"), true);
    assert.equal(arrived.querySelector(".new-mark").textContent, "New");
    assert.equal(completed.classList.contains("row-done"), true);
    timers.find((t) => t.ms === 220).fn();
    assert.equal(target.children.includes(completed), false);
    timers.find((t) => t.ms === 2000).fn();
    assert.equal(arrived.classList.contains("row-new"), false);
    const empty = new Node("section");
    reconcileRows(target, empty);
    assert.equal(retained.classList.contains("row-done"), false);
    document.activeElement = null;
    retained.listeners.focusout();
    await Promise.resolve();
    assert.equal(retained.classList.contains("row-done"), true);
  } finally { Object.assign(globalThis, previous); }
});
