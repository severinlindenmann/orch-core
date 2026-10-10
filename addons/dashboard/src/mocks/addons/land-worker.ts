// The land addon's landing worker (D53), as plain state the mock advances one step at a time. One serial worker:
// at most one attempt runs at a time, across every queue. A queue is one (canonical remote, target branch).
//  - An attempt rebases the approved source onto the target head. A clean rebase keeps the approval; the checks (T2
//    integration checks, then the pull request's CI) run on that exact candidate. If the target moved meanwhile, the
//    candidate is rebuilt and checked again (outcome `requeued`). All green on an unchanged target: merged.
//  - A conflict or a red check fails the attempt: the ticket gets a "needs" item for its agent and the queue moves on.
//    Any resolution or fix is new code, so core voids the verify approval (store.landingResolved) and the ticket goes
//    back to review with the resolution diff. The worker never writes gate events itself.
//  - Stacked tickets land parent first; a descendant whose parent failed waits and never lands without it.
//  - A merge is recorded (phase 1) before the queue is tidied (phase 2). After a crash the worker finds the recorded
//    outcome and only tidies: it never merges twice.
//  - Pushes go only to the ticket's own branch (--force-with-lease); the worker never pushes to a shared branch.
import { fnvHex } from '../derive'
import type { MockStore } from '../store'

export const ADDON = { kind: 'addon', id: 'land' } as const
export const REMOTES = { dbt: 'github.com/acme-energy/energy-dbt', api: 'github.com/acme-energy/billing-api' } as const
/** Never an allowed landing target (D33): people merge to main by hand. */
export const NEVER_TARGET = 'main'
export const DEFAULT_TIMEOUT_MIN = 30

export type CheckResult = 'pass' | 'fail' | 'running' | 'waiting' | 'timeout'
export interface Check {
  name: string
  level: 'T2' | 'CI'
  result: CheckResult
  url: string
  seconds?: number
}
export type Outcome = 'merged' | 'failed' | 'requeued'
export type Reason = 'conflict' | 'red_checks' | 'timeout' | 'target_moved'
/** What the simulated world does to an entry's next attempts, in order (seeds and the demo worker only). */
export type Script = 'clean' | 'moves' | 'conflict' | 'red' | 'timeout'

export interface Attempt {
  n: number
  ticket: string
  queue: string
  remote: string
  target: string
  source_sha: string
  target_sha: string
  candidate_sha: string
  rebase: 'clean' | 'conflict'
  checks: Check[]
  outcome: Outcome | null
  reason?: Reason
  /** The file a conflict was in. */
  file?: string
  started: string
  ended?: string
  timeout_at: string
  /** What the simulated world does to this attempt (not shown). */
  script?: Script
}
export interface Entry {
  ticket: string
  branch: string
  source_sha: string
  enqueued: string
  by: string
  /** The ticket whose branch this one is stacked on: it lands first. */
  stacked_on?: string
  script?: Script[]
}
export interface Queue {
  id: string
  remote: string
  target: string
  /** The target branch's head as the worker last saw it. */
  head: string
  entries: Entry[]
  /** Entries from other workspaces on this machine that share the queue (shown as a count only). */
  foreign: number
}
export interface Need {
  id: string
  ticket: string
  kind: 'conflict' | 'red_checks'
  attempt: number
  /** The agent that picks it up (`agent:session:person`), or null when nobody has. */
  agent: string | null
  /** A person must resolve it (the agent could not, or there is none). */
  person: boolean
  /** A person already answered the Today decision (an agent or that person has it now). */
  decided?: boolean
  /** The person who took it to resolve by hand ("I will resolve it"); they record the resolution with Mark resolved. */
  owner?: string
  /** The agent saw it on a worker step and resolves it on the next. */
  picked?: boolean
  open: boolean
  at: string
  file: string
  check?: string
}
export interface Resolution {
  ticket: string
  attempt: number
  kind: 'conflict' | 'red_checks'
  by: string
  at: string
  file: string
  diff: string
}
export interface LandState {
  settings: { targets: string[]; timeout_minutes: number }
  queues: Queue[]
  attempts: Attempt[]
  needs: Need[]
  resolutions: Resolution[]
  worker: { current: number | null; resumed: { from: number; at: string } | null }
  seq: number
  [k: string]: unknown
}

export const sha = (...parts: string[]) => fnvHex(parts.join('|'), 7)
export const asLand = (state: Record<string, unknown>) => state as LandState

