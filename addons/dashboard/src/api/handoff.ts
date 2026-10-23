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
  | { action: 'accept_criterion'; key: string; ac: string; note?: string }
  | { action: 'withdraw_acceptance'; key: string; ac: string; reason: string }
  | { action: 'mark_task_done'; key: string; task: string }
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
  /**
   * B11 (finding 9): a new ticket WITH its fields and written text on ONE review and ONE confirmation. The signer signs
   * `ticket.created`, the fields and one `ticket.updated` per written section, each checked against this intent.
   */
  | { action: 'new_sequence'; title: string; type: 'feature' | 'bug' | 'chore' | 'spike' | 'epic'; priority?: 'low' | 'high' | 'urgent'; size?: 'xs' | 's' | 'm' | 'l' | 'xl'; labels?: string[]; parent?: string; sections: Partial<Record<'summary' | 'context' | 'requirements' | 'out_of_scope' | 'plan' | 'decisions' | 'verification' | 'findings' | 'current_state', string>>; acceptance?: string[]; people?: { owner?: string; assignees?: string[]; reviewers?: string[] }; agent?: { hours: number }; images?: { name: string; sha256: string; bytes: number; alt: string }[] }
  /**
   * B12D (N3): an edit of an EXISTING ticket on ONE review and ONE confirmation: the changed sections (their new text), the
   * criteria to change or add, the repositories (and a branch each) the ticket is linked to, and the people to add or remove. `head_seq` is the log head the page showed: the server
   * refuses the edit when the ticket has changed since.
   */
  | { action: 'edit_sequence'; key: string; head_seq: number; sections?: Partial<Record<'summary' | 'context' | 'requirements' | 'out_of_scope' | 'plan' | 'decisions' | 'verification' | 'findings' | 'current_state', string>>; acceptance?: { edit: Record<string, string>; add: string[] }; tasks?: { edit: Record<string, { text?: string; verify?: string | null; proves?: string[] }>; add: { text: string; verify?: string; proves: string[] }[] }; links?: { repos: string[]; branches: Record<string, string> }; people?: Partial<Record<'owner' | 'assignees' | 'reviewers', { add: string[]; remove: string[] }>>; images?: { name: string; sha256: string; bytes: number; alt: string }[] }
  /** B11 step (ii): a quick task on ONE review and ONE confirmation: ticket, requirements, plan, both approvals and, if asked, agent access last. */
  | { action: 'quick_start'; title: string; description?: string; checks?: { text: string; cmd?: string }[]; agent?: { hours: number } }
  /** `via: 'estimate'`: the Estimate box (size only, 'none' = not estimated); the server refuses it while the add-on is off. */
  | { action: 'details'; key: string; priority?: 'low' | 'medium' | 'high' | 'urgent'; size?: 'xs' | 's' | 'm' | 'l' | 'xl' | 'none'; labels?: string[]; parent?: string; via?: 'estimate' }
  /** One body section, replaced (the description follow-up, `section.set`). Spikes keep "findings" where other types have "verification". */
  | { action: 'section'; key: string; section: 'summary' | 'context' | 'requirements' | 'out_of_scope' | 'plan' | 'decisions' | 'verification' | 'findings' | 'current_state'; text: string }

  /** Reduced workspace settings (M4). Repos have their own actions (`repo_set`, `repo_remove`). */
  | { action: 'settings_set'; sign_methods?: import('./signing').SignMethod[]; grant_hours?: number; claim_ttl_min?: number; lease_ttl_min?: number }
  /** The workspace icon (`settings.set` of `icon`, a click): an icon and a colour of the closed lists, or null for the automatic one. */
  | { action: 'icon_set'; icon: { icon: string; colour: string } | null }
  /** Model routing: the model per agent and mode (`settings.set` of `models`, the typed word). An empty table clears it. */
  | { action: 'models_set'; models: import('./modelRouting').ModelTable }
  /** Add or change one workspace repository (Repos add-on); the host checks the folder before anything is signed. */
  | { action: 'repo_set'; name: string; path: string; remote?: string; default_branch?: string }
  /** Take one repository off the list. The folder and its files stay on disk. */
  | { action: 'repo_remove'; name: string }
  /** The workspace default policy of one gate (F1 5.7 `Policy`, exactly). The code gate: `not` has `assignees`, `independent` is true. */
  | { action: 'policy_set'; gate: 'requirements' | 'plan' | 'verify' | 'code'; policy: { approvers: string[]; count: number; not: string[]; applies: 'all' | 'off' | string[]; independent: boolean } }
  /** The invitee's own public key (base64url, 65 bytes) and first device certificate (strict JSON text): their files, uploaded by the person. */
  | { action: 'member_add'; name: string; role: 'owner' | 'maintainer' | 'member' | 'viewer'; pk: string; cert: string }
  | { action: 'member_role'; person: string; role: 'owner' | 'maintainer' | 'member' | 'viewer' }
  | { action: 'member_remove'; person: string }
  | { action: 'grant_revoke'; grant: string; reason?: string }
  /** R2-2: turn a shipped add-on on (`addon.grant`, the typed word) or off (`addon.disable`, one click). */
  | { action: 'addon_grant'; name: 'start-agent' | 'terminals' | 'worktrees' | 'publish' | 'usage' | 'estimate' | 'explorer' | 'model-routing' }
  | { action: 'addon_disable'; name: 'start-agent' | 'terminals' | 'worktrees' | 'publish' | 'usage' | 'estimate' | 'explorer' | 'model-routing' }
  /** Turn on an add-on that needs settings: the settings and the grant in ONE review (`addon_enable` sequence). Publish: terminal only. */
  | { action: 'addon_enable'; name: 'publish' | 'model-routing'; publish?: { server: string; share: string }; models?: import('./modelRouting').ModelTable }
  /** Publish (P6): where shared files are sent and where a link opens (`settings.set` of `publish`). Terminal only. */
  | { action: 'publish_setup'; server: string; share: string }
  /** Publish (P6): a person's signature over the exact words of one staged copy (`publish.share`). Terminal only. */
  | { action: 'publish_share'; key: string; staged_id: string }
  /** The runner grant, signed in the terminal where one click is not used (`orch sign` hands the secret to the runner). */
  | { action: 'runner_grant'; hours: number; scope: 'workable' | 'all'; extend?: string }
  /** pair/1 (contract v1.9): sign `relay.pair_device` (F1 `device.added`) for an offer whose certificate is in. */
  | { action: 'pair_device'; offer: string }

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
  grants?: Record<string, unknown>
  /** B11: the whole intent of a sequence (bound by `review_digest`); the first request of one signer's run. */
  sequence?: { digest: string; of: number; intent: Record<string, unknown> }
  /** #740: the manual acceptance statement file of `ac.accept` / `ac.unaccept` (bound by `review_digest`). */
  statement?: string
  /** #740: a verify gate's manual acceptances, per host (bound by `review_digest`). */
  manual_acceptances?: { ac: string; name: string; by: string }[]
  review: HandoffReview[]
}

