// M4-1: the Hub settings page (#1460) against a tiny DOM stub. Text only: no HTML sink, no button that does nothing.
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
const { hubSettingsView, hubSettingsPage, page } = await import("../../src/orch_hub/web/pages/hub-settings.js");

// the demo seed (the mockup's Acme AG hub)
export const DEMO = {
  configured: true, name: "Acme Hub", company: "Acme AG", fixed_at_install: true,
  addresses: [{ kind: "private", web: "http://100.71.188.237:8100" }, { kind: "server", web: "http://127.0.0.1:8100" }, { kind: "office", web: "http://192.168.60.30:8100" }],
  sign_in: [
    { id: "password", label: "Name + password + 6-digit code", note: "For everyone", on: true },
  ],
};

test("the page registers as hub-settings for admins (settings is a route of orch's own)", () => {
  assert.equal(page.path, "hub-settings"); assert.equal(page.nav.admin, true); assert.equal(typeof page.render, "function");
});

test("the read card shows name, company, every address in plain words and the sign-in rows", () => {
  const t = hubSettingsView(DEMO).textContent;
  for (const s of ["Acme Hub", "Acme AG", "Private network (VPN)", "http://100.71.188.237:8100", "On this server", "on the server itself, use this one", "Office network", "http://192.168.60.30:8100", "chosen when the hub was installed", "Name + password + 6-digit code"]) assert.ok(t.includes(s), s);
  assert.match(t, /Public address\s*Not set up/);          // a kind without an address says so
  assert.doesNotMatch(t, /NetBird|WireGuard|Tailscale/i);   // owner rule: never a VPN product name
});

test("nothing on the page can be clicked: no button, link, input or listener", () => {
  const nodes = hubSettingsView(DEMO).all();
  assert.deepEqual(nodes.filter((n) => ["button", "a", "input", "select", "textarea"].includes(n.tag)), []);
  assert.deepEqual(nodes.filter((n) => Object.keys(n.listeners).length), []);
});

test("text from the hub is shown as text, not markup", () => {
  const view = hubSettingsView({ ...DEMO, name: "<img src=x onerror=alert(1)>", company: "a‮b" });
  assert.ok(view.textContent.includes("<img src=x onerror=alert(1)>"));
  assert.ok(!view.textContent.includes("‮"));
  assert.ok(view.all().every((n) => n.tag !== "img"));
});

test("a hub started by hand says there is no settings file", () => {
  const t = hubSettingsView({ configured: false, problem: null }).textContent;
  assert.match(t, /no settings file/);
  assert.match(hubSettingsView({ configured: false, problem: "The settings file cannot be read." }).textContent, /cannot be read/);
});

test("the page asks the admin route, sends non-admins home and shows an error in words", async () => {
  const calls = []; const shown = [];
  const d = (user, api) => ({ pageLoading() {}, refresh: async () => true, state: { user }, loadRail: async () => {}, say: (e) => "Why: " + e,
    api, shell: (id, ...n) => shown.push([id, n.map((x) => x.textContent).join("")]), home: () => "home", loginView: () => "login", reauthView: () => "reauth" });
  assert.equal(await hubSettingsPage(d({ is_admin: false }, async () => { throw new Error("no call"); })), "home");
  await hubSettingsPage(d({ is_admin: true }, async (m, p) => { calls.push([m, p]); return { status: 200, body: DEMO }; }));
  assert.deepEqual(calls, [["GET", "/admin/settings"]]);
  assert.equal(shown[0][0], "hub-settings"); assert.match(shown[0][1], /Acme Hub/);
  await hubSettingsPage(d({ is_admin: true }, async () => ({ status: 500, body: { error: "unavailable" } })));
  assert.match(shown[1][1], /Cannot read the settings\. Why: unavailable/);
});
