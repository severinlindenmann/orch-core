import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { enter, replaceContent } from "../../src/orch_hub/web/feel.js";
import { parseRoute } from "../../src/orch_hub/web/route.js";

class Element {
  constructor(tag) { this.tag = tag; this.nodeType = 1; this.childNodes = []; this.attrs = {}; this.className = ""; this.listeners = {}; }
  append(...nodes) { for (const n of nodes) { n.parent = this; this.childNodes.push(n); } }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
  replaceWith(node) { const p = this.parent; p.childNodes[p.childNodes.indexOf(this)] = node; node.parent = p; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(k, fn) { this.listeners[k] = fn; }
  get classList() { return { add: (c) => this.classList.toggle(c, true), remove: (c) => this.classList.toggle(c, false), toggle: (c, on) => { const classes = new Set(this.className.split(" ")); on ? classes.add(c) : classes.delete(c); this.className = [...classes].join(" "); } }; }
  all() { return [this, ...this.childNodes.flatMap((c) => c.all())]; }
  querySelectorAll(selector) { return this.all().slice(1).filter((n) => selector.startsWith(".") ? n.className.split(" ").includes(selector.slice(1)) : n.getAttribute(selector.slice(1, -1)) !== null); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
}
const source = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
test("switching sections preserves rail, buttons, top bar and updates selected tab", () => {
  const root = new Element("main");
  const state = { user: { name: "Ann", is_admin: true }, rail: { hubName: "Company", rows: [{ id: "ab".repeat(16), name: "Legal", online: false }] }, menu: false };
  const location = { hash: "#/home" };
  const context = vm.createContext({ clearInterval, root, state, PAGES: [], location, parseRoute, enter, replaceContent: (el, nodes) => el.replaceChildren(...nodes), renderText: String, initials: (s) => s.slice(0, 2), colour: () => "c0", navigate() {}, signOutMenu: () => new Element("button"), document: { createElement: (t) => new Element(t), createTextNode: (text) => Object.assign(new Element("#text"), { textContent: text }) } });
  vm.runInContext(source.slice(source.indexOf("function h("), source.indexOf("const field =")) + source.slice(source.indexOf("function shell("), source.indexOf("async function loadRail(")), context);
  context.shell("home", new Element("h1"));
  const rail = root.querySelector(".rail"), top = root.querySelector(".topbar"), button = rail.childNodes[0];
  for (const page of ["workspaces", "services", "admin", "activity", "account", "handoffs", "phones", "home"]) {
    location.hash = "#/" + page;
    context.shell(page, new Element("h1"));
    assert.equal(root.querySelector(".rail"), rail);
    assert.equal(root.querySelector(".topbar"), top);
    assert.equal(rail.childNodes[0], button);
    assert.equal(top.querySelectorAll("[data-tab]").find((n) => n.attrs["aria-current"] === "page").attrs["data-tab"], page);
  }
  location.hash = "#/w/" + state.rail.rows[0].id;
  context.shell("workspaces", new Element("h1"));
  assert.equal(top.querySelector(".breadcrumb").textContent, "Company › Legal");
  assert.match(rail.querySelectorAll("[data-workspace]")[1].className, /offline.*on/);
});

test("hash changes and sign-in continuation dispatch the wanted view", async () => {
  const visits = [], handlers = {};
  const location = { hash: "#/admin" };
  const context = vm.createContext({ location, parseRoute, PAGES: [], registerPages() {}, window: { addEventListener: (event, fn) => { handlers[event] = fn; } },
    phones: async () => visits.push("phones"), handoffs: async () => visits.push("handoffs"), account: async () => visits.push("account"), home: async () => visits.push("home"), approvals: async () => visits.push("approvals"), workspaces: async () => visits.push("workspaces"), services: async () => visits.push("services"), admin: async () => visits.push("admin"), registerRoute: async () => visits.push("register"), activity: async () => visits.push("activity"),
    loginView: () => visits.push("login"), addonView: async (w, k) => visits.push("addon:" + k), settingsView: async (w, k) => visits.push("settings:" + k), setupView: () => visits.push("setup"), landFromTicket: async () => visits.push("ticket") });
  vm.runInContext(source.slice(source.indexOf("// Serialize renders")), context);
  await context.renderRoute();
  assert.equal(visits.at(-1), "admin");
  for (const page of ["home", "approvals", "services", "activity", "account", "handoffs", "phones", "register", "home"]) { location.hash = "#/" + page; await handlers.hashchange(); assert.equal(visits.at(-1), page); }
  location.hash = "#/w/" + "ab".repeat(16) + "/addon/terms";
  vm.runInContext(source.slice(source.indexOf("async function afterLogin()"), source.indexOf("// FB15")), context);
  await context.afterLogin();
  assert.equal(visits.at(-1), "addon:terms");
});

test("offline workspace stays inside the shell and never requests a switch ticket", async () => {
  const ws = "ab".repeat(16), calls = [];
  let output;
  const context = vm.createContext({ pageLoading() {}, loading: () => () => {}, skeleton: () => ({}), show() {}, state: { rail: { rows: [{ id: ws, name: "Legal", online: false, badge: 3 }] } },
    refresh: async () => true, loadRail: async () => {}, renderText: String,
    workspaceStatus: () => "offline since yesterday", h: (tag, props, ...kids) => ({ tag, ...props, kids }),
    shell: (active, ...nodes) => { output = { active, nodes }; },
    api: async (method, path) => { calls.push(path); return { status: 200, body: { items: [] } }; } });
  vm.runInContext(source.slice(source.indexOf("async function workspacePage("), source.indexOf("// Serialize renders")), context);
  await context.workspacePage({ kind: "workspace", workspace: ws });
  assert.equal(output.active, "workspaces");
  assert.equal(output.nodes[0].text, "Legal is offline since yesterday");
  assert.match(output.nodes[2].text, /3 things need you/);
  assert.deepEqual(calls, ["/me/needs-you", "/me/shared?workspace_id=" + ws]);
});

test("sign out clears all person data before the login screen", async () => {
  const state = { user: { name: "Ann" }, rail: { hubName: "Private", rows: [1] }, me: {}, channels: [1], adminPage: [1], notice: {}, menu: true };
  const context = vm.createContext({ state, api: async () => ({}), loginView() { assert.equal(state.user, null); assert.equal(state.rail.hubName, "Hub"); assert.equal(state.rail.rows.length, 0); } });
  vm.runInContext(source.slice(source.indexOf("function clearSession()"), source.indexOf("function registerView(")), context);
  await context.signOut();
  assert.equal(state.me, null);
  assert.equal(state.adminPage, null);
  assert.equal(state.channels.length, 0);
  assert.equal(state.notice, null);
  assert.equal(state.menu, false);
});
test("only first paint and route changes show skeletons", () => {
  const calls = [];
  const context = vm.createContext({ frame: null, paintedRoute: null, location: { hash: "#/home" }, state: { user: {} }, skeleton: k => k, shell: (...args) => calls.push(args), show() {} });
  vm.runInContext(source.slice(source.indexOf("function pageLoading("), source.indexOf("function shell(")), context);
  context.pageLoading("home");
  context.frame = {}; context.paintedRoute = "#/home";
  context.pageLoading("home");
  context.location.hash = "#/services";
  context.pageLoading("services");
  assert.deepEqual(calls, [["home", "home"], ["services", "services"]]);
});

test("a page module is gated by the shell, not by itself: nobody signed in goes to sign-in, a non-admin never reaches an admin page", async () => {
  const visits = [];
  const mk = (path, admin) => ({ path, nav: { label: path, admin, after: "services" }, render: async (d) => { visits.push("render:" + path); await d.refresh(); await d.refresh(); } });
  const PAGES = [mk("secret", true), mk("open", false)];
  let signedIn = false;
  const state = { user: null };
  const context = vm.createContext({ PAGES, state, home: async () => visits.push("home"), loginView: () => visits.push("login"), refresh: async () => { visits.push("refresh"); return signedIn; },
    api() {}, loadRail() {}, reauthView() {}, pageLoading() {}, shell() {}, say() {}, banner() {} });
  vm.runInContext(source.slice(source.indexOf("const pageRender"), source.indexOf("registerPages(PAGES")) + "\nthis.pageRender = pageRender;", context);
  assert.equal(context.pageRender("nope"), null);
  await context.pageRender("secret")();
  assert.deepEqual(visits.splice(0), ["refresh", "login"]);                              // not signed in: sign-in, the page never runs
  signedIn = true; state.user = { is_admin: false };
  await context.pageRender("secret")();
  assert.deepEqual(visits.splice(0), ["refresh", "home"]);                               // a non-admin is sent home before render
  await context.pageRender("open")();
  assert.deepEqual(visits.splice(0), ["refresh", "render:open", "refresh"]);            // the page's first refresh() is answered from the shell's, the second is real
  state.user = { is_admin: true };
  await context.pageRender("secret")();
  assert.deepEqual(visits.splice(0), ["refresh", "render:secret", "refresh"]);
});

test("the workspace page lists shared tickets under the waiting count, for the owner with whom and for others by whom", async () => {
  const ws = "ab".repeat(16);
  let output;
  const shared = [{ workspace_id: ws, title: "Invoice export for Ben", shared_with: ["Ben Test"] }, { workspace_id: ws, title: "Other", shared_by: "Anna Test" }, { workspace_id: "cd".repeat(16), title: "Elsewhere" }];
  const context = vm.createContext({ pageLoading() {}, loading: () => () => {}, skeleton: () => ({}), show() {}, state: { rail: { rows: [{ id: ws, name: "Anna Test's workspace", online: true, canOpen: true }] } },
    refresh: async () => true, loadRail: async () => {}, renderText: String, workspaceStatus: () => "", OPEN_FIRST: "", openWorkspace() {},
    h: (tag, props, ...kids) => ({ tag, ...props, kids: kids.flat() }),
    shell: (active, ...nodes) => { output = nodes; },
    api: async (method, path) => ({ status: 200, body: path.startsWith("/me/shared") ? { items: shared } : { items: [] } }) });
  vm.runInContext(source.slice(source.indexOf("async function workspacePage("), source.indexOf("// Serialize renders")), context);
  await context.workspacePage({ kind: "workspace", workspace: ws });
  const section = output.find((n) => n?.tag === "section");
  assert.equal(section.kids[0].text, "Shared tickets (2)"); // another workspace's ticket is not listed
  const lines = JSON.stringify(section.kids.slice(1));
  assert.match(lines, /Shared with Ben Test/); assert.match(lines, /Shared by Anna Test/);
});

test("a failed first read of the workspaces never turns the existing ones into joins", async () => {
  const timers = [], renders = [];
  const one = { status: 200, body: { workspaces: [{ workspace_id: "a".repeat(32) }] } };
  const reads = [{ status: 500, body: {} }, one, one, { status: 200, body: { workspaces: [{ workspace_id: "a".repeat(32) }, { workspace_id: "b".repeat(32) }] } }];
  let n = 0;
  const context = vm.createContext({ pageLoading() {}, loading: () => () => {}, refresh: async () => true, loadRail: async () => {}, state: { user: { user_id: "u" }, menu: false },
    api: async (m, path) => path === "/me/workspaces" ? reads[Math.min(n++, 3)] : { status: 200, body: {} },
    workspacesView: (data) => { renders.push(data); return []; }, shell() {}, closeDialog() {}, openWorkspace() {}, dialog() {}, workspaceDetailsView() {}, h() {}, copyText() {}, pollLive() {},
    newWorkspaces: (known, rows) => (rows ?? []).filter((w) => !known.has(w.workspace_id)), clearInterval() {}, document: { hidden: false },
    setInterval: (fn) => { timers.push(fn); return timers.length; }, viewRevision: 0, workspacesTimer: 0 });
  vm.runInContext(source.slice(source.indexOf("async function workspaces("), source.indexOf("async function copyText")), context);
  await context.workspaces();                 // first read fails: nothing known yet
  await timers.at(-1)();                       // first good poll: drawn, not announced as a join
  assert.equal(renders.at(-1).joined.length, 0);
  await timers.at(-1)();
  assert.equal(JSON.stringify(renders.at(-1).joined.map((w) => w.workspace_id)), JSON.stringify(["b".repeat(32)]));
});
