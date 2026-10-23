// What the sign prompt shows and checks. Pure (no DOM): the DOM code in app.js only copies these strings into
// textContent. Everything here is derived from `ctx`, the object whose digest is signed; the hub's `shown` is never used
// (protocol.md section 8.4 step 3, design-a8).
import { cj, cjBytes, hex, sha256, utf8 } from "./cj.js";

export const LABEL_DIGEST = "orch/v2/hub-event-digest|";

export class Refusal extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

// The one text rule's refused set (section 8.5, L2P4-3): Cc Cf Zl Zp Cs Co Cn and Default_Ignorable_Code_Point.
const BAD = /^(?:\p{Cc}|\p{Cf}|\p{Zl}|\p{Zp}|\p{Cs}|\p{Co}|\p{Cn}|\p{Default_Ignorable_Code_Point})$/u;

/** A string from the workspace's view, as the person sees it: every refused character other than LF as <U+XXXX>. */
export function renderText(s) {
  let out = "";
  for (const ch of String(s)) {
    if (ch !== "\n" && BAD.test(ch)) out += "<U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0") + ">";
    else out += ch;
  }
  return out;
}

/** The digest of a context, recomputed here: H("orch/v2/hub-event-digest|" || cj(ctx)). */
export async function contextDigest(ctx) {
  return sha256(utf8(LABEL_DIGEST + cj(ctx)));
}

const CTX_KEYS = ["contract", "event", "log", "suite", "workspace_id"];

/** Protocol 8.4 step 3 (reference check_prepared): exact shape, then the digest recomputed from the context we render.
 *  Returns the digest bytes we will sign. Throws Refusal("malformed" | "event_changed"). */
export async function checkPrepared(ctx, digestHex) {
  const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
  if (
    !isObj(ctx) ||
    Object.keys(ctx).sort().join() !== CTX_KEYS.join() ||
    ctx.contract !== 1 ||
    ctx.suite !== 2 ||
    !isObj(ctx.event) ||
    "channel_proof" in ctx.event ||
    ctx.event.auth !== "hub"
  )
    throw new Refusal("malformed");
  let d;
  try {
    d = await contextDigest(ctx);
  } catch {
    throw new Refusal("malformed");
  }
  if (typeof digestHex !== "string" || hex(d) !== digestHex) throw new Refusal("event_changed");
  return d;
}

// The four decision types a prompt may offer to sign (L2B-3), with the payload fields each may carry.
const ENVELOPE = [
  "actor", "auth", "based_on", "channel", "channel_cert", "channel_factor", "hash_v", "hub_id", "id", "item_hash",
  "roster_v", "snapshot_version", "type", "uk_cert", "v",
];
export const DECISIONS = {
  "question.answered": ["question", "hash", "option", "text"],
  "gate.approved": ["gate", "hash", "gate_gen", "policy_hash", "source_sha"],
  "gate.changes_requested": ["gate", "hash", "text", "gate_gen", "policy_hash"],
  "verdict.given": ["outcome", "hash", "text", "gate_gen", "policy_hash", "source_sha"],
};
// D15b: the two status moves a prompt may offer; `from` is the host's own value, copied by the hub from P.
export const MOVES = {
  "status.changed": ["to", "from", "reason"],
  "ticket.moved": ["to", "from", "reason"],
};

/** Short form of a fingerprint or hash for the person: "193B F2F3". */
export function shortHash(h) {
  const m = /^(?:sha256:)?([0-9a-f]{8})/.exec(String(h));
  return m ? m[1].slice(0, 4).toUpperCase() + " " + m[1].slice(4).toUpperCase() : "";
}

const q = (s) => "“" + renderText(s) + "”";

/** D15b-2 (protocol 8.4 step 3): the projection-derived values the person must see, from ctx only. A decision shows
 *  the gate, the short hash, gate_gen and, with a source list (code approval, verdict), each repo with its short sha;
 *  a move shows "from -> to". Unexpected shapes are shown as their JSON, never skipped. */
export function facts(type, ev) {
  const out = [];
  const r = (v) => (typeof v === "string" ? renderText(v) : renderText(cj(v)));
  if (type in DECISIONS && type !== "question.answered") {
    out.push(["Gate", type === "verdict.given" ? "verify" : r(ev.gate)]);
    out.push(["Content reference", typeof ev.hash === "string" && shortHash(ev.hash) ? shortHash(ev.hash) : r(ev.hash)]);
    out.push(["Generation", r(ev.gate_gen)]);
    if ("source_sha" in ev) {
      const list = Array.isArray(ev.source_sha) ? ev.source_sha : [ev.source_sha];
      for (const e of list) {
        const ok = e !== null && typeof e === "object" && Object.keys(e).sort().join() === "ref,repo,sha" && typeof e.repo === "string" && typeof e.sha === "string";
        out.push(["Code", ok ? renderText(e.repo) + " " + renderText(e.sha.slice(0, 8)) : r(e)]);
      }
    }
  } else if (type in MOVES) {
    out.push(["Status", r(ev.from) + " \u2192 " + r(ev.to)]);
  }
  return out;
}

