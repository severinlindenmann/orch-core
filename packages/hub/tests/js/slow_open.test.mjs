// run17 P05: the hub never sits on a bare "Loading…". It says "Opening your hub…", and after 5 s without an answer it
// says "Taking longer than usual" with Try again and Sign in. Calls to the hub give up after 10 s.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { API_TIMEOUT, SLOW_AFTER, createSlowWatch } from "../../src/orch_hub/web/feel.js";

const source = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../../src/orch_hub/web/index.html", import.meta.url), "utf8");

test("the first paint says what it is doing, never a bare Loading…", () => {
  assert.match(html, /<h1>Opening your hub…<\/h1>/);
  assert.doesNotMatch(html, />Loading…</);
  assert.equal(SLOW_AFTER, 5000);
  assert.equal(API_TIMEOUT, 10000);
});

test("a call that never answers: the watch fires once after 5 s, only while the page is still opening", () => {
  let fn, delay, opening = true, slow = 0;
  const w = createSlowWatch(() => opening, () => { slow++; }, (f, ms) => { fn = f; delay = ms; return 1; }, () => { fn = null; });
  w.start();
  assert.equal(delay, 5000);
  fn();
  assert.equal(slow, 1);
  w.start(); opening = false; fn();
  assert.equal(slow, 1); // a page replaced the card in time: nothing to say
  w.start(); w.cancel();
  assert.equal(fn, null); // an answer in time cancels it
});

test("api() gives up on a silent hub as 'unavailable' (the timeout aborts the fetch)", async () => {
  let abort;
  const timers = [];
  const fetch = (_url, init) => new Promise((_res, rej) => { init.signal.addEventListener("abort", () => rej(new Error("aborted"))); abort = init.signal; });
  const body = source.slice(source.indexOf("const inflight = new Set()"), source.indexOf("const WORDS = {"));
  let failed = 0;
  const c = vm.createContext({ fetch, AbortController, API_TIMEOUT, connectionFailed() { failed++; }, TextDecoder, parseStrict: JSON.parse,
    setTimeout: (f, ms) => { timers.push({ f, ms }); return timers.length; }, clearTimeout() {} });
  vm.runInContext(body, c);
  const pending = c.api("GET", "/switch/peek");
  assert.equal(timers[0].ms, 10000);
  timers[0].f();
  assert.equal(JSON.stringify(await pending), JSON.stringify({ status: 0, body: { error: "unavailable" } }));
  assert.equal(abort.aborted, true);
  assert.equal(failed, 1);
  c.api("POST", "/services/x/stop"); // actions that may take long get longer than a read
  assert.equal(timers[1].ms, 60000);
});

test("the slow card has the words, Try again and Sign in; the ticket landing shows the opening card while peek runs", () => {
  assert.match(source, /"Taking longer than usual"/);
  assert.match(source, /data-retry": "slow", text: "Try again"/);
  assert.match(source, /text: "Sign in with your password"/);
  const land = source.slice(source.indexOf("async function landFromTicket("), source.indexOf("// A session that came from a workspace ticket"));
  assert.ok(land.indexOf("show(openingCard())") > 0 && land.indexOf("show(openingCard())") < land.indexOf('"/switch/peek"'));
});
