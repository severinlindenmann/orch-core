import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { enter, replaceContent } from "../../src/orch_hub/web/feel.js";
import { parseRoute } from "../../src/orch_hub/web/route.js";

class Element {
  constructor(tag) { this.tag = tag; this.nodeType = 1; this.childNodes = []; this.attrs = {}; this.className = ""; this.listeners = {}; }
  get isConnected() { return true; }
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
  const context = vm.createContext({ slowWatch: { start() {}, cancel() {} }, location, parseRoute, PAGES: [], registerPages() {}, window: { addEventListener: (event, fn) => { handlers[event] = fn; } },
    phones: async () => visits.push("phones"), handoffs: async () => visits.push("handoffs"), account: async () => visits.push("account"), home: async () => visits.push("home"), approvals: async () => visits.push("approvals"), workspaces: async () => visits.push("workspaces"), services: async () => visits.push("services"), admin: async () => visits.push("admin"), registerRoute: async () => visits.push("register"), activity: async () => visits.push("activity"),
    loginView: () => visits.push("login"), addonView: async (w, k) => visits.push("addon:" + k), settingsView: async (w, k) => visits.push("settings:" + k), setupView: () => visits.push("setup"), landFromTicket: async () => visits.push("ticket") });
  vm.runInContext(source.slice(source.indexOf("// Serialize renders")), context);
  await context.renderRoute();
  assert.equal(visits.at(-1), "admin");
  for (const page of ["home", "approvals", "services", "activity", "account", "handoffs", "phones", "register", "home"]) { location.hash = "#/" + page; await handlers.hashchange(); assert.equal(visits.at(-1), page); }
  location.hash = "#/w/" + "ab".repeat(16) + "/addon/terms";
  vm.runInContext(source.slice(source.indexOf("// The page to land on after the next sign-in"), source.indexOf("// FB15")), context);
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
  assert.equal(output.nodes[1].text, "It cannot be opened until it is back. 3 things wait for you.");
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
    refresh: async () => true, loadRail: async () => {}, renderText: String, workspaceStatus: () => "", OPEN_FIRST: "", OPEN_ON_ITS_COMPUTER: "", openWorkspace() {},
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

test("run15c P08: one click on an online, openable workspace opens it; offline or not openable lands on the page that says why", async () => {
  const A = "a".repeat(32), B = "b".repeat(32), C = "c".repeat(32), D = "d".repeat(32);
  const rows = [{ id: A, online: true, canOpen: true }, { id: B, online: false, canOpen: true }, { id: C, online: true, canOpen: false, switchable: false }, { id: D, online: true, canOpen: false, switchable: true }];
  const calls = [];
  const context = vm.createContext({ state: { rail: { rows } }, openWorkspace: (w) => calls.push(["open", w.id]), navigate: (h) => calls.push(["go", h]) });
  vm.runInContext(source.slice(source.indexOf("function openOrPage("), source.indexOf("const HUB_SETTINGS")), context);
  for (const id of [A, B, C, D, "e".repeat(32)]) context.openOrPage(id);
  assert.deepEqual(calls, [["open", A], ["go", "#/w/" + B], ["go", "#/w/" + C], ["go", "#/w/" + D], ["go", "#/w/" + "e".repeat(32)]]);
  // the rail button and the Home Open button both go through it
  assert.match(source, /title: renderText\(w\.name\), onclick: \(\) => openOrPage\(w\.id\) \}/);
  assert.match(source, /open: \(id\) => openOrPage\(id\), dismissEnded/);
  // the only landings the console asks the hub for are "/" and the workspace's hub settings
  assert.match(source, /return_to: returnTo === HUB_SETTINGS \? HUB_SETTINGS : "\/"/);
});

