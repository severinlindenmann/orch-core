// P13: the /c page turns a valid connect fragment into a deep link to the workspace and refuses anything else.
import test from "node:test";
import assert from "node:assert/strict";

const { parseFragment, canonicalLink, deepLink, cleanOrigin, DEFAULT_DASHBOARD, knownDashboard, rememberDashboard } =
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

test("the remembered dashboard address falls back to the local default and ignores bad stored values", () => {
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  assert.equal(knownDashboard(), DEFAULT_DASHBOARD);
  rememberDashboard("javascript:alert(1)");
  assert.equal(knownDashboard(), DEFAULT_DASHBOARD);
  rememberDashboard("http://127.0.0.1:50123/some/path");
  assert.equal(knownDashboard(), "http://127.0.0.1:50123");
  store.set("orch.dashboard_origin", "javascript:alert(1)");
  assert.equal(knownDashboard(), DEFAULT_DASHBOARD);
  globalThis.localStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.equal(knownDashboard(), DEFAULT_DASHBOARD);
  rememberDashboard("http://127.0.0.1:1");
  delete globalThis.localStorage;
});
