// Run-11 #12/#13: the Services and Workspaces pages, against a tiny DOM stub.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

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
const { workspacesView, privateRows, workspaceDetailsView, connectWorkspaceView, newWorkspaces } = await import("../../src/orch_hub/web/workspaces.js");
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

test("workspaces keep waiting counts without the hand-offs table", () => {
  const out = workspacesView({ workspaces: [{ workspace_id: "a", name: "Product" }],
    handoffs: [{ status: "waiting", to: { workspace_id: "a" } }] }, NOW);
  assert.match(text(out), /1 hand-off is waiting for this workspace/);
  assert.doesNotMatch(text(out), /Hand-offs between workspaces|From → to/);
});

test("empty states and times", () => {
  const t = text(workspacesView({ handoffs: [], workspaces: [] }, NOW));
  assert.match(t, /No workspaces yet/);
  assert.doesNotMatch(t, /No hand-offs/);
  assert.equal(when(NOW - 60000, NOW), "Today 11:59");
  assert.equal(ago(NOW - 120000, NOW), "2 min ago");
});

const workspace = (extra = {}) => ({ workspace_id: "a".repeat(32), name: "Product", mode: "full", online: true,
  version: "0.2.0", commit: "abcdef123456", host: "Office", address: "10.0.0.2", version_state: "newest", ...extra });
const clickButton = (out, label) => find(out, (n) => n.tag === "button" && n.textContent === label)[0].listeners.click();

test("workspace table shows modes, status, versions, hosts, warnings without a hand-offs table", () => {
  const out = workspacesView({ workspaces: [workspace(), workspace({ workspace_id: "b", name: "Legal", mode: "selective",
    online: false, last_seen_ms: NOW - 120000, version_state: "older" }),
  workspace({ workspace_id: "c", name: "Archive", version_state: "unsupported", host: null, address: null })],
  handoffs: [], hub: { version: "0.3.0", min_supported: "0.1.0" } }, NOW);
  const t = text(out);
  assert.match(t, /2 workspaces run an older orch/);
  assert.match(t, /Legal, Archive/);
  assert.match(t, /The hub still works with 0.1.0 and newer\./);
  assert.match(t, /Shares everything/);
  assert.match(t, /Shares only what you choose/);
  assert.match(t, /Personal: only its owner/);
  assert.match(t, /Offline.*since 2 min ago/);
  assert.match(t, /0.2.0 · abcdef1/);
  assert.match(t, /newest/);
  assert.match(t, /update to 0.3.0/);
  assert.match(t, /not supported any more/);
  assert.match(t, /Office10.0.0.2/);
  assert.match(t, /unknownunknown/);
  assert.doesNotMatch(t, /Hand-offs between workspaces/);
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
  assert.match(text(hidden), /Shares only what you choose/);
  assert.match(text(hidden), /To change what this workspace shares, open it and use Settings > Hub. The workspace owner confirms with one click on their dashboard./);
  const out = workspaceDetailsView(w, { roles: { owner: "Anna", groups: [{ group: "Design", role: "edit", members: ["Lee", "Sam"] }] },
    actions: { open: (row) => { opened = row; } } }, NOW);
  assert.match(text(out), /Who has accessOwner: AnnaDesign · editLee, Sam/);
  clickButton(out, "Open Product");
  assert.equal(opened, w);
  assert.equal(find(out, (n) => n.tag === "button").length, 1);
});

test("connect link is copied unchanged, shows expiry, and Copy says what to do next", async () => {
  let copied;
  const out = [connectWorkspaceView({ link: "/join#one-time", expires_ms: NOW + 1800000 }, { copy: async (s) => { copied = s; return true; } })];
  assert.match(text(out), /Works once, for 30 minutes \(until 12:30\)/);
  assert.doesNotMatch(text(out), /Suggested|Everything|Mission Control/);
  assert.doesNotMatch(text(out), /Now open your workspace/, "the next step shows after Copy");
  const input = find(out, (n) => n.tag === "input")[0];
  assert.equal(input.getAttribute("readonly"), "");
  assert.equal(input.getAttribute("value"), "/join#one-time");
  await clickButton(out, "Copy");
  assert.equal(copied, "/join#one-time");
  assert.match(text(out), /Copied ✓/);
  let opened;
  const withOpen = [connectWorkspaceView({ link: "/join#one-time", expires_ms: NOW + 1800000 }, { openLink: (l) => { opened = l; } }, { selected: "u1", self: "u1" })];
  clickButton(withOpen, "Open link");
  assert.equal(opened, "/join#one-time");
  assert.match(text(out), /Copied ✓ Send it to the workspace owner\. They open their workspace, go to Settings › Hub and press Connect a hub\./);
});

