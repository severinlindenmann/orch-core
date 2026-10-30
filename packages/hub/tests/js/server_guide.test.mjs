// HUB-WS-07: the "New workspace on a server" guide (steps and copyable commands only), against a tiny DOM stub.
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; this.value = ""; this.disabled = false; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { serverGuideView, tunnelCommand, installerCommand, validUser, validServer } = await import("../../src/orch_hub/web/serverguide.js");

const OFFICIAL = "https://raw.githubusercontent.com/severinlindenmann/orch-core/main/install.sh";
const find = (root, pred) => root.all().filter(pred);
const codes = (root) => find(root, (n) => n.tag === "code").map((n) => n.textContent);
function fill(root, id, value) {
  const el = find(root, (n) => n.attrs.id === id)[0];
  el.value = value;
  el.listeners.input();
}

test("the exact tunnel command, same as orch core", () => {
  assert.equal(tunnelCommand("alice", "srv.example.com"), "ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:47821:127.0.0.1:47821 alice@srv.example.com");
  assert.equal(tunnelCommand("bob", "fe80::1", 5000), "ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:5000:127.0.0.1:5000 bob@fe80::1");
});

test("hostile user or server names give no command", () => {
  for (const bad of ["a;b", "a$(x)", "a`x`", "a b", "a\nb", "-oProxyCommand=x", "", "é", "a|b", "a'b", "a\"b", "a&b", "a/b", "[::1]"]) {
    assert.equal(validUser(bad), false, JSON.stringify(bad));
    assert.equal(validServer(bad), false, JSON.stringify(bad));
    assert.equal(tunnelCommand(bad, "srv"), null);
    assert.equal(tunnelCommand("alice", bad), null);
  }
  assert.equal(tunnelCommand("-oProxyCommand=x", "srv"), null);
  assert.equal(tunnelCommand("alice", "srv", 0), null);
  assert.equal(tunnelCommand("a".repeat(65), "srv"), null);
});

test("the guide: four steps, the command appears only for valid input, and never for a hostile one", () => {
  const root = serverGuideView({ copy: async () => true }, { connectView: async () => { throw new Error("no"); } });
  assert.equal(find(root, (n) => n.tag === "li").length, 4);
  assert.deepEqual(codes(root), ["orch setup --no-browser"]);
  fill(root, "sg-user", "alice");
  fill(root, "sg-server", "srv.example.com");
  assert.ok(codes(root).includes("ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:47821:127.0.0.1:47821 alice@srv.example.com"));
  fill(root, "sg-server", "x; rm -rf ~");
  assert.deepEqual(codes(root), ["orch setup --no-browser"]);
  assert.match(root.textContent, /Use only letters/);
  fill(root, "sg-server", "-oProxyCommand=evil");
  assert.ok(!codes(root).some((c) => c.includes("evil")));
});

test("Copy rechecks the input at the moment of copying", async () => {
  let copied = null;
  const root = serverGuideView({ copy: async (t) => { copied = t; return true; } }, { connectView: async () => null });
  fill(root, "sg-user", "alice");
  fill(root, "sg-server", "srv");
  const copy = find(root, (n) => n.tag === "button" && n.textContent === "Copy")[1];
  find(root, (n) => n.attrs.id === "sg-server")[0].value = "srv;evil"; // changed without an input event
  await copy.listeners.click();
  assert.equal(copied, null);
});

test("installer: hidden until published and verified, then only the official https origin", () => {
  const off = serverGuideView({}, {});
  assert.match(off.textContent, /not published yet/);
  assert.ok(!codes(off).some((c) => c.includes("curl")));
  assert.equal(installerCommand(OFFICIAL, false), null);
  assert.equal(installerCommand(OFFICIAL, undefined), null);
  assert.equal(installerCommand(OFFICIAL, true), "curl -LsSf " + OFFICIAL + " | sh");
  for (const bad of ["http://raw.githubusercontent.com/x/install.sh", "https://evil.example/install.sh", "https://raw.githubusercontent.com.evil.example/x", OFFICIAL + "; rm -rf ~", OFFICIAL + "?a=1", "https://user@raw.githubusercontent.com/x"])
    assert.equal(installerCommand(bad, true), null, bad);
  const on = serverGuideView({}, { installerUrl: OFFICIAL, installerVerified: true });
  assert.ok(codes(on).includes("curl -LsSf " + OFFICIAL + " | sh"));
});

test("no request until step 4; then one connect link per press; failure shows a plain message", async () => {
  let calls = 0;
  const root = serverGuideView({ copy: async () => true }, { connectView: async () => { calls++; return new Node_("p", "LINK-HERE"); } });
  assert.equal(calls, 0);
  assert.ok(!/LINK-HERE|#h1\./.test(root.textContent));
  const make = find(root, (n) => n.tag === "button" && n.textContent === "Make the connect link")[0];
  await make.listeners.click();
  assert.equal(calls, 1);
  assert.match(root.textContent, /LINK-HERE/);
  const bad = serverGuideView({}, { connectView: async () => { throw new Error("x"); } });
  await find(bad, (n) => n.tag === "button" && n.textContent === "Make the connect link")[0].listeners.click();
  assert.match(bad.textContent, /Could not create a link/);
});

test("plain words: no VPN vendor name, no secret-looking text before step 4", () => {
  const t = serverGuideView({}, {}).textContent;
  assert.doesNotMatch(t, /netbird|tailscale|wireguard|zerotier|launch=|h1\.|token|password/i);
});

test("Workspaces page: the guide button opens the guide with no request, only for people who may connect", async () => {
  const { workspacesView } = await import("../../src/orch_hub/web/workspaces.js");
  let calls = 0;
  const actions = { people: async () => { calls++; return []; }, connect: async () => { calls++; return null; } };
  const view = workspacesView({ workspaces: [], handoffs: [], admin: [], actions, hub: {}, self: "u" });
  const nodes = view.filter(Boolean).flatMap((n) => n.all());
  const btn = nodes.find((n) => n.tag === "button" && n.textContent === "New workspace on a server");
  assert.ok(btn);
  btn.listeners.click();
  assert.equal(calls, 0);
  assert.ok(view.filter(Boolean).flatMap((n) => n.all()).some((n) => n.attrs["aria-label"] === "New workspace on a server"));
  const member = workspacesView({ workspaces: [], handoffs: [], admin: null, actions, hub: {}, self: "u" }).filter(Boolean).flatMap((n) => n.all());
  assert.ok(!member.some((n) => n.textContent === "New workspace on a server"));
});