export interface HandoffRequest {
  headline?: string
  content?: import('./signing').SignContent
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
/** Core's limits of one sequence (orch.host.sequence): criteria a sequence adds, and signatures in all. */
export const MAX_CRITERIA = 8
export const MAX_SEQUENCE_STEPS = 16

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
  /** B11: a sequence (one review, N signatures) that has landed `done` of its `of` steps; absent for other requests. */
  progress?: { done: number; of: number; last?: 'grant' } | null
  /** One click only: where the runner grant's secret went (a sequence that ends with agent access). */
  deposit?: 'confirmed' | 'rejected' | 'unknown'
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

/** kind -> [required, allowed]: core signer `_MATERIAL_KEYS` for these kinds (a server test keeps them equal). */
const MATERIAL_KEYS: Record<string, [string[], string[]]> = {
  gate: [['kind', 'gate', 'input', 'policy', 'texts', 'ticket', 'review'], ['kind', 'gate', 'input', 'policy', 'texts', 'ticket', 'review', 'manual_acceptances']],
  question: [['kind', 'ticket', 'question', 'review'], ['kind', 'ticket', 'question', 'review']],
  payload: [['kind', 'ticket', 'review'], ['kind', 'ticket', 'review', 'texts', 'before', 'sequence', 'statement', 'grants']],
}
/** The only ops whose material may carry a manual acceptance `statement` (core signer `_person_op`). */
const STATEMENT_OPS = ['ac.accept', 'ac.unaccept']

const manualRowOk = (row: unknown): boolean =>
  !!row && typeof row === 'object' && !Array.isArray(row) &&
  cj(Object.keys(row).sort()) === cj(['ac', 'by', 'name']) &&
  Object.values(row).every((v) => typeof v === 'string')

/** Fail closed on a top-level key outside the `Material` schema (host contract 0.8.1), and on a `statement` or
 * `manual_acceptances` where the signer does not allow one (the same rules as the server's `material_keys_ok`). */
function materialKeysOk(material: HandoffMaterial, op: string): boolean {
  const keys = MATERIAL_KEYS[material.kind]
  const have = Object.keys(material)
  if (!keys || !keys[0].every((k) => have.includes(k)) || !have.every((k) => keys[1].includes(k))) return false
  if ('statement' in material && (!STATEMENT_OPS.includes(op) || typeof material.statement !== 'string')) return false
  if ('manual_acceptances' in material) {
    const rows: unknown = material.manual_acceptances
    return material.gate === 'verify' && Array.isArray(rows) && rows.every(manualRowOk)
  }
  return true
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
      materialKeysOk(request.material, request.op) &&
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

/**
 * The word a terminal asks for when the request is one step of a sequence: `GRANT` when the reviewed intent includes
 * agent access (the signer's own `confirm_word`: the strongest step's word), else null (the ticket's own word applies).
 */
export const sequenceWord = (material: Pick<HandoffMaterial, 'sequence'>): string | null => {
  const intent = material.sequence?.intent as { agent?: unknown } | undefined
  return intent && intent.agent !== null && intent.agent !== undefined ? 'GRANT' : null
}

/** The exact line the person runs. A code is 4+4 characters of the Crockford alphabet, so it is safe to put in a shell. */
export const CODE = /^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$/
/**
 * The word the signer asks for on a request that is not about a ticket: a member's person id for the member operations,
 * `GRANT.REVOKE` for a revoke, and `<PREFIX>~<WS8>/SETTINGS` or `<PREFIX>~<WS8>/POLICY` for the workspace settings and policy (stream
 * A's decision; WS8 = last 8 characters of the workspace id; the signer's header keeps the full id). `null` where this dashboard does not know the word.
 */
export function workspaceConfirmWord(request: Pick<HandoffRequest, 'op' | 'events' | 'material'>, workspace?: { prefix: string; id: string } | null): string | null {
  // A sequence that gives agents access (Extend, Start again, setup): the terminal asks for GRANT, whatever the first step is.
  const sequence = sequenceWord(request.material)
  if (sequence) return sequence
  if (request.material.ticket) return null
  const intent = request.material.sequence?.intent
  if (intent?.kind === 'addon_enable' && typeof intent.name === 'string') return workspace ? `${intent.name}~${workspace.id.slice(-8)}` : null
  if (request.op === 'member.add' || request.op === 'member.role' || request.op === 'member.remove') {
    const person = request.events[0]?.person
    return typeof person === 'string' ? person : null
  }
  // Stream A: <PREFIX>~<WS8>/SETTINGS and <PREFIX>~<WS8>/POLICY, WS8 the last 8 characters of the workspace id.
  if (request.op === 'settings.set' || request.op === 'policy.set') return workspace ? `${workspace.prefix}~${workspace.id.slice(-8)}/${request.op === 'settings.set' ? 'SETTINGS' : 'POLICY'}` : null
  // R2-2: <name>~<WS8>, the add-on's name from the event the host prepared (core's signer.confirm_word builds the same word; src/test/confirm-words.json pins it).
  if (request.op === 'addon.grant' || request.op === 'addon.purge') {
    const name = request.events[0]?.name
    return workspace && typeof name === 'string' ? `${name}~${workspace.id.slice(-8)}` : null
  }
  // Turning an add-on off: the terminal asks for the add-on's plain name (signer.confirm_word).
  if (request.op === 'addon.disable') {
    const name = request.events[0]?.name
    return typeof name === 'string' ? name : null
  }
  // Agent access (Extend, Start again, a plain runner grant): the signer's word is GRANT (signer.confirm_word, op "grant").
  if (request.op === 'grant') return 'GRANT'
  return request.op === 'grant.revoke' ? 'GRANT.REVOKE' : null
}

export const signCommand = (code: string): string => `orch sign ${code}`

/** Codes of a refusal that mean the work is already done (design 4.3): no signature is needed. */
export const ALREADY_DONE = new Set(['gate.already_approved', 'transition.refused', 'addon.already'])
/**
 * The server derives the host's idem from the action itself (one action intent -> at most one request), so the browser sends
 * no key. These are its answers about the intent: it is already done (a request of it was signed), its request ended
 * (cancelled or expired), or the host holds an attempt that cannot be reached from here (another signing path or device binding).
 * The first two are left only by an explicit click ("Do it again", "Start again"), which advances `retry`.
 */
export const INTENT_DONE = 'intent.done'
export const INTENT_ENDED = 'intent.ended'
export const INTENT_OPEN = 'intent.open'

/** Codes that mean the state moved between prepare and submit: refetch, then offer to prepare again. */
export const CHANGED = new Set(['gate.stale', 'members.stale', 'question.stale', 'artifact.mismatch', 'observe.unavailable', 'source.missing', 'conflict.field', 'conflict.section'])
