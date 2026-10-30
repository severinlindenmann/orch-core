// Left workspaces are hidden by default and can be removed with confirmation.
// admin.js builds real DOM, so this runs it against a tiny DOM stub (no browser needed).
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); } // like the DOM: non-nodes become text, so null -> "null"
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  focus() {} select() {}
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  get tagName() { return this.tag.toUpperCase(); }
  querySelectorAll() { return this.all().slice(1).filter(n => ["input", "select", "textarea", "button", "summary", "details"].includes(n.tag)); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = {
  createElement: (tag) => new Node_(tag),
  createTextNode: (t) => new Node_("#text", t),
  getElementById: () => null,
};
const { adminView, endOfDayMs, endsWords } = await import("../../src/orch_hub/web/admin.js");

const DAY = 86_400_000;
const U1 = "01".repeat(16);
const bodies = () => ({
  users: { users: [{ user_id: U1, name: "Sam", groups: ["temps"], disabled: false }] },
  groups: { groups: [{ name: "temps", members: [U1], ends: { [U1]: Date.now() + 3 * DAY }, grants: [] }], default_group: "temps" },
  grants: { grants: [] }, workspaces: { workspaces: [] }, channels: { channels: [] },
  "market-policy": { version: 1, official_url: "https://x.test/o", policy: { markets: [], addons: { allow: null, block: [] }, ceiling: null } },
  audit: { records: [] },
  review: { items: [{ group: "temps", user_id: U1, name: "Sam", ends_ms: Date.now() + 3 * DAY - 1000, ended: false }] },
});
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };

test("an end date is the start of the NEXT local day, so the whole picked day still counts", () => {
  const ends = endOfDayMs("2026-11-14");
  assert.equal(ends, new Date(2026, 10, 15).getTime());
  assert.equal(endOfDayMs("2026-12-31"), new Date(2027, 0, 1).getTime());
  assert.equal(endOfDayMs(""), null);
  assert.equal(endOfDayMs("14 Nov"), null);
});

test("plain words for the review list", () => {
  const now = 1_000_000_000_000;
  assert.equal(endsWords(now + 3 * DAY - 1000, now), "ends in 3 days");
  assert.equal(endsWords(now + 1000, now), "ends today or tomorrow");
  assert.equal(endsWords(now - 2 * DAY - 5, now), "ended 2 days ago");
  assert.equal(endsWords(now - 5, now), "ended today");
});

test("Review access lists the person with Still needed / Remove, and one click asks the hub", async () => {
  const calls = [];
  const b = bodies();
  const d = { say: (c) => c, refresh: async () => {}, api: async (method, path, body) => {
    calls.push([method, path, body]);
    if (method !== "GET") return { status: 200, body: { ok: true } };
    return { status: 200, body: b[path.replace("/admin/", "").split("?")[0]] };
  } };
  const [, , , panels] = adminView(d);
  await settle();
  const access = panels.children[1];
  assert.match(access.textContent, /Review access/);
  assert.match(access.textContent, /Sam/);
  assert.match(access.textContent, /temps · ends in 3 days/);
  const keep = access.all().find((n) => n.tag === "button" && n.textContent === "Still needed");
  keep.listeners.click();
  await settle();
  assert.deepEqual(calls.filter(([m]) => m === "POST"), [["POST", "/admin/review/keep", { group: "temps", user_id: U1 }]]);
});

test("the add-to-group form takes an optional end day; groups show who is dated; default group is chosen in plain words", async () => {
  const calls = [];
  const b = bodies();
  const d = { say: (c) => c, refresh: async () => {}, api: async (method, path, body) => {
    calls.push([method, path, body]);
    if (method !== "GET") return { status: 200, body: { ok: true } };
    return { status: 200, body: b[path.replace("/admin/", "").split("?")[0]] };
  } };
  const [, , , panels] = adminView(d);
  await settle();
  const people = panels.children[0];
  assert.match(people.textContent, /Access ends \(optional\)/);
  assert.match(people.textContent, /Sam · until /);
  assert.match(people.textContent, /New people join/);
  assert.match(people.textContent, /never become administrators/);
  const date = people.all().find((n) => n.attrs.id === "member-ends");
  date.value = "2026-11-14";
  const form = people.all().find((n) => n.tag === "form" && n.all().some((x) => x === date));
  await form.listeners.submit({ preventDefault() {}, submitter: null });
  await settle();
  const put = calls.find(([m, path]) => m === "PUT" && path.includes("/members/"));
  assert.ok(put, JSON.stringify(calls));
  assert.deepEqual(put[2], { ends_ms: new Date(2026, 10, 15).getTime() });
  const save = people.all().find((n) => n.tag === "button" && n.textContent === "Save");
  save.listeners.click();
  await settle();
  assert.deepEqual(calls.filter(([m, path]) => m === "PUT" && path === "/admin/default-group").map((c) => c[2]), [{ group: "temps" }]);
});
