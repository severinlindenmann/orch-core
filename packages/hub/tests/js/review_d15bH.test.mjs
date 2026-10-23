// Adversarial tests from the Opus conformance review of orch-hub#47 (D15b), tag l2-d15bH.
import test from "node:test";
import assert from "node:assert/strict";
import { describe } from "../../src/orch_hub/web/render.js";

const D = new Uint8Array(32);
const LOG = "01J9ZP0000000000000000TKT1";
const move = (ev, sent = { action: { type: "status.changed", to: "backlog" } }) =>
  describe({ log: LOG, event: { auth: "hub", type: "status.changed", to: "backlog", ...ev } }, D, sent);

test("D15bH-2 (proof): a non-string `from` is coerced in the sentence, so sentence and signed bytes differ", () => {
  const d = move({ from: ["testing"] });
  // String(["testing"]) === "testing": the sentence reads like a plain status while ctx carries an array
  assert.equal(d.signable, false);
  assert.deepEqual(d.facts, []);
});

test("D15bH-2 (wanted): a move or decision whose shown string fields are not strings is not signable", () => {
  assert.equal(move({ from: ["testing"] }).signable, false);
  assert.equal(move({ from: "open", reason: { x: 1 } }).signable, false);
});

test("D15bH-1 (wanted): a status move is signable only against an action this page sent", () => {
  // describe() has no notion of `sent`; today only startSign() (which always has one) calls it.
  assert.equal(move({ from: "open" }, null).signable, false);
  assert.equal(move({ from: "open" }).signable, true);
  assert.equal(move({ from: "open", to: "open" }).signable, false, "ctx differs from the sent action");
  assert.equal(move({ from: "open", reason: "x" }).signable, false, "a reason the page did not send");
  assert.equal(move({ from: "open" }, { action: { type: "ticket.moved", to: "backlog" } }).signable, false);
});
