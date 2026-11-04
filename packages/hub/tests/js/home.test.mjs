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
const { homeView, dismissEnded, homeSummary, workspaceStatus } = await import("../../src/orch_hub/web/home.js");
const spaces = [{ workspace_id: "a", name: "Product", online: true }, { workspace_id: "b", name: "Legal", online: false }];
test("summary excludes another person's workspace", () => {
  const out = homeSummary(spaces, [{ workspace_id: "a", signable: true }, { workspace_id: "secret" }]);
  assert.equal(out.waiting, 1);
  assert.equal(out.online, 1);
  assert.equal(out.offline, 1);
});
test("home groups rows, names their workspace, and shows health", () => {
  const out = homeView({ workspaces: spaces, items: [
    { workspace_id: "a", title: "First", signable: true },
    { workspace_id: "secret", title: "Secret", signable: true },
    { workspace_id: "a", title: "Second", signable: true },
  ], actions: {} });
  const nodes = out.flatMap((n) => n.all());
  const text = out.map((n) => n.textContent).join(" ");
  assert.match(text, /1 workspace online, 1 offline, 2 things need you/);
  assert.doesNotMatch(text, /Secret/);
  const groups = nodes.filter((n) => n.className === "workspace-needs");
  assert.equal(groups.length, 1);
  assert.match(groups[0].textContent, /Product.*First.*Product.*Second.*Product/);
  assert.match(text, /offline since an unknown time/);
  assert.match(text, /Hand-offs and approvals wait/);
});
test("offline time is honest and formats available last-seen data", () => {
  assert.equal(workspaceStatus({ online: true }), "online");
  assert.equal(workspaceStatus({ online: false }), "offline since an unknown time");
  assert.equal(workspaceStatus({ last_seen: "bad" }), "offline since an unknown time");
  assert.equal(workspaceStatus({ last_seen: 1700000000 }), "offline since " + new Date(1700000000000).toLocaleString());
});

test("summary and chip share the actionable list, excluding offline and hidden items", () => {
  const items = [
    { workspace_id: "a", signable: true }, { workspace_id: "a", summary_only: true },
    { workspace_id: "a", mode: "selective" }, { workspace_id: "a" },
    { workspace_id: "b", signable: true }, { workspace_id: "a", online: false, signable: true },
    { workspace_id: "secret", signable: true },
  ];
  const out = homeView({ workspaces: spaces, items, actions: {} });
  assert.match(out.map(n => n.textContent).join(" "), /3 things need you/);
  const needs = out.flatMap(n => n.all()).find(n => n.className === "card needs-you");
  assert.equal(needs.all().find(n => n.className === "chip ").textContent, "3");
  assert.equal(homeSummary(spaces, items, ["a"]).waiting, 0);
});

test("with a selective workspace the needs card says quietly that only shared items appear; otherwise it does not", () => {
  const text = (n) => n.flatMap((x) => x.all()).map((x) => x.textContent).join(" ");
  const sel = homeView({ workspaces: [{ workspace_id: "s", name: "S", mode: "selective", online: true }], items: [], actions: {} });
  assert.match(text(sel), /Only items you share appear here/);
  const full = homeView({ workspaces: [{ workspace_id: "f", name: "F", mode: "full", online: true }], items: [], actions: {} });
  assert.doesNotMatch(text(full), /Only items you share appear here/);
});

test("first-run home shows only the connect card", () => {
  const out = homeView({ user: { name: "Anna", is_admin: true }, workspaces: [], items: [], channels: [], thisBrowser: { state: "unregistered" }, actions: { connect() {} } });
  const text = out.map((n) => n.textContent).join(" ");
  assert.match(text, /Connect your first workspace/);
  assert.match(text, /Connect a workspace/);
  assert.match(text, /We make a link for your workspace\. You use it once\./);
  assert.doesNotMatch(text, /one-time|Settings/);
  assert.match(text, /More areas \(approvals, add-ons, activity…\) appear here once a workspace is connected\./);
  const member = homeView({ user: { name: "Ben" }, workspaces: [], items: [], channels: [], shared: [], thisBrowser: { state: "unregistered" }, actions: {} }).map((n) => n.textContent).join(" ");
  assert.match(member, /Ask your hub admin to connect a workspace for you\./);
  assert.doesNotMatch(text, /Where you can sign|Register this browser|Needs you|My workspaces/);
  const withWs = homeView({ user: { name: "Anna", is_admin: true }, workspaces: [{ workspace_id: "a", name: "P", online: true }], items: [], channels: [], thisBrowser: { state: "unregistered" }, actions: {} });
  const t2 = withWs.map((n) => n.textContent).join(" ");
  assert.match(t2, /Where you can sign/);
  assert.doesNotMatch(t2, /Connect your first workspace/);
});

