// M3-2: the Shared spaces page (#1443 #1444 #1447) against a tiny DOM stub.
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { spacesView, historyView, newSpaceDialog, STATEMENT, NO_START, LATER, RESTORE_LINE, HINT_PICK, HINT_TICK } = await import("../../src/orch_hub/web/pages/spaces.js");
const NOW = 1_800_000_000_000;
const find = (n, pred) => n.all().filter(pred);
const data = { spaces: [
  { space_id: "s1", name: "Finance", epoch: 2, items: 3, revoked: 0, archived_ms: null, members: [{ workspace_id: "a", name: "Product", mode: "edits" }, { workspace_id: "b", name: "Legal", mode: "reads" }] },
  { space_id: "s2", name: "Old <b>wiki</b>", epoch: 1, items: 1, revoked: 0, archived_ms: NOW - 86400_000, members: [{ workspace_id: "a", name: "Product", mode: "edits" }] },
] };

test("the page says plainly that the hub can read, lists members with Reads/Edits, and hides archived spaces", () => {
  const calls = [];
  const view = spacesView(data, { setMode: (s, m, mode) => calls.push(["mode", s.space_id, m.workspace_id, mode]), archive: (s, a) => calls.push(["archive", s.space_id, a]) }, NOW);
  assert.match(view.textContent, /The hub and its admins can read this space\./);
  assert.equal(find(view, (n) => n.className === "banner warn" || n.attrs.class === "banner warn").length, 1); // shown once a space exists
  assert.match(view.textContent, /Finance/); assert.doesNotMatch(view.textContent, /Old/);   // archived: hidden by default
  assert.match(view.textContent, /Show archived \(1\)/);
  const selects = find(view, (n) => n.tag === "select");
  assert.deepEqual(selects.map((s) => s.value), ["edits", "reads"]);
  selects[0].value = "reads"; selects[0].listeners.change();
  assert.deepEqual(calls[0], ["mode", "s1", "a", "reads"]);
  const archive = find(view, (n) => n.tag === "button" && n.textContent === "Archive")[0];
  archive.listeners.click();                                  // the first click only asks "Sure?"
  assert.equal(calls.length, 1); assert.equal(archive.textContent, "Sure? Archive it");
  archive.listeners.click();
  assert.deepEqual(calls[1], ["archive", "s1", true]);
  find(view, (n) => n.attrs.id === "toggle-archived")[0].listeners.click();
  assert.match(view.textContent, /Old <b>wiki<\/b>/);          // text, never markup
  assert.match(view.textContent, /Nobody can add to it; it can still be read\./);
  assert.equal(find(view, (n) => n.tag === "b" && n.textContent === "wiki").length, 0);
  find(view, (n) => n.tag === "button" && n.textContent === "Bring back")[0].listeners.click();
  assert.deepEqual(calls[2], ["archive", "s2", false]);
});

test("no spaces: a plain empty state", () => {
  const text = spacesView({ spaces: [] }, {}, NOW).textContent;
  assert.match(text, /No shared spaces yet/);
  assert.match(text, /folder of documents/);
  assert.equal(find(spacesView({ spaces: [] }, {}, NOW), (n) => n.attrs.class === "banner warn").length, 0, "no warning on a page with no spaces");
  assert.doesNotMatch(text, /command line|orch-hub/i);
});

const clickAll = (n, label) => find(n, (x) => x.tag === "button" && x.textContent === label);
const WS = [{ workspace_id: "a", name: "Product", mode: "full" }, { workspace_id: "c", name: "Legal", mode: "selective" }];

