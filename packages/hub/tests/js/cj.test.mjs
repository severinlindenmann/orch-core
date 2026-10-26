// cj.js against the hub's own vectors (vectors/vectors_hub.json) and the A8-9 refusals.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CanonError, b64u, cj, hex, parseStrict, sha256, unb64u, utf8 } from "../../src/orch_hub/web/cj.js";
import { contextDigest } from "../../src/orch_hub/web/render.js";

const V = JSON.parse(readFileSync(new URL("../../vectors/vectors_hub.json", import.meta.url)));

test("cj matches RFC 8785 on the subset", () => {
  assert.equal(cj({ b: [1, "x", null, true], a: { z: -5, y: "é\n\u0001" } }), '{"a":{"y":"é\\n\\u0001","z":-5},"b":[1,"x",null,true]}');
  assert.equal(cj(-0), "0");
});

test("cj refuses floats, big integers, lone surrogates, odd keys, deep nesting", () => {
  for (const bad of [1.5, 2 ** 53, -(2 ** 53), NaN, Infinity, "\ud800", "a\udc00b", { "é": 1 }, { "": 1 }, undefined, () => 1, new Date(), 10n]) {
    assert.throws(() => cj(bad), CanonError, String(bad));
  }
  let deep = 1;
  for (let i = 0; i < 16; i++) deep = [deep];
  assert.doesNotThrow(() => cj(deep));
  assert.throws(() => cj([deep]), CanonError);
});

test("parseStrict refuses what JSON.parse would round, merge or accept", () => {
  for (const bad of [
    "1.0", "1e3", "9007199254740992", "-9007199254740992", "12345678901234567890", '{"a":1,"a":2}', '{"é":1}', '"\\ud800"', '"\\udc00x"',
    "[1,]", "{,}", "01", "-", "NaN", "[1] x", '"\u0001"', "{\"a\":1", "[".repeat(17) + "]".repeat(17),
  ]) {
    assert.throws(() => parseStrict(bad), CanonError, bad);
  }
  assert.deepEqual(parseStrict('{"a":[1,-2,"\\ud83d\\ude00"],"b":null}').a, [1, -2, "\u{1f600}"]);
  assert.equal(parseStrict("9007199254740991"), 9007199254740991);
  assert.equal(Object.keys(parseStrict('{"__proto__":1}')).join(), "__proto__"); // a plain key, no prototype change
  assert.equal(parseStrict("-0"), 0);
});

test("b64u and hex are canonical", () => {
  assert.equal(b64u(new Uint8Array([251, 255])), "-_8");
  assert.deepEqual([...unb64u("-_8")], [251, 255]);
  for (const bad of ["-_9", "AA=", "A", "a+b"]) assert.throws(() => unb64u(bad), CanonError, bad);
});

test("the digest of the hub's custodial vector, recomputed from its context, equals the vector", async () => {
  const c = V.custodial;
  const { channel_proof, ...e } = c.event;
  const sigless = Object.fromEntries(Object.entries(e).filter(([k]) => k !== "sig"));
  const ctx = { contract: 1, suite: 2, workspace_id: c.workspace_id, log: c.log, event: sigless };
  assert.equal(hex(await contextDigest(ctx)), c.digest_hex);
  assert.equal(hex(await sha256(utf8("abc"))).slice(0, 8), "ba7816bf");
});
