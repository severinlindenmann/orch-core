// M2-10 (#1427 #1428 #1429): the Phones & browsers page against a tiny DOM stub.
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; this.disabled = false; this.value = "";
    const set = new Set(); this.classList = { add: (c) => set.add(c), contains: (c) => set.has(c), remove: (c) => set.delete(c) }; }
  getBoundingClientRect() { return { bottom: 1, height: 0 }; }
  remove() { this.removedNow = true; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  replaceChildren(...kids) { this.children = []; this._text = ""; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
  querySelector(sel) { const m = /^\[(\w+)=(\w+)\]$/.exec(sel); return this.all().find((n) => m && n.attrs[m[1]] === m[2]) ?? null; }
}
globalThis.document = { createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { phonesView, newChannelsNotice, pairCard, remainingText, REMOVE_WARNING, PAIR_STEPS } = await import("../../src/orch_hub/web/phones.js");
const { homeView } = await import("../../src/orch_hub/web/home.js");
const { parseRoute } = await import("../../src/orch_hub/web/route.js");

const NOW = new Date(2026, 9, 27, 12, 0).getTime();
const ANN = "a1".repeat(16), BOB = "b2".repeat(16), ADM = "ad".repeat(16);
const row = (id, user, over = {}) => ({ channel_id: id.repeat(32).slice(0, 32), user_id: user, kind: "phone", label: "Phone " + id, factor: "se-unlocked", created_ms: NOW - 3600000, status: "active", app_version: null, update_needed: false, ...over });
const view = (data, actions = {}) => phonesView({ user_id: ANN, admin: false, channels: [], users: [], ...data }, actions, NOW);
const text = (nodes) => nodes.flat().map((n) => n.textContent).join("\n");
const find = (nodes, pred) => nodes.flatMap((n) => n.all()).filter(pred);
const button = (nodes, label) => find(nodes, (n) => n.tag === "button" && n.textContent === label);
const input = (nodes, id) => find(nodes, (n) => n.attrs.id === id)[0];
const sections = (nodes) => find(nodes, (n) => n.attrs["data-person"] !== undefined);

test("the page is routable", () => {
  assert.deepEqual(parseRoute("#/phones"), { kind: "phones" });
});

test("an admin sees every person's channels grouped by person, themself first", () => {
  const users = [{ user_id: BOB, name: "Bob" }, { user_id: ADM, name: "Admin" }, { user_id: ANN, name: "Ann" }];
  const channels = [row("b", BOB), row("c", ANN, { kind: "browser", label: "Ann's laptop" }), row("d", ADM), row("e", BOB, { label: "Bob's second" })];
  const out = view({ admin: true, user_id: ADM, users, channels });
  const groups = sections(out);
  assert.deepEqual(groups.map((g) => g.attrs["data-person"]), [ADM, ANN, BOB]);
  assert.match(groups[0].all().find((n) => n.tag === "h2").textContent, /^Admin \(you\)$/);
  assert.deepEqual(find([groups[2]], (n) => n.attrs["data-channel"]).map((n) => n.attrs["data-channel"].slice(0, 1)), ["b", "e"]);
  assert.equal(find([groups[1]], (n) => n.attrs["data-channel"]).length, 1);
  assert.match(text(out), /Every phone and browser that can approve for someone on this hub/);
  assert.equal(button(out, "Remove").length, 4);
});

test("a person with nothing registered still has a group, and a stray owner is not hidden", () => {
  const out = view({ admin: true, user_id: ADM, users: [{ user_id: ADM, name: "Admin" }, { user_id: BOB, name: "Bob" }], channels: [row("f", "ff".repeat(16))] });
  assert.match(text(sections(out).filter((g) => g.attrs["data-person"] === BOB)), /No phones or browsers yet/);
  assert.match(text(out), /Someone no longer on this hub/);
});

test("a person sees only their own, on the same page, with no names of others", () => {
  const out = view({ channels: [row("a", ANN), row("c", ANN, { kind: "browser", label: "This laptop" })], users: [{ user_id: BOB, name: "Bob" }] });
  assert.equal(sections(out).length, 1);
  const t = text(out);
  assert.match(t, /Your phones and browsers/);
  assert.match(t, /Phone a/);
  assert.match(t, /This laptop/);
  assert.doesNotMatch(t, /Bob/);
  assert.match(t, /Phones & browsers/);
});

test("Remove asks first, in plain words, and then removes exactly that channel", async () => {
  const removed = [];
  const c = row("a", ANN);
  const out = view({ channels: [c] }, { remove: async (ch) => removed.push(ch.channel_id) });
  button(out, "Remove")[0].listeners.click();
  assert.match(text(out), new RegExp(REMOVE_WARNING.replace(/[.]/g, "\\.")));
  assert.equal(REMOVE_WARNING, "It can no longer approve. This cannot be undone.");
  assert.deepEqual(removed, []); // nothing yet
  const asking = find(out, (n) => n.attrs["data-channel"] === c.channel_id)[0];
  await button(out, "Remove").at(-1).listeners.click();
  assert.deepEqual(removed, [c.channel_id]);
  // done on the server: the row slides out, and there is no Undo (a removed phone cannot be un-removed)
  assert.equal(asking.classList.contains("row-done"), true);
  assert.doesNotMatch(text(out), /Undo|Could not remove/);
});

test("Keep it goes back without removing; a failed removal says so and can be retried", async () => {
  let calls = 0;
  const out = view({ channels: [row("a", ANN)] }, { remove: async () => { calls++; throw new Error("no"); } });
  button(out, "Remove")[0].listeners.click();
  button(out, "Keep it")[0].listeners.click();
  assert.equal(calls, 0);
  assert.equal(button(out, "Remove").length, 1);
  assert.doesNotMatch(text(out), /cannot be undone/);
  button(out, "Remove")[0].listeners.click();
  const yes = button(out, "Remove").at(-1);
  await yes.listeners.click();
  assert.match(text(out), /Could not remove it/);
  assert.equal(yes.disabled, false);
});

test("a removed or expired channel has no Remove button", () => {
  const out = view({ channels: [row("a", ANN, { status: "revoked" }), row("b", ANN, { status: "expired" }), row("c", ANN)] });
  assert.equal(button(out, "Remove").length, 1);
  assert.match(text(out), /Removed/);
  assert.match(text(out), /Expired/);
  const order = find(out, (n) => n.attrs["data-channel"]).map((n) => n.attrs["data-status"]);
  assert.deepEqual(order, ["active", "revoked", "expired"]); // what can still approve comes first
});

test("Update needed shows an amber chip only for a phone the hub flags, and shows nothing when no version was reported", () => {
  const out = view({ channels: [row("a", ANN, { app_version: "0.0.9", update_needed: true }), row("b", ANN, { app_version: "0.2.0" }), row("c", ANN)] });
  const chips = find(out, (n) => n.className.includes("chip") && n.textContent === "Update needed");
  assert.equal(chips.length, 1);
  assert.match(chips[0].className, /warn/);
  const rows = find(out, (n) => n.attrs["data-channel"]);
  assert.match(rows[0].textContent, /App version 0\.0\.9/);
  assert.match(rows[0].textContent, /Update needed/);
  assert.doesNotMatch(rows[1].textContent, /Update needed/);
  assert.match(rows[1].textContent, /App version 0\.2\.0/);
  assert.doesNotMatch(rows[2].textContent, /Update needed|App version/); // none reported: nothing at all
});

test("names and versions from a phone are escaped, never markup", () => {
  const out = view({ channels: [row("a", ANN, { label: "x\u202e<img src=x onerror=1>", app_version: "1.0\u202e" })] });
  const t = text(out);
  assert.doesNotMatch(t, /\u202e/);
  assert.match(t, /<U\+202E>/);
  assert.equal(find(out, (n) => n.tag === "img").length, 0);
});

test("the new-phone notice says who, when and what to do, and dismiss clears it", async () => {
  const log = [];
  const nc = [{ channel_id: "ab".repeat(16), label: "Ann's iPhone", kind: "phone", created_ms: NOW - 60000, seen: false }];
  const out = view({ newChannels: nc }, { remove: async (c) => log.push(["remove", c.channel_id]), dismiss: async () => log.push(["dismiss"]) });
  const banner = find(out, (n) => n.className.includes("new-channel"));
  assert.equal(banner.length, 1);
  assert.match(banner[0].textContent, /A new phone was added: Ann's iPhone at Today 11:59\. Not you\? Remove it\./);
  await button(out, "Remove it")[0].listeners.click();
  assert.equal(banner[0].classList.contains("row-done"), true);
  button(out, "That was me")[0].listeners.click();
  assert.deepEqual(log, [["remove", "ab".repeat(16)], ["dismiss"]]);
  assert.deepEqual(newChannelsNotice([], {}, NOW), []);
  assert.deepEqual(newChannelsNotice([{ ...nc[0], seen: true }], {}, NOW), []);
});

test("the same notice is on Home (the page where the person lands)", () => {
  const nc = [{ channel_id: "ab".repeat(16), label: "Ann's iPhone", kind: "phone", created_ms: NOW - 60000, seen: false }];
  const calls = [];
  const out = homeView({ user: { name: "Ann" }, workspaces: [], items: [], channels: [], newChannels: nc, thisBrowser: { state: "unregistered" },
    actions: { removeChannel: (id) => calls.push(["remove", id]), dismissNew: () => calls.push(["dismiss"]) } });
  assert.match(text(out), /A new phone was added: Ann's iPhone/);
  button(out, "Remove it")[0].listeners.click();
  button(out, "That was me")[0].listeners.click();
  assert.deepEqual(calls, [["remove", "ab".repeat(16)], ["dismiss"]]);
  assert.doesNotMatch(text(homeView({ user: { name: "Ann" }, workspaces: [], items: [], channels: [], thisBrowser: { state: "unregistered" }, actions: {} })), /A new phone/);
});

test("remaining time is plain, counts down and ends in an expired sentence", () => {
  assert.equal(remainingText(NOW + 299000, NOW), "Time left: 4 min 59 s");
  assert.equal(remainingText(NOW + 61000, NOW), "Time left: 1 min 01 s");
  assert.equal(remainingText(NOW + 5000, NOW), "Time left: 0 min 05 s");
  assert.match(remainingText(NOW, NOW), /expired/);
  assert.match(remainingText(NOW - 1, NOW), /expired/);
  assert.match(remainingText(NaN, NOW), /expired/);
});

function stubbed(pair, extra = {}) {
  let clock = NOW;
  const ticks = [], copied = [];
  const actions = { pair, copy: async (t) => { copied.push(t); return true; }, now: () => clock,
    tick: (fn) => { const t = { fn, stopped: false }; ticks.push(t); return () => { t.stopped = true; }; }, ...extra };
  return { actions, ticks, copied, advance: (ms) => { clock += ms; } };
}

test("Pair a phone: password and code, then the code, the time left, the steps, the address and the link", async () => {
  const sent = [];
  const s = stubbed(async (creds) => { sent.push(creds); return { ok: true, code: "ab12-cd34-ef56", expires_ms: NOW + 300000, address: "https://hub.example", link: "orch://hub-pair/hub.example#h1.x" }; });
  const out = view({}, s.actions);
  assert.match(text(out), /Pair a phone/);
  assert.equal(input(out, "pair-pw").attrs.type, "password");
  assert.equal(input(out, "pair-totp").attrs.maxlength, "6");
  assert.equal(find(out, (n) => n.attrs.id === "pair-code-text").length, 0); // no code before the person asks
  input(out, "pair-pw").value = "correct horse";
  input(out, "pair-totp").value = " 123456 ";
  const form = find(out, (n) => n.tag === "form")[0];
  await form.listeners.submit({ preventDefault() {} });
  assert.deepEqual(sent, [{ password: "correct horse", totp: "123456" }]);
  assert.equal(input(out, "pair-pw").value, "");
  assert.equal(input(out, "pair-totp").value, ""); // the secrets do not stay in the page
  assert.equal(input(out, "pair-code-text").textContent, "ab12-cd34-ef56");
  const t = text(out);
  assert.ok(t.includes(PAIR_STEPS));
  assert.equal(PAIR_STEPS, "Open the orch app, choose Add hub, enter the address and this code.");
  assert.match(t, /Address: https:\/\/hub\.example/);
  assert.match(t, /Time left: 5 min 00 s/);
  assert.match(t, /orch:\/\/hub-pair\/hub\.example#h1\.x/);
  assert.match(t, /A QR code is not shown here yet/);
  assert.doesNotMatch(t, /terminal|command line|VPN/i);
  const copy = button(out, "Copy")[0];
  await copy.listeners.click();
  await button(out, "Copy link")[0].listeners.click();
  assert.deepEqual(s.copied, ["ab12-cd34-ef56", "orch://hub-pair/hub.example#h1.x"]);
  assert.equal(copy.textContent, "Copied");
});

test("the countdown ticks and stops by itself when the code ends", async () => {
  const s = stubbed(async () => ({ ok: true, code: "ab12-cd34-ef56", expires_ms: NOW + 3000 }));
  const out = view({}, s.actions);
  await find(out, (n) => n.tag === "form")[0].listeners.submit({ preventDefault() {} });
  const left = find(out, (n) => n.className === "pair-left")[0];
  assert.equal(s.ticks.length, 1);
  s.advance(1000);
  s.ticks[0].fn();
  assert.equal(left.textContent, "Time left: 0 min 02 s");
  assert.equal(s.ticks[0].stopped, false);
  s.advance(2000);
  s.ticks[0].fn();
  assert.match(left.textContent, /expired/);
  assert.equal(s.ticks[0].stopped, true);
});

test("a new code replaces the old one and stops the old countdown", async () => {
  let n = 0;
  const s = stubbed(async () => ({ ok: true, code: "aaaa-bbbb-" + String(++n).padStart(4, "0"), expires_ms: NOW + 300000 }));
  const out = view({}, s.actions);
  const form = find(out, (x) => x.tag === "form")[0];
  await form.listeners.submit({ preventDefault() {} });
  await form.listeners.submit({ preventDefault() {} });
  assert.equal(find(out, (x) => x.attrs.id === "pair-code-text").length, 1);
  assert.equal(input(out, "pair-code-text").textContent, "aaaa-bbbb-0002");
  assert.deepEqual(s.ticks.map((t) => t.stopped), [true, false]);
});

test("a refusal shows plain words, no code, and clears the fields", async () => {
  const s = stubbed(async () => ({ ok: false, error: "Wrong user id, password or code. A code works once; wait for the next one." }));
  const out = view({}, s.actions);
  input(out, "pair-pw").value = "nope";
  input(out, "pair-totp").value = "000000";
  await find(out, (n) => n.tag === "form")[0].listeners.submit({ preventDefault() {} });
  assert.match(text(out), /No code was made/);
  assert.match(text(out), /wait for the next one/);
  assert.equal(find(out, (n) => n.attrs.id === "pair-code-text").length, 0);
  assert.equal(input(out, "pair-pw").value, "");
  assert.equal(s.ticks.length, 0);
  const t = stubbed(async () => { throw new Error("boom"); });
  const out2 = view({}, t.actions);
  await find(out2, (n) => n.tag === "form")[0].listeners.submit({ preventDefault() {} });
  assert.match(text(out2), /The hub cannot be reached/);
});

test("with no link from the hub only the code and the address steps are shown", async () => {
  const s = stubbed(async () => ({ ok: true, code: "ab12-cd34-ef56", expires_ms: NOW + 300000 }));
  const out = pairCard(s.actions, NOW);
  await find([out], (n) => n.tag === "form")[0].listeners.submit({ preventDefault() {} });
  const t = text([out]);
  assert.doesNotMatch(t, /pairing link|Address:/);
  assert.match(t, /Open the orch app, choose Add hub/);
  assert.equal(button([out], "Copy link").length, 0);
});
