// #1123 D-hub: the hub web checks the question hash itself (frozen core fixtures, tests/data/core_f1), shows the selected
// option in the sign prompt, and never signs an option it has not verified.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { checkBindings, describe, questionRenderable, renderMulti, verifyQuestion } from "../../src/orch_hub/web/render.js";

const FORMS = JSON.parse(readFileSync(new URL("../data/core_f1/questions.json", import.meta.url))).forms;
const HASHES = JSON.parse(readFileSync(new URL("../data/core_f1/hashes.json", import.meta.url)));
const form = (n) => FORMS.find((f) => f.name === n);
const clone = (x) => JSON.parse(JSON.stringify(x));
const needs = (f) => ({ item: f.hub_needs.row, content: f.hub_needs.content });

test("the hash is recomputed in the browser for every fixture form and matches core", async () => {
  for (const f of FORMS) {
    const { item, content } = needs(f);
    const qv = await verifyQuestion(item, content);
    assert.ok(qv, f.name);
    assert.equal(qv.hash, f.raw_event.hash);
  }
});

test("a changed byte anywhere, or an unknown part, makes the question unverifiable", async () => {
  const f = form("choice_full");
  const muts = [
    (c) => (c.text += "!"), (c) => (c.spec.why = "x"), (c) => delete c.spec.details, (c) => (c.spec.recommended = "api"),
    (c) => delete c.spec.allow_other, (c) => (c.options[0].label = "Monthly CSV."), (c) => c.options.reverse(),
    (c) => c.options[0].facts.reverse(), (c) => delete c.options[1].text, (c) => (c.spec.kind = "text"),
    (c) => (c.spec.future = "x"), (c) => (c.options[0].preview = "p"), (c) => (c.options[0].facts = [{ label: "a", value: "b" }]),
    (c) => (c.unverifiable = true), (c) => delete c.spec, (c) => delete c.options,
  ];
  for (const [i, m] of muts.entries()) {
    const c = clone(f.hub_needs.content);
    m(c);
    assert.equal(await verifyQuestion(f.hub_needs.row, c), null, "mutation " + i);
  }
  const row = { ...f.hub_needs.row, hash: "sha256:" + "00".repeat(32) };
  assert.equal(await verifyQuestion(row, f.hub_needs.content), null);
  assert.equal(await verifyQuestion({ ...f.hub_needs.row, log: "01J9ZK4Q7M3R8T2V6X0B5N1C9E" }, f.hub_needs.content), null);
});

test("the old label-only question has no spec: not verifiable, not renderable", async () => {
  const c = { question: "Q1", text: "Ok?", options: [{ key: "a", label: "A" }, { key: "b", label: "B" }] };
  assert.equal(await verifyQuestion({ item_id: "x", log: "y", hash: "sha256:" + "0".repeat(64) }, c), null);
  assert.equal(questionRenderable("Ok?", c.options, undefined, true), false);
});

// What only core checks (each: the host re-checks on admission, a question with it never reaches a ticket):
const CORE_ONLY = new Set(["option_title_leading_mark"]); // needs Unicode Mn/Mc tables; the rest of the fold cases are approximated here
test("every shape core refuses is not renderable here, but for a short allowlist", () => {
  for (const v of HASHES.question_hash_refused) {
    if (CORE_ONLY.has(v.name)) continue;
    const q = v.question;
    if (!q || typeof q !== "object") continue;
    const { id, to, blocking, text, options, ...spec } = q;
    assert.equal(questionRenderable(text, options, spec, blocking), false, v.name);
  }
});

test("tab: refused in one-line fields, shown as spaces in multi-line ones", () => {
  assert.equal(renderMulti("a\tb"), "a    b");
});

function ctxFor(ev) {
  return { contract: 1, suite: 2, workspace_id: "aa".repeat(16), log: "01J9ZK4Q7M3R8T2V6X0B5N1C9D", event: { auth: "hub", type: "question.answered", question: "Q1", ...ev } };
}
const D = new Uint8Array(32);

test("the prompt shows the selected option (title, meaning, facts, cost, question, details) before signing", async () => {
  const f = form("choice_full");
  const qv = await verifyQuestion(f.hub_needs.row, f.hub_needs.content);
  const d = describe(ctxFor({ hash: qv.hash, option: "csv" }), D, null, qv);
  assert.equal(d.signable, true);
  assert.match(d.sentence, /with “Monthly CSV”/);
  const rows = Object.fromEntries(d.facts);
  assert.equal(d.facts[0][0], "You chose");
  assert.equal(rows["You chose"], "Monthly CSV");
  assert.equal(rows["What it means"], "One file a month, checked by hand");
  assert.equal(rows["Fact: Effort"], "½ day");
  assert.equal(rows["Fact: Cost"], "CHF 400");
  assert.equal(rows["Option key"], "csv");
  assert.match(rows.Recommended, /^yes/);
  assert.equal(rows.Question, f.raw_event.question.text);
  assert.match(rows.Details, /we import monthly/);
});

