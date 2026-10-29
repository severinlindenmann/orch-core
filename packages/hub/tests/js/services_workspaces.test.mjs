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
const { workspacesView, workspaceDetailsView, connectWorkspaceView, newWorkspaces } = await import("../../src/orch_hub/web/workspaces.js");
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
  assert.match(t, /Everything/);
  assert.match(t, /Only what I choose/);
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

test("details Open passes the plain workspace with no pending mode or deep-link value", async () => {
  const w = workspace({ mode: "selective" });
  let args;
  const nodes = workspaceDetailsView(w, { actions: { open: (...value) => { args = value; } } });
  clickButton(nodes, "Open Product");
  assert.deepEqual(args, [w]);
  const { readFile } = await import("node:fs/promises");
  const app = await readFile(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  const open = app.slice(app.indexOf("async function openWorkspace(w)"), app.indexOf("async function openWorkspace(w)") + 230);
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