const SENTENCE_STRINGS = ["from", "to", "reason", "gate", "outcome", "text", "option", "question", "hash"];

function sentence(type, log, ev) {
  const on = "ticket " + renderText(log);
  switch (type) {
    case "gate.approved":
      return "I approve the " + q(ev.gate) + " gate of " + on + ".";
    case "gate.changes_requested":
      return "I ask for changes on the " + q(ev.gate) + " gate of " + on + ": " + q(ev.text) + ".";
    case "status.changed":
      return "I change the status of " + on + " from " + q(ev.from) + " to " + q(ev.to) + (ev.reason ? ": " + q(ev.reason) : "") + ".";
    case "ticket.moved":
      return "I move " + on + " from " + q(ev.from) + " to " + q(ev.to) + (ev.reason ? ": " + q(ev.reason) : "") + ".";
    case "verdict.given":
      return "I give the verdict " + q(ev.outcome) + " on " + on + (ev.text ? ": " + q(ev.text) : "") + ".";
    default:
      return "I answer question " + renderText(ev.question) + " on " + on + " with " + q(ev.option ?? ev.text) + ".";
  }
}

/** What the prompt shows, from ctx only. `signable` is true only for the four decision types with no field we do not
 *  know; otherwise `sentence` is null and the prompt lists every field and offers no Sign button. Never throws on
 *  content (a surprise is a reason not to sign, not a crash). */
export function describe(ctx, digest, sent = null) {
  const ev = ctx.event;
  const type = typeof ev.type === "string" ? ev.type : "";
  const known = Object.hasOwn(DECISIONS, type) ? DECISIONS[type] : Object.hasOwn(MOVES, type) ? MOVES[type] : undefined;
  const fields = Object.keys(ev)
    .filter((k) => k !== "channel_cert" && k !== "uk_cert")
    .sort()
    .map((k) => [renderText(k), typeof ev[k] === "string" ? renderText(ev[k]) : renderText(cj(ev[k]))]);
  const extra = known ? Object.keys(ev).filter((k) => !known.includes(k) && !ENVELOPE.includes(k)) : [];
  // D15bH-2: every field the sentence names must be a real string (nothing is flattened by String())
  const stringsOk = SENTENCE_STRINGS.every((k) => !(k in ev) || typeof ev[k] === "string");
  // D15bH-1: a status move is signable only against the action THIS page sent, and it must match that action exactly;
  // a pushed prepare (no `sent`) never makes a move signable. The phone stays at the four decision types.
  const moveOk = !Object.hasOwn(MOVES, type) || (sent?.action?.type === type && sent.action.to === ev.to && sent.action.reason === ev.reason);
  const signable = Boolean(known) && extra.length === 0 && stringsOk && moveOk && typeof ctx.log === "string" && ctx.log !== "workspace";
  return {
    type: renderText(type),
    signable,
    sentence: signable ? sentence(type, ctx.log, ev) : null,
    fingerprint: shortHash(hex(digest)),
    contentHash: typeof ev.hash === "string" ? shortHash(ev.hash) : "",
    facts: signable ? facts(type, ev) : [],
    fields,
  };
}

/** Section 8.4 step 2/3 bindings against what THIS browser knows: its channel, its user, its hub, and the action it
 *  sent (when it sent one). Throws Refusal("event_changed") on any difference; nothing is signed. */
export function checkBindings(ctx, me, sent) {
  const ev = ctx.event;
  const bad = () => {
    throw new Refusal("event_changed");
  };
  if (ev.channel !== me.channelId || ev.channel_cert?.o?.channel_id !== me.channelId) bad();
  if (ev.channel_cert?.o?.key_pub !== me.keyPub) bad();
  if (ev.channel_cert?.o?.user_id !== me.userId || ev.actor?.id !== "u_" + me.userId || ev.actor?.via !== me.channelId) bad();
  if (me.hubId && ev.hub_id !== me.hubId) bad();
  if (ev.channel_factor !== "password+totp" || ev.channel_cert?.o?.factor !== "password+totp") bad();
  if (sent) {
    if (ctx.workspace_id !== sent.workspaceId || ctx.log !== sent.action.log || ev.type !== sent.action.type) bad();
    for (const k of ["hash", "question", "gate", "option", "text", "outcome", "to", "reason"]) {
      if (k in sent.action && ev[k] !== sent.action[k]) bad();
    }
  }
}

export { cjBytes };
