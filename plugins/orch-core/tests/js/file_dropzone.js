// Unit test for static/files.js (run by tests/test_file_dropzone.py; plain node, a small fake DOM, no packages): a
// pasted screenshot becomes screenshot-<timestamp>.png in the input's files, a drop adds, Remove takes one out, a file
// over the limit is refused with a plain message, and a native pick adds to what is listed instead of replacing it.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

let active = null;
class El {
  constructor(tag) { Object.assign(this, { tag, children: [], parent: null, dataset: {}, className: "", textContent: "",
    attrs: {}, listeners: {}, classList: { add: (c) => { this.className += " " + c; }, remove: (c) => { this.className = this.className.replace(" " + c, ""); } } }); }
  setAttribute(k, v) { this.attrs[k] = v; }
  append(...xs) { xs.forEach((x) => { x.parent = this; this.children.push(x); }); }
  after(x) { x.parent = this.parent; const sib = this.parent.children; sib.splice(sib.indexOf(this) + 1, 0, x); }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) {
    if (sel === ".dz-remove") return this.all().filter((c) => c.className.includes("dz-remove"));
    if (sel === "input[type=file][data-dropzone]") return this.all().filter((c) => c.tag === "input" && "dropzone" in c.dataset);
    return [];
  }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  dispatch(t, ev = {}) { const e = { type: t, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...ev };
    (this.listeners[t] || []).forEach((fn) => fn(e)); return e; }
  focus() { active = this; }
  click() { this.clicked = (this.clicked || 0) + 1; this.dispatch("click"); }
  set textContent(v) { this._text = v; if (v === "") this.children = []; }
  get textContent() { return this._text; }
}
class FakeFile { constructor(parts, name, opts = {}) { this.name = name; this.type = opts.type || ""; this.size = opts.size !== undefined ? opts.size : parts.reduce((n, p) => n + (p.size || String(p).length), 0); } }
class FakeDataTransfer { constructor() { const list = []; this.files = list; this.items = { add: (f) => list.push(f) }; } }
class FakeReader { readAsDataURL(f) { this.result = "data:" + f.type + ";base64,AAAA"; this.onload(); } }
const body = new El("body");
const ask = new El("textarea"); ask.id = "ask";
const input = new El("input"); input.type = "file"; input.dataset.dropzone = ""; input.dataset.pasteFrom = "ask";
input.dataset.maxBytes = String(50 * 1048576); input.files = [];
body.append(ask, input);
const doc = {
  body, documentElement: body,
  get activeElement() { return active; },
  createElement: (t) => new El(t),
  getElementById: (id) => (id === "ask" ? ask : null),
  addEventListener() {},
  querySelectorAll: (sel) => body.querySelectorAll(sel),
};
const win = { DataTransfer: FakeDataTransfer, File: FakeFile, FileReader: FakeReader };
const ctx = { window: win, document: doc, console, Date };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);

const zone = body.children[2];
assert.ok(zone && zone.className === "dropzone", "the zone sits right after the input");
const text = (el) => [el.textContent || "", ...el.children.map(text)].join(" ");
assert.ok(text(zone).includes("paste a screenshot with Ctrl/Cmd+V into the Ask box") && text(zone).includes("Up to 50 MB per file."));
const choose = zone.children.find((c) => c.tag === "button");
assert.strictEqual(choose.textContent, "Choose files"); assert.strictEqual(choose.type, "button");
assert.strictEqual(input.tabIndex, -1);
choose.click(); assert.strictEqual(input.clicked, 1, "Choose files opens the native picker");
const list = zone.children.find((c) => c.tag === "ul");
const names = () => input.files.map((f) => f.name);

// 1. a pasted image: renamed, attached, the paste itself stopped; plain text pastes stay text
const shot = new FakeFile(["x"], "image.png", { type: "image/png", size: 2048 });
const ev = ask.dispatch("paste", { clipboardData: { files: [shot] } });
assert.ok(ev.defaultPrevented);
assert.ok(/^screenshot-\d{8}-\d{6}\.png$/.test(names()[0]), names()[0]);
assert.strictEqual(input.files[0].type, "image/png");
assert.strictEqual(ask.dispatch("paste", { clipboardData: { files: [] } }).defaultPrevented, false);
// the list: thumbnail (a data: URL, CSP allows no blob:), name, size, a Remove button named for its file
const item = list.children[0];
assert.strictEqual(item.children[0].tag, "img"); assert.ok(item.children[0].src.startsWith("data:image/png"));
assert.strictEqual(item.children[2].textContent, "2 KB");
assert.ok(item.children[3].attrs["aria-label"].startsWith("Remove screenshot-"));

// 2. a drop adds; a file over the limit is refused with a plain message and never reaches the input
const pdf = new FakeFile([], "spec.pdf", { type: "application/pdf", size: 3 * 1048576 });
const huge = new FakeFile([], "video.mov", { type: "video/quicktime", size: 61 * 1048576 });
assert.ok(zone.dispatch("drop", { dataTransfer: { files: [pdf, huge] } }).defaultPrevented);
assert.deepStrictEqual(names().slice(1), ["spec.pdf"]);
const msg = zone.children.find((c) => c.className.startsWith("dz-msg"));
assert.ok(msg.className.includes("is-bad") && msg.textContent.includes("video.mov (61 MB)") && msg.textContent.includes("50 MB per file"), msg.textContent);
assert.strictEqual(list.children[1].children[0].textContent, "PDF");  // no thumbnail for a non-image
assert.ok(zone.dispatch("dragover").defaultPrevented);

// 3. the native picker replaces input.files with its pick: the listed files stay and the pick is added
const picked = new FakeFile([], "notes.txt", { type: "text/plain", size: 10 });
input.files = [picked];
input.dispatch("change");
assert.strictEqual(names().length, 3); assert.strictEqual(names()[2], "notes.txt");

// 4. Remove takes exactly that file out of the input and moves focus to the next Remove
list.children[1].children[3].click();
assert.deepStrictEqual(names().slice(1), ["notes.txt"]);
assert.strictEqual(active, list.children[1].children[3]);
list.children[1].children[3].click(); list.children[0].children[3].click();
assert.deepStrictEqual(names(), []); assert.strictEqual(active, choose, "with nothing left, focus goes back to Choose files");

// 5. names
const { screenshotName, size } = win.orchFiles;
assert.strictEqual(screenshotName("image/png", new Date(2026, 9, 7, 9, 5, 3).getTime(), 0), "screenshot-20261007-090503.png");
assert.strictEqual(screenshotName("image/jpeg", 0, 1).endsWith("-2.jpg"), true);
assert.strictEqual(size(1536), "2 KB"); assert.strictEqual(size(1572864), "1.5 MB");
console.log("file dropzone ok");
