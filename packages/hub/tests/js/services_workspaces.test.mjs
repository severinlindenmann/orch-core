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
const { workspacesView, statusOf } = await import("../../src/orch_hub/web/workspaces.js");
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
