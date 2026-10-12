// The terminal hand-off (design 4, host contract v1.4 section 0.8.1): types, and the two hashes the person compares.
//
// The dashboard never signs and never holds a factor. It asks the host to prepare a sign request, shows the host's
// `material` (never the cached ticket) with a fingerprint, and the person signs in a terminal with `orch sign CODE`.
// The fingerprint is computed here exactly as `orch sign` computes it, from what the server sent:
//   review_digest = "sha256:" + sha256(cj(material))
//   fingerprint   = first 12 hex of sha256(cj({op, review_digest, events: [event without sig, actor.device, auth]}))
// where `cj` is canonical JSON (RFC 8785 on the restricted subset: integers, ASCII keys). A request whose hashes do not
// recompute is never shown as valid.
import { sha256Hex } from './sha256'

export type HandoffAction =
  | { action: 'approve'; key: string; gate: 'requirements' | 'plan' | 'code' }
  | { action: 'request_changes'; key: string; gate: 'requirements' | 'plan' | 'verify' | 'code'; message: string }
  | { action: 'verdict'; key: string; outcome: 'pass' | 'fail'; message?: string }
  | { action: 'answer'; key: string; question: string; option?: string; message?: string }
  | { action: 'comment'; key: string; message: string }
  | { action: 'label'; key: string; label: string }
  | { action: 'close'; key: string; resolution?: 'wont_do' | 'duplicate' | 'obsolete' | 'other'; duplicate_of?: string; message?: string }
  | { action: 'reopen'; key: string; message?: string }
  /** Return unclaimed work to backlog or open (host `move`, H6b): owners and maintainers. */
  | { action: 'move'; key: string; to: 'backlog' | 'open'; reason?: string }
  /** A person's `new` carries the title and type only. Everything else is a follow-up hand-off (`details`, `section`). */
  | { action: 'new'; title: string; type?: 'feature' | 'bug' | 'chore' | 'spike' | 'epic' }
  | { action: 'details'; key: string; priority?: 'low' | 'medium' | 'high' | 'urgent'; size?: 'xs' | 's' | 'm' | 'l' | 'xl'; labels?: string[]; parent?: string }
  /** One body section, replaced (the description follow-up, `section.set`). Spikes keep "findings" where other types have "verification". */
  | { action: 'section'; key: string; section: 'summary' | 'context' | 'requirements' | 'out_of_scope' | 'plan' | 'decisions' | 'verification' | 'findings' | 'current_state'; text: string }

  /** Reduced workspace settings (M4). `repos` is not here: the Repos addon owns repo editing. */
  | { action: 'settings_set'; grant_hours?: number; claim_ttl_min?: number; lease_ttl_min?: number }
  /** The workspace default policy of one gate (F1 5.7 `Policy`, exactly). The code gate: `not` has `assignees`, `independent` is true. */
  | { action: 'policy_set'; gate: 'requirements' | 'plan' | 'verify' | 'code'; policy: { approvers: string[]; count: number; not: string[]; applies: 'all' | 'off' | string[]; independent: boolean } }
  /** The invitee's own public key (base64url, 65 bytes) and first device certificate (strict JSON text): their files, uploaded by the person. */
  | { action: 'member_add'; name: string; role: 'owner' | 'maintainer' | 'member' | 'viewer'; pk: string; cert: string }
  | { action: 'member_role'; person: string; role: 'owner' | 'maintainer' | 'member' | 'viewer' }
  | { action: 'member_remove'; person: string }
  | { action: 'grant_revoke'; grant: string; reason?: string }

/** The hand-offs that follow a signed `new` for what it cannot carry. */
export type FollowUpAction = Extract<HandoffAction, { action: 'details' | 'section' }>

export type HandoffStateName = 'prepared' | 'bound' | 'submitted' | 'cancelled' | 'expired'

export interface HandoffEvent {
  type: string
  actor: { kind: string; id: string; device: string | null }
  auth: string | null
  [field: string]: unknown
}

/** One event as the signer reviews it: the payload (every key, verbatim) and the values the signer derives itself. */
export interface HandoffReview {
  event_id: string
  type: string
  payload: Record<string, unknown>
  derived: Record<string, unknown>
}