test("an option answer is unsignable without the verified question that lists it", async () => {
  const f = form("choice_full");
  const qv = await verifyQuestion(f.hub_needs.row, f.hub_needs.content);
  assert.equal(describe(ctxFor({ hash: qv.hash, option: "csv" }), D).signable, false, "no question at all");
  assert.equal(describe(ctxFor({ hash: qv.hash, option: "zzz" }), D, null, qv).signable, false, "option not listed");
  assert.equal(describe(ctxFor({ hash: "sha256:" + "11".repeat(32), option: "csv" }), D, null, qv).signable, false, "other hash");
  assert.equal(describe(ctxFor({ hash: qv.hash, option: "csv", smuggled: 1 }), D, null, qv).signable, false);
});

test("the prompt escapes hidden characters in the shown option", async () => {
  const f = form("choice_full");
  const c = clone(f.hub_needs.content);
  c.options[0].label = "Pay‮out";
  const row = { ...f.hub_needs.row, hash: "x" };
  // not the fixture hash: build the verified object by hand to test display escaping alone
  const qv = { text: c.text, options: c.options, spec: c.spec, hash: row.hash };
  const d = describe(ctxFor({ hash: "x", option: "csv" }), D, null, qv);
  assert.match(d.sentence, /Pay<U\+202E>out/);
});

test("EVERY question.answered needs the verified question; a text-only answer without one is unsignable", async () => {
  assert.equal(describe(ctxFor({ hash: "sha256:" + "11".repeat(32), text: "free text" }), D).signable, false);
  assert.equal(describe(ctxFor({ hash: "sha256:" + "11".repeat(32), text: "free text" }), D, null, null).signable, false);
  const t = form("text_with_why_and_details");
  const tq = await verifyQuestion(t.hub_needs.row, t.hub_needs.content);
  assert.equal(describe(ctxFor({ hash: tq.hash, text: "Acme" }), D, null, tq).signable, true);
  assert.equal(describe(ctxFor({ hash: tq.hash, option: "a" }), D, null, tq).signable, false, "text kind takes no option");
  const c = form("choice_full");
  const cq = await verifyQuestion(c.hub_needs.row, c.hub_needs.content);
  assert.equal(describe(ctxFor({ hash: cq.hash, text: "else" }), D, null, cq).signable, true, "Other with allow_other");
  const noOther = { ...cq, spec: { ...cq.spec, allow_other: undefined } };
  delete noOther.spec.allow_other;
  assert.equal(describe(ctxFor({ hash: cq.hash, text: "else" }), D, null, noOther).signable, false, "Other without allow_other");
  assert.equal(describe(ctxFor({ hash: cq.hash }), D, null, cq).signable, false, "neither option nor text");
});

test("a fact label cannot imitate a row of this page", async () => {
  const f = form("choice_full");
  const c = clone(f.hub_needs.content);
  c.options[0].facts[0][0] = "You chose";
  const qv = { text: c.text, options: c.options, spec: c.spec, hash: "h" };
  const labels = describe(ctxFor({ hash: "h", option: "csv" }), D, null, qv).facts.map((r) => r[0]);
  assert.equal(labels.filter((l) => l === "You chose").length, 1);
  assert.ok(labels.includes("Fact: You chose"));
});

test("bindings: an answer carries exactly the option/text keys that were sent", () => {
  const me = { channelId: "c".repeat(32), keyPub: "KEY", userId: "u".repeat(32), hubId: "h".repeat(32) };
  const ev = (extra) => ({ ...ctxFor({ hash: "h", option: "a", ...extra }), event: { ...ctxFor({ hash: "h", option: "a", ...extra }).event, channel: me.channelId, actor: { kind: "person", id: "u_" + me.userId, via: me.channelId }, hub_id: me.hubId, channel_factor: "password+totp", channel_cert: { o: { channel_id: me.channelId, key_pub: "KEY", user_id: me.userId, factor: "password+totp" } } } });
  const sent = { workspaceId: "aa".repeat(16), action: { type: "question.answered", log: "01J9ZK4Q7M3R8T2V6X0B5N1C9D", question: "Q1", hash: "h", option: "a" } };
  assert.doesNotThrow(() => checkBindings(ev({}), me, sent));
  assert.throws(() => checkBindings(ev({ text: "added note" }), me, sent));
  assert.throws(() => checkBindings(ev({}), me, { ...sent, action: { ...sent.action, text: "n" } }));
});