test("New shared space: three steps, the statement is on the page and in the dialog, the tick is required", () => {
  const made = [];
  const view = spacesView({ ...data, workspaces: WS }, { create: (f) => made.push(f) }, NOW);
  assert.ok(view.textContent.includes(STATEMENT));
  find(view, (n) => n.attrs.id === "new-space")[0].listeners.click();
  const dlg = find(view, (n) => n.attrs.id === "space-dialog")[0];
  assert.match(dlg.textContent, /step 1 of 3/);
  assert.equal(find(dlg, (n) => n.attrs["data-type"] === "wiki")[0].attrs["aria-pressed"], "true", "Wiki is chosen when the dialog opens");
  assert.ok(dlg.textContent.includes("Most common"));
  clickAll(dlg, "Next")[0].listeners.click();                        // Next works without any click on a kind
  assert.match(dlg.textContent, /step 2 of 3/);
  clickAll(dlg, "Back")[0].listeners.click();
  find(dlg, (n) => n.attrs["data-type"] === "board")[0].listeners.click();   // choosing a kind does not move on by itself
  assert.match(dlg.textContent, /step 1 of 3/);
  assert.equal(find(dlg, (n) => n.attrs["data-type"] === "board")[0].attrs["aria-pressed"], "true");
  assert.equal(find(dlg, (n) => n.attrs["data-type"] === "wiki")[0].attrs["aria-pressed"], "false");
  clickAll(dlg, "Next")[0].listeners.click();
  assert.match(dlg.textContent, /step 2 of 3/);
  clickAll(dlg, "Back")[0].listeners.click();
  clickAll(dlg, "Next")[0].listeners.click();
  assert.equal(find(dlg, (n) => n.attrs["data-type"] === "board").length, 0);
  clickAll(dlg, "Next")[0].listeners.click();                        // no name yet: stays on step 2
  assert.match(dlg.textContent, /step 2 of 3/);
  const [name, purpose] = find(dlg, (n) => n.tag === "input");
  name.value = " Launches "; name.listeners.input(); purpose.value = "Dates"; purpose.listeners.input();
  clickAll(dlg, "Next")[0].listeners.click();
  assert.match(dlg.textContent, /step 3 of 3/);
  assert.ok(dlg.textContent.includes(STATEMENT) && dlg.textContent.includes("shares only selected items") && dlg.textContent.includes("joins now"));
  const create = clickAll(dlg, "Create")[0];
  const hint = find(dlg, (n) => n.attrs.id === "space-hint")[0];
  const [box_a] = find(dlg, (n) => n.tag === "input" && n.attrs.type === "checkbox" && n.attrs["aria-label"] === "Product");
  assert.equal(box_a.checked, true, "the workspace that joins now is already ticked");
  assert.equal(create.disabled, true);
  assert.equal(hint.textContent, HINT_TICK); assert.equal(create.attrs["aria-describedby"], "space-hint");
  create.listeners.click();                                          // not ticked: nothing is sent
  assert.equal(made.length, 0); assert.equal(create.disabled, true);
  box_a.checked = false; box_a.listeners.change();                   // untick it: the hint changes to say what is missing
  assert.equal(hint.textContent, HINT_PICK);
  box_a.checked = true; box_a.listeners.change();
  const tick = find(dlg, (n) => n.attrs.id === "space-understood")[0];
  tick.checked = true; tick.listeners.change();
  assert.equal(create.disabled, false);
  assert.equal(hint.textContent, ""); assert.equal(hint.hidden, true); assert.equal(create.attrs["aria-describedby"], undefined);
  create.listeners.click();
  assert.deepEqual(made[0], { type: "board", name: "Launches", purpose: "Dates", members: ["a"], understood: true });
  assert.equal(find(view, (n) => n.attrs.id === "space-dialog")[0].children.length, 0);   // the dialog closed
});