test("a member without a workspace sees shared tickets first and can open one", () => {
  const ticket = { workspace_id: "a", key: "AT-0002", title: "Invoice CSV export", shared_by: "Anna Test" };
  let opened;
  const base = { user: { name: "Ben Test" }, workspaces: [], items: [], channels: [], shared: [ticket],
    actions: { openShared: (it) => { opened = it; }, connect() {} } };
  const out = homeView(base);
  const text = out.map((n) => n.textContent).join(" ");
  assert.doesNotMatch(text, /1 ticket shared with you\./);
  assert.match(text, /Shared with you \(1\)/);
  assert.match(text, /Invoice CSV export/);
  assert.match(text, /Shared by Anna Test/);
  assert.match(text, /For your own workspace, ask your hub admin\./);
  assert.doesNotMatch(text, /Ask your hub admin to connect a workspace for you\.|Connect your first workspace|A workspace|Needs you|My workspaces/);
  const buttons = out.flatMap((n) => n.all()).filter((n) => n.tag === "button");
  assert.deepEqual(buttons.map((n) => n.textContent), ["Open"]);
  assert.equal(buttons[0].className, "pri");
  buttons[0].listeners.click();
  assert.equal(opened, ticket);
  const admin = homeView({ ...base, user: { name: "Anna", is_admin: true } });
  assert.match(admin.map((n) => n.textContent).join(" "), /Connect your first workspace/);
  const ended = homeView({ ...base, shared: [], ended: [{ id: "a:AT-0002", by: "Anna Test", title: ticket.title }] });
  assert.match(ended.map((n) => n.textContent).join(" "), /Anna Test stopped sharing Invoice CSV export/);
});

