// Run-11 #12/#13: the Services and Workspaces pages, against a tiny DOM stub.
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { servicesView, summary, stateOf } = await import("../../src/orch_hub/web/services.js");
const { workspacesView, workspaceDetailsView, connectWorkspaceView, statusOf } = await import("../../src/orch_hub/web/workspaces.js");
const { when, ago } = await import("../../src/orch_hub/web/ui.js");

const NOW = new Date(2026, 9, 27, 12, 0).getTime();
const base = () => ({
  hub: { state: "up", version: "0.1.0", started_ms: NOW - 3 * 86400000 },
  relay: { name: "relay", state: "up", version: "0.1.0", url: "http://relay.test", checked_ms: NOW - 2000, last_ok_ms: NOW - 2000 },
  publish: { name: "publish", state: "up", version: null, url: "http://pub.test", checked_ms: NOW, last_ok_ms: NOW },
  workspaces: { count: 3, online: 2 },
});
const text = (nodes) => nodes.flat().map((n) => n.textContent).join("\n");
const find = (nodes, pred) => nodes.flatMap((n) => n.all()).filter(pred);

test("everything up: green banner, no restart button, a command with Copy per service", () => {
  const out = servicesView(base(), { check() {}, open() {}, copy: async () => true }, NOW);
  const t = text(out);
  assert.match(t, /All services are working/);
  assert.match(t, /Version 0\.1\.0/);
  assert.match(t, /Version -/); // publish reports none
  assert.doesNotMatch(t, /Restart hub/);
  assert.deepEqual(find(out, (n) => n.tag === "code").map((n) => n.textContent), ["orch install hub", "orch install relay", "orch install publish"]);
  assert.match(t, /3 workspaces, 2 online/);
});

test("a service that does not answer turns the banner amber and says when it last answered", () => {
  const d = base();
  d.publish = { name: "publish", state: "down", url: "http://pub.test", version: null, last_ok_ms: NOW - 14 * 60000 };
  assert.deepEqual(summary(d), { kind: "warn", text: "Publish is not answering" });
  assert.deepEqual(stateOf({ ...d.publish }), { label: "Not answering", kind: "warn", dot: "warn" });
  const t = text(servicesView(d, { check() {}, open() {}, copy: async () => true }, NOW));
  assert.match(t, /Last answered 14 min ago/);
});

test("not set up is not an error", () => {
  const d = base();
  d.relay = { name: "relay", state: "not_set_up", url: null, version: null, last_ok_ms: null };
  assert.equal(summary(d).kind, "");
  assert.match(summary(d).text, /Relay is not set up/);
});

test("Check now re-probes, Copy copies the exact command", async () => {
  let checked = 0, copied = null;
  const out = servicesView(base(), { check: () => checked++, open() {}, copy: async (c) => { copied = c; return true; } }, NOW);
  find(out, (n) => n.tag === "button" && n.textContent === "Check now")[0].listeners.click();
  assert.equal(checked, 1);
  const btn = find(out, (n) => n.tag === "button" && n.textContent === "Copy")[1];
  await btn.listeners.click();
  assert.equal(copied, "orch install relay");
  assert.equal(btn.textContent, "Copied");
});

test("hand-offs: names, sent time, Delivered / Waiting; a stranger is 'Another workspace'", () => {
  const A = { workspace_id: "a".repeat(32), name: "Product", online: true };
  const L = { workspace_id: "b".repeat(32), name: "Legal", online: false };
  const X = { workspace_id: null, name: null, online: false };
  const handoffs = [
    { id: "1", from: A, to: L, sent_ms: NOW - 86400000, status: "waiting" },
    { id: "2", from: A, to: X, sent_ms: NOW - 3600000, status: "delivered" },
  ];
  const out = workspacesView({ handoffs, workspaces: [{ ...A, mode: "full" }, { ...L, mode: "full" }] }, NOW);
  const t = text(out);
  assert.match(t, /Product→Legal/);
  assert.match(t, /Waiting for Legal to come online/);
  assert.match(t, /since Yesterday 12:00/);
  assert.match(t, /Product→Another workspace/);
  assert.match(t, /Delivered/);
  assert.match(t, /1 hand-off is waiting for this workspace/);
  assert.equal(statusOf({ ...handoffs[0], to: { ...L, online: true } }, NOW).label, "Waiting for Legal to take it");
});

test("empty states and times", () => {
  const t = text(workspacesView({ handoffs: [], workspaces: [] }, NOW));
  assert.match(t, /No workspaces yet/);
  assert.match(t, /No tickets have been sent between workspaces yet/);
  assert.equal(when(NOW - 60000, NOW), "Today 11:59");
  assert.equal(ago(NOW - 120000, NOW), "2 min ago");
});

const workspace = (extra = {}) => ({ workspace_id: "a".repeat(32), name: "Product", mode: "full", online: true,
  version: "0.2.0", commit: "abcdef123456", host: "Office", address: "10.0.0.2", version_state: "newest", ...extra });
const clickButton = (out, label) => find(out, (n) => n.tag === "button" && n.textContent === label)[0].listeners.click();

