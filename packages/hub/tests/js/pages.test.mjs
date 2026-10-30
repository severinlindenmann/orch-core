// The page registry: every module of web/pages/ exports one `page` ({path, nav, render}); the server builds /pages.js from the
// folder; app.js and route.js list no page. These tests fail when a page does not fit, or when a page name is hand-kept.
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

const WEB = new URL("../../src/orch_hub/web/", import.meta.url);
const CORE_TABS = ["home", "approvals", "workspaces", "phones", "handoffs", "account", "services", "activity", "admin"];
const files = readdirSync(new URL("pages/", WEB)).filter((f) => !f.startsWith(".") && f !== "__pycache__").sort();
const modules = await Promise.all(files.map(async (f) => [f, (await import(new URL("pages/" + f, WEB).href)).page]));

const routeSource = readFileSync(new URL("route.js", WEB), "utf8");
const coreRoutes = new Set(/\^#\\\/\(([a-z|]+)\)\$/.exec(routeSource)[1].split("|"));
// route names that are not a plain tab: the workspace routes and the fragments of the entry links
const RESERVED = new Set([...coreRoutes, "w", "item", "addon", "settings", "access", "setup", "invite", "ticket", "workspace"]);

/** Every way a page can be wrong, as sentences. [] = fits. `entries` is [[fileName, page]]. */
function problems(entries) {
  const out = [], paths = new Set(), known = new Set(CORE_TABS);
  for (const [f, page] of entries) {
    if (!/^[a-z][a-z0-9-]{0,31}\.js$/.test(f)) out.push(f + ": not a page module name");
    if (!page || typeof page !== "object") { out.push(f + ": no `page` export"); continue; }
    if (page.path !== f.slice(0, -3)) out.push(f + ": path " + JSON.stringify(page.path) + " is not the file name");
    if (RESERVED.has(page.path)) out.push(f + ": " + page.path + " collides with a route of orch's own");
    if (paths.has(page.path)) out.push(f + ": duplicate path " + page.path);
    paths.add(page.path);
    if (typeof page.nav?.label !== "string" || !page.nav.label || page.nav.label.length > 24) out.push(f + ": nav.label");
    if (typeof page.nav?.admin !== "boolean") out.push(f + ": nav.admin");
    if (page.nav?.needsWorkspace !== undefined && typeof page.nav.needsWorkspace !== "boolean") out.push(f + ": nav.needsWorkspace");
    if (typeof page.render !== "function") out.push(f + ": render");
    if (JSON.stringify(Object.keys(page).sort()) !== '["nav","path","render"]') out.push(f + ": a page exports exactly path, nav, render");
  }
  for (let todo = entries.map(([, p]) => p).filter((p) => p && p.nav), moved = true; todo.length && moved;) {
    moved = false;
    for (const p of [...todo]) if (known.has(p.nav.after)) { known.add(p.path); todo.splice(todo.indexOf(p), 1); moved = true; }
    if (!moved) for (const p of todo) out.push(p.path + ": nav.after " + JSON.stringify(p.nav.after) + " is not an existing tab or page");
  }
  return out;
}

test("every file of pages/ is a well-formed page whose path is its file name", () => {
  assert.ok(files.length >= 5);
  assert.deepEqual(problems(modules), []);
  for (const [f, page] of modules) assert.equal(page.path, f.slice(0, -3));
});

test("the checks catch what they are for: a core route name, a wrong path, an unresolved position, a duplicate", () => {
  const ok = () => ({ path: "x", nav: { label: "X", admin: true, after: "services" }, render() {} });
  assert.deepEqual(problems([["x.js", ok()]]), []);
  assert.match(problems([["home.js", { ...ok(), path: "home" }]]).join("\n"), /home collides with a route of orch's own/);
  assert.match(problems([["admin.js", { ...ok(), path: "admin" }]]).join("\n"), /admin collides/);
  assert.match(problems([["y.js", ok()]]).join("\n"), /path "x" is not the file name/);
  assert.match(problems([["x.js", { ...ok(), nav: { label: "X", admin: true, after: "nowhere" } }]]).join("\n"), /nav\.after "nowhere" is not an existing tab or page/);
  assert.match(problems([["x.js", { ...ok(), nav: { label: "X", admin: true, after: "x" } }]]).join("\n"), /nav\.after "x"/);   // after itself
  assert.match(problems([["x.js", ok()], ["x.js", ok()]]).join("\n"), /duplicate path x/);
  assert.match(problems([["X.js", { ...ok(), path: "X" }]]).join("\n"), /not a page module name/);
  assert.match(problems([["x.js", { ...ok(), extra: 1 }]]).join("\n"), /exactly path, nav, render/);
  assert.deepEqual(problems([["a.js", { ...ok(), path: "a" }], ["b.js", { ...ok(), path: "b", nav: { label: "B", admin: false, after: "a" } }]]), []);   // after another page
});

test("every core tab is a route of orch's own", () => {
  for (const t of CORE_TABS) assert.ok(coreRoutes.has(t), "route.js lists " + t);
});

test("app.js and route.js name no page of the folder (they are built from the generated list)", () => {
  const app = readFileSync(new URL("app.js", WEB), "utf8");
  assert.match(app, /import \{ PAGES \} from "\.\/pages\.js";/);
  assert.doesNotMatch(app, /from "\.\/pages\/|import\s*\(/);
  for (const [, p] of modules) {
    assert.doesNotMatch(app, new RegExp("\\b" + p.path + "Page\\b"), "app.js names " + p.path);
    assert.doesNotMatch(app, new RegExp('tab\\("' + p.path + '"'), "app.js lists the tab " + p.path);
    assert.ok(!routeSource.includes("|" + p.path + "|") && !routeSource.includes("|" + p.path + ")"), "route.js names " + p.path);
  }
});