test("the first-run home carries a root marker and gives way once a workspace or item exists", async () => {
  const { firstRunOutgrown } = await import("../../src/orch_hub/web/home.js");
  const out = homeView({ user: { name: "Anna" }, workspaces: [], items: [], channels: [], actions: {} });
  assert.ok(out.flatMap((n) => n.all()).some((n) => (n.className || "").split(" ").includes("first-run-root")));
  assert.equal(firstRunOutgrown([], []), false);
  assert.equal(firstRunOutgrown([{ id: "a" }], []), true);
  assert.equal(firstRunOutgrown([], [{ key: "k" }]), true);
  assert.equal(firstRunOutgrown([], [], [{}]), true);
  const src = (await import("node:fs")).readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  assert.match(src, /querySelector\("\.first-run-root"\) && firstRunOutgrown\(/);
});

test("P03: a nameless workspace uses its owner's name", () => {
  const out = homeView({ user: { name: "Anna", is_admin: true }, workspaces: [{ workspace_id: "a", owner_name: "Ben", online: true }], items: [], channels: [],
    thisBrowser: { state: "unregistered" }, actions: { register() {} } });
  const t = out.map((n) => n.textContent ?? "").join(" ");
  assert.match(t, /Ben's workspace/);
  assert.doesNotMatch(t, /A workspace\b|not registered/i);
});

const all = (n) => [n, ...n.children.flatMap(all)];
const textOf = (out) => out.flatMap(all).map((n) => n.textContent).join(" ");
test("run15 P03 N4: a switch session that holds a local key says it is ready and needs one sign-in", () => {
  const out = homeView({ user: { name: "Anna", is_admin: true }, workspaces: [{ workspace_id: "a", name: "P", online: true }], items: [], channels: [],
    thisBrowser: { state: "active", label: "Chrome", needsSignIn: true }, actions: {} });
  const t = out.flatMap((n) => n.all()).map((n) => n.textContent).join(" ");
  assert.match(t, /Ready\. Sign in once to approve\./);
  assert.doesNotMatch(t, /holds a signing key/);
});

test("run15 P03: a browser with no key gets one green button, Set up signing, and no 'sign in here once' detour", () => {
  let called = 0;
  for (const state of ["unregistered"]) {
    const out = homeView({ user: { name: "Anna", is_admin: true }, workspaces: [{ workspace_id: "a", name: "P", online: true }], items: [], channels: [],
      thisBrowser: { state }, actions: { register: () => { called++; } } });
    const t = textOf(out);
    assert.match(t, /Approvals need a signing key in this browser/);
    assert.doesNotMatch(t, /cannot sign yet|Sign in here once|You came here from a workspace|Register this browser/);
    const btn = out.flatMap(all).filter((n) => n.tag === "button" && n.textContent === "Set up signing");
    assert.equal(btn.length, 1);
    assert.equal(btn[0].className, "pri");
    btn[0].listeners.click();
  }
  assert.equal(called, 1);
  const ok = textOf(homeView({ user: { name: "Anna", is_admin: true }, workspaces: [{ workspace_id: "a", name: "P", online: true }], items: [], channels: [], thisBrowser: { state: "active", label: "Chrome" }, actions: {} }));
  assert.match(ok, /This browser holds a signing key for you/);
  assert.doesNotMatch(ok, /Set up signing/);
});

test("run15c P08: a member with nothing to sign sees no 'Where you can sign'; it appears with an item, a phone, a key or admin rights", () => {
  const ws = [{ workspace_id: "a", name: "P", online: true, mode: "full" }];
  const base = { user: { name: "Ben" }, workspaces: ws, items: [], channels: [], thisBrowser: { state: "unregistered" }, actions: {} };
  assert.doesNotMatch(textOf(homeView(base)), /Where you can sign|signing key/);
  const signable = homeView({ ...base, items: [{ workspace_id: "a", title: "Q", signable: true }] });
  assert.match(textOf(signable), /Where you can sign/);
  assert.match(textOf(signable), /Set up signing/);
  const summaryOnly = homeView({ ...base, workspaces: [{ ...ws[0], mode: "selective" }], items: [{ workspace_id: "a", title: "Q", summary_only: true }] });
  assert.doesNotMatch(textOf(summaryOnly), /Where you can sign/);  // nothing here that this person can sign
  assert.match(textOf(homeView({ ...base, channels: [{ channel_id: "c", kind: "phone", status: "active" }] })), /Where you can sign/);
  assert.match(textOf(homeView({ ...base, user: { name: "Anna", is_admin: true } })), /Where you can sign/);
  assert.match(textOf(homeView({ ...base, thisBrowser: { state: "active", label: "x" } })), /Where you can sign/);
  assert.match(textOf(homeView({ ...base, thisBrowser: { state: "dead" } })), /Set up signing again/);
});

test("run18 P04: a key that really stopped working says so once, with one button and no Forget; a first-time person never reads 'removed'", () => {
  let called = 0;
  const mk = (state) => homeView({ user: { name: "Anna", is_admin: true }, workspaces: [{ workspace_id: "a", name: "P", online: true }], items: [], channels: [],
    thisBrowser: { state }, actions: { register: () => { called++; } } });
  const dead = mk("dead"), t = textOf(dead);
  assert.match(t, /This browser's key no longer works/);
  assert.match(t, /Set it up again to keep approving here\./);
  assert.doesNotMatch(t, /Forget|removed|missing/);
  const btns = dead.flatMap(all).filter((n) => n.tag === "button" && /signing/.test(n.textContent));
  assert.deepEqual(btns.map((b) => b.textContent), ["Set up signing again"]);
  btns[0].listeners.click(); assert.equal(called, 1);
  const fresh = textOf(mk("unregistered"));
  assert.match(fresh, /One step: confirm with your password and a code\./);
  assert.doesNotMatch(fresh, /removed|missing|no longer works|Forget/);
});

test("P07: Shared tickets card lists tickets shared with me with workspace and who shared; hidden when none", () => {
  const ws = [{ workspace_id: "w1", name: "Product", online: true, mode: "selective" }];
  const textOf = (out) => out.flat().map((n) => n.textContent).join("\n");
  const none = homeView({ workspaces: ws, items: [], shared: [], actions: {} });
  assert.doesNotMatch(textOf(none), /Shared with you/);
  let opened;
  const out = homeView({ workspaces: ws, items: [], actions: { openShared: (it) => { opened = it.workspace_id + "/" + it.key; } },
    shared: [{ workspace_id: "w1", key: "PW-1", title: "Invoice CSV export", shared_by: "Anna" }] });
  const t = textOf(out);
  assert.match(t, /Shared with you \(1\)/);
  assert.match(t, /Invoice CSV export/);
  assert.match(t, /Product · Shared by Anna/);
  const all = (n) => [n, ...n.children.flatMap(all)];
  const open = out.flatMap(all).filter((n) => n.tag === "button" && n.textContent === "Open" && n.listeners.click);
  open[0].listeners.click();
  assert.equal(opened, "w1/PW-1");
});

test("B08: dismiss uses the server and no previous-read memory survives reload", async () => {
  let called;
  await dismissEnded(async (...args) => { called = args; }, { workspace_id: "w1", item_id: "i1" });
  assert.deepEqual(called, ["POST", "/me/shared/ended/dismiss", { workspace_id: "w1", item_id: "i1" }]);
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  assert.doesNotMatch(src, /sharedMemory|trackShared|loadEnded/);
  assert.match(src, /sharedRes.body.ended/);
});

test("P04: ended lines show in the Shared tickets card (even with nothing shared) and Dismiss clears them", () => {
  const textOf = (out) => out.flat().map((n) => n.textContent).join("\n");
  const ws = [{ workspace_id: "w1", name: "Product", online: true, mode: "selective" }];
  let dismissed;
  const out = homeView({ workspaces: ws, items: [], shared: [], ended: [{ workspace_id: "w1", item_id: "i1", by: "Anna", title: "Invoice export" }], actions: { dismissEnded: (id) => { dismissed = id; } } });
  assert.match(textOf(out), /Anna stopped sharing Invoice export/);
  const all = (n) => [n, ...n.children.flatMap(all)];
  out.flatMap(all).find((n) => n.tag === "button" && n.textContent === "Dismiss").listeners.click();
  assert.equal(dismissed.item_id, "i1");
  assert.doesNotMatch(textOf(homeView({ workspaces: ws, items: [], shared: [], ended: [], actions: {} })), /Shared with you/);
});

test("run18 P04: Home calls a stored key dead only when it is this person's own; another person's key is never classed or forgotten", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  const line = src.split("\n").find((l) => l.includes("const dead = "));
  assert.match(line, /!mine && state\.me\?\.userId === user\.user_id;/);
  assert.ok(!src.includes("forgetLocal"), "no Forget path: setting up again replaces the old key");
});

const { workspacesView } = await import("../../src/orch_hub/web/workspaces.js");
test("workspaces without own rows show each shared ticket and retain the original empty state otherwise", () => {
  const shared = [{ workspace_id: "a", key: "A-1", title: "Invoice CSV export", shared_by: "Anna Test" }, { workspace_id: "b", key: "B-1", title: "Another ticket", shared_by: "Carla" }];
  const opened = [];
  const data = { workspaces: [], handoffs: [], shared, actions: { openShared: (it) => opened.push(it) } };
  const out = workspacesView(data);
  const text = out.map((n) => n.textContent).join(" ");
  assert.match(text, /No workspaces of your own yet.*Shared with you \(2\)/);
  assert.doesNotMatch(text, /Connected workspaces|tickets shared with you/);
  assert.match(text, /Shared by Anna Test/);
  const buttons = out.flatMap((n) => n.all()).filter((n) => n.tag === "button" && n.textContent === "Open");
  assert.equal(buttons.length, 2);
  buttons.forEach((b) => b.listeners.click());
  assert.deepEqual(opened, shared);
  assert.match(workspacesView({ ...data, shared: shared.slice(0, 1) }).map((n) => n.textContent).join(" "), /Shared with you \(1\)/);
  assert.match(workspacesView({ ...data, shared: [] }).map((n) => n.textContent).join(" "), /No workspaces yet/);
});