test("workspace table shows modes, status, versions, hosts, warnings and hand-offs below it", () => {
  const out = workspacesView({ workspaces: [workspace(), workspace({ workspace_id: "b", name: "Legal", mode: "selective",
    online: false, last_seen_ms: NOW - 120000, version_state: "older" }),
  workspace({ workspace_id: "c", name: "Archive", version_state: "unsupported", host: null, address: null })],
  handoffs: [], hub: { version: "0.3.0", min_supported: "0.1.0" } }, NOW);
  const t = text(out);
  assert.match(t, /2 workspaces run an older orch/);
  assert.match(t, /Legal, Archive/);
  assert.match(t, /The hub still works with 0.1.0 and newer\./);
  assert.match(t, /Everything/);
  assert.match(t, /Only what I choose/);
  assert.match(t, /Offline.*since 2 min ago/);
  assert.match(t, /0.2.0 · abcdef1/);
  assert.match(t, /newest/);
  assert.match(t, /update to 0.3.0/);
  assert.match(t, /not supported any more/);
  assert.match(t, /Office10.0.0.2/);
  assert.match(t, /unknownunknown/);
  assert.ok(t.indexOf("Connected workspaces") < t.indexOf("Hand-offs between workspaces"));
  assert.equal(find(out, (n) => n.tag === "th" && n.textContent === "Runs on").length, 1);
});

test("old responses render without hub metadata or admin controls; actions use the selected workspace", () => {
  const w = workspace({ version_state: "unknown", version: null, commit: null });
  const called = [];
  const out = workspacesView({ workspaces: [w], handoffs: [], admin: null,
    actions: { open: (row) => called.push(["open", row]), details: (row) => called.push(["details", row]) } }, NOW);
  assert.doesNotMatch(text(out), /Connect a workspace|Show left|newest|update to|older orch/);
  clickButton(out, "Open"); clickButton(out, "Details");
  assert.deepEqual(called, [["open", w], ["details", w]]);
});

test("details show access only when allowed and send mode changes to the workspace", () => {
  const w = workspace({ mode: "selective" });
  let opened;
  const hidden = workspaceDetailsView(w, { roles: null, owner: "Anna" }, NOW);
  assert.doesNotMatch(text(hidden), /Who has access/);
  assert.match(text(hidden), /OwnerAnna/);
  assert.match(text(hidden), /Only what I choose/);
  assert.match(text(hidden), /To change what this workspace shares, open it and use Settings > Hub. The owner confirms there./);
  const out = workspaceDetailsView(w, { roles: { owner: "Anna", groups: [{ group: "Design", role: "edit", members: ["Lee", "Sam"] }] },
    actions: { open: (row) => { opened = row; } } }, NOW);
  assert.match(text(out), /Who has accessOwner: AnnaDesign · editLee, Sam/);
  clickButton(out, "Open Product");
  assert.equal(opened, w);
  assert.equal(find(out, (n) => n.tag === "button").length, 1);
});

test("connect link keeps the suggestion out of the copied link and shows expiry", async () => {
  let copied;
  const out = [connectWorkspaceView({ link: "/join#one-time", expires_ms: NOW + 1800000 }, { copy: async (s) => { copied = s; return true; } })];
  assert.match(text(out), /Works once, for 30 minutes \(until 12:30\)/);
  assert.match(text(out), /Suggested: Only what I choose\./);
  const input = find(out, (n) => n.tag === "input")[0];
  assert.equal(input.getAttribute("readonly"), "");
  assert.equal(input.getAttribute("value"), "/join#one-time");
  clickButton(out, "Everything");
  assert.match(text(out), /Suggested: Everything. The workspace owner confirms the mode with one click in Mission Control./);
  assert.deepEqual(find(out, (n) => n.getAttribute("aria-pressed") !== null).map((n) => n.getAttribute("aria-pressed")), ["false", "true"]);
  await clickButton(out, "Copy");
  assert.equal(copied, "/join#one-time");
  assert.match(text(out), /Copied/);
});

test("admin empty state connects; left disclosure includes only unbound rows and removes the selected row", async () => {
  const left = workspace({ workspace_id: "left", name: "Old", status: "unbound", left_ms: NOW });
  let removed, connected = 0;
  const out = workspacesView({ workspaces: [], handoffs: [], admin: [left, workspace({ status: "bound" })], actions: {
    connect: async () => { connected++; return { link: "/join#fresh", expires_ms: NOW + 1800000 }; },
    remove: async (w) => { removed = w; },
  } }, NOW);
  assert.match(text(out), /No workspaces yet/);
  assert.match(text(out), /Show left workspaces \(1\)/);
  assert.match(text(out), new RegExp("Left " + new Date(NOW).toLocaleDateString().replaceAll("/", "\\/")));
  assert.doesNotMatch(text(out), /Product/);
  await clickButton(out, "Connect a workspace");
  assert.equal(connected, 1);
  assert.equal(find(out, (n) => n.tag === "input")[0].getAttribute("value"), "/join#fresh");
  await clickButton(out, "Remove");
  assert.equal(removed, left);
});

test("failed admin actions show plain errors and allow retry", async () => {
  const fail = async () => { throw new Error("unavailable"); };
  const out = workspacesView({ workspaces: [], handoffs: [], admin: [workspace({ status: "unbound" })],
    actions: { connect: fail, remove: fail } }, NOW);
  await clickButton(out, "Connect a workspace");
  await clickButton(out, "Remove");
  assert.match(text(out), /Could not create a link. Please try again./);
  assert.match(text(out), /Could not remove this workspace. Please try again./);
  assert.ok(find(out, (n) => n.tag === "button").every((n) => !n.disabled));
});

test("workspace and access names stay text even when they contain markup", () => {
  const name = '<img src=x onerror="bad()">';
  const out = workspaceDetailsView(workspace({ name }), { roles: { owner: name, groups: [{ group: name, role: "read", members: [name] }] } }, NOW);
  assert.match(text(out), /<img/);
  assert.equal(find(out, (n) => n.tag === "img").length, 0);
});