test("r11 P03: Open link is offered only for the signed-in person's own workspace, Copy for everyone", async () => {
  const openBtn = (out) => find(out, (n) => n.tag === "button" && n.textContent === "Open link")[0];
  const L = { link: "/c#x", expires_ms: NOW + 1800000 };
  assert.equal(openBtn([connectWorkspaceView(L, {}, { selected: "u1", self: "u1" })]).hidden, false);
  assert.equal(openBtn([connectWorkspaceView(L, {}, { selected: "u2", self: "u1" })]).hidden, true);
  assert.equal(openBtn([connectWorkspaceView(L, {}, { selected: "u1" })]).hidden, true, "unknown signed-in person: Copy only");
  const out = workspacesView({ workspaces: [], handoffs: [], admin: [], self: "u1", actions: {
    people: async () => [{ user_id: "u1", name: "Anna" }, { user_id: "u2", name: "Ben" }],
    connect: async (owner) => ({ link: "/c#" + owner, expires_ms: NOW + 1800000 }) } }, NOW);
  await clickButton(out, "Connect a workspace");
  const select = find(out, (n) => n.tag === "select")[0];
  select.value = "u2";
  await select.listeners.change({ target: select });
  assert.equal(openBtn(out).hidden, true, "Ben's link is not opened in Anna's browser");
  assert.ok(find(out, (n) => n.tag === "button" && n.textContent === "Copy")[0]);
  select.value = "u1";
  await select.listeners.change({ target: select });
  assert.equal(openBtn(out).hidden, false, "Anna's own link can be opened here");
});

