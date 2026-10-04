// #45: the delayed send with Undo (answers, change requests, send-backs, comments) must post once the Undo window
// ends, and every way it can fail must be said in place, with the form back. Run by tests/test_answer_issue45.py
// with plain node and a small fake DOM (no packages). Unlike a selector table, this DOM keeps text nodes as children
// and implements :last-child / :nth-of-type as browsers do, which is what hid the bug: the receipt is
// [mark span, " ", words span, " · ", Undo button], so "span:last-child" matches nothing.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const listeners = {};
const doc = {
  documentElement: { dataset: { shortcuts: "on" } },
  activeElement: null,
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tag) { return new El(tag); },
};

class Text { constructor(t) { this.nodeType = 3; this.textContent = t; this.parent = null; } }
class El {
  constructor(tag, props = {}) {
    this.nodeType = 1; this.tag = tag; this.childNodes = []; this.parent = null; this.dataset = {}; this.hidden = false;
    this.attrs = {}; this.className = ""; this._text = ""; Object.assign(this, props);
  }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === root; }
  get textContent() { return this.childNodes.length ? this.childNodes.map((c) => c.textContent).join("") : this._text; }
  set textContent(v) { this.childNodes = []; this._text = String(v); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  append(...xs) { xs.forEach((x) => { const n = typeof x === "string" ? new Text(x) : x; if (n.parent) detach(n); n.parent = this; this.childNodes.push(n); }); }
  before(x) { const p = this.parent; if (x.parent) detach(x); x.parent = p; p.childNodes.splice(p.childNodes.indexOf(this), 0, x); }
  remove() { if (this.parent) detach(this); }
  replaceWith(x) { const p = this.parent; if (!p) return; if (x.parent) detach(x); x.parent = p; p.childNodes[p.childNodes.indexOf(this)] = x; this.parent = null; }
  addEventListener(type, fn) { (this.on ||= {})[type] = fn; }
  focus() { doc.activeElement = this; }
  matchesOne(sel) {
    const m = /^(\w+)?(?:\.([\w-]+))?(?::last-child|:nth-of-type\((\d+)\))?$/.exec(sel);
    if (sel === "[data-decision]") return "decision" in this.dataset;
    if (sel === "[data-ticket]") return Boolean(this.dataset.ticket);
    if (sel === "form[data-delayed-send]") return this.tag === "form" && "delayedSend" in this.dataset;
    if (sel.includes("[")) return false;  // other attribute selectors (form[data-busy], input:not(...)): not on these nodes
    if (!m) throw new Error("fake DOM: unsupported selector " + sel);
    if (m[1] && m[1] !== this.tag) return false;
    if (m[2] && !this.className.split(" ").includes(m[2])) return false;
    const sibs = this.parent ? this.parent.children : [this];
    if (sel.endsWith(":last-child") && sibs[sibs.length - 1] !== this) return false;
    if (m[3] && sibs.filter((s) => s.tag === this.tag).indexOf(this) !== Number(m[3]) - 1) return false;
    return true;
  }
  matches(sel) { return sel.split(",").some((s) => this.matchesOne(s.trim())); }
  closest(sel) { for (let n = this; n && n.nodeType === 1; n = n.parent) if (n.matches(sel)) return n; return null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => n.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const detach = (n) => { n.parent.childNodes = n.parent.childNodes.filter((c) => c !== n); n.parent = null; };
const root = new El("main");

const stored = new Map();
const win = { sessionStorage: { getItem: (k) => stored.get(k) || null, setItem: (k, v) => stored.set(k, v), removeItem: (k) => stored.delete(k) },
              matchMedia: () => ({ matches: false }), addEventListener() {},
              alert() { throw new Error("browser popup: alert"); }, confirm() { throw new Error("browser popup: confirm"); } };
let timers = [];
const posts = [];
let respond = null;  // (url, body) => Promise<response>
const ctx = {
  window: win, document: doc, Date, console, URL,
  setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {},
  setInterval: () => 0, clearInterval() {},
  fetch: (url, opts) => { posts.push({ url, body: opts.body }); return respond(url, opts.body); },
  FormData: class { constructor(form) { this.m = new Map(form.fields || []); } has(k) { return this.m.has(k); } append(k, v) { this.m.set(k, v); } },
  URLSearchParams: class { constructor(d) { this.m = d.m; } get(k) { return this.m.get(k); } },
};
win.fetch = ctx.fetch; win.URLSearchParams = ctx.URLSearchParams;
vm.createContext(ctx);
const src = fs.readFileSync(process.argv[2] || require("path").join(__dirname, "../../src/orch/dashboard/static/app.js"), "utf8");
vm.runInContext(src, ctx);

const fire = (type, props) => { const ev = { type, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props };
  for (const fn of listeners[type] || []) fn(ev); return ev; };
const flush = () => new Promise((r) => setImmediate(r));
const runTimers = () => { const t = timers; timers = []; t.forEach((fn) => fn()); };

// The Today / Board card for the #45 question: options rendered B, A, C (recommended first).
const QHASH = "sha256:" + "ab".repeat(32);
const makeCard = (tid) => {
  const card = new El("article", { dataset: { decision: "", ticket: tid } });
  const question = new El("div", { className: "question" });
  const form = new El("form", { dataset: { delayedSend: "Sending your answer to " + tid }, action: "/t/" + tid + "/answer",
                                fields: [["qid", "Q1"], ["qhash", QHASH], ["next", "/"]] });
  const opts = ["B", "A", "C"].map((k) => new El("button", { name: "value", value: k, type: "submit" }));
  form.append(...opts); question.append(form); card.append(question); root.append(card);
  return { card, question, form, b: opts[0] };
};
const receipts = () => root.querySelectorAll(".receipt");
const send = async (tid, response) => {
  const c = makeCard(tid);
  respond = response;
  posts.length = 0;
  fire("submit", { target: c.form, submitter: c.b });
  assert.strictEqual(c.question.hidden, true, "the answered question is held");
  const pending = receipts().find((r) => r.className.includes("receipt-pending"));
  assert.ok(pending && /Sending your answer to .* in 5 s · Undo/.test(pending.textContent), pending && pending.textContent);
  runTimers();  // the Undo window ends
  await flush(); await flush();
  return c;
};
const ok = (tid, msg) => () => Promise.resolve({ ok: true, status: 200, url: "http://x/?msg=" + encodeURIComponent(msg) });

(async () => {
  // 1. the answer is posted once the window ends, with the key the human picked and the hash of what was shown
  let c = await send("B-2847", ok("B-2847", "answered Q1"));
  assert.strictEqual(posts.length, 1, "nothing was posted after the Undo window");
  assert.strictEqual(posts[0].url, "/t/B-2847/answer");
  assert.strictEqual(posts[0].body.get("value"), "B");
  assert.strictEqual(posts[0].body.get("qid"), "Q1");
  assert.strictEqual(posts[0].body.get("qhash"), QHASH);
  assert.ok(!c.card.isConnected, "the answered card stays");
  // the card (and the receipt in it) goes; the receipt is kept for the page the live refresh brings
  const kept = JSON.parse(stored.get("orch-receipts") || "[]").map((x) => x.text);
  assert.ok(kept.some((t) => /^Answered Q1 · B-2847 · /.test(t)), JSON.stringify(kept));
  assert.ok(!receipts().some((r) => r.className.includes("receipt-pending")), "a pending receipt stayed behind");
  root.childNodes = [];

  // 2. the server refuses (stale question, ledger, agent detected): its reason, in place, and the options come back
  c = await send("B-1", () => Promise.resolve({ ok: true, status: 200,
    url: "http://x/?err=" + encodeURIComponent("the question changed since this answer was given (Q1); nothing was applied") }));
  let r = receipts().find((x) => x.className.includes("receipt-err"));
  assert.ok(r && r.textContent.includes("Not recorded: the question changed since"), r && r.textContent);
  assert.strictEqual(r.attrs.role, "alert");
  assert.strictEqual(c.question.hidden, false, "the options did not come back");
  assert.ok(c.card.isConnected);
  root.childNodes = [];

  // 3. an HTTP error without err= (signed out, 500) still says why
  c = await send("B-2", () => Promise.resolve({ ok: false, status: 401, url: "http://x/t/B-2/answer" }));
  r = receipts().find((x) => x.className.includes("receipt-err"));
  assert.ok(r && /Not recorded: the dashboard answered 401/.test(r.textContent), r && r.textContent);
  assert.strictEqual(c.question.hidden, false);
  root.childNodes = [];

  // 4. the network fails
  c = await send("B-3", () => Promise.reject(new TypeError("Failed to fetch")));
  r = receipts().find((x) => x.className.includes("receipt-err"));
  assert.ok(r && /Not recorded: could not reach the dashboard \(Failed to fetch\)/.test(r.textContent), r && r.textContent);
  assert.strictEqual(c.question.hidden, false);
  root.childNodes = [];

  // 5. anything throwing while sending is said in place too; nothing is left at "… in 1 s · Undo"
  c = await send("B-4", () => { throw new Error("boom"); });
  r = receipts().find((x) => x.className.includes("receipt-err"));
  assert.ok(r && /Not recorded: boom/.test(r.textContent), r && r.textContent);
  assert.ok(!receipts().some((x) => x.className.includes("receipt-pending")));
  assert.strictEqual(c.question.hidden, false);

  assert.ok(!/\b(alert|confirm|prompt)\s*\(/.test(src.replace(/\/\/.*$/gm, "")), "app.js uses a browser popup");
  console.log("delayed send ok");
})().catch((e) => { console.error(e); process.exit(1); });