test("New shared space: a waiting workspace is greyed out with its reason from the start and cannot be ticked", () => {
  const made = [];
  const view = spacesView({ ...data, workspaces: WS }, { create: (f) => made.push(f) }, NOW);
  find(view, (n) => n.attrs.id === "new-space")[0].listeners.click();
  const dlg = find(view, (n) => n.attrs.id === "space-dialog")[0];
  clickAll(dlg, "Next")[0].listeners.click();
  const name = find(dlg, (n) => n.tag === "input")[0]; name.value = "X"; name.listeners.input();
  clickAll(dlg, "Next")[0].listeners.click();
  const box = (label) => find(dlg, (n) => n.tag === "input" && n.attrs["aria-label"] === label)[0];
  const create = clickAll(dlg, "Create")[0];
  const reason = find(dlg, (n) => n.attrs.id === "space-no-start")[0];
  const tick = find(dlg, (n) => n.attrs.id === "space-understood")[0]; tick.checked = true; tick.listeners.change();
  assert.equal(box("Legal").disabled, true); assert.equal(box("Product").disabled, undefined);
  assert.ok(dlg.textContent.includes(LATER));
  assert.equal(find(dlg, (n) => n.tag === "label" && n.className === "check disabled").length, 1, "the row is dimmed");
  assert.equal(reason.hidden, true, "no warning when one workspace can start");
  assert.equal(find(dlg, (n) => n.attrs.class === "banner warn" && !n.hidden).length, 0);
  assert.equal(box("Product").checked, true);                                     // preselected
  assert.equal(create.disabled, false);
  box("Product").checked = false; box("Product").listeners.change();               // nothing ticked
  assert.equal(create.disabled, true);
  create.listeners.click(); assert.equal(made.length, 0);
  box("Product").checked = true; box("Product").listeners.change();                // one that joins now
  assert.equal(create.disabled, false);
  create.listeners.click(); assert.deepEqual(made[0].members, ["a"]);
});

test("New shared space: when no workspace can start, the reason is shown at once and Create stays off", () => {
  const view = spacesView({ ...data, workspaces: [WS[1]] }, { create: () => assert.fail("nothing may be created") }, NOW);
  find(view, (n) => n.attrs.id === "new-space")[0].listeners.click();
  const dlg = find(view, (n) => n.attrs.id === "space-dialog")[0];
  clickAll(dlg, "Next")[0].listeners.click();
  const name = find(dlg, (n) => n.tag === "input")[0]; name.value = "X"; name.listeners.input();
  clickAll(dlg, "Next")[0].listeners.click();
  const reason = find(dlg, (n) => n.attrs.id === "space-no-start")[0];
  assert.equal(reason.hidden, false); assert.equal(reason.textContent, NO_START); assert.match(NO_START, /None of your workspaces can start a space yet/);
  const tick = find(dlg, (n) => n.attrs.id === "space-understood")[0]; tick.checked = true; tick.listeners.change();
  const create = clickAll(dlg, "Create")[0];
  assert.equal(create.disabled, true); assert.equal(create.attrs["aria-describedby"], "space-no-start");
  create.listeners.click();
});

test("history: the person / agent note shows only once there are entries", () => {
  const empty = historyView({ names: [], removed: [] }, NOW).textContent;
  assert.match(empty, /Nothing has been put in this space yet\./); assert.doesNotMatch(empty, /person \/ agent/); assert.ok(empty.includes(RESTORE_LINE));
  const one = historyView({ names: [{ name: "a", versions: [{ item_id: "i", created_ms: NOW, author: "W", by: "person", restored: false, content: { text: "x" } }] }], removed: [] }, NOW).textContent;
  assert.match(one, /person \/ agent/);
});

test("a member is removed only on the second click and added from the picker", () => {
  const calls = [];
  const view = spacesView({ ...data, workspaces: WS, about: { s1: { type: "wiki", purpose: "How we <b>work</b>" } } },
    { removeMember: (s, m) => calls.push(["rm", s.space_id, m.workspace_id]), addMember: (s, id) => calls.push(["add", s.space_id, id]) }, NOW);
  assert.match(view.textContent, /Wiki/); assert.match(view.textContent, /How we <b>work<\/b>/);
  assert.equal(find(view, (n) => n.tag === "b" && n.textContent === "work").length, 0);
  const rm = find(view, (n) => n.tag === "button" && n.textContent === "Remove")[0];
  rm.listeners.click(); assert.equal(calls.length, 0); assert.equal(rm.textContent, "Sure? Remove");
  rm.listeners.click(); assert.deepEqual(calls[0], ["rm", "s1", "a"]);
  const pick = find(view, (n) => n.tag === "select" && n.attrs["aria-label"] === "Add a workspace")[0];
  assert.deepEqual(pick.children.map((o) => o.attrs.value), ["", "c"]);   // only the workspace that is not in yet
  assert.match(pick.textContent, /shares only selected items; its owner accepts first/);
  clickAll(view, "Add")[0].listeners.click(); assert.equal(calls.length, 1);   // nothing picked
  pick.value = "c"; clickAll(view, "Add")[0].listeners.click();
  assert.deepEqual(calls[1], ["add", "s1", "c"]);
});

