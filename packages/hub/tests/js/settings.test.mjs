// M4 (#1460 #1461): the Hub settings page against a tiny DOM stub. Text only: no HTML sink, no button that does nothing.
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { body: new Node_("body"), createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { hubSettingsView, hubSettingsPage, addressBody, hostOf, page } = await import("../../src/orch_hub/web/pages/hub-settings.js");

// the demo seed (the mockup's Acme AG hub)
export const DEMO = {
  configured: true, name: "Acme Hub", company: "Acme AG", phone_kind: "private", can_change_addresses: true, restarts: true,
  addresses: [{ kind: "private", web: "http://100.71.188.237:8100" }, { kind: "server", web: "http://127.0.0.1:8100" }, { kind: "office", web: "http://192.168.60.30:8100" }],
  sign_in: [
    { id: "password", label: "Name + password + 6-digit code", note: "For everyone", on: true },
  ],
};
const DETECTED = [{ ip: "127.0.0.1", kind: "server" }, { ip: "100.71.188.237", kind: "private" }, { ip: "192.168.60.30", kind: "office" }, { ip: "10.1.1.4", kind: "office" }];
const byTag = (view, tag) => view.all().filter((n) => n.tag === tag);
const card = (view, id) => view.all().find((n) => n.attrs["data-card"] === id);
const button = (root, label) => root.all().find((n) => n.tag === "button" && n.textContent === label);

test("the page registers as hub-settings for admins (settings is a route of orch's own)", () => {
  assert.equal(page.path, "hub-settings"); assert.equal(page.nav.admin, true); assert.equal(typeof page.render, "function");
});

test("the cards show name, company, every address in plain words and the sign-in rows", () => {
  const t = hubSettingsView(DEMO).textContent;
  for (const s of ["Hub name", "Company", "Private network (VPN)", "http://100.71.188.237:8100", "phones use this one", "On this server", "on the server itself, use this one", "Office network", "http://192.168.60.30:8100", "Name + password + 6-digit code"]) assert.ok(t.includes(s), s);
  assert.match(t, /Public address\s*Not set up/);          // a kind without an address says so
  assert.doesNotMatch(t, /NetBird|WireGuard|Tailscale/i);   // owner rule: never a VPN product name
});

test("General: the two fields hold the current words and ONE Save sends both (no confirm word)", () => {
  const sent = [];
  const view = hubSettingsView(DEMO, { saveGeneral: (b) => sent.push(b) });
  const g = card(view, "general");
  const [name, company] = byTag(g, "input");
  assert.equal(name.value, "Acme Hub"); assert.equal(company.value, "Acme AG");
  assert.deepEqual(byTag(g, "button").map((b) => b.textContent), ["Save"]);
  name.value = "Beta Hub"; company.value = "";
  button(g, "Save").listeners.click();
  assert.deepEqual(sent, [{ name: "Beta Hub", company: "" }]);
});

test("Address: the help says workspaces pick the new address up when they connect; the phone address is not editable", () => {
  const view = hubSettingsView(DEMO, { detected: DETECTED });
  const a = card(view, "address");
  assert.match(a.textContent, /Workspaces pick up the new address the next time they connect/);
  assert.equal(a.all().find((n) => n.attrs["data-kind"] === "private").tag, "div");
  assert.ok(!byTag(a.all().find((n) => n.attrs["data-kind"] === "private"), "select").length);   // the phone address
  assert.match(a.all().find((n) => n.attrs["data-kind"] === "private").textContent, /It stays as it is/);
  const office = byTag(a.all().find((n) => n.attrs["data-kind"] === "office"), "select")[0];
  assert.deepEqual(office.children.map((o) => o.textContent), ["Not used", "192.168.60.30", "10.1.1.4"]);   // only this kind's own addresses
  assert.equal(office.value, "192.168.60.30");
});

test("Address: Change address opens the picker and asks for this server's addresses once", () => {
  let asked = 0;
  const view = hubSettingsView(DEMO, { loadDetected: () => asked++ });
  const a = card(view, "address");
  const open = button(a, "Change address");
  const form = a.all().find((n) => n.className === "address-form");
  assert.ok("hidden" in form.attrs);
  open.listeners.click();
  assert.equal(form.hidden, false); assert.equal(asked, 1);
});

test("Address: Save sends the phone address first, the chosen ones after it, and leaves a cleared kind out", () => {
  const sent = [];
  const view = hubSettingsView(DEMO, { detected: DETECTED, saveAddresses: (b) => sent.push(b) });
  const a = card(view, "address");
  const sel = (kind) => byTag(a.all().find((n) => n.attrs["data-kind"] === kind), "select")[0];
  sel("office").value = "10.1.1.4"; sel("server").value = "";
  assert.deepEqual(byTag(a, "input"), []);   // no field for the public address: it is set up with the edge
  button(a, "Save").listeners.click();
  assert.deepEqual(sent, [{ addresses: [
    { kind: "private", host: "100.71.188.237" }, { kind: "office", host: "10.1.1.4" }] }]);
});

test("Address: a hub that only answers on this server says plainly who to ask instead of showing a picker", () => {
  const view = hubSettingsView({ ...DEMO, can_change_addresses: false });
  const a = card(view, "address");
  assert.deepEqual(byTag(a, "button"), []); assert.deepEqual(byTag(a, "select"), []);
  assert.match(a.textContent, /Addresses can't be changed on this page\. To add or change one, ask the person who installed this hub\./);
});

test("addressBody and hostOf: the phone address is kept as it is, blanks are dropped, IPv6 loses its brackets", () => {
  assert.equal(hostOf("http://[::1]:8100"), "::1"); assert.equal(hostOf("javascript:alert(1)"), "");
  const d = { phone_kind: "public", addresses: [{ kind: "public", web: "https://hub.example.com:8100" }, { kind: "server", web: "http://127.0.0.1:8100" }] };
  assert.deepEqual(addressBody(d, { server: "127.0.0.1", office: "  ", public: "other.example" }),
    { addresses: [{ kind: "public", host: "hub.example.com", scheme: "https" }, { kind: "server", host: "127.0.0.1" }] });
  assert.deepEqual(addressBody(DEMO, { public: "x.example", office: "10.1.1.4" }).addresses.map((e) => e.kind), ["private", "office"]);
});

test("text from the hub is shown as text, not markup", () => {
  const view = hubSettingsView({ ...DEMO, name: "<img src=x onerror=alert(1)>", company: "a\u202eb" });
  const [name, company] = byTag(card(view, "general"), "input");
  assert.equal(name.value, "<img src=x onerror=alert(1)>"); assert.ok(!company.value.includes("\u202e"));
  assert.ok(view.all().every((n) => n.tag !== "img"));
});

test("a hub started by hand says there is no settings file", () => {
  const t = hubSettingsView({ configured: false, problem: null }).textContent;
  assert.match(t, /no settings file/);
  assert.match(hubSettingsView({ configured: false, problem: "The settings file cannot be read." }).textContent, /cannot be read/);
});

const dev = (user, api, extra = {}) => ({ pageLoading() {}, refresh: async () => true, state: { user }, loadRail: async () => {}, say: (e) => "Why: " + e,
  banner: (kind, title) => kind + ":" + title, restartPoll: { tries: 3, pause: 1 }, api,
  shell: (id, ...n) => (extra.shown ??= []).push([id, n.map((x) => (x && x.textContent) ?? String(x)).join("")]), home: () => "home", loginView: () => "login", reauthView: () => "reauth", ...extra });

test("the page asks the admin route, sends non-admins home and shows an error in words", async () => {
  const calls = []; const shown = [];
  assert.equal(await hubSettingsPage(dev({ is_admin: false }, async () => { throw new Error("no call"); })), "home");
  await hubSettingsPage(dev({ is_admin: true }, async (m, p) => { calls.push([m, p]); return { status: 200, body: DEMO }; }, { shown }));
  assert.deepEqual(calls, [["GET", "/admin/settings"], ["GET", "/admin/backup"]]);   // M4 S5: the Backup card has its own read
  assert.equal(shown[0][0], "hub-settings"); assert.match(shown[0][1], /Hub name/);
  await hubSettingsPage(dev({ is_admin: true }, async () => ({ status: 500, body: { error: "unavailable" } }), { shown }));
  assert.match(shown[1][1], /Cannot read the settings\. Why: unavailable/);
});

test("saving the name puts to the general route and shows the page again; a refusal is said in words", async () => {
  const calls = []; let shownView = null; const d0 = {};
  const d = dev({ is_admin: true }, async (m, p, b) => { calls.push([m, p, b]); return m === "PUT" ? { status: 400, body: { error: "malformed" } } : { status: 200, body: DEMO }; },
    { shell: (id, notice, view) => { shownView = view; d0.notice = notice; } });
  await hubSettingsPage(d);
  const [name] = byTag(card(shownView, "general"), "input"); name.value = "";
  await button(card(shownView, "general"), "Save").listeners.click();
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(calls.filter((c) => c[0] === "PUT"), [["PUT", "/admin/settings/general", { name: "", company: "Acme AG" }]]);
  assert.equal(d0.notice, "bad:Not changed");   // the refusal is shown on the page that comes back
});

test("an address change that restarts the hub waits until the hub answers again", async () => {
  const calls = []; let shownView = null; let polls = 0;
  const d = dev({ is_admin: true }, async (m, p, b) => {
    calls.push([m, p]);
    if (m === "PUT") return { status: 202, body: { restarting: true } };
    if (p === "/hub/card") return ++polls < 2 ? { status: 503, body: {} } : { status: 200, body: {} };
    return { status: 200, body: DEMO };
  }, { shell: (id, notice, view) => { shownView = view; } });
  await hubSettingsPage(d, DETECTED);
  await button(card(shownView, "address"), "Save").listeners.click();
  // wait for the condition (not a fixed sleep): the restart poll pauses 1 ms between tries; give it up to 5 s on a loaded machine
  for (const deadline = Date.now() + 5000; polls < 2 && Date.now() < deadline;) await new Promise((r) => setTimeout(r, 5));
  assert.ok(calls.some((c) => c[0] === "PUT" && c[1] === "/admin/settings/addresses"));
  assert.ok(polls >= 2);
});