/** Which repository a ticket's branch lives in: the billing epic's children and billing work go to billing-api. */
export function remoteOf(store: MockStore, key: string): string {
  const t = store.ticket(key)
  return t && (t.parent === 'DEMO-0050' || t.labels.includes('billing')) ? REMOTES.api : REMOTES.dbt
}

const slug = (title: string) =>
  title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 32)
    .replace(/-+$/, '')
export const branchOf = (store: MockStore, key: string) => `feat/${key}-${slug(store.ticket(key)?.title ?? key)}`

/**
 * The commit the verdict signed (owner decision 2026-10-10): the `source_sha` of the ticket's latest verify approval,
 * read from core's gate record (a voided one too, for the history of earlier attempts). Never inferred.
 */
export function signedSource(store: MockStore, key: string): string {
  const v = store.ticket(key)?.gates.verify
  const all = [...(v?.voided ?? []), ...(v?.approvals ?? [])].sort((a, b) => a.at.localeCompare(b.at))
  return all[all.length - 1]?.source_sha ?? ''
}

/** The last agent that worked on the ticket (`agent:session:person`), or null. */
export function agentOf(store: MockStore, key: string): string | null {
  const e = [...store.eventsOf(key)].reverse().find((x) => x.actor.kind === 'agent')
  return e && e.actor.kind === 'agent' ? `${e.actor.id}:${e.actor.session}:${e.actor.for}` : null
}

/**
 * Only tickets with a standing verify approval (the verdict) enter and land, on the commit it signed; with the code
 * review gate on for the ticket, that approval must stand too, on the same commit.
 */
export function approved(store: MockStore, key: string): boolean {
  const t = store.ticket(key)
  if (!t || t.status !== 'done' || t.gates.verify.state !== 'approved') return false
  const code = t.gates.code
  return !code.required || (code.state === 'approved' && code.source_sha === t.gates.verify.source_sha)
}

export function queueFor(state: LandState, remote: string, target: string): Queue {
  let q = state.queues.find((x) => x.remote === remote && x.target === target)
  if (!q) {
    q = { id: `q-${sha(remote, target)}`, remote, target, head: sha(remote, target, 'head'), entries: [], foreign: 0 }
    state.queues.push(q)
  }
  return q
}

export function checksFor(remote: string, candidate: string): Check[] {
  const run = parseInt(candidate, 16) % 900_000_000
  return [
    { name: 'T2 integration checks', level: 'T2', result: 'waiting', url: `https://${remote}/commit/${candidate}/checks` },
    { name: 'CI on the pull request', level: 'CI', result: 'waiting', url: `https://${remote}/actions/runs/${1_000_000_000 + run}` },
  ]
}

const FILES: Record<string, string> = { [REMOTES.dbt]: 'models/marts/fct_billing.sql', [REMOTES.api]: 'models/marts/fct_invoice_lines.sql' }
export const conflictFile = (remote: string) => FILES[remote] ?? 'README.md'

/** The unified diff a resolution leaves (diff widget). */
export function resolutionDiff(kind: 'conflict' | 'red_checks', file: string): string {
  if (kind === 'red_checks')
    return ['@@ -31,4 +31,5 @@', '   select', '     line_id,', '-    amount_chf', '+    round(amount_chf, 2) as amount_chf,', '+    billing_run_id', '   from lines'].join('\n')
  return [
    `@@ -18,6 +18,7 @@ ${file.split('/').pop()}`,
    '   select',
    '     l.line_id,',
    '-    l.tariff_id,',
    '+    l.tariff_version_id,',
    '+    r.billing_run_id,',
    '     l.amount_chf',
    '   from invoice_lines l',
  ].join('\n')
}

export const attemptOf = (state: LandState, n: number | null) => (n === null ? undefined : state.attempts.find((a) => a.n === n))
const queueOf = (state: LandState, id: string) => state.queues.find((q) => q.id === id)

/** Was the ticket merged since its approval was last voided? */
export function mergedNow(state: LandState, key: string): Attempt | undefined {
  const lastVoid = state.resolutions.filter((r) => r.ticket === key).reduce((m, r) => Math.max(m, r.attempt), 0)
  return [...state.attempts].reverse().find((a) => a.ticket === key && a.outcome === 'merged' && a.n > lastVoid)
}

