import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { renderText } from "../../src/orch_hub/web/render.js";

const source = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const node = (tag, props = {}, ...children) => ({ tag, ...props, children: children.flat().filter(Boolean),
  append(...kids) { this.children.push(...kids); }, prepend(...kids) { this.children.unshift(...kids); } });
const text = n => typeof n === "string" ? n : (n.text ?? "") + (n.children ?? []).map(text).join(" ");

test("workspace sign-in renders the named source and heading inside the form card", () => {
  for (const workspace of [null, {}, { name: "Design <team>" }]) {
    const c = vm.createContext({ ticketWorkspace: workspace, clearSession() {}, h: node, field: (_label, input) => input,
      namePrefill: () => "", renderText, api: async () => ({ status: 404 }), companyButton: () => null,
      document: { getElementById: () => null }, show: (...nodes) => { c.output = nodes; } });
    vm.runInContext(section("function loginView(", "// First admin"), c);
    c.loginView();
    assert.equal(c.output.length, 1);
    const form = c.output[0];
    assert.equal(form.tag, "form");
    assert.equal(form.children[0].tag, "h1");
    if (workspace) assert.ok(text(form).includes(workspace.name ? "You came from Design <team>." : "You came from your workspace."));
  }
});

test("Continue renders the known workspace name, with a fallback when unknown", async () => {
  for (const workspace of [null, { name: "Design <team>" }]) {
    const c = vm.createContext({ history: { replaceState() {} }, location: { pathname: "/" }, show() {}, openingCard() {},
      api: async () => ({ status: 200, body: { name: "Ben", workspace } }), renderText, landingPlan: () => "confirm", h: node,
      plain: (...nodes) => { c.output = nodes; } });
    vm.runInContext(section("async function landFromTicket(", '// "Opening your hub'), c);
    await c.landFromTicket("ticket");
    assert.ok(c.output.map(text).join(" ").includes(workspace ? "from Design <team>'s workspace." : "from your workspace."));
  }
});

test("focus and visible events re-read Workspaces shares through live polling, throttled to five seconds", async () => {
  let now = 0, shared = [], redraws = 0;
  const calls = [], listeners = {};
  const c = vm.createContext({ state: { user: { user_id: "ben" }, rail: {} }, polling: false, offline: false,
    Date: { now: () => now }, updateOffline() {}, frame: { isConnected: true, querySelector: () => null },
    location: { hash: "#/workspaces" }, parseRoute: () => ({ kind: "workspaces" }), viewRevision: 1,
    workspacesShared: "[]", railRows: () => [], railView() {}, reconcileRows() {},
    document: { visibilityState: "visible", querySelector: () => null, addEventListener: (name, fn) => { listeners[name] = fn; } },
    window: { addEventListener: (name, fn) => { listeners[name] = fn; } },
    api: async (_method, path) => { calls.push(path); return { status: 200, body: { items: path === "/me/shared" ? shared : [], workspaces: [] } }; },
    workspaces: async () => { redraws++; c.workspacesShared = JSON.stringify(shared); } });
  vm.runInContext(section("async function pollLive()", "setInterval(") + section("let lastResumePoll", "function openShortcuts("), c);
  shared = [{ key: "T-1", title: "New share" }];
  await listeners.focus();
  assert.deepEqual(calls, ["/me/needs-you", "/rail", "/me/shared"]);
  assert.equal(redraws, 1);
  await listeners.visibilitychange();
  now = 4999; await listeners.focus();
  assert.equal(calls.length, 3);
  now = 5000; await listeners.visibilitychange();
  assert.equal(calls.length, 6);
  assert.equal(redraws, 1, "unchanged shares preserve the page");
  c.document.visibilityState = "hidden";
  now = 10000; await listeners.visibilitychange(); await listeners.focus();
  assert.equal(calls.length, 6);
});
