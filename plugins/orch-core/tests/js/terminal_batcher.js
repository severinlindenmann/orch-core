// Unit test for makeBatcher in static/terminal.js (plain node): keys leave in typed order, one post at a time, each
// numbered with a rising n; a post that did not arrive (status 0) or was rate limited (429) is sent again unchanged
// with the same n before anything newer; any other answer ends it.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(process.argv[2], "utf8");
const m = src.match(/\/\/ batcher:begin\n([\s\S]*?)\/\/ batcher:end/);
assert(m, "batcher markers");
const make = vm.runInNewContext("(() => {" + m[1] + "; return makeBatcher; })()", { Date, Math, setTimeout, Promise, Error });

const rig = (answers) => {
  const timers = [];
  const sent = [];
  let clock = 1000;
  const b = make({ ms: 750, now: () => clock, timer: (fn, ms) => { assert.strictEqual(ms, 750); timers.push(fn); },
    send: async (items, n) => { sent.push({ items: JSON.parse(JSON.stringify(items)), n }); return answers.length ? answers.shift() : 204; } });
  const tick = async () => { const t = timers.splice(0); for (const fn of t) await fn(); };
  return { b, sent, tick, timers, setClock: (c) => { clock = c; } };
};

(async () => {
  // typed order is kept, text coalesces, a key splits it, and one flush carries the lot
  let r = rig([]);
  for (const i of [{ text: "a" }, { text: "b" }, { key: "Enter" }, { text: "c" }]) r.b.push(i);
  assert.strictEqual(r.timers.length, 1, "one timer for the whole burst");
  await r.tick();
  assert.deepStrictEqual(r.sent.map((s) => s.items), [[{ text: "ab" }, { key: "Enter" }, { text: "c" }]]);

  // numbers rise, and never fall below the clock
  r = rig([]);
  r.b.push({ text: "x" }); await r.tick();
  r.b.push({ text: "y" }); await r.tick();
  assert.strictEqual(r.sent[0].n, 1000);
  assert.strictEqual(r.sent[1].n, 1001);
  r.setClock(5000);
  r.b.push({ text: "z" }); await r.tick();
  assert.strictEqual(r.sent[2].n, 5000);

  // a lost post and a rate-limited one are sent again unchanged, ahead of newer keys, with the same number
  r = rig([0, 429, 204, 204]);
  r.b.push({ text: "one" }); await r.tick();
  r.b.push({ text: "two" });  // typed while the first is unconfirmed
  await r.tick();
  await r.tick();
  await r.tick();
  assert.deepStrictEqual(r.sent.map((s) => [s.items, s.n]), [
    [[{ text: "one" }], 1000], [[{ text: "one" }], 1000], [[{ text: "one" }], 1000], [[{ text: "two" }], 1001]]);

  // a refusal that is not retryable (409, 400) ends that post; the next one goes on
  r = rig([409, 204]);
  r.b.push({ text: "dup" }); await r.tick();
  r.b.push({ text: "next" }); await r.tick();
  assert.deepStrictEqual(r.sent.map((s) => s.items[0].text), ["dup", "next"]);

  // one post in flight at a time
  let release;
  const timers = [];
  const sent = [];
  const b = make({ ms: 750, now: () => 1, timer: (fn) => timers.push(fn),
    send: (items, n) => { sent.push(n); return new Promise((res) => { release = () => res(204); }); } });
  b.push({ text: "a" });
  const first = timers.shift()();
  b.push({ text: "b" });
  await timers.shift()();  // the second timer fires while the first post is still out
  assert.strictEqual(sent.length, 1);
  release(); await first;
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