test("run15c P08: the workspace page (the fallback) says why it cannot be opened, not 'Online' and a count", async () => {
  const ws = "ab".repeat(16);
  const run = async (row, needsItems = []) => {
    let output;
    const context = vm.createContext({ pageLoading() {}, loading: () => () => {}, skeleton: () => ({}), show() {}, state: { rail: { rows: [{ id: ws, name: "Anna Test's workspace", ...row }] } },
      refresh: async () => true, loadRail: async () => {}, renderText: String, workspaceStatus: () => "offline since an unknown time", OPEN_FIRST: "FIRST", OPEN_ON_ITS_COMPUTER: "ITS-COMPUTER", openWorkspace() {},
      sharedTicketsView: () => null, h: (tag, props, ...kids) => ({ tag, ...props, kids: kids.flat() }),
      shell: (active, ...nodes) => { output = nodes; },
      api: async (method, path) => ({ status: 200, body: path.startsWith("/me/shared") ? { items: [] } : { items: needsItems.map((k) => ({ workspace_id: ws, key: k })) } }) });
    vm.runInContext(source.slice(source.indexOf("async function workspacePage("), source.indexOf("// Serialize renders")), context);
    await context.workspacePage({ kind: "workspace", workspace: ws });
    return JSON.stringify(output);
  };
  const off = await run({ online: false, switchable: true, canOpen: true, badge: 2 });
  assert.match(off, /Anna Test's workspace is offline since an unknown time/);
  assert.match(off, /It cannot be opened until it is back\. 2 things wait for you\./);
  assert.doesNotMatch(off, /Open workspace|"Online"/);
  const selective = await run({ online: true, switchable: false, canOpen: false });
  assert.match(selective, /ITS-COMPUTER/);
  assert.doesNotMatch(selective, /Open workspace|"Online"|0 things need you/);
  const first = await run({ online: true, switchable: true, canOpen: false }, ["K-1"]);
  assert.match(first, /FIRST/);
  assert.match(first, /1 thing needs you/);
});

test("a failed first read of the workspaces never turns the existing ones into joins", async () => {
  const timers = [], renders = [];
  const one = { status: 200, body: { workspaces: [{ workspace_id: "a".repeat(32) }] } };
  const reads = [{ status: 500, body: {} }, one, one, { status: 200, body: { workspaces: [{ workspace_id: "a".repeat(32) }, { workspace_id: "b".repeat(32) }] } }];
  let n = 0;
  const context = vm.createContext({ pageLoading() {}, loading: () => () => {}, refresh: async () => true, loadRail: async () => {}, state: { user: { user_id: "u" }, menu: false },
    api: async (m, path) => path === "/me/workspaces" ? reads[Math.min(n++, 3)] : { status: 200, body: {} },
    workspacesView: (data) => { renders.push(data); return []; }, shell() {}, closeDialog() {}, openWorkspace() {}, dialog() {}, workspaceDetailsView() {}, h() {}, copyText() {}, pollLive() {},
    newWorkspaces: (known, rows) => (rows ?? []).filter((w) => !known.has(w.workspace_id)), privateRows: () => [], clearInterval() {}, document: { hidden: false },
    setInterval: (fn) => { timers.push(fn); return timers.length; }, viewRevision: 0, workspacesTimer: 0, canAdmin: () => false, signInToAddWorkspace() {} });
  vm.runInContext(source.slice(source.indexOf("async function workspaces("), source.indexOf("async function copyText")), context);
  await context.workspaces();                 // first read fails: nothing known yet
  await timers.at(-1)();                       // first good poll: drawn, not announced as a join
  assert.equal(renders.at(-1).joined.length, 0);
  await timers.at(-1)();
  assert.equal(JSON.stringify(renders.at(-1).joined.map((w) => w.workspace_id)), JSON.stringify(["b".repeat(32)]));
});

test("r14 P05: an admin's join watch also sees a private workspace; a failed admin read joins nothing", async () => {
  const { privateRows, newWorkspaces } = await import("../../src/orch_hub/web/workspaces.js");
  const A = "a".repeat(32), B = "b".repeat(32);
  const mineBody = { status: 200, body: { workspaces: [{ workspace_id: A }] } };
  const adminOf = (extra) => ({ status: 200, body: { workspaces: [{ workspace_id: A, status: "bound" }, ...extra] } });
  const ben = { workspace_id: B, name: "Ben's workspace", owner_name: "Ben", mode: "selective", status: "bound" };
  for (const [label, second, expected, adminCalls] of [["gained", adminOf([ben]), [B], 3], ["failed admin read", { status: 500, body: {} }, [], 2]]) {
    const timers = [], renders = [], paths = [];
    let adminN = 0;
    const context = vm.createContext({ pageLoading() {}, loading: () => () => {}, refresh: async () => true, loadRail: async () => {}, state: { user: { user_id: "u" }, menu: false },
      api: async (m, path) => { paths.push(path); if (path === "/me/workspaces") return mineBody; if (path === "/admin/workspaces") return adminN++ === 0 ? adminOf([]) : second; return { status: 200, body: {} }; },
      workspacesView: (data) => { renders.push(data); return []; }, shell() {}, closeDialog() {}, openWorkspace() {}, dialog() {}, workspaceDetailsView() {}, h() {}, copyText() {}, pollLive() {},
      newWorkspaces, privateRows, clearInterval() {}, document: { hidden: false },
      setInterval: (fn) => { timers.push(fn); return timers.length; }, viewRevision: 0, workspacesTimer: 0, canAdmin: () => true, signInToAddWorkspace() {} });
    vm.runInContext(source.slice(source.indexOf("async function workspaces("), source.indexOf("async function copyText")), context);
    await context.workspaces();
    await timers.at(-1)();
    assert.equal(JSON.stringify(renders.at(-1).joined.map((w) => w.workspace_id)), JSON.stringify(expected), label);
    assert.equal(paths.filter((x) => x === "/admin/workspaces").length, adminCalls, label);
  }
  // a member never reads the admin list from the watch
  const paths = [];
  const timers = [];
  const context = vm.createContext({ pageLoading() {}, loading: () => () => {}, refresh: async () => true, loadRail: async () => {}, state: { user: { user_id: "u" }, menu: false },
    api: async (m, path) => { paths.push(path); return mineBody; },
    workspacesView: () => [], shell() {}, closeDialog() {}, openWorkspace() {}, dialog() {}, workspaceDetailsView() {}, h() {}, copyText() {}, pollLive() {},
    newWorkspaces, privateRows, clearInterval() {}, document: { hidden: false },
    setInterval: (fn) => { timers.push(fn); return 1; }, viewRevision: 0, workspacesTimer: 0, canAdmin: () => false, signInToAddWorkspace() {} });
  vm.runInContext(source.slice(source.indexOf("async function workspaces("), source.indexOf("async function copyText")), context);
  await context.workspaces(); await timers.at(-1)();
  assert.ok(!paths.includes("/admin/workspaces"));
});

test("P03: a shared ticket opens read only with title, description and who shared it; a vanished one says sharing ended", async () => {
  const ws = "ab".repeat(16);
  for (const [items, status] of [[[{ workspace_id: ws, key: "PW-1", title: "Invoice export", shared_by: "Anna", status: "open", description: "CSV please" }], 200], [[], 200], [[], 503]]) {
    let output;
    const context = vm.createContext({ pageLoading() {}, loading: () => () => {}, refresh: async () => true, loadRail: async () => {}, renderText: String, navigate() {},
      h: (tag, props, ...kids) => ({ tag, ...props, kids }), shell: (active, ...nodes) => { output = nodes.filter(Boolean); },
      api: async (method, path) => ({ status, body: { items } }) });
    vm.runInContext(source.slice(source.indexOf("async function sharedTicketPage("), source.indexOf("async function workspacePage(")), context);
    await context.sharedTicketPage({ kind: "shared", workspace: ws, key: "PW-1" });
    const texts = output.map((n) => n.text);
    if (items.length) {
      assert.equal(texts[0], "Invoice export");
      assert.match(texts[1], /Shared by Anna · open · read only/);
      assert.ok(texts.includes("CSV please"));
      assert.equal(output.at(-1).text, "Back");
    } else if (status === 200) {
      assert.equal(texts[0], "Sharing ended");
      assert.match(texts[1], /stopped sharing/);
      assert.equal(output.at(-1).text, "Back to Home");
    } else assert.match(texts[1], /could not be loaded/);
  }
});

test("P14: an empty hub shows 6 menu entries; every area appears once a workspace is connected", async () => {
  const { readdirSync } = await import("node:fs");
  const dir = new URL("../../src/orch_hub/web/pages/", import.meta.url);
  const PAGES = await Promise.all(readdirSync(dir).filter((f) => f.endsWith(".js")).map(async (f) => (await import(new URL(f, dir).href)).page));
  const paint = (rail) => {
    const root = new Element("main");
    const state = { user: { name: "Ann", is_admin: true }, rail, menu: false };
    const context = vm.createContext({ clearInterval, root, state, PAGES, location: { hash: "#/home" }, parseRoute, enter, replaceContent: (el, nodes) => el.replaceChildren(...nodes), renderText: String, initials: (s) => s.slice(0, 2), colour: () => "c0", navigate() {}, signOutMenu: () => new Element("button"), document: { createElement: (t) => new Element(t), createTextNode: (text) => Object.assign(new Element("#text"), { textContent: text }) } });
    vm.runInContext(source.slice(source.indexOf("function h("), source.indexOf("const field =")) + source.slice(source.indexOf("function shell("), source.indexOf("async function loadRail(")), context);
    context.shell("home", new Element("h1"));
    return root.querySelector(".top-nav").querySelectorAll("[data-tab]").map((n) => n.attrs["data-tab"]);
  };
  assert.deepEqual(paint({ hubName: "H", rows: [], ready: true }).sort(), ["account", "admin", "home", "hub-settings", "phones", "workspaces"]);
  assert.equal(paint({ hubName: "H", rows: [{ id: "ab".repeat(16), name: "Legal", online: true }], ready: true }).length, 17);
  assert.equal(paint({ hubName: "H", rows: [], ready: false }).length, 17, "a rail that could not be read hides nothing");
});

test("P08: sidebar groups preserve destinations, open the active page and remember toggles", async () => {
  const { readdirSync } = await import("node:fs");
  const dir = new URL("../../src/orch_hub/web/pages/", import.meta.url);
  const PAGES = await Promise.all(readdirSync(dir).filter(f => f.endsWith(".js")).map(async f => (await import(new URL(f, dir))).page));
  const paint = ({ admin = true, empty = false, active = "home", stored = {}, compact = false } = {}) => {
    const root = new Element("main"), location = { hash: "#/" + active };
    const state = { navGroups: stored, user: { name: "Ann", is_admin: admin }, rail: { hubName: "H", ready: true, rows: empty ? [] : [{ id: "ab".repeat(16), name: "Legal" }] } };
    const context = vm.createContext({ clearInterval, root, state, PAGES, location, parseRoute, enter, replaceContent, renderText: String, initials: s => s.slice(0, 2), colour: () => "c0", navigate() {}, signOutMenu: () => new Element("button"), document: { createElement: t => new Element(t), createTextNode: text => Object.assign(new Element("#text"), { textContent: text }) } });
    vm.runInContext(source.slice(source.indexOf("function h("), source.indexOf("const field =")) + source.slice(source.indexOf("function shell("), source.indexOf("async function loadRail(")), context);
    context.shell(active, new Element("h1"));
    return root.querySelector(compact ? ".top-nav" : ".side");
  };
  const ids = nodes => nodes.map(n => n.attrs["data-tab"]);
  const side = paint(), groups = side.querySelectorAll(".grp");
  assert.deepEqual(ids(side.childNodes.filter(n => n.tag === "a")), ["home", "approvals", "workspaces", "activity"]);
  assert.deepEqual(groups.map(g => g.childNodes[0].textContent), ["Sharing & apps", "People & settings"]);
  assert.ok(groups.every(g => g.getAttribute("open") === null));
  assert.equal(side.querySelector(".who").querySelector("[data-tab]").attrs["data-tab"], "account");
  assert.equal(side.querySelectorAll("[data-tab]").length, 17);
  const compact = paint({ compact: true });
  assert.deepEqual(ids(compact.childNodes.filter(n => n.tag === "a")), ["home", "approvals", "workspaces", "activity"]);
  assert.deepEqual(compact.querySelectorAll(".grp").map(g => g.childNodes[0].textContent), ["Sharing & apps", "People & settings"]);
  assert.ok(compact.querySelectorAll(".grp").every(g => g.getAttribute("open") === null));
  assert.equal(compact.querySelector(".who").childNodes[0].textContent, "Ann");
  assert.equal(compact.querySelector(".who").querySelector("[data-tab]").textContent, "Account");
  assert.equal(paint({ compact: true, active: "mcp" }).querySelectorAll(".grp")[0].getAttribute("open"), "");
  for (const nav of [side, compact]) {
    const links = nav.querySelectorAll("[data-tab]");
    assert.equal(links.find(n => n.attrs["data-tab"] === "spaces").textContent, "Shared spaces");
    assert.equal(links.find(n => n.attrs["data-tab"] === "mcp").textContent, "Tool servers");
  }
  const css = readFileSync(new URL("../../src/orch_hub/web/app.css", import.meta.url), "utf8");
  assert.match(css, /@media \(max-width:760px\)[\s\S]*\.side \{ display:none; \}/);
  assert.match(css, /\.main \.topbar \.top-nav \{ display:flex; flex-direction:column;/);

  assert.deepEqual(ids(paint({ empty: true }).childNodes.filter(n => n.tag === "a")), ["home", "workspaces", "phones", "admin", "hub-settings"]);
  assert.equal(paint({ empty: true }).querySelectorAll(".grp").length, 0);
  const activeGroup = paint({ active: "secrets" }).querySelectorAll(".grp")[0];
  assert.equal(activeGroup.getAttribute("open"), "");
  assert.ok(ids(activeGroup.querySelectorAll("[data-tab]")).includes("secrets"));
  const people = groups[1].querySelectorAll("[data-tab]");
  assert.equal(people.find(n => n.attrs["data-tab"] === "admin").textContent, "People");
  assert.match(people.find(n => n.attrs["data-tab"] === "hub-settings").textContent, /[Bb]ackups/);
  assert.deepEqual(ids(paint({ admin: false }).querySelectorAll("[data-tab]")).sort(), ["account", "approvals", "handoffs", "home", "phones", "workspaces"]);
  const stored = {}, group = paint({ stored }).querySelectorAll(".grp")[0];
  group.open = true; group.listeners.toggle();
  assert.equal(paint({ stored }).querySelectorAll(".grp")[0].getAttribute("open"), "");
  group.open = false; group.listeners.toggle();
  assert.equal(paint({ stored }).querySelectorAll(".grp")[0].getAttribute("open"), null);
  assert.equal(paint({ stored, active: "secrets" }).querySelectorAll(".grp")[0].getAttribute("open"), "");
});

test("P20: a failed sign-in says name, never user id", () => {
  assert.match(source, /bad_credentials: "Wrong name, password or code\. A code works once; wait for the next one\."/);
  assert.doesNotMatch(source, /Wrong user id/);
});

test("workspace sign-in keeps the return origin and copy after an unsuccessful sign-in", async () => {
  const { backToWorkspace, namePrefill } = await import("../../src/orch_hub/web/switch.js");
  const shown = [], assigned = [];
  const h = (tag, props = {}, ...children) => ({ tag, ...props, children: children.flat(), append(n) { this.children.push(n); } });
  const workspace = { origin: "https://workspace.example.test/path?ignored=1" };
  const context = vm.createContext({ ticketWorkspace: workspace, clearSession() {}, h, field: (label, input) => h("label", {}, label, input), show: (...nodes) => shown.push(nodes), banner: () => null,
    api: async (method) => method === "POST" ? { status: 401, body: { error: "bad_credentials" } } : { status: 404 }, say: String, document: { getElementById: () => null },
    backToWorkspace, namePrefill, location: { assign: (u) => assigned.push(u) }, history: { length: 1 }, afterLogin() {} });
  vm.runInContext(source.slice(source.indexOf("function loginView("), source.indexOf("function loginView(") + source.slice(source.indexOf("function loginView(")).indexOf("\n}\n") + 3), context);
  vm.runInContext('loginView(null, { prefill: "Anna Test" })', context);
  const form = shown.at(-1).find((n) => n?.tag === "form");
  assert.ok(form.children.some((n) => n.text === "Use the code shown for orch in the code app on your phone."));
  form.children.find((n) => n.tag === "a").onclick({ preventDefault() {} });
  assert.deepEqual(assigned, ["https://workspace.example.test/"]);
  for (const node of form.children.filter((n) => n.tag === "label")) node.children[1].value = "bad";
  await form.onsubmit({ preventDefault() {} });
  assert.ok(shown.at(-1).find((n) => n?.tag === "form").children.some((n) => n.text === "Back to my workspace"));
});