test("admin empty state connects; left disclosure includes only unbound rows and removes the selected row", async () => {
  const left = workspace({ workspace_id: "left", name: "Old", status: "unbound", left_ms: NOW });
  let removed, connected = 0;
  const out = workspacesView({ workspaces: [], handoffs: [], admin: [left], actions: {
    connect: async () => { connected++; return { link: "/join#fresh", expires_ms: NOW + 1800000 }; },
    remove: async (w) => { removed = w; },
  } }, NOW);
  assert.match(text(out), /No workspaces yet/);
  assert.match(text(out), /Show left workspaces \(1\)/);
  assert.match(text(out), new RegExp("Left " + new Date(NOW).toLocaleDateString().replaceAll("/", "\\/")));
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

test("details Open passes the plain workspace with no pending mode or deep-link value", async () => {
  const w = workspace({ mode: "selective" });
  let args;
  const nodes = workspaceDetailsView(w, { actions: { open: (...value) => { args = value; } } });
  clickButton(nodes, "Open Product");
  assert.deepEqual(args, [w]);
  const { readFile } = await import("node:fs/promises");
  const app = await readFile(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  const start = app.indexOf("async function openWorkspace(w)");
  const open = app.slice(start, app.indexOf("\n}\n", start)); // the whole function, not a fixed length
  assert.match(open, /workspace_id: w.id, return_to: "\/", kind: "open"/);
  assert.doesNotMatch(open, /pending|mode|return_to: "[^" ]*[?#]/);
});

const actions = { check() {}, open() {}, copy: async () => true };
const diagram = (out) => find(out, (n) => n.attrs["aria-labelledby"] === "connections-title")[0];

test("connection diagram has accessible workspace and service nodes with text and colour states", () => {
  const d = base();
  d.workspaces_list = [{ name: "Product", online: true }, { name: "<Legal>", online: false }];
  d.relay.state = "down";
  d.publish.state = "not_set_up";
  d.edge = { state: "up", url: "https://hub.test" };
  const out = servicesView(d, actions, NOW), flow = diagram(out);
  const nodes = find([flow], (n) => n.className.startsWith("node "));
  assert.deepEqual(nodes.map((n) => [n.tag, n.textContent, n.className]), [
    ["li", "Productonline", "node ok"], ["li", "<Legal>offline", "node muted"],
    ["li", "HubRunning", "node ok"], ["li", "RelayNot answering", "node warn"],
    ["li", "PublishNot set up", "node muted"], ["li", "EdgeRunning", "node ok"],
  ]);
  assert.equal(find([flow], (n) => n.className === "arrow")[0].textContent, "link");
  assert.ok(out.indexOf(flow) < out.findIndex((n) => n.className === "card svc-list"));
  const edge = find(out, (n) => n.attrs.id === "svc-edge")[0];
  assert.match(edge.textContent, /Edge: puts the hub on a public address\./);
  assert.match(edge.textContent, /https:\/\/hub.test/);
  assert.match(edge.textContent, /Running/);
  assert.equal(find([edge], (n) => n.tag === "button" || n.tag === "details").length, 0);
});

test("workspace diagram caps names at eight and reports how many remain", () => {
  const d = base();
  d.workspaces_list = Array.from({ length: 11 }, (_, i) => ({ name: "Workspace " + i, online: true }));
  const flow = diagram(servicesView(d, actions, NOW));
  assert.equal(find([flow], (n) => n.className.startsWith("node ")).length, 12);
  assert.match(flow.textContent, /Workspace 7online\+3 more/);
  assert.doesNotMatch(flow.textContent, /Workspace 8/);
});

test("old response shape and an empty workspace list still render", () => {
  const d = base();
  let out = servicesView(d, actions, NOW);
  assert.match(diagram(out).textContent, /3 workspaces, 2 online/);
  assert.match(find(out, (n) => n.attrs.id === "svc-edge")[0].textContent, /Not set up/);
  assert.match(diagram(out).textContent, /RelayRunningPublishRunningEdgeNot set up/);
  d.workspaces_list = [];
  out = servicesView(d, actions, NOW);
  assert.match(diagram(out).textContent, /0 workspaces, 0 online/);
});

test("services refresh every 15 seconds, pause while hidden, and stop after navigation", async () => {
  const { readFileSync } = await import("node:fs");
  const { runInNewContext } = await import("node:vm");
  const source = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  const timers = new Map(), requests = [];
  let nextTimer = 0, rendered, release;
  const context = {
    root: { classList: { remove() {} }, replaceChildren() {} },
    document: { hidden: false }, state: { user: { is_admin: true } },
    refresh: async () => true, loadRail: async () => {},
    api: async (_, path) => {
      requests.push(path);
      if (release && path === "/admin/services") await new Promise((resolve) => { release = resolve; });
      return { status: 200, body: path === "/admin/services" ? base() : { workspaces: [{ name: "Product", online: true }] } };
    },
    servicesView: (data) => { rendered = data; return []; },
    workspaces() {}, copyText() {},
    setInterval: (fn, ms) => { assert.equal(ms, 15000); timers.set(++nextTimer, fn); return nextTimer; },
    clearInterval: (id) => timers.delete(id), loading: () => () => {}, pageLoading() {}, loginView() {}, home() {}, navigate() {},
    banner() {}, h() {}, say() {}, reauthView() {},
  };
  runInNewContext(source.match(/let servicesTimer[^\n]+/)[0] + "\n" + "let paintedRoute = null;\n" + source.match(/const show = [^\n]+/)[0] + "\n" +
    "function shell() { show(); }\n" + source.slice(source.indexOf("async function services("), source.indexOf("async function home()")), context);
  await context.services();
  assert.deepEqual(requests, ["/admin/services", "/me/workspaces"]);
  assert.equal(rendered.workspaces_list[0].name, "Product");
  assert.equal(timers.size, 1);
  context.document.hidden = true;
  await [...timers.values()][0]();
  assert.equal(requests.length, 2);
  context.document.hidden = false;
  await [...timers.values()][0]();
  assert.equal(requests.length, 4);
  assert.equal(timers.size, 1);
  // A reply already in flight must not reopen Services after another page renders.
  release = true;
  const pending = [...timers.values()][0]();
  while (release === true) await Promise.resolve();
  context.shell("home");
  assert.equal(timers.size, 0);
  release();
  await pending;
  assert.equal(timers.size, 0);
});

test("a workspace that joined replaces the empty state and the used link with a green message", async () => {
  const w = workspace({ name: "Anna's workspace" });
  const opened = [];
  const out = workspacesView({ workspaces: [w], handoffs: [], admin: [w], joined: [w], actions: { open: (x) => opened.push(x.workspace_id) } }, NOW);
  const t = text(out);
  assert.doesNotMatch(t, /No workspaces yet/);
  const banner = find(out, (n) => n.attrs?.role === "status" || n.getAttribute?.("role") === "status")[0];
  assert.match(banner.textContent, /Anna's workspace joined/);
  assert.match(banner.textContent, /It is online now/);
  clickButton(out, "Open Anna's workspace");
  assert.deepEqual(opened, [w.workspace_id]);
});

test("only workspaces that were not on screen count as joined", () => {
  const known = new Set(["a", "b"]);
  assert.deepEqual(newWorkspaces(known, [{ workspace_id: "a" }, { workspace_id: "c" }, {}]).map((x) => x.workspace_id), ["c"]);
  assert.deepEqual(newWorkspaces(known, undefined), []);
});

test("the admin empty state points to the Connect button; a member is told to ask the admin", () => {
  assert.match(text(workspacesView({ handoffs: [], workspaces: [], admin: [] })), /Press .Connect a workspace./);
  assert.match(text(workspacesView({ handoffs: [], workspaces: [], admin: null })), /Ask your hub admin/);
});

test("workspaces are named after their owner and connect asks whose workspace it is", async () => {
  const a = workspace({ name: null, owner_name: "Ben Test" }), b = workspace({ workspace_id: "b".repeat(32), name: null, owner_name: "Anna Test" });
  const sent = [];
  const out = workspacesView({ workspaces: [a, b], handoffs: [], admin: [], self: "u1", actions: {
    people: async () => [{ user_id: "u1", name: "Anna Test" }, { user_id: "u2", name: "Ben Test" }],
    connect: async (owner) => { sent.push(owner); return { link: "/c#" + owner, expires_ms: NOW + 1800000 }; } } }, NOW);
  const t = text(out);
  assert.match(t, /Ben Test's workspace/); assert.match(t, /Owner: Ben Test/); assert.match(t, /Anna Test's workspace/);
  assert.doesNotMatch(t, /Another workspace/);
  await clickButton(out, "Connect a workspace");
  assert.deepEqual(sent, []); // r9 P04: with several people nothing is preselected
  assert.match(text(out), /Whose workspace is this\?/);
  const select = find(out, (n) => n.tag === "select")[0];
  assert.deepEqual(find([select], (n) => n.tag === "option").map((o) => o.textContent), ["Choose…", "Anna Test", "Ben Test", "Someone new (add them now)"]);
  select.value = "u2";
  await select.listeners.change({ target: select });
  assert.deepEqual(sent, ["u2"]);
  assert.equal(find(out, (n) => n.tag === "input" && n.getAttribute("aria-label") === "Connect link")[0].value, "/c#u2");
});

test("a workspace-session admin still sees Connect a workspace; the click asks for one sign-in and mints nothing", async () => {
  let asked = 0, minted = 0;
  const out = workspacesView({ workspaces: [], handoffs: [], admin: null, canConnect: true, needsSignIn: true,
    actions: { signInToConnect: () => { asked++; }, connect: async () => { minted++; return {}; } } }, NOW);
  assert.match(text(out), /Connect a workspace/);
  assert.match(text(out), /No workspaces yet/);
  await clickButton(out, "Connect a workspace");
  assert.equal(asked, 1);
  assert.equal(minted, 0);
  const member = workspacesView({ workspaces: [], handoffs: [], admin: null, actions: {} }, NOW);
  assert.doesNotMatch(text(member), /Connect a workspace/);
});

test("the return route after sign-in is a short same-origin hash only", () => {
  const source = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  const re = new RegExp(source.match(/const SAFE_RETURN = \/(.*)\/i;/)[1], "i");
  assert.ok(re.test("#/workspaces"));
  for (const bad of ["https://evil.test/", "//evil.test", "#//evil.test", "javascript:alert(1)", "#/a?x=1", "#/" + "a".repeat(60), "workspaces"]) assert.ok(!re.test(bad), bad);
});

test("a full admin whose admin list failed to load still mints a link (no sign-out)", async () => {
  let asked = 0, minted = 0;
  const out = workspacesView({ workspaces: [], handoffs: [], admin: null, canConnect: true, needsSignIn: false,
    actions: { signInToConnect: () => { asked++; }, people: async () => [], connect: async () => { minted++; return { link: "/j", expires_ms: NOW + 1000 }; } } }, NOW);
  await clickButton(out, "Connect a workspace");
  assert.equal(asked, 0);
  assert.equal(minted, 1);
});

test("one workspace on an older orch is worded in the singular", () => {
  const out = workspacesView({ workspaces: [workspace(), workspace({ workspace_id: "b", name: "Legal", version_state: "older" })],
    handoffs: [], hub: { version: "0.3.0", min_supported: "0.1.0" } }, NOW);
  assert.match(text(out), /1 workspace runs an older orch/);
  assert.doesNotMatch(text(out), /1 workspaces/);
});

// r9 P04: the admin can pick "Someone new..." in Connect a workspace; nothing is silently preselected.
const twoPeople = [{ user_id: "a".repeat(32), name: "Admin" }, { user_id: "b".repeat(32), name: "Ben" }];

test("P04: with several people nothing is preselected and no workspace link is minted until one is chosen", async () => {
  const connected = [];
  const out = workspacesView({ workspaces: [], handoffs: [], admin: [], self: "a".repeat(32), actions: {
    people: async () => twoPeople,
    connect: async (who) => { connected.push(who); return { link: "/c#fresh", expires_ms: NOW + 1800000 }; },
  } }, NOW);
  await clickButton(out, "Connect a workspace");
  assert.deepEqual(connected, [], "no link before a person is chosen");
  const select = find(out, (n) => n.tag === "select")[0];
  const options = find([select], (n) => n.tag === "option");
  assert.deepEqual(options.map((o) => o.textContent), ["Choose…", "Admin", "Ben", "Someone new (add them now)"]);
  assert.ok(options.every((o) => o.getAttribute("selected") === null || o === options[0]));
  assert.equal(find(out, (n) => n.className === "ws-link")[0].hidden, true, "the link is hidden until a person is chosen");
  select.value = "b".repeat(32);
  await select.listeners.change({ target: select });
  assert.deepEqual(connected, ["b".repeat(32)]);
  assert.equal(find(out, (n) => n.className === "ws-link")[0].hidden, false);
});

test("P04: Someone new shows a name field, calls invite (not connect), and shows the sign-in link", async () => {
  const connected = [], invited = [];
  let refreshed = 0;
  const out = workspacesView({ workspaces: [], handoffs: [], admin: [], self: "a".repeat(32), actions: {
    people: async () => { refreshed++; return twoPeople; },
    connect: async (who) => { connected.push(who); return { link: "/c#fresh", expires_ms: NOW + 1800000 }; },
    invite: async (name) => { invited.push(name); return { link: "http://hub.test/#invite=tok", expires_ms: NOW + 1800000 }; },
    copy: async () => true,
  } }, NOW);
  await clickButton(out, "Connect a workspace");
  const select = find(out, (n) => n.tag === "select")[0];
  select.value = "__new__";
  await select.listeners.change({ target: select });
  assert.deepEqual(connected, [], "Someone new never mints a workspace link");
  const name = find(out, (n) => n.tag === "input" && n.getAttribute("aria-label") === "Their name")[0];
  assert.ok(name);
  assert.match(text(out), /Create sign-in link/);
  await clickButton(out, "Create sign-in link");
  assert.deepEqual(invited, [], "an empty name is not sent");
  name.value = "  Ben Muster ";
  await clickButton(out, "Create sign-in link");
  assert.deepEqual(invited, ["Ben Muster"]);
  assert.deepEqual(connected, []);
  assert.equal(find(out, (n) => n.tag === "input" && n.getAttribute("aria-label") === "Sign-in link")[0].getAttribute("value"), "http://hub.test/#invite=tok");
  assert.match(text(out), /come back here to connect their workspace/);
  assert.ok(refreshed >= 2, "the people list is refreshed after the invite");
  find(out, (n) => n.tag === "section")[0].stopWaiting();
});

// r11 P12/P12b: after a sign-in link the panel waits for THAT link and turns into its person's workspace link by itself.
const ANNA_ID = "a".repeat(32), BEN_ID = "c".repeat(32), STRANGER_ID = "d".repeat(32);
const waitingPanel = (t, { peopleSeq, statusSeq, alive = () => true, expires = NOW + 1800000, inviteId = "0123456789abcdef" } = {}) => {
  t.mock.timers.enable({ apis: ["setInterval", "Date"], now: NOW });
  const connected = [], asked = [];
  let calls = 0, people = 0;
  const out = workspacesView({ workspaces: [], handoffs: [], admin: [], self: ANNA_ID, actions: {
    alive,
    people: async () => peopleSeq(++people),
    inviteStatus: async (id) => { asked.push(id); calls++; return statusSeq(calls); },
    connect: async (who) => { connected.push(who); return { link: "/c#" + who, expires_ms: NOW + 1800000 }; },
    invite: async () => ({ link: "http://hub.test/#invite=tok", expires_ms: expires, invite_id: inviteId }),
    copy: async () => true,
  } }, NOW);
  return { out, connected, asked, calls: () => calls };
};
const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const startInvite = async (out, name = "Ben") => {
  await clickButton(out, "Connect a workspace");
  const select = find(out, (n) => n.tag === "select")[0];
  select.value = "__new__";
  await select.listeners.change({ target: select });
  find(out, (n) => n.tag === "input" && n.getAttribute("aria-label") === "Their name")[0].value = name;
  await clickButton(out, "Create sign-in link");
  return select;
};
const anna = { user_id: ANNA_ID, name: "Anna" }, ben = { user_id: BEN_ID, name: "Ben" }, stranger = { user_id: STRANGER_ID, name: "Ben" };
const old = { user_id: "e".repeat(32), name: "Old" };

test("r11 P12b: a stranger joining first is ignored; the person who used THIS link is the one connected", async (t) => {
  const { out, connected, asked, calls } = waitingPanel(t, {
    peopleSeq: (n) => n <= 2 ? [anna, old] : [anna, old, stranger, ben], // the stranger (also "Ben") is listed before ours
    statusSeq: (n) => n <= 2 ? { state: "waiting" } : { state: "joined", user_id: BEN_ID, name: "Ben" } });
  const select = await startInvite(out);
  assert.match(text(out), /Waiting for Ben to sign in…/);
  assert.doesNotMatch(text(out), /appears here once/);
  t.mock.timers.tick(3000); await settle();
  t.mock.timers.tick(3000); await settle();
  assert.deepEqual(connected, [], "still waiting: the stranger in the people list is not our person");
  t.mock.timers.tick(3000); await settle();
  assert.deepEqual(asked, ["0123456789abcdef", "0123456789abcdef", "0123456789abcdef"]);
  assert.deepEqual(connected, [BEN_ID], "exactly the user id the link status names");
  assert.equal(select.value, BEN_ID);
  assert.match(text(out), /✓ Ben has signed in/);
  assert.equal(find(out, (n) => n.className === "ws-link")[0].hidden, false);
  assert.equal(find(out, (n) => n.tag === "input" && n.getAttribute("aria-label") === "Connect link")[0].value, "/c#" + BEN_ID);
  assert.equal(find(out, (n) => n.tag === "button" && n.textContent === "Open link")[0].hidden, true, "Ben's link is copied, not opened here");
  const before = calls();
  t.mock.timers.tick(9000); await settle();
  assert.equal(calls(), before, "the poll stopped");
});

test("r11 P12b: on link expiry, an ended link or a status without a user, the page asks to pick, with nothing selected", async (t) => {
  for (const [label, opts, ticks] of [
    ["expiry", { statusSeq: () => ({ state: "waiting" }), expires: NOW + 4000 }, 2],
    ["ended", { statusSeq: () => ({ state: "ended" }) }, 1],
    ["joined without user", { statusSeq: () => ({ state: "joined", user_id: 7 }) }, 1],
  ]) {
    const p = waitingPanel(t, { peopleSeq: () => [anna, stranger], ...opts });
    const select = await startInvite(p.out);
    for (let i = 0; i < ticks; i++) { t.mock.timers.tick(3000); await settle(); }
    assert.match(text(p.out), /Pick the person/, label);
    assert.match(text(p.out), /Choose who signed in with your link/, label);
    assert.equal(select.value, "", label + ": nobody preselected");
    assert.deepEqual(p.connected, [], label + ": never guesses");
    assert.doesNotMatch(text(p.out), /Waiting for Ben/, label);
    const n = p.calls();
    t.mock.timers.tick(9000); await settle();
    assert.equal(p.calls(), n, label + ": stopped");
    t.mock.timers.reset();
  }
});

test("r11 P12b: a hand pick while the people list is loading wins; the invited person is not chosen", async (t) => {
  let release;
  const p = waitingPanel(t, {
    peopleSeq: (n) => n <= 2 ? [anna, old] : new Promise((r) => { release = () => r([anna, old, ben]); }),
    statusSeq: () => ({ state: "joined", user_id: BEN_ID, name: "Ben" }) });
  const select = await startInvite(p.out);
  t.mock.timers.tick(3000); await settle();
  assert.ok(release, "the people list is loading");
  select.value = old.user_id;
  await select.listeners.change({ target: select });
  release(); await settle();
  assert.deepEqual(p.connected, [old.user_id], "only the hand pick is connected");
  assert.equal(select.value, old.user_id);
  assert.doesNotMatch(text(p.out), /Ben has signed in/);
});

test("r11 P12: the poll stops when the admin picks someone by hand or leaves the page", async (t) => {
  const a = waitingPanel(t, { peopleSeq: () => [anna, old], statusSeq: () => ({ state: "waiting" }) });
  const select = await startInvite(a.out);
  select.value = ANNA_ID;
  await select.listeners.change({ target: select });
  const n1 = a.calls();
  t.mock.timers.tick(9000); await settle();
  assert.equal(a.calls(), n1, "a hand pick stops the watch");
  t.mock.timers.reset();

  let here = true;
  const b = waitingPanel(t, { peopleSeq: () => [anna, old], statusSeq: () => ({ state: "waiting" }), alive: () => here });
  await startInvite(b.out);
  t.mock.timers.tick(3000); await settle();
  const n2 = b.calls();
  assert.ok(n2 >= 1);
  here = false;
  t.mock.timers.tick(9000); await settle();
  assert.equal(b.calls(), n2, "leaving the page stops the watch");
});

test("P04: an invite that fails shows a plain error", async () => {
  const out = workspacesView({ workspaces: [], handoffs: [], admin: [], self: "a".repeat(32), actions: {
    people: async () => twoPeople, invite: async () => { throw new Error("x"); } } }, NOW);
  await clickButton(out, "Connect a workspace");
  const select = find(out, (n) => n.tag === "select")[0];
  select.value = "__new__";
  await select.listeners.change({ target: select });
  find(out, (n) => n.tag === "input" && n.getAttribute("aria-label") === "Their name")[0].value = "Ben";
  await clickButton(out, "Create sign-in link");
  assert.match(text(out), /Could not create a sign-in link\. Please try again\./);
});

test("P05: a hub admin also sees a connected workspace private to someone else, without Open, roles, host or address; others see only their own", () => {
  const ben = { workspace_id: "ben", name: "Ben Test's workspace", owner_name: "Ben Test", mode: "selective", status: "bound", host: "ben-mac", address: "10.9.9.9", version: "0.2.0", grants: [{ group: "x" }] };
  const left = { workspace_id: "old", name: "Left one", status: "unbound" };
  const mine = [workspace()];
  const asAdmin = workspacesView({ workspaces: mine, handoffs: [], admin: [{ ...mine[0], status: "bound" }, ben, left] }, NOW);
  const t = text(asAdmin);
  assert.match(t, /Ben Test's workspace/);
  assert.match(t, /Private to Ben Test/);
  assert.match(t, /Only Ben Test can open it/);
  assert.doesNotMatch(t, /10\.9\.9\.9|ben-mac/);
  const rows = find(asAdmin, (n) => n.tag === "tr" && n.getAttribute("data-workspace"));
  assert.deepEqual(rows.map((r) => r.getAttribute("data-workspace")), [mine[0].workspace_id, "ben"]);
  assert.equal(find([rows[1]], (n) => n.tag === "button" && n.textContent === "Open").length, 0);
  assert.equal(find([rows[0]], (n) => n.tag === "button" && n.textContent === "Open").length, 1);
  const none = workspacesView({ workspaces: mine, handoffs: [], admin: null }, NOW);
  assert.doesNotMatch(text(none), /Ben Test/);
  const details = text(workspaceDetailsView(privateRows(mine, [ben])[0], {}, NOW));
  assert.doesNotMatch(details, /Open Ben|Change mode|Runs on/);
  assert.match(details, /Only Ben Test can open it/);
});

test("P05: the Home count includes private workspaces for a hub admin and nobody else", async () => {
  const { homeView } = await import("../../src/orch_hub/web/home.js");
  const ws = [{ workspace_id: "a", name: "Anna", online: true, mode: "selective" }];
  const ben = { workspace_id: "ben", name: "Ben", owner_name: "Ben", mode: "selective", status: "bound" };
  const line = (d) => homeView({ workspaces: ws, items: [], shared: [], actions: {}, ...d }).flat().map((n) => n.textContent).join("\n").match(/\d+ workspaces? online, \d+ offline/)[0];
  assert.equal(line({ adminWorkspaces: [ben] }), "2 workspaces online, 0 offline");
  assert.equal(line({ adminWorkspaces: null }), "1 workspace online, 0 offline");
});

test("P12: after Copy the banner names the person and the exact route", async () => {
  const out = [connectWorkspaceView({ link: "/join#x", expires_ms: NOW + 1800000 }, { copy: async () => true }, { people: [{ user_id: "u2", name: "Ben Test" }], selected: "u2", self: "u1" })];
  await clickButton(out, "Copy");
  assert.match(text(out), /Copied ✓ Send it to Ben Test\. They open their workspace, go to Settings › Hub and press Connect a hub\./);
});

// HUB-WS-04 (#1385): ask the owners of older workspaces to update.
test("HUB-WS-04: an admin can ask the owners to update; it is asked once and then shows when", async () => {
  let asked = 0;
  const rows = [workspace(), workspace({ workspace_id: "b", name: "Legal", version_state: "older" })];
  const out = workspacesView({ workspaces: rows, handoffs: [], admin: [], hub: { version: "0.3.0", min_supported: "0.1.0" },
    actions: { askUpdate: async () => { asked += 1; return { asked: 1, at_ms: NOW }; } } }, NOW);
  assert.match(text(out), /Ask owners to update/);
  await clickButton(out, "Ask owners to update");
  assert.equal(asked, 1);
  assert.match(text(out), /Asked/);
  assert.doesNotMatch(text(out), /Ask owners to update/);
  // a later visit (the hub remembers the first time): no button, only when it was asked
  const later = workspacesView({ workspaces: [workspace({ workspace_id: "b", version_state: "older", update_asked_ms: NOW - 120000, update_asked_target: "0.3.0" })],
    handoffs: [], admin: [], hub: { version: "0.3.0" }, actions: { askUpdate: async () => { asked += 1; } } }, NOW);
  assert.match(text(later), /Asked 2 min ago/);
  assert.doesNotMatch(text(later), /Ask owners to update/);
});

test("HUB-WS-04: only admins see the button, and a failed ask says so and can be repeated", async () => {
  const rows = [workspace({ version_state: "older" })];
  const member = workspacesView({ workspaces: rows, handoffs: [], admin: null, hub: { version: "0.3.0" }, actions: { askUpdate: async () => ({}) } }, NOW);
  assert.doesNotMatch(text(member), /Ask owners to update/);
  const out = workspacesView({ workspaces: rows, handoffs: [], admin: [], hub: { version: "0.3.0" }, actions: { askUpdate: async () => { throw new Error("x"); } } }, NOW);
  await clickButton(out, "Ask owners to update");
  assert.match(text(out), /Could not send the request/);
  assert.match(text(out), /Ask owners to update/);
});

test("HUB-WS-04 L1: 'Asked' shows only for the hub's current version; an older ask shows the button again", () => {
  const row = (target) => workspace({ workspace_id: "b", version_state: "older", update_asked_ms: NOW - 60000, update_asked_target: target });
  const mk = (r) => workspacesView({ workspaces: [r], handoffs: [], admin: [], hub: { version: "0.3.0" }, actions: { askUpdate: async () => ({}) } }, NOW);
  assert.match(text(mk(row("0.3.0"))), /Asked 1 min ago/);
  assert.match(text(mk(row("0.2.0"))), /Ask owners to update/);
});

test("SV-01: every part shows its address; Edge down is called out; no address is said plainly", () => {
  const d = base();
  d.hub.url = "http://192.168.1.5:8100";
  d.relay.url = "http://100.64.0.2:8101";
  d.publish.url = null; d.publish.state = "up";
  d.edge = { state: "down", url: "https://hub.test", last_ok_ms: null };
  const out = servicesView(d, actions, NOW);
  const row = (n) => find(out, (x) => x.attrs.id === "svc-" + n)[0].textContent;
  assert.match(row("hub"), /Address http:\/\/192\.168\.1\.5:8100/);
  assert.match(row("relay"), /Address http:\/\/100\.64\.0\.2:8101/);
  assert.match(row("publish"), /No address/);
  assert.match(row("edge"), /Not answering/);
  assert.match(row("edge"), /Address https:\/\/hub\.test/);
  assert.equal(summary(d).text, "Edge is not answering");
});

test("r14 P05: the joined banner for a private workspace has no Open button and names who can open it", async () => {
  const { joinedBannerView } = await import("../../src/orch_hub/web/workspaces.js");
  const opened = [];
  const row = { workspace_id: "b".repeat(32), name: "Ben Test's workspace", owner_name: "Ben Test", private: true };
  const el = joinedBannerView(row, { open: (x) => opened.push(x) });
  assert.match(el.textContent, /Ben Test's workspace joined/);
  assert.match(el.textContent, /It is online now\. Only Ben Test can open it\./);
  assert.doesNotMatch(el.textContent, /Open |from here/);
  const plain = joinedBannerView({ workspace_id: "a", name: "Anna" }, {});
  assert.match(plain.textContent, /You can open it from here/);
});

test("P08: the Workspaces page shows the Restored note at the top until Got it is clicked", () => {
  let n = 0;
  const out = workspacesView({ workspaces: [], handoffs: [], restored: { made_ms: NOW - 3600000, restored_ms: NOW, suspended: 1 },
    actions: { dismissRestored: () => n++ } }, NOW);
  const t = text(out);
  assert.match(t, /Restored from the backup of/); assert.match(t, /Check that your workspaces show Online/);
  assert.match(t, /1 person is switched off/);
  const got = find(out, (x) => x.tag === "button" && x.textContent === "Got it");
  assert.equal(got.length, 1);
  got[0].listeners.click();
  assert.equal(n, 1);
  assert.doesNotMatch(text(workspacesView({ workspaces: [], handoffs: [] }, NOW)), /Restored from/);
});