/** Why a queued entry cannot go yet: its parent is ahead in a queue (`ahead`) or failed to land (`failed`). */
export function blockOf(state: LandState, e: Entry): { parent: string; why: 'ahead' | 'failed' } | null {
  const p = e.stacked_on
  if (!p || mergedNow(state, p)) return null
  const queued = state.queues.some((q) => q.entries.some((x) => x.ticket === p)) || attemptOf(state, state.worker.current)?.ticket === p
  return { parent: p, why: queued ? 'ahead' : 'failed' }
}

function landEvent(store: MockStore, a: Attempt) {
  store.append(a.ticket, {
    type: 'land.attempt',
    actor: ADDON,
    attempt: a.n,
    ticket: a.ticket,
    remote: a.remote,
    target: a.target,
    source_sha: a.source_sha,
    target_sha: a.target_sha,
    candidate_sha: a.candidate_sha,
    checks: a.checks.map((c) => ({ name: c.name, result: c.result, url: c.url })),
    outcome: a.outcome,
    ...(a.reason ? { reason: a.reason } : {}),
  })
}

function openNeed(store: MockStore, state: LandState, a: Attempt, kind: Need['kind'], check?: string) {
  const agent = agentOf(store, a.ticket)
  state.needs.push({ id: `N-${a.n}`, ticket: a.ticket, kind, attempt: a.n, agent, person: !agent, open: true, at: store.now(), file: a.file ?? conflictFile(a.remote), ...(check ? { check } : {}) })
}

/** Phase 2 of an ended attempt: the entry leaves its queue and the worker is free. Safe to run twice. */
export function tidy(state: LandState, a: Attempt) {
  const q = queueOf(state, a.queue)
  if (q && a.outcome !== 'requeued') q.entries = q.entries.filter((e) => e.ticket !== a.ticket)
  if (state.worker.current === a.n) state.worker.current = null
}

/** Phase 1 of a merge: the outcome is recorded and the target head moves to the candidate. */
export function recordMerge(store: MockStore, state: LandState, a: Attempt) {
  a.outcome = 'merged'
  a.ended = store.now()
  const q = queueOf(state, a.queue)
  if (q) q.head = a.candidate_sha
  landEvent(store, a)
}

/** Start an attempt for `e` in `q`: rebase onto the head (a conflict fails it at once), then T2 starts. */
export function startAttempt(store: MockStore, state: LandState, q: Queue, e: Entry): Attempt {
  const now = store.now()
  const script = e.script?.shift() ?? 'clean'
  const n = ++state.seq
  const candidate = sha(e.source_sha, q.head)
  const a: Attempt = {
    n,
    ticket: e.ticket,
    queue: q.id,
    remote: q.remote,
    target: q.target,
    source_sha: e.source_sha,
    target_sha: q.head,
    candidate_sha: candidate,
    rebase: script === 'conflict' ? 'conflict' : 'clean',
    checks: script === 'conflict' ? [] : checksFor(q.remote, candidate),
    outcome: null,
    started: now,
    timeout_at: new Date(Date.parse(now) + state.settings.timeout_minutes * 60_000).toISOString().replace('.000Z', 'Z'),
    script,
  }
  state.attempts.push(a)
  state.worker.current = n
  if (script === 'conflict') {
    a.file = conflictFile(q.remote)
    a.outcome = 'failed'
    a.reason = 'conflict'
    a.ended = now
    landEvent(store, a)
    openNeed(store, state, a, 'conflict')
    tidy(state, a)
  } else a.checks[0].result = 'running'
  return a
}

/** The next entry the worker takes: the oldest eligible one across queues (approved, parent landed, target allowed). */
function nextEntry(store: MockStore, state: LandState): { q: Queue; e: Entry } | null {
  let best: { q: Queue; e: Entry } | null = null
  for (const q of state.queues) {
    if (!state.settings.targets.includes(q.target)) continue
    // An entry whose approval no longer stands, or stands for another commit, leaves the queue.
    for (const e of [...q.entries]) {
      if (!approved(store, e.ticket) || e.source_sha !== signedSource(store, e.ticket)) {
        q.entries = q.entries.filter((x) => x !== e)
        store.append(e.ticket, { type: 'land.dequeued', actor: ADDON, reason: 'the approval no longer stands' })
      }
    }
    const e = q.entries.find((x) => !blockOf(state, x))
    if (e && (!best || e.enqueued < best.e.enqueued)) best = { q, e }
  }
  return best
}

