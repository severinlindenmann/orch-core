// P13: the /c page turns a valid connect fragment into a deep link to the workspace and refuses anything else.
import test from "node:test";
import assert from "node:assert/strict";

const { parseFragment, canonicalLink, deepLink, cleanOrigin, loopbackOrigin, knownDashboard, rememberDashboard, rememberName, knownName, pageModel } =
  await import("../../src/orch_hub/web/connect.js");

const HUB = "9f3c1ab277de40c8aa11bb22cc33dd44", OWNER = "0123456789abcdef0123456789abcdef";
const ORIGIN = "http://hub.test:8106";

test("a valid fragment is parsed with and without endpoint and owner", () => {
  assert.deepEqual(parseFragment(`#h1.${HUB}.7QK2M9XD`), { hubId: HUB, code: "7QK2M9XD", endpoint: null, ownerUser: null });
  assert.deepEqual(parseFragment(`#h1.${HUB}.7QK2M9XD.10.0.0.5:8105~${OWNER}`), { hubId: HUB, code: "7QK2M9XD", endpoint: "10.0.0.5:8105", ownerUser: OWNER });
  assert.equal(parseFragment(`#h1.${HUB}.abc.local`).endpoint, "local");
  assert.equal(parseFragment(`#h1.${HUB}.abc.[fe80::1]:8105`).endpoint, "[fe80::1]:8105");
});

test("anything that does not fully match is refused", () => {
  const bad = ["", "h1." + HUB + ".abc", "#", "#h2." + HUB + ".abc", "#h1.XYZ.abc", `#h1.${HUB.toUpperCase()}.abc`, `#h1.${HUB}.`, `#h1.${HUB}.a.b`,
    `#h1.${HUB}.${"a".repeat(65)}`, `#h1.${HUB}.abc.host`, `#h1.${HUB}.abc.host:80 evil`, `#h1.${HUB}.abc~short`, `#h1.${HUB}.abc~${OWNER}~${OWNER}`,
    `#h1.${HUB}.abc.<img>:1`, `#h1.${HUB}.ab+c`, null, undefined, 5];
  for (const hash of bad) assert.equal(parseFragment(hash), null, String(hash));
});

test("the deep link is rebuilt from the parsed parts, never from the raw hash", () => {
  const p = parseFragment(`#h1.${HUB}.7QK2M9XD.10.0.0.5:8105~${OWNER}`);
  const link = canonicalLink(ORIGIN, p);
  assert.equal(link, `${ORIGIN}/c#h1.${HUB}.7QK2M9XD.10.0.0.5:8105~${OWNER}`);
  const deep = deepLink("http://127.0.0.1:47821/ignored/path?x=1", link);
  assert.equal(deep, "http://127.0.0.1:47821/settings/hub#connect=" + encodeURIComponent(link));
  assert.equal(new URL(deep).searchParams.has("x"), false);
});

test("only plain http(s) origins without credentials are used for the workspace address", () => {
  assert.equal(cleanOrigin("https://ws.example:8443/x"), "https://ws.example:8443");
  for (const bad of ["javascript:alert(1)", "data:text/html,x", "ftp://x", "http://user:pw@host", "not a url", "", null, 7]) assert.equal(cleanOrigin(bad), null, String(bad));
  assert.equal(deepLink("javascript:alert(1)", "x"), null);
});

const ANNA = "a".repeat(32), BEN = "b".repeat(32);
const memory = () => { const store = new Map(); globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) }; return store; };

test("r11 P03: the dashboard is remembered per person; nothing remembered means no target (no default port)", () => {
  memory();
  assert.equal(knownDashboard(ANNA), null);
  assert.equal(knownDashboard(null), null);
  rememberDashboard("javascript:alert(1)", ANNA);
  assert.equal(knownDashboard(ANNA), null);
  rememberDashboard("http://127.0.0.1:8151/some/path", ANNA);
  rememberDashboard("http://127.0.0.1:47821/", BEN);
  assert.equal(knownDashboard(ANNA), "http://127.0.0.1:8151");
  assert.equal(knownDashboard(BEN), "http://127.0.0.1:47821");
  rememberDashboard("http://127.0.0.1:9000", "not-a-user");
  rememberDashboard("http://127.0.0.1:9000", undefined);
  assert.equal(knownDashboard(ANNA), "http://127.0.0.1:8151", "no owner never overwrites anyone");
  globalThis.localStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.equal(knownDashboard(ANNA), null);
  rememberDashboard("http://127.0.0.1:1", ANNA);
  delete globalThis.localStorage;
});

