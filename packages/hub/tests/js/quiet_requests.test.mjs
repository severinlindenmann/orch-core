import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
const body = source.slice(source.indexOf("const canAdmin"), source.indexOf("function pageLoading("));

function run(session) {
  const calls = [];
  const state = { user: null, channels: [], newChannels: [], me: null };
  const api = async (method, path) => {
    calls.push(path);
    return path === "/session" ? { status: 200, body: session } : { status: 200, body: { channels: [], new_channels: [] } };
  };
  const context = vm.createContext({ state, api, clearSession() {}, loadChannel: async () => null });
  vm.runInContext(body, context);
  return { calls, context, state };
}

test("a switch session (needs_sign_in) never asks for /me/channels", async () => {
  const { calls, context } = run({ user_id: "u", is_admin: true, needs_sign_in: true });
  assert.equal(await context.refresh(), true);
  assert.deepEqual(calls, ["/session"]);
  assert.equal(vm.runInContext("canAdmin()", context), false);
  assert.equal(vm.runInContext("canAdmin({ role: 'owner' })", context), false);
});

test("a full session still asks for its channels; admin routes only for admins, roles only for admin or owner roles", async () => {
  const { calls, context } = run({ user_id: "u", is_admin: false, needs_sign_in: false });
  await context.refresh();
  assert.deepEqual(calls, ["/session", "/me/channels"]);
  assert.equal(vm.runInContext("canAdmin()", context), false);
  assert.equal(vm.runInContext("canAdmin({ role: 'member' })", context), false);
  assert.equal(vm.runInContext("canAdmin({ role: 'admin' })", context), true);
  const admin = run({ user_id: "a", is_admin: true, needs_sign_in: false });
  await admin.context.refresh();
  assert.equal(vm.runInContext("canAdmin()", admin.context), true);
});

test("the workspaces page asks for admin rows and roles only through canAdmin", () => {
  assert.match(source, /canAdmin\(\) \? api\("GET", "\/admin\/workspaces"\)/);
  assert.match(source, /canAdmin\(w\) \? await api\("GET", "\/workspaces\/"/);
});
