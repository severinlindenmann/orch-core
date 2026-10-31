import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { busyButton } from "../../src/orch_hub/web/feel.js";

const source = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
function session(extra = {}) {
  const c = vm.createContext({ state: { user: { user_id: "old" }, rail: { rows: [1] } }, homeData: { user_id: "old" }, liveItems: { user_id: "old", items: [1] },
    api: async () => ({ status: 401, body: {} }), loadChannel: async () => null,
    h: (_tag, props, ...children) => ({ ...props, children }), show(node) { c.output = node; }, plain() {}, landing: () => ({ kind: "signout" }),
    loginView() { c.clearSession(); c.login = true; }, ...extra });
  vm.runInContext(section("function clearSession()", "function registerView(") + section("async function refresh()", "function pageLoading(") + section("async function reauthView()", "async function admin()") + section("async function redeemTicket(", "async function landFromTicket("), c);
  return c;
}
function cleared(c) {
  assert.equal(c.homeData, null);
  assert.equal(c.liveItems.items.length, 0);
  assert.equal(c.state.rail.rows.length, 0);
}
test("all session endings discard cached home, search and rail data", async () => {
  for (const end of [c => c.signOut(), c => c.refresh(), c => c.redeemTicket("t", true), c => c.reauthView()]) {
    const c = session(); await end(c); cleared(c); assert.equal(c.state.user, null);
  }
});
test("refresh discards previous user's caches but retains same user's caches", async () => {
  for (const user_id of ["old", "new"]) {
    const c = session({ api: async () => ({ status: 200, body: { user_id, channels: [] } }) });
    assert.equal(await c.refresh(), true);
    assert.equal(c.state.user.user_id, user_id);
    if (user_id === "new") cleared(c); else assert.equal(c.liveItems.items.length, 1);
  }
});
test("live authentication failures leave offline mode and sign in; only network and 5xx stay offline", async () => {
  for (const status of [0, 500, 503, 401, 403, 400, 429, 200]) {
    for (const failedPath of ["/rail", "/me/needs-you"]) {
      const c = session({ offline: true, failures: 2, nextPoll: 0, polling: false, frame: null,
        document: { visibilityState: "visible" }, updateOffline() {}, retryDelay: () => 2000,
        api: async (_method, path) => ({ status: path === failedPath ? status : 200, body: { error: "malformed" } }) });
      vm.runInContext(section("async function pollLive()", "setInterval("), c);
      await c.pollLive();
      assert.equal(c.offline, status === 0 || status >= 500, `${failedPath}: ${status}`);
      assert.equal(!!c.login, status === 401 || status === 403);
      if (c.login) cleared(c);
      assert.equal(c.polling, false);
    }
  }
});
test("search never draws another user's cache and clears cache after failed fetch", async () => {
  for (const owner of ["old", "another"]) {
    let resolve;
    const drawn = [];
    const node = () => ({ classList: { add() {} }, addEventListener() {}, replaceChildren() {}, append() {}, setAttribute() {}, removeAttribute() {}, focus() {}, value: "", isConnected: true });
    const c = session({ liveItems: { user_id: owner, items: ["secret"] }, document: { querySelector: () => null },
      h: node, dialog: node, closeDialog() {}, renderText: String, filterSearch: x => x,
      searchEntries: data => { drawn.push(data.items); return []; } });
    // All requests share a controlled response, allowing inspection before it arrives.
    const response = new Promise(r => { resolve = r; }); c.api = () => response;
    vm.runInContext(section("async function openSearch()", "let prefixUntil"), c);
    const pending = c.openSearch();
    assert.equal(drawn[0].length, owner === "old" ? 1 : 0);
    resolve({ status: 403, body: {} }); await pending;
    assert.equal(c.liveItems.items.length, 0);
    assert.equal(drawn.at(-1).length, 0);
  }
});
test("recovery restarts arming and cannot enable an unarmed, hidden or busy signing button", async () => {
  let now = 0, id = 0;
  const timers = new Map();
  const button = { disabled: true, dataset: {}, textContent: "Sign", getAttribute: () => null };
  const unarmed = { ...button, dataset: {} };
  const c = vm.createContext({ offline: false, document: { visibilityState: "visible", querySelectorAll: () => [button, unarmed] },
    setTimeout: (fn, ms) => { timers.set(++id, { fn, due: now + ms }); return id; }, clearTimeout: id => timers.delete(id) });
  vm.runInContext(section("function arm(", "new MutationObserver"), c);
  const advance = ms => { now += ms; for (const [id, t] of timers) if (t.due <= now) { timers.delete(id); t.fn(); } };
  button.arm = () => c.arm(button);
  button.arm(); advance(500);
  c.offline = true; c.syncSignButtons();
  c.offline = false; c.syncSignButtons();
  advance(500); assert.equal(button.disabled, true);
  advance(500); assert.equal(button.disabled, false); assert.equal(unarmed.disabled, true);
  for (const mode of ["hidden", "busy"]) {
    button.arm(); c.document.visibilityState = mode === "hidden" ? "hidden" : "visible";
    button.getAttribute = () => mode === "busy" ? "true" : null;
    advance(1000); assert.equal(button.disabled, true);
  }
  c.document.visibilityState = "visible"; button.getAttribute = () => null;
  button.arm(); advance(1000);
  button.setAttribute = () => {}; button.removeAttribute = () => {};
  await busyButton(button, async () => {});
  assert.equal(button.disabled, true); advance(1000); assert.equal(button.disabled, false);
});
test("in-flight rail and live responses cannot restore data after sign-out", async () => {
  for (const fn of ["loadRail", "pollLive"]) {
    let resolve;
    const response = new Promise(r => { resolve = r; });
    const c = session({ offline: false, failures: 0, nextPoll: 0, polling: false, frame: null,
      document: { visibilityState: "visible" }, updateOffline() {}, railRows: body => body.workspaces,
      api: () => response });
    vm.runInContext(section("async function loadRail()", "async function openWorkspace(") + section("async function pollLive()", "setInterval("), c);
    const pending = c[fn](); c.clearSession();
    resolve({ status: 200, body: { items: [1], workspaces: [1] } });
    await pending; cleared(c);
  }
});

test("P02: reauth goes straight to the sign-in form with the name filled in and returns to the asked page", async () => {
  const calls = [];
  const c = session({ state: { user: { user_id: "anna", needs_sign_in: true }, rail: { rows: [] } }, location: { hash: "#/admin" },
    loginView(err, opts) { calls.push(opts); c.clearSession(); } });
  c.api = async (m, p) => { calls.push(m + " " + p); return { status: 200, body: {} }; };
  await c.reauthView();
  assert.equal(calls[0], "DELETE /session");
  assert.equal(calls[1].prefill, "anna");
  assert.match(calls[1].title, /Sign in to open Admin/);
  assert.equal(c.returnHash, "#/admin");
});