/** What the host says the person is about to sign. This is the only source of the preview. */
export interface HandoffMaterial {
  kind: 'gate' | 'question' | 'payload'
  ticket?: { key: string; uid: string } | null
  gate?: string
  input?: Record<string, unknown>
  /** Host v1.5 (optional; absent = not shown): the gate policy the host states. */
  policy?: Record<string, unknown>
  question?: Record<string, unknown>
  texts?: Record<string, string>
  before?: Record<string, unknown>
  review: HandoffReview[]
}

export interface HandoffRequest {
  id: string
  code: string | null
  expires_at: string
  state: HandoffStateName
  op: string
  key: string | null
  fingerprint: string
  review_digest: string
  gate_hash: string | null
  material: HandoffMaterial
  events: HandoffEvent[]
  origin?: { uid: number; pid: number; client: string; created_at: string } | null
}

/** A written section is at most 64 KiB of UTF-8 (P1 and the host), counted in bytes like the server. */
export const MAX_SECTION_BYTES = 65536

/** `GET /handoff/<id>`: where the request is now (host `human.pending`). `request` only with `?full=1` while open. */
export interface HandoffState {
  id: string
  state: HandoffStateName
  op: string
  key: string | null
  expires_at: string
  event_ids: string[]
  seq: number | null
  /** Host v1.5, optional: why a signer's submit failed (a catalog code). */
  error?: { code: string } | null
}

type Json = null | boolean | number | string | Json[] | { [key: string]: Json }