test("r11 P03: two loopback dashboards on one computer, each person's link opens only their own", () => {
  memory();
  rememberDashboard("http://127.0.0.1:8151", ANNA);
  rememberDashboard("http://127.0.0.1:47821", BEN); // Ben's opened last: must not win for Anna
  const forAnna = pageModel(`#h1.${HUB}.CODEA~${ANNA}`, ORIGIN, "hub.test:8106");
  const forBen = pageModel(`#h1.${HUB}.CODEB~${BEN}`, ORIGIN, "hub.test:8106");
  assert.equal(new URL(forAnna.open).origin, "http://127.0.0.1:8151");
  assert.equal(new URL(forBen.open).origin, "http://127.0.0.1:47821");
  assert.ok(forAnna.open.endsWith("#connect=" + encodeURIComponent(`${ORIGIN}/c#h1.${HUB}.CODEA~${ANNA}`)));
  assert.ok(!forAnna.open.includes("CODEB") && !forBen.open.includes("CODEA"));
  delete globalThis.localStorage;
});

test("r11 P03: a link for someone not remembered here, or naming nobody, offers only Copy", () => {
  memory();
  rememberDashboard("http://127.0.0.1:47821", BEN);
  rememberName(ANNA, "Anna");
  const m = pageModel(`#h1.${HUB}.CODEA~${ANNA}`, ORIGIN, "hub.test:8106");
  assert.equal(m.open, null, "Ben's dashboard is never a target for Anna's link");
  assert.equal(m.title, "Connect Anna's workspace");
  assert.equal(m.lead, "Open this page on Anna's computer, then press the button.");
  assert.equal(m.link, `${ORIGIN}/c#h1.${HUB}.CODEA~${ANNA}`);
  const nobody = pageModel(`#h1.${HUB}.CODEX`, ORIGIN, "hub.test:8106");
  assert.equal(nobody.open, null, "a link without an owner never goes to any remembered dashboard");
  assert.equal(nobody.title, "Connect your workspace to hub.test:8106");
  assert.equal(pageModel("#garbage", ORIGIN, "x"), null);
  delete globalThis.localStorage;
});

test("r11 P03: names are display text from the hub, bounded, and never a target", () => {
  memory();
  rememberName(ANNA, "x".repeat(81));
  assert.equal(knownName(ANNA), null);
  rememberName(ANNA, " Anna <b> ");
  assert.equal(knownName(ANNA), "Anna <b>");
  assert.equal(knownDashboard(ANNA), null);
  assert.equal(knownName("__proto__"), null);
  delete globalThis.localStorage;
});

test("a non-loopback origin (e.g. reported by a workspace host) is never remembered or used", () => {
  const store = memory();
  for (const bad of ["https://evil.example", "http://10.0.0.5:47821", "http://127.0.0.1.evil.example", "http://localhost.evil.example"]) {
    rememberDashboard(bad, ANNA);
    assert.equal(store.size, 0, bad);
    assert.equal(deepLink(bad, "x"), null, bad);
    store.set("orch.connect_people", JSON.stringify({ [ANNA]: { o: bad } }));
    assert.equal(knownDashboard(ANNA), null, bad);
    assert.equal(pageModel(`#h1.${HUB}.C~${ANNA}`, ORIGIN, "h").open, null, bad);
    store.clear();
  }
  for (const ok of ["http://127.0.0.1:5", "http://localhost:3000", "http://[::1]:47821"]) assert.ok(loopbackOrigin(ok), ok);
  delete globalThis.localStorage;
});
