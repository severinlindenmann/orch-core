// Unit test for static/widgets/orch-frames.js, run by tests/test_widget_frames.py with plain node and a tiny fake DOM:
// the frame's first load is the widget; a second load (it navigated itself) removes the frame and shows the text.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.style = {}; this.on = {}; this.hidden = false;
    this.textContent = ""; this.parent = null; this.contentWindow = tag === "iframe" ? { postMessage() {} } : null; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  insertBefore(c) { return this.appendChild(c); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); this.parent = null; }
  addEventListener(t, fn) { (this.on[t] ||= []).push(fn); }
  dispatchEvent() { return true; }
  closest() { return null; }
  querySelector() { return null; }
}
const box = new El("div");
Object.assign(box.attrs, { "data-doc-url": "/w/L-1/0?n=abcdefgh12", "data-nonce": "abcdefgh12", "data-min-height": "160" });
const html = new El("html");
const doc = { documentElement: html, readyState: "complete", createElement: (t) => new El(t),
              querySelectorAll: () => [box], addEventListener() {} };
const win = { matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {} };
const ctx = { window: win, document: doc, setTimeout: () => 1, clearTimeout() {}, CustomEvent: function () {},
              MutationObserver: function () { this.observe = () => {}; }, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);

const frame = () => box.children.find((c) => c.tag === "iframe");
const iframe = frame();
assert.ok(iframe && iframe.attrs.sandbox === "allow-scripts", "the frame starts");
iframe.on.load.forEach((fn) => fn());
assert.ok(frame(), "the first load is the widget");
iframe.on.load.forEach((fn) => fn());
assert.strictEqual(frame(), undefined, "a second load removes the frame");
assert.strictEqual(box.getAttribute("data-state"), "error");
const status = box.children.find((c) => c.className === "w-frame-status");
assert.match(status.textContent, /tried to leave the page/);
console.log("frames navigate ok");