test("history: newest first as sent, person/agent badge from a fixed table, restored, removed, file size, no restore button", () => {
  const v = (extra) => ({ item_id: "i", created_ms: NOW - 1000, author: "Product", by: "person", restored: false, revoked: false, content: { text: "hello" }, ...extra });
  const view = historyView({ names: [{ name: "plan.md", versions: [v({ by: "agent" }), v({ by: "root:x", restored: true }), v({ content: { file: 5000 } })] }], removed: [{ item_id: "r", author: "Legal", revoked: true }] }, NOW);
  const chips = find(view, (n) => n.tag === "span" && /chip by-/.test(n.className)).map((n) => n.textContent);
  assert.deepEqual(chips, ["agent", "unknown", "person"]);
  assert.equal(find(view, (n) => n.tag === "span" && /chip by-/.test(n.className))[0].attrs.title, "As the workspace says");
  assert.match(view.textContent, /restored/); assert.match(view.textContent, /file \(5 KB\)/);
  assert.match(view.textContent, /removed/); assert.match(view.textContent, /put by Legal/);
  assert.ok(view.textContent.includes(RESTORE_LINE));
  assert.equal(find(view, (n) => n.tag === "button").length, 0);        // no Restore button, no button at all
  assert.equal(find(view, (n) => n.tag === "a").length, 0);
});

test("history prints hostile item text inert: text only, no element, no link", () => {
  const evil = "<img src=x onerror=alert(1)><a href='javascript:alert(2)'>x</a> https://evil.example \u202e";
  const view = historyView({ names: [{ name: "<b>n</b>", versions: [{ item_id: "i", created_ms: NOW, author: "<i>W</i>", by: "person", restored: false, content: { text: evil, cut: true } }] }], removed: [] }, NOW);
  assert.ok(view.textContent.includes("<img src=x onerror=alert(1)>"));
  assert.ok(view.textContent.includes("<b>n</b>") && view.textContent.includes("(cut here)"));
  assert.ok(view.textContent.includes("<U+202E>"));
  for (const tag of ["img", "a", "b", "i", "script"]) assert.equal(find(view, (n) => n.tag === tag).length, 0, tag);
});

test("the History button loads the history once opened and hides it again", async () => {
  const view = spacesView(data, { history: async (s) => ({ names: [{ name: "n.md", versions: [{ item_id: "i", created_ms: NOW, by: "agent", content: { text: "hi" } }] }], removed: [] }) }, NOW);
  const btn = find(view, (n) => n.attrs.class === undefined && n.className === "space-history-btn")[0];
  await btn.listeners.click();
  await new Promise((r) => setTimeout(r, 0));
  assert.match(view.textContent, /n\.md/); assert.equal(btn.textContent, "Hide history");
  await btn.listeners.click();
  assert.doesNotMatch(view.textContent, /n\.md/);
});

test("no innerHTML (or any HTML sink) anywhere in the page, and item data only reaches textContent", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../src/orch_hub/web/pages/spaces.js", import.meta.url), "utf8");
  for (const bad of [/innerHTML/, /outerHTML/, /insertAdjacentHTML/, /\bhref\b/, /\.src\b/, /setAttribute\(\s*["']on/, /document\.write/]) assert.doesNotMatch(src, bad);
  assert.doesNotMatch(src, /h\("a"/);                                  // never a link from item data
});

test("say() gives plain words for the space codes and never echoes a raw code", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  const words = src.slice(src.indexOf("const WORDS = {"), src.indexOf("const say ="));
  const say = new Function(`${words}; const renderText = String; return (code) => WORDS[code] ?? ${src.match(/const say = \(code\) => WORDS\[code\] \?\? (.*);/)[1]};`)();
  for (const code of ["wrong_mode", "unknown_workspace", "exists", "not_accepted", "some_new_code"]) {
    const t = say(code);
    assert.doesNotMatch(t, /[(_]/, code); assert.ok(t.length > 10);
  }
});
