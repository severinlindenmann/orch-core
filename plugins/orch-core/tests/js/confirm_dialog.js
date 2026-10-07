// Unit test for static/confirm.js (run by tests/test_confirm_dialog_js.py; plain node, a small fake DOM, no packages):
// the first submit is stopped and opens the one dialog; Cancel, Esc and the backdrop send nothing; the primary fills
// the typed words (and a required reason) only after the person confirms and sends the form again with
// requestSubmit; the Dark start's checklist follows the chosen options.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

// ---- a fake DOM: just enough selectors for confirm.js --------------------------------------------------------------
const simple = (el, sel) => {
  const m = sel.match(/^([a-z0-9]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/i);
  if (!m) return false;
  if (m[1] && el.tag !== m[1]) return false;
  for (const c of (m[2] || "").split(".").filter(Boolean)) if (!el.className.split(/\s+/).includes(c)) return false;
  for (const a of (m[3] || "").match(/\[[^\]]+\]/g) || []) {
    const [, k, v] = a.match(/^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
    const have = k.startsWith("data-") ? el.dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] : el.attr(k);
    if (have === undefined || have === null || have === false) return false;
    if (v !== undefined && String(have) !== v) return false;
  }
  return true;
};
const matches = (el, sel) => sel.split(",").some((one) => {
  const parts = one.trim().split(/\s+/);
  if (!simple(el, parts.pop())) return false;
  let n = el.parent;
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    while (n && !simple(n, parts[i])) n = n.parent;
    if (!n) return false;
    n = n.parent;
  }
  return true;
});
let active = null;
class El {
  constructor(tag) { Object.assign(this, { tag, children: [], parent: null, dataset: {}, className: "", textContent: "",
    attrs: {}, listeners: {}, value: "", required: false, disabled: false }); }
  attr(k) { return k in this ? this[k] : this.attrs[k]; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  append(...xs) { xs.forEach((x) => { if (typeof x === "string") { const t = new El("#text"); t.textContent = x; x = t; }
    x.parent = this; this.children.push(x); }); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === body; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelector(sel) { return this.all().find((c) => matches(c, sel)) || null; }
  querySelectorAll(sel) { return this.all().filter((c) => matches(c, sel)); }
  matches(sel) { return matches(this, sel); }
  closest(sel) { for (let n = this; n; n = n.parent) if (n.matches && n.matches(sel)) return n; return null; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  dispatch(t, ev = {}) { const e = { type: t, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...ev };
    (this.listeners[t] || []).forEach((fn) => fn(e)); return e; }
  focus() { active = this; }
  click() { this.dispatch("click"); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  get text() { return this.tag === "#text" ? this.textContent : this.textContent + this.children.map((c) => c.text).join(""); }
}
const body = new El("body");
const docListeners = {};
const doc = {
  documentElement: { classList: { s: new Set(), add(c) { this.s.add(c); } } },
  body,
  get activeElement() { return active; },
  createElement: (t) => new El(t),
  createElementNS: (ns, t) => new El(t),
  addEventListener(t, fn, capture) { (docListeners[t] ||= []).push(fn); },
  querySelectorAll: (sel) => body.querySelectorAll(sel),
};
const win = { HTMLDialogElement: function () {}, addEventListener() {} };
const ctx = { window: win, document: doc, console, JSON };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
assert.ok(doc.documentElement.classList.s.has("confirm-js"), "html.confirm-js hides the no-JS fallbacks");
const flush = () => new Promise((r) => setImmediate(r));

// A form as the templates render it; elements by name.
const formOf = (data, fields) => {
  const form = new El("form");
  Object.assign(form.dataset, data);
  form.elements = {};
  for (const f of fields) {
    const el = new El(f.tag || "input");
    Object.assign(el, f);
    if (f.word) el.dataset.confirmWord = f.word;
    let parent = form;
    if (f.nojs) { parent = new El("div"); parent.className = "nojs-only"; form.append(parent); }
    parent.append(el);
    if (f.name) form.elements[f.name] = el;
  }
  form.sent = [];
  form.requestSubmit = (submitter) => { form.sent.push(submit(form, submitter)); };
  body.append(form);
  return form;
};
// what a browser does for a submit: the document's listeners (capture first, as confirm.js registers them), then the
// form posts unless one prevented it; `reached` says whether a later listener (another interceptor) saw it.
let reached = 0;
const submit = (form, submitter) => {
  let stopped = false;
  const ev = { type: "submit", target: form, submitter, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; },
               stopImmediatePropagation() { stopped = true; } };
  for (const fn of docListeners.submit || []) { fn(ev); if (stopped) break; }
  if (!stopped) reached += 1;
  return { posted: !ev.defaultPrevented, words: Object.fromEntries(Object.entries(form.elements).map(([k, v]) => [k, v.value])) };
};
const dialog = () => body.querySelector("dialog.confirm-dialog");
const button = (label) => dialog().querySelectorAll("button").find((b) => b.textContent === label);

(async () => {
  // 1. a declarative confirm: stopped before any other listener, the dialog names the action, Cancel has focus
  const stop = formOf({ confirmTitle: "Stop the run?", confirmBody: "Sessions end.", confirmOk: "Stop the run",
                        confirmItems: JSON.stringify([{ icon: "pause", text: "No new children are handed out" }]),
                        confirmStays: "You can start a new run afterwards." }, [{ name: "next", value: "/factory/X-1" }]);
  let r = submit(stop);
  assert.strictEqual(r.posted, false); assert.strictEqual(reached, 0, "the first submit reached another listener");
  const d = dialog();
  assert.ok(d && d.open, "the dialog opens modal");
  assert.ok(d.text.includes("Stop the run?") && d.text.includes("No new children are handed out") && d.text.includes("What stays yours"));
  assert.ok(button("Stop the run"), "the primary says the specific verb");
  assert.strictEqual(active, button("Cancel"), "Cancel has focus first");
  button("Cancel").click(); await flush();
  assert.strictEqual(dialog(), null); assert.deepStrictEqual(stop.sent, [], "Cancel sent the form");
  // Esc and the backdrop cancel too
  submit(stop); dialog().dispatch("cancel"); await flush();
  assert.deepStrictEqual(stop.sent, []);
  submit(stop); const dd = dialog(); dd.dispatch("click", { target: dd }); await flush();
  assert.deepStrictEqual(stop.sent, []);
  // a click inside the dialog's content is not the backdrop
  submit(stop); dialog().dispatch("click", { target: dialog().children[0] }); assert.ok(dialog()); button("Stop the run").click(); await flush();
  assert.strictEqual(stop.sent.length, 1); assert.strictEqual(stop.sent[0].posted, true, "the confirmed submit goes out");
  assert.strictEqual(reached, 1, "the confirmed submit reaches the next listener (an interceptor)");
  assert.strictEqual(stop.dataset.confirmed, undefined, "the pass is used once");
  // ... and the next press asks again
  assert.strictEqual(submit(stop).posted, false); button("Cancel").click(); await flush();

  // 2. the Dark start: the typed words are filled only after the primary, never before, never on Cancel
  const dark = formOf({ confirmBuild: "start", newForm: "", factoryConfirm: "up to 25 children or 72 hours, children of size m or smaller" }, [
    { name: "title", value: "Nightly backup" }, { name: "mode", value: "dark" }, { name: "release", value: "dev" },
    { name: "rollback", type: "checkbox", checked: false }, { name: "close", type: "checkbox", checked: false },
    { name: "confirm_dark", word: "dark", nojs: true }, { name: "confirm_production", word: "production", nojs: true }]);
  dark.querySelector = (sel) => (sel === "[data-release-choice]" ? {} : El.prototype.querySelector.call(dark, sel));
  submit(dark);
  assert.strictEqual(dark.elements.confirm_dark.value, "", "the word was filled before the person confirmed");
  let text = dialog().text;
  assert.ok(text.includes("Start this Dark run?") && text.includes("“Nightly backup”"), text);
  assert.ok(text.includes("Up to 25 children or 72 hours") && text.includes("deploys dev") && text.includes("You give the verdict"), text);
  assert.ok(!dialog().className.includes("is-care"), "a plain Dark start is the standard dialog");
  button("Cancel").click(); await flush();
  assert.strictEqual(dark.elements.confirm_dark.value, "");
  submit(dark); button("Start Dark run").click(); await flush();
  assert.deepStrictEqual([dark.sent[0].words.confirm_dark, dark.sent[0].posted], ["dark", true]);
  // production, rollback and the close: the care dialog, the amber rows and the specific primary
  dark.elements.release.value = "prod"; dark.elements.rollback.checked = true; dark.elements.close.checked = true;
  submit(dark);
  assert.ok(dialog().className.includes("is-care"));
  const care = dialog().querySelectorAll("li.is-care").map((li) => li.text);
  assert.strictEqual(care.length, 3, care.join(" | "));
  assert.ok(care[0].includes("production") && care[1].includes("rollback") && care[2].includes("in place of your verdict"));
  assert.strictEqual(active, button("Cancel"), "Cancel stays the default even in the care dialog");
  assert.ok(button("Start and release to production"));
  button("Start and release to production").click(); await flush();
  assert.deepStrictEqual([dark.sent[1].words.confirm_dark, dark.sent[1].words.confirm_production], ["dark", "production"]);
  // Ticket mode posts at once, nothing asked
  dark.elements.mode.value = "ticket";
  assert.strictEqual(submit(dark).posted, true); assert.strictEqual(dialog(), null);

  // 3. a required reason asked in the dialog: it has focus, the primary waits for it, it lands in the form's field
  const skip = formOf({ confirmTitle: "Close without releasing?", confirmOk: "Close without releasing", confirmTone: "care",
                        confirmField: "skip_release", confirmFieldLabel: "Why close it without releasing" },
                      [{ name: "skip_release", nojs: true, required: true, maxLength: 300 }]);
  const sb = new El("button"); sb.form = skip; skip.append(sb);
  for (const fn of docListeners.click || []) fn({ target: sb });
  assert.strictEqual(skip.elements.skip_release.required, false, "the hidden no-JS field still blocks validation");
  submit(skip);
  const area = dialog().querySelector("textarea");
  assert.strictEqual(active, area, "the reason field has focus");
  assert.strictEqual(area.maxLength, 300);
  assert.strictEqual(button("Close without releasing").disabled, true);
  button("Close without releasing").click(); await flush();
  assert.ok(dialog(), "an empty reason confirmed");
  area.value = "  checked by hand  "; area.dispatch("input");
  button("Close without releasing").click(); await flush();
  assert.strictEqual(skip.sent[0].words.skip_release, "checked by hand");

  // 4. a dialog-tier form: its ask=1 (the server's no-JS confirm page) is cleared only when confirmed
  const rel = formOf({ confirmTitle: "Release the claim?", confirmOk: "Release claim", confirmCancel: "Keep claim" }, [{ name: "ask", value: "1" }]);
  const relButton = new El("button"); relButton.form = rel; rel.append(relButton);
  active = null;
  submit(rel, relButton); button("Keep claim").click(); await flush();
  assert.strictEqual(active, relButton, "focus returns to the button that opened it (Safari never focused it)");
  assert.strictEqual(rel.elements.ask.value, "1", "Cancel keeps the server's confirm page for a no-JS retry");
  submit(rel); button("Release claim").click(); await flush();
  assert.strictEqual(rel.sent[0].words.ask, "");

  // 5. the epic's approve form: delegation or a factory start, from its fields
  const { startConfig } = win.orchConfirm;
  const charter = { dataset: { confirmBuild: "start", confirmEpic: "epic ab12cd34 with 2 children", factoryConfirm: "up to 9 children" },
                    elements: { start: { value: "" }, delegate: { checked: true }, max_children: { value: "4" }, max_size: { value: "s" } },
                    querySelector: () => null };
  let cfg = startConfig(charter);
  assert.strictEqual(cfg.ok, "Approve epic");
  assert.ok(cfg.body.includes("epic ab12cd34 with 2 children") && cfg.items[0].text.includes("up to 4 children") && cfg.items[0].text.includes("size s"));
  charter.elements.delegate.checked = false;
  assert.ok(startConfig(charter).items[0].text.startsWith("No delegation"));
  charter.dataset.confirmReapprove = "";
  assert.strictEqual(startConfig(charter).ok, "Re-approve epic");
  charter.elements.start.value = "factory";
  cfg = startConfig(charter);
  assert.strictEqual(cfg.ok, "Start AI Factory"); assert.ok(cfg.items.some((i) => i.text === "Up to 9 children"));
  // a release posted without the choice on the form counts as nothing; a disabled box never counts
  charter.elements.start.value = "dark"; charter.elements.release = { value: "prod" }; charter.elements.close = { checked: true, disabled: true };
  cfg = startConfig(charter);
  assert.strictEqual(cfg.ok, "Start Dark run"); assert.ok(cfg.items.some((i) => i.text.startsWith("Nothing is released")));
  assert.strictEqual(cfg.tone, "");
  // every primary label stays short
  for (const ok of ["Start Dark run", "Start AI Factory", "Approve epic", "Re-approve epic", "Start and release to production"]) assert.ok(ok.split(" ").length <= 5);
  console.log("confirm dialog ok");
})().catch((e) => { console.error(e); process.exit(1); });