/** Core voids the approval for a resolution; the addon records the resolution and its diff. */
export function resolve(store: MockStore, state: LandState, need: Need, by: string): Resolution {
  const r: Resolution = { ticket: need.ticket, attempt: need.attempt, kind: need.kind, by, at: store.now(), file: need.file, diff: resolutionDiff(need.kind, need.file) }
  state.resolutions.push(r)
  need.open = false
  store.append(need.ticket, { type: 'land.resolved', actor: ADDON, attempt: need.attempt, kind: need.kind, file: need.file, by })
  // Core checks the attempt and the resolution in the log and writes its own reason (store.landingResolved).
  if (store.ticket(need.ticket)?.gates.verify.state === 'approved') store.landingResolved(need.ticket, { addon: 'land', attempt: need.attempt })
  return r
}

const AGENT_LABEL: Record<string, string> = { 'claude-code': 'Claude Code', codex: 'Codex' }
export const agentLabel = (agent: string) => AGENT_LABEL[agent.split(':')[0]] ?? agent.split(':')[0]

/** Move a running attempt on by one check (or end it). */
function progress(store: MockStore, state: LandState, a: Attempt): string {
  const q = queueOf(state, a.queue)!
  const now = store.now()
  const running = a.checks.find((c) => c.result === 'running')
  if (running) {
    const fails = running.level === 'CI' && (a.script === 'red' || a.script === 'timeout')
    running.result = fails ? (a.script === 'timeout' ? 'timeout' : 'fail') : 'pass'
    running.seconds = running.level === 'T2' ? 384 : fails && a.script === 'timeout' ? state.settings.timeout_minutes * 60 : 512
    // The target moved while the candidate was checked (a push outside the queue): rebuild and check again.
    if (running.level === 'T2' && a.script === 'moves') {
      q.head = sha(q.head, 'moved', now)
      a.outcome = 'requeued'
      a.reason = 'target_moved'
      a.ended = now
      landEvent(store, a)
      tidy(state, a)
      return `#${a.n} ${a.ticket}: ${a.target} moved, candidate rebuilt`
    }
    const next = a.checks.find((c) => c.result === 'waiting')
    if (!fails && next) {
      next.result = 'running'
      return `#${a.n} ${a.ticket}: ${running.level} passed`
    }
  }
  if (a.checks.some((c) => c.result === 'running' || c.result === 'waiting')) return `#${a.n} ${a.ticket}: checking`
  const bad = a.checks.find((c) => c.result === 'fail' || c.result === 'timeout')
  if (bad) {
    a.outcome = 'failed'
    a.reason = bad.result === 'timeout' ? 'timeout' : 'red_checks'
    a.ended = now
    landEvent(store, a)
    openNeed(store, state, a, 'red_checks', bad.name)
    tidy(state, a)
    return `#${a.n} ${a.ticket}: red checks`
  }
  if (q.head !== a.target_sha) {
    a.outcome = 'requeued'
    a.reason = 'target_moved'
    a.ended = now
    landEvent(store, a)
    tidy(state, a)
    return `#${a.n} ${a.ticket}: ${a.target} moved, candidate rebuilt`
  }
  recordMerge(store, state, a)
  tidy(state, a)
  return `#${a.n} ${a.ticket}: merged`
}

/**
 * One step of the serial worker. Returns what happened, or null when there is nothing to do.
 * Order: finish a recorded outcome left by a crash (never merge again), move the current attempt on, let an agent
 * resolve a need it picked up, then start the next attempt.
 */
export function step(store: MockStore, state: LandState): string | null {
  const current = attemptOf(state, state.worker.current)
  if (current?.outcome) {
    tidy(state, current)
    state.worker.resumed = { from: current.n, at: store.now() }
    return `resumed from attempt #${current.n}, no repeat merge`
  }
  if (current) return progress(store, state, current)
  state.worker.current = null
  const need = state.needs.find((x) => x.open && !x.person && !x.owner && x.agent && x.picked)
  if (need) {
    resolve(store, state, need, need.agent!)
    return `${need.ticket}: ${agentLabel(need.agent!)} resolved the ${need.kind === 'conflict' ? 'conflict' : 'red checks'}`
  }
  for (const x of state.needs) if (x.open && !x.person && !x.owner && x.agent) x.picked = true
  const next = nextEntry(store, state)
  if (!next) return null
  const a = startAttempt(store, state, next.q, next.e)
  return a.outcome ? `#${a.n} ${a.ticket}: conflict` : `#${a.n} ${a.ticket}: checking`
}