/** Canonical JSON on the restricted subset: sorted keys, no spaces, strings escaped as RFC 8785 does. */
export function cj(value: unknown): string {
  if (value === null || typeof value === 'boolean') return JSON.stringify(value)
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) throw new Error('canonical JSON allows integers only')
    return String(value)
  }
  if (typeof value === 'string') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(cj).join(',')}]`
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, Json>)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${cj(v)}`).join(',')}}`
  }
  throw new Error('not canonical JSON')
}

export const reviewDigestOf = (material: unknown): string => `sha256:${sha256Hex(cj(material))}`

/** An event as the fingerprint covers it: without `sig`, `actor.device` and `auth` (and a grant's `secret_hash`). */
function stripped(event: HandoffEvent): unknown {
  const { sig: _sig, auth: _auth, actor, ...rest } = event as HandoffEvent & { sig?: unknown }
  const { device: _device, ...who } = actor
  const out: Record<string, unknown> = { ...rest, actor: who }
  if (event.type === 'grant.issued') delete out.secret_hash
  return out
}

export const fingerprintOf = (digest: string, events: HandoffEvent[], op: string): string =>
  sha256Hex(cj({ op, review_digest: digest, events: events.map(stripped) })).slice(0, 12)

const ENVELOPE = new Set(['v', 'id', 'type', 'actor', 'auth', 'hash_v', 'roster_v', 'based_on', 'sig'])
/** `gate_gen` leaves the payload on the three decision types only; it is shown on its own line there. */
const GATE_GEN_ENVELOPE = new Set(['gate.approved', 'gate.changes_requested', 'verdict.given'])

/** Each review block describes exactly its event: same id and type, and the payload is the event minus the envelope. */
function reviewMatches(material: HandoffMaterial, events: HandoffEvent[]): boolean {
  if (!Array.isArray(material.review) || material.review.length !== events.length) return false
  return material.review.every((block, i) => {
    const event = events[i]
    const payload = Object.fromEntries(Object.entries(event).filter(([k]) => !ENVELOPE.has(k) && !(k === 'gate_gen' && GATE_GEN_ENVELOPE.has(event.type))))
    return block.event_id === event.id && block.type === event.type && cj(block.payload) === cj(payload)
  })
}

const MATERIAL_KEYS: Record<string, [string[], string[]]> = {
  gate: [['kind', 'gate', 'input', 'policy', 'texts', 'ticket', 'review'], ['kind', 'gate', 'input', 'policy', 'texts', 'ticket', 'review']],
  question: [['kind', 'ticket', 'question', 'review'], ['kind', 'ticket', 'question', 'review']],
  payload: [['kind', 'ticket', 'review'], ['kind', 'ticket', 'review', 'texts', 'before']],
}

/** Fail closed on a top-level key outside the `Material` schema (host contract 0.8.1). */
function materialKeysOk(material: HandoffMaterial): boolean {
  const keys = MATERIAL_KEYS[material.kind]
  const have = Object.keys(material)
  return !!keys && keys[0].every((k) => have.includes(k)) && have.every((k) => keys[1].includes(k))
}

/** A request lives ten minutes on the host. A bad or far-off `expires_at` is not a request of this host. */
export function expiryMs(expiresAt: string, now = Date.now()): number | null {
  const at = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(expiresAt) ? Date.parse(expiresAt) : NaN
  return Number.isFinite(at) && at > now - 60_000 && at < now + 15 * 60_000 ? at : null
}

/** Only a request that has ended is excused the expiry window; anything else must still be live. */
const isOpen = (state: HandoffStateName) => state !== 'submitted' && state !== 'cancelled' && state !== 'expired'

/** True only when the digest, the fingerprint, the review and the expiry the server sent are what this browser computes. */
export function requestIsConsistent(request: HandoffRequest): boolean {
  try {
    const digest = reviewDigestOf(request.material)
    return (
      digest === request.review_digest &&
      fingerprintOf(digest, request.events, request.op) === request.fingerprint &&
      materialKeysOk(request.material) &&
      reviewMatches(request.material, request.events) &&
      // A retry with the same key returns the request in its current state: an ended one keeps the time it had.
      (isOpen(request.state) ? expiryMs(request.expires_at) !== null : /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(request.expires_at))
    )
  } catch {
    return false
  }
}

/** The word typed to confirm a ticket operation: the key as the host gives it, and the last 8 characters of the signed log's uid. */
export const confirmWord = (key: string, uid: string): string => `${key}/${uid.slice(-8)}`

/** The exact line the person runs. A code is 4+4 characters of the Crockford alphabet, so it is safe to put in a shell. */
export const CODE = /^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$/
/**
 * The word the signer asks for on a request that is not about a ticket: a member's person id for the member operations,
 * `GRANT.REVOKE` for a revoke, and `<PREFIX>~<WS8>/SETTINGS` or `<PREFIX>~<WS8>/POLICY` for the workspace settings and policy (stream
 * A's decision; WS8 = last 8 characters of the workspace id; the signer's header keeps the full id). `null` where this dashboard does not know the word.
 */
export function workspaceConfirmWord(request: Pick<HandoffRequest, 'op' | 'events' | 'material'>, workspace?: { prefix: string; id: string } | null): string | null {
  if (request.material.ticket) return null
  if (request.op === 'member.add' || request.op === 'member.role' || request.op === 'member.remove') {
    const person = request.events[0]?.person
    return typeof person === 'string' ? person : null
  }
  // Stream A: <PREFIX>~<WS8>/SETTINGS and <PREFIX>~<WS8>/POLICY, WS8 the last 8 characters of the workspace id.
  if (request.op === 'settings.set' || request.op === 'policy.set') return workspace ? `${workspace.prefix}~${workspace.id.slice(-8)}/${request.op === 'settings.set' ? 'SETTINGS' : 'POLICY'}` : null
  return request.op === 'grant.revoke' ? 'GRANT.REVOKE' : null
}

export const signCommand = (code: string): string => `orch sign ${code}`

const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'

/** `opts.idem` for one click: `a_` and 26 Crockford characters (a ULID shape; the first is 0..7). Reused on a retry. */
export function newIdem(random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
  const bytes = random(26)
  return 'a_' + [...bytes].map((b, i) => CROCKFORD[i === 0 ? b & 7 : b & 31]).join('')
}

export const IDEM = /^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/

/** Codes of a refusal that mean the work is already done (design 4.3): no signature is needed. */
export const ALREADY_DONE = new Set(['gate.already_approved', 'transition.refused'])
/**
 * Codes of the host about the KEY of a prepare (host contract 0.7): the same key with other arguments, and a key whose record
 * is gone (kept 24 h after the request ended). Neither is retried: the person starts again, with a new key, on their click.
 */
export const KEY_REUSED = 'idem.conflict'
export const KEY_TOO_OLD = 'idem.expired'

/** Codes that mean the state moved between prepare and submit: refetch, then offer to prepare again. */
export const CHANGED = new Set(['gate.stale', 'members.stale', 'question.stale', 'artifact.mismatch', 'observe.unavailable', 'source.missing', 'conflict.field', 'conflict.section'])
