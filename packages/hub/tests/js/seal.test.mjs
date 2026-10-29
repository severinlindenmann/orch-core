// M3 secrets S-1: the browser module replays the WHOLE vector file (fixed-ephemeral seam, never served) and the served
// module exports exactly sealOrgSecret.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as served from "../../src/orch_hub/web/seal.js";
import { internals, sealFixed, openFixed } from "./seal_seam.mjs";

const DOC = JSON.parse(readFileSync(new URL("../../../../contracts/hub/vectors_org_secrets.json", import.meta.url), "utf8"));
const REQUIRED_CASES = [
  "open fails with changed hub_id",
  "open fails with changed workspace_id",
  "open fails with changed secret_id",
  "open fails with changed version",
  "open fails with changed wxk_version",
  "open fails with changed expires_ms",
  "open fails with changed name",
  "open fails with a shorter name",
  "open fails for a wrap sealed under purpose hub-share",
  "open refuses a sealed short value",
  "open refuses a sealed control character value",
  "open refuses a sealed non-ASCII value",
  "open refuses a sealed leading space value",
  "open refuses a sealed trailing space value",
  "open refuses a sealed long value",
  "open refuses more than 366 days after issue",
  "open ok exactly 366 days after issue",
  "open refuses one ms past 366 days",
];
const toHex = (u) => Buffer.from(u).toString("hex");
const fields = (a) => ({
  wxkPub: Uint8Array.from(Buffer.from(DOC.keys.wxk_pub, "hex")), hubId: a.hub_id, workspaceId: a.workspace_id, secretId: a.secret_id,
  version: a.version, wxkVersion: a.wxk_version, expiresMs: a.expires_ms, name: a.name, value: a.value,
});
const codeOf = (e) => (e.message === "value" ? "value" : e.message === "malformed" ? "malformed" : e.message);

test("the served module exports exactly sealOrgSecret", () => {
  assert.deepEqual(Object.keys(served), ["sealOrgSecret"]);
  const src = readFileSync(new URL("../../src/orch_hub/web/seal.js", import.meta.url), "utf8");
  assert.ok(!/exportKey\s*\(\s*["'](pkcs8|jwk)|importKey\s*\(\s*["']jwk/.test(src), "no private-key export");
  assert.ok(/generateKey\([^)]*,\s*false\s*,/.test(src), "the ephemeral key is generated non-extractable");
});

test("the required security cases are in the vector file", () => {
  const names = new Set(DOC.cases.map((c) => c.name));
  assert.deepEqual(REQUIRED_CASES.filter((n) => !names.has(n)), []);
});

test("every case of the vector file is replayed", async () => {
  let handled = 0;
  for (const c of DOC.cases) {
    const a = c.args;
    let got;
    try {
      if (c.kind === "value") got = { problem: internals.valueProblem(a.value) };
      else if (c.kind === "aad") got = { hex: toHex(internals.wrapAadExtra(a.hub_id, a.workspace_id, a.wxk_version, a.expires_ms, a.name)) };
      else if (c.kind === "seal") got = { sealed: toHex(await sealFixed(DOC.keys.eph_scalar, fields(a))) };
      else if (c.kind === "open") got = { value: await openFixed(DOC.keys[a.key + "_scalar"], a, a.now_ms, a.issued_ms, DOC.consts.expiry_max_ms) };
      else assert.fail(`unhandled kind ${c.kind}`);
    } catch (e) {
      got = { error: codeOf(e) };
    }
    assert.deepEqual(got, c.expect, c.name);
    handled++;
  }
  assert.equal(handled, DOC.cases.length);
  assert.ok(handled >= 60);
  assert.equal(DOC.consts.value_min, 8);
  assert.equal(DOC.consts.value_max, internals.VALUE_MAX);
});

test("the served path: fresh ephemeral per call, opens with the vector key, refuses a bad value", async () => {
  const base = DOC.cases.find((c) => c.name === "seal ok").args;
  const f = fields(base);
  const one = await served.sealOrgSecret(f), two = await served.sealOrgSecret(f);
  assert.equal(one.length, two.length);
  assert.notEqual(toHex(one), toHex(two));
  const ctx = { ...base, sealed: toHex(one) };
  const got = await openFixed(DOC.keys.wxk_scalar, { ...ctx, hub_id: base.hub_id }, base.expires_ms - 1, base.expires_ms - 1000, DOC.consts.expiry_max_ms);
  assert.equal(got, base.value);
  await assert.rejects(served.sealOrgSecret({ ...f, value: "short" }), /value/);
  await assert.rejects(served.sealOrgSecret({ ...f, name: "Bad Name" }), /malformed/);
});
