// What the sign prompt shows and checks. Pure (no DOM): the DOM code in app.js only copies these strings into
// textContent. Everything here is derived from `ctx`, the object whose digest is signed; the hub's `shown` is never used
// (protocol.md section 8.4 step 3, design-a8).
import { cj, cjBytes, hex, sha256, utf8 } from "./cj.js";

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

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

/** Multi-line question text (question, why, details): a tab shows as spaces; every other refused character is escaped. */
export function renderMulti(s) {
  return renderText(String(s).replaceAll("\t", "    "));
}

/** The digest of a context, recomputed here: H("orch/v2/hub-event-digest|" || cj(ctx)). */
export async function contextDigest(ctx) {
  return sha256(utf8(LABEL_DIGEST + cj(ctx)));
}

const CTX_KEYS = ["contract", "event", "log", "suite", "workspace_id"];

/** Protocol 8.4 step 3 (reference check_prepared): exact shape, then the digest recomputed from the context we render.
 *  Returns the digest bytes we will sign. Throws Refusal("malformed" | "event_changed"). */
export async function checkPrepared(ctx, digestHex) {
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
// FB15 (protocol 8.8b): the owner's ask to turn an add-on on. The words here are this page's own: the host's text never
// reaches the sentence. Unknown capability tokens, any other field or any other shape is "cannot render", never a guess.
export const ADDON_REQUEST = ["name", "version", "package_sha256", "capabilities", "runtime", "handoff"];
export const CAPABILITY_WORDS = {
  serve_http: "Run a small web page on the computer that hosts the workspace",
  spawn_agent: "Start agents",
  pty: "Open terminals",
  network: "Use the network",
  git_push: "Push code to git",
  choose_model: "Choose which AI model runs",
  credential: "Keep a secret key for the workspace",
};
const CAPS = Object.keys(CAPABILITY_WORDS);
const RUNTIME_KINDS = { "python-requirements": "Python packages", "node-package-lock": "Node packages" };
/** True only for exactly the six fields of protocol 8.8b, each in its grammar (the hub's check_action, mirrored). */
export function addonTermsOk(t) {
  if (!isObj(t) || !Object.keys(t).every((k) => ADDON_REQUEST.includes(k))) return false;
  const str = (x, re) => typeof x === "string" && re.test(x);
  if (!str(t.name, /^[a-z][a-z0-9-]{0,39}$/) || !str(t.version, /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/)) return false;
  if (!str(t.package_sha256, /^sha256:[0-9a-f]{64}$/) || !str(t.handoff, /^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$/)) return false;
  if (!Array.isArray(t.capabilities) || t.capabilities.length > CAPS.length || !t.capabilities.every((c) => CAPS.includes(c))) return false;
  if (t.capabilities.join() !== [...new Set(t.capabilities)].sort().join()) return false;
  if (!("runtime" in t)) return true;
  const r = t.runtime;
  return isObj(r) && Object.keys(r).sort().join() === "kind,lock,packages" && Object.hasOwn(RUNTIME_KINDS, r.kind) && typeof r.lock === "string" && r.lock !== "" && r.lock.length <= 200 && renderText(r.lock) === r.lock && Number.isInteger(r.packages) && r.packages >= 1 && r.packages <= 1000;
}
/** The rows a person reads before signing an add-on request, from the terms only. */
export function addonFacts(t) {
  const rows = [["Add-on", renderText(t.name) + " " + renderText(t.version)]];
  rows.push(["It may", t.capabilities.length ? t.capabilities.map((c) => CAPABILITY_WORDS[c]).join("; ") : "Nothing beyond reading and writing its own add-on data"]);
  if (t.runtime) rows.push(["It installs", String(t.runtime.packages) + " " + RUNTIME_KINDS[t.runtime.kind] + " (list: " + renderText(t.runtime.lock) + ")"]);
  rows.push(["Package fingerprint", renderText(t.package_sha256)]);
  return rows;
}
// D15b: the two status moves a prompt may offer; `from` is the host's own value, copied by the hub from P.
export const MOVES = {
  "status.changed": ["to", "from", "reason"],
  "ticket.moved": ["to", "from", "reason"],
};

const groups4 = (x) => x.slice(0, 4) + " " + x.slice(4, 8) + " " + x.slice(8, 12);

/** Protocol 8.4 (orch-hub#55): the one short form of a content hash, 12 lower-case hex digits in groups of 4,
 *  "abcd ef01 2345". "" when it is not `sha256:` plus 64 lower-case hex digits (the caller then shows the full field). */
export function shortHash(h) {
  const m = /^sha256:([0-9a-f]{12})[0-9a-f]{52}$/.exec(typeof h === "string" ? h : "");
  return m ? groups4(m[1]) : "";
}

/** The same form for a 64-digit lower-case hex digest (the "signature reference"). */
export function shortDigest(d) {
  const m = /^([0-9a-f]{12})[0-9a-f]{52}$/.exec(typeof d === "string" ? d : "");
  return m ? groups4(m[1]) : "";
}

// --- questions (design #1123 rev 4): the raw-question hash, checked here, and what this page can render -----------------

const SPEC_KEYS = ["kind", "why", "details", "recommended", "allow_other"];
const OPTION_KEYS = ["key", "label", "text", "facts", "cost"];
const str1 = (x) => typeof x === "string" && x !== "";

// The cheap core limits, mirrored (core check_plain): code points, bytes, NFC, not blank (White_Space), no refused character,
// no LF or tab in a one-line field, core L2 (no leading or trailing White_Space in a one-line field) and L3 (no combining mark,
// \p{M}, at the start of any line; the engine's Unicode tables stand in for core's 16.0 ones). Exact fold tables stay core's.
const WS = new Set("\t\n\v\f\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000");
const cpLen = (s) => Array.from(s).length;
function field(x, n, oneLine = false) {
  if (typeof x !== "string" || x === "" || cpLen(x) > n || x.trim() === "" || utf8(x).length > 4096) return false;
  if (x.normalize("NFC") !== x || (oneLine && /[\n\t]/.test(x))) return false;
  if (oneLine && (WS.has(x[0]) || WS.has(x[x.length - 1]))) return false;
  if (/(^|\n)\p{M}/u.test(x)) return false;
  return renderText(x.replaceAll("\t", " ")) === x.replaceAll("\t", " ");
}
// casefold(NFC(.)): toLowerCase plus the full-fold cases it lacks (ß ẞ ſ ς and the f-ligatures); approximate by design.
const FOLD = { "ß": "ss", "ſ": "s", "ς": "σ", "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st" };
const fold = (s) => s.normalize("NFC").toLowerCase().replace(/[ßſςﬀ-ﬆ]/g, (c) => FOLD[c]).normalize("NFC");
const uniq = (xs) => new Set(xs).size === xs.length;

function optionOk(o) {
  if (!isObj(o) || !Object.keys(o).every((k) => OPTION_KEYS.includes(k)) || !field(o.key, 32) || !/^[a-z][a-z0-9_]*$/.test(o.key) || !field(o.label, 60, true)) return false;
  if (("text" in o && !field(o.text, 200, true)) || ("cost" in o && !field(o.cost, 60, true))) return false;
  if (!("facts" in o)) return true;
  const f = o.facts;
  return Array.isArray(f) && f.length >= 1 && f.length <= 3 && f.every((p) => Array.isArray(p) && p.length === 2 && field(p[0], 24, true) && field(p[1], 40, true)) && uniq(f.map((p) => fold(p[0])));
}

/** True only when this page knows every part of the question and the cheap core limits hold: kind choice or text, only known
 *  spec and option keys, [label, value] facts arrays, unique titles and fact labels under fold, boolean blocking.
 *  Anything else is "cannot render": never an Answer button. `blocking` is the hub's content `blocking` flag. */
export function questionRenderable(text, options, spec, blocking) {
  if (typeof blocking !== "boolean" || !field(text, 140) || !isObj(spec) || !Object.keys(spec).every((k) => SPEC_KEYS.includes(k))) return false;
  if (("why" in spec && !field(spec.why, 4096)) || ("details" in spec && !field(spec.details, 1500)) || ("allow_other" in spec && spec.allow_other !== true)) return false;
  if (spec.kind === "text") return options === undefined && !("recommended" in spec) && !("allow_other" in spec);
  if (spec.kind !== "choice" || !Array.isArray(options) || options.length < 2 || options.length > 6 || !options.every(optionOk)) return false;
  const keys = options.map((o) => o.key);
  return uniq(keys) && uniq(options.map((o) => fold(o.label))) && (!("recommended" in spec) || keys.includes(spec.recommended));
}

/** The question the hub listed, verified HERE: H("orch/v2/question|" || cj({question_id, ticket, text, options or [], spec}))
 *  over exactly what the hub sent must equal the hash the row was listed with. Returns {text, options, spec, hash}, or null
 *  when the hub said unverifiable, this page cannot render it, or the hash differs. `item` is the needs row. */
export async function verifyQuestion(item, c) {
  try {
    if (!isObj(c) || c.unverifiable === true || !isObj(item) || typeof item.hash !== "string") return null;
    if (!questionRenderable(c.text, c.options, c.spec, c.blocking)) return null;
    const body = { question_id: item.item_id, ticket: item.log, text: c.text, options: c.options ?? [], spec: c.spec };
    const h = "sha256:" + hex(await sha256(utf8("orch/v2/question|" + cj(body))));
    return h === item.hash ? { text: c.text, options: c.options ?? [], spec: c.spec, hash: h } : null;
  } catch {
    return null;
  }
}

const q = (s) => "“" + renderText(s) + "”";

/** Every question.answered needs the verified question it answers (same hash) and must fit its kind: a text question takes
 *  `text` only; a choice takes an option it lists, or `text` alone only when the question allows Other. */
function answerFits(ev, qv) {
  if (!qv || ev.hash !== qv.hash) return false;
  if (qv.spec.kind === "text") return !("option" in ev) && typeof ev.text === "string";
  if (qv.spec.kind !== "choice") return false;
  return "option" in ev ? chosen(ev, qv) !== null : qv.spec.allow_other === true && typeof ev.text === "string";
}

/** The option an answer picks, from the verified question; null when there is none or it is not that question's. */
function chosen(ev, qv) {
  if (!qv || ev.hash !== qv.hash || typeof ev.option !== "string") return null;
  return qv.options.find((o) => o.key === ev.option) ?? null;
}

/** D15b-2 (protocol 8.4 step 3): the projection-derived values the person must see, from ctx only. A decision shows
 *  the gate, the short hash, gate_gen and, with a source list (code approval, verdict), each repo with its short sha;
 *  a move shows "from -> to". Unexpected shapes are shown as their JSON, never skipped. */
export function facts(type, ev, qv = null) {
  const out = [];
  const r = (v) => (typeof v === "string" ? renderText(v) : renderText(cj(v)));
  if (type === "question.answered") {
    const o = chosen(ev, qv);
    if (o) {
      // the selected option FIRST, from the question this page verified against ev.hash (frozen order, design #1123 §4).
      // Agent-written fact rows are prefixed "Fact: " so a label like "You chose" cannot imitate a row of this page.
      out.push(["You chose", renderText(o.label)]);
      out.push(["Option key", renderText(o.key)]);
      if (qv.spec.recommended === o.key) out.push(["Recommended", "yes, the asker recommends this"]);
      else if (qv.spec.recommended) {
        const rec = qv.options.find((x) => x.key === qv.spec.recommended);
        out.push(["Recommended", "no, the asker recommends “" + renderText(rec ? rec.label : qv.spec.recommended) + "”"]);
      }
      if (o.text) out.push(["What it means", renderText(o.text)]);
      for (const [l, v] of o.facts ?? []) out.push(["Fact: " + renderText(l), renderText(v)]);
      if (o.cost) out.push(["Cost", renderText(o.cost)]);
      if ("text" in ev) out.push(["Your note", renderText(ev.text)]);
    } else if ("text" in ev) {
      out.push(["Your answer", renderText(ev.text)]);
    }
    if (qv) {
      out.push(["Question", renderMulti(qv.text)]);
      if (qv.spec.details) out.push(["Details", renderMulti(qv.spec.details)]);
      if (qv.spec.why) out.push(["Why it is asked", renderMulti(qv.spec.why)]);
    }
  } else if (type in DECISIONS) {
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
  } else if (type === "addon.requested") {
    out.push(...addonFacts(ev));
  } else if (type in MOVES) {
    out.push(["Status", r(ev.from) + " \u2192 " + r(ev.to)]);
  }
  return out;
}

const SENTENCE_STRINGS = ["from", "to", "reason", "gate", "outcome", "text", "option", "question", "hash"];

function sentence(type, log, ev, qv = null) {
  const on = "ticket " + renderText(log);
  switch (type) {
    case "addon.requested":
      return "I allow the add-on " + q(ev.name + " " + ev.version) + " to be installed and turned on in this workspace.";
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
      return "I answer question " + renderText(ev.question) + " on " + on + " with " + q(chosen(ev, qv)?.label ?? ev.option ?? ev.text) + ".";
  }
}

/** What the prompt shows, from ctx only. `signable` is true only for the four decision types with no field we do not
 *  know; otherwise `sentence` is null and the prompt lists every field and offers no Sign button. Never throws on
 *  content (a surprise is a reason not to sign, not a crash). */
export function describe(ctx, digest, sent = null, qv = null) {
  const ev = ctx.event;
  const type = typeof ev.type === "string" ? ev.type : "";
  const isAddon = type === "addon.requested";
  const known = isAddon ? ADDON_REQUEST : Object.hasOwn(DECISIONS, type) ? DECISIONS[type] : Object.hasOwn(MOVES, type) ? MOVES[type] : undefined;
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
  // an answer that picks an option is signable only against the verified question that lists it: never a blind label
  const answerOk = type !== "question.answered" || answerFits(ev, qv);
  const addonOk = !isAddon || (ctx.log === "workspace" && addonTermsOk(Object.fromEntries(ADDON_REQUEST.filter((k) => k in ev).map((k) => [k, ev[k]]))));
  const signable = Boolean(known) && answerOk && addonOk && extra.length === 0 && stringsOk && moveOk && typeof ctx.log === "string" && (isAddon ? ctx.log === "workspace" : ctx.log !== "workspace");
  return {
    type: renderText(type),
    signable,
    sentence: signable ? sentence(type, ctx.log, ev, qv) : null,
    fingerprint: shortDigest(hex(digest)),
    contentHash: typeof ev.hash === "string" ? shortHash(ev.hash) : "",
    facts: signable ? facts(type, ev, qv) : [],
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
    for (const k of ["hash", "question", "gate", "option", "text", "outcome", "to", "reason", "name", "version", "package_sha256", "handoff"]) {
      if (k in sent.action && ev[k] !== sent.action[k]) bad();
    }
    if (ev.type === "addon.requested") for (const k of ["capabilities", "runtime"]) if ((k in ev) !== (k in sent.action) || (k in ev && cj(ev[k]) !== cj(sent.action[k]))) bad();
    // an answer carries exactly the option/text keys that were sent, never one more (an added note or option)
    if (ev.type === "question.answered") for (const k of ["option", "text"]) if ((k in ev) !== (k in sent.action)) bad();
  }
}

export { cjBytes };
