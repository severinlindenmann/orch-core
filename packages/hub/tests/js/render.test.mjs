// render.js: the render_* and prepared_* cases of the hub vectors, and the allow-list.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DECISIONS, Refusal, checkBindings, checkPrepared, contextDigest, describe, renderText } from "../../src/orch_hub/web/render.js";
import { hex, parseStrict } from "../../src/orch_hub/web/cj.js";

const V = JSON.parse(readFileSync(new URL("../../vectors/vectors_hub.json", import.meta.url)));
const cases = (prefix) => V.cases.filter((c) => c.name.startsWith(prefix));

test("render_* vectors", () => {
  const cs = cases("render_");
  assert.ok(cs.length >= 3);
  for (const c of cs) assert.equal(renderText(c.args.text), c.expect.ok.shown, c.name);
});

test("lone surrogates and invisible characters are escaped, ordinary text is not", () => {
  assert.equal(renderText("a\ud800b"), "a<U+D800>b");
  assert.equal(renderText("xㅤy​z"), "x<U+3164>y<U+200B>z");
  assert.equal(renderText("café \u{1f600}\nok"), "café \u{1f600}\nok");
});

test("prepared_* vectors: the same accept/refuse as the hub's check_prepared", async () => {
  const cs = cases("prepared_");
  assert.ok(cs.length >= 4);
  for (const c of cs) {
    const exp = c.expect;
    let got;
    try {
      await checkPrepared(c.args.ctx, c.args.digest_hex);
      got = "ok";
    } catch (e) {
      got = e instanceof Refusal ? e.code : "other:" + e;
    }
    const want = "ok" in exp ? "ok" : exp.error;
    if ("ok" in exp) assert.equal(hex(await checkPrepared(c.args.ctx, c.args.digest_hex)), exp.ok.digest_hex);
    assert.equal(got, want, c.name + " " + JSON.stringify(exp));
  }
});

function ctxOf(type, extra = {}) {
  const ev = { auth: "hub", type, hash: "sha256:" + "ab".repeat(32), gate: "plan", gate_gen: 1, policy_hash: "sha256:" + "cd".repeat(32), ...extra };
  return { contract: 1, suite: 2, workspace_id: "aa".repeat(16), log: "01J9ZP0000000000000000TKT1", event: ev };
}
const D = new Uint8Array(32);

test("only the four decision types are signable, and an unknown field makes any of them unsignable", () => {
  for (const t of Object.keys(DECISIONS)) {
    const ctx = ctxOf(t, { outcome: "pass", text: "ok", question: "Q1", option: "a", source_sha: [] });
    for (const k of Object.keys(ctx.event)) if (k !== "auth" && k !== "type" && !DECISIONS[t].includes(k)) delete ctx.event[k];
    assert.equal(describe(ctx, D).signable, true, t);
    assert.ok(describe(ctx, D).sentence.length > 10);
    assert.equal(describe({ ...ctx, event: { ...ctx.event, smuggled: 1 } }, D).signable, false, t + " + unknown field");
  }
  for (const t of ["ticket.created", "ticket.closed", "policy.changed", "settings.changed", "addon.purged", "", "gate.approved "]) {
    const d = describe(ctxOf(t), D);
    assert.equal(d.signable, false, t);
    assert.equal(d.sentence, null);
    assert.ok(d.fields.length > 0);
  }
  assert.equal(describe({ ...ctxOf("gate.approved"), log: "workspace" }, D).signable, false);
});

test("the sentence escapes workspace text", () => {
  const d = describe(ctxOf("gate.changes_requested", { text: "fix ‮payroll‬" }), D);
  assert.match(d.sentence, /fix <U\+202E>payroll<U\+202C>/);
});

test("bindings: own channel, user, hub, factor and the action sent", () => {
  const me = { channelId: "c".repeat(32), keyPub: "KEY", userId: "u".repeat(32), hubId: "h".repeat(32) };
  const good = () => ctxOf("gate.approved", {
    channel: me.channelId, actor: { kind: "person", id: "u_" + me.userId, via: me.channelId }, hub_id: me.hubId,
    channel_factor: "password+totp", channel_cert: { o: { channel_id: me.channelId, key_pub: "KEY", user_id: me.userId, factor: "password+totp" } },
  });
  const sent = { workspaceId: "aa".repeat(16), action: { type: "gate.approved", log: "01J9ZP0000000000000000TKT1", gate: "plan", hash: "sha256:" + "ab".repeat(32) } };
  assert.doesNotThrow(() => checkBindings(good(), me, sent));
  const mutate = (f) => { const c = good(); f(c); return c; };
  for (const f of [
    (c) => (c.event.channel = "d".repeat(32)), (c) => (c.event.hub_id = "e".repeat(32)), (c) => (c.event.actor.id = "u_x"),
    (c) => (c.event.channel_factor = "passkey"), (c) => (c.event.hash = "sha256:" + "00".repeat(32)), (c) => (c.event.type = "verdict.given"),
    (c) => (c.workspace_id = "bb".repeat(16)), (c) => (c.log = "01J9ZP0000000000000000OTHR"), (c) => (c.event.channel_cert.o.key_pub = "OTHER"),
  ]) assert.throws(() => checkBindings(mutate(f), me, sent), Refusal);
});

test("the vector digest is what checkPrepared returns", async () => {
  const p = V.cases.find((c) => c.name === "prepared_digest_matches");
  const d = await checkPrepared(parseStrict(JSON.stringify(p.args.ctx)), p.args.digest_hex);
  assert.equal(hex(d), p.args.digest_hex);
  assert.equal(hex(await contextDigest(p.args.ctx)), p.args.digest_hex);
});
