// Unit test for the host adapter at the top of static/app.js, run by tests/test_dashboard_host_adapter.py with plain
// node: the local defaults do what the page did, web storage and the cookie may throw, and a host that sets
// window.orchHost first replaces single methods.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(process.argv[2], "utf8");
const m = src.match(/\/\/ host-adapter:begin\n([\s\S]*?)\/\/ host-adapter:end/);
assert(m, "adapter markers");

const load = (win, doc) => {
  vm.runInNewContext(m[1], { window: win, document: doc, URL, Promise, Map, Object, Boolean, Error });
  return win.orchHost;
};
const html = (path) => ({ dataset: path === undefined ? {} : { path } });

// -- local default: the path comes from data-path, not from the address
{
  const win = { location: { pathname: "/elsewhere", search: "?x=1", hash: "#h", href: "http://h/elsewhere?x=1#h", origin: "http://h", reload() { this.reloaded = true; } } };
  const host = load(win, { documentElement: html("/board?q=a"), cookie: "" });
  assert.strictEqual(host.path(), "/board");
  assert.strictEqual(host.search(), "?q=a");
  assert.strictEqual(host.url(), "/board?q=a");
  assert.strictEqual(host.hash(), "#h");
  const r = host.resolve("/t/X-1?y=2#z");
  assert.strictEqual(JSON.stringify(r), JSON.stringify({ href: "http://h/t/X-1?y=2#z", path: "/t/X-1", search: "?y=2", hash: "#z", internal: true }));
  assert.strictEqual(host.resolve("http://other/board").internal, false);
  host.navigate("/board");
  assert.strictEqual(win.location.href, "/board");
  host.reload();
  assert.strictEqual(win.location.reloaded, true);
  assert.strictEqual(host.openLink({}), false);
  assert.strictEqual(host.download({}), false);
}

// -- no data-path (a page the dashboard did not render): the address is the only thing left
{
  const host = load({ location: { pathname: "/a", search: "?b=1" } }, { documentElement: html() });
  assert.strictEqual(host.url(), "/a?b=1");
  assert.strictEqual(host.path(), "/a");
}

// -- history
{
  const calls = [];
  const win = { location: { pathname: "/p", search: "?q", hash: "#f" }, history: { pushState: (...a) => calls.push(["push", ...a]), replaceState: (...a) => calls.push(["replace", ...a]) } };
  const host = load(win, { documentElement: html("/p") });
  assert.strictEqual(host.pageHistory.canPush(), true);
  host.pageHistory.push("/board");
  host.pageHistory.replace("/board?q=1");
  assert.strictEqual(JSON.stringify(calls), JSON.stringify([["push", { orch: true }, "", "/board"], ["replace", null, "", "/board?q=1"]]));
  assert.strictEqual(host.pageHistory.current(), "/p?q#f");
  assert.strictEqual(load({ location: {} }, { documentElement: html("/p") }).pageHistory.canPush(), false);
  load({}, { documentElement: html("/p") }).pageHistory.replace("/x");  // no history at all: nothing happens
}

// -- storage works, and falls back to memory when the browser refuses it
{
  const data = new Map();
  const good = { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, v), removeItem: (k) => data.delete(k) };
  const host = load({ sessionStorage: good, localStorage: good }, { documentElement: html("/") });
  assert.strictEqual(host.session.get("k"), null);
  host.session.set("k", "v");
  assert.strictEqual(data.get("k"), "v");
  assert.strictEqual(host.local.get("k"), "v");
  host.session.remove("k");
  assert.strictEqual(host.session.get("k"), null);

  const refuse = () => { throw new Error("SecurityError"); };
  const win = {};
  for (const k of ["sessionStorage", "localStorage"]) Object.defineProperty(win, k, { get: refuse });
  const off = load(win, { documentElement: html("/") });
  assert.strictEqual(off.local.get("a"), null);
  off.local.set("a", "1");
  assert.strictEqual(off.local.get("a"), "1");  // kept for this page
  off.local.remove("a");
  assert.strictEqual(off.local.get("a"), null);

  const full = { getItem: () => null, setItem: refuse, removeItem: refuse };
  const quota = load({ localStorage: full }, { documentElement: html("/") });
  quota.local.set("b", "2");
  assert.strictEqual(quota.local.get("b"), "2");
}

// -- theme cookie: written, and a throwing cookie does not throw
{
  const doc = { documentElement: html("/"), cookie: "" };
  load({}, doc).setTheme("dark");
  assert.ok(doc.cookie.startsWith("orch_theme=dark;"));
  const raw = { documentElement: html("/") };
  Object.defineProperty(raw, "cookie", { set() { throw new Error("SecurityError"); } });
  load({}, raw).setTheme("dark");
}

// -- clipboard
(async () => {
  const sent = [];
  const host = load({ navigator: { clipboard: { writeText: (t) => { sent.push(t); return Promise.resolve(); } } } }, { documentElement: html("/") });
  await host.copy("hello");
  assert.strictEqual(sent.join(), "hello");
  await assert.rejects(load({}, { documentElement: html("/") }).copy("x"));

  // -- a host that came first replaces single methods and keeps the rest
  const win = { orchHost: { path: () => "/remote", openLink: () => true } };
  const mine = load(win, { documentElement: html("/board") });
  assert.strictEqual(mine.path(), "/remote");
  assert.strictEqual(mine.openLink({}), true);
  assert.strictEqual(mine.download({}), false);
  assert.strictEqual(mine.url(), "/board");
  console.log("host adapter ok");
})().catch((e) => { console.error(e); process.exit(1); });
