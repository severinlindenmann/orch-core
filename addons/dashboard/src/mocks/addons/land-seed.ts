// Seeds of the land addon: the landing history as attempt records (no events are written while seeding; the
// matching core events — verdicts, the voided approval of DEMO-0053 — are in the demo fixtures).
//  Normal day (DEMO): #9–#11 merged; DEMO-0053's candidate was rebuilt when develop moved (#12), then hit a conflict
//  (#13) that Claude Code resolved, which voided its approval (back to review); DEMO-0052 is being checked now (#14).
//  Busy day: the same, plus the generated tickets that passed their verdict — a long merged history with rebuilt
//  candidates, one red check and one conflict waiting for a person, a stacked pair, a descendant blocked by its
//  failed parent, and the worker's crash-resume note.
import type { MockStore } from '../store'
import type { Rng } from '../busy/rng'
import {
  agentOf,
  approved,
  branchOf,
  checksFor,
  conflictFile,
  DEFAULT_TIMEOUT_MIN,
  queueFor,
  remoteOf,
  REMOTES,
  resolutionDiff,
  sha,
  sourceOf,
  type Attempt,
  type LandState,
  type Script,
} from './land-worker'

interface Spec {
  ticket: string
  /** Earliest start (the verdict, or when someone queued it). */
  at: string
  /** One attempt per script, in order. */
  plan: Script[]
  /** `current`: the last attempt is running now; `queued`: no attempt yet. */
  final?: 'current' | 'queued'
  stacked_on?: string
  /** The need is the agent's but a person must resolve it (the agent could not). */
  person?: boolean
  /** The need was resolved by this agent at `resolvedAt` (the approval was voided by core). */
  resolved?: boolean
  remote?: string
}

const plus = (iso: string, min: number) => new Date(Date.parse(iso) + min * 60_000).toISOString().replace('.000Z', 'Z')
const later = (a: string, b: string) => (a > b ? a : b)

export function emptyState(): LandState {
  return {
    settings: { targets: ['develop'], timeout_minutes: DEFAULT_TIMEOUT_MIN },
    queues: [],
    attempts: [],
    needs: [],
    resolutions: [],
    worker: { current: null, resumed: null },
    seq: 0,
  }
}

/** Replays specs into attempt records, serially (one worker), numbering from `first`. */
function build(store: MockStore, state: LandState, specs: Spec[], first: number) {
  let n = first - 1
  let cursor = ''
  const minutes = state.settings.timeout_minutes
  for (const s of [...specs].sort((a, b) => a.at.localeCompare(b.at))) {
    const q = queueFor(state, s.remote ?? remoteOf(store, s.ticket), 'develop')
    const source = sourceOf(store, s.ticket)
    const entry = { ticket: s.ticket, branch: branchOf(store, s.ticket), source_sha: source, enqueued: s.at, by: 'p_sev', ...(s.stacked_on ? { stacked_on: s.stacked_on } : {}) }
    if (s.final === 'queued') {
      q.entries.push({ ...entry, ...(s.plan.length ? { script: [...s.plan] } : {}) })
      continue
    }
    s.plan.forEach((script, i) => {
      const start = later(cursor, s.at)
      const candidate = sha(source, q.head)
      const a: Attempt = {
        n: ++n,
        ticket: s.ticket,
        queue: q.id,
        remote: q.remote,
        target: q.target,
        source_sha: source,
        target_sha: q.head,
        candidate_sha: candidate,
        rebase: script === 'conflict' ? 'conflict' : 'clean',
        checks: script === 'conflict' ? [] : checksFor(q.remote, candidate),
        outcome: null,
        started: start,
        timeout_at: plus(start, minutes),
      }
      const [t2, ci] = a.checks
      const current = s.final === 'current' && i === s.plan.length - 1
      let end = start
      if (current) {
        t2.result = 'pass'
        t2.seconds = 384
        ci.result = 'running'
        state.worker.current = a.n
        q.entries.push(entry)
      } else if (script === 'conflict') {
        a.outcome = 'failed'
        a.reason = 'conflict'
        a.file = conflictFile(q.remote)
        end = plus(start, 2)
      } else if (script === 'moves') {
        t2.result = 'pass'
        t2.seconds = 384
        a.outcome = 'requeued'
        a.reason = 'target_moved'
        q.head = sha(q.head, 'moved', start)
        end = plus(start, 7)
      } else if (script === 'red' || script === 'timeout') {
        t2.result = 'pass'
        t2.seconds = 384
        ci.result = script === 'red' ? 'fail' : 'timeout'
        ci.seconds = script === 'red' ? 431 : minutes * 60
        a.outcome = 'failed'
        a.reason = script === 'red' ? 'red_checks' : 'timeout'
        end = plus(start, script === 'red' ? 14 : minutes + 7)
      } else {
        t2.result = 'pass'
        t2.seconds = 384
        ci.result = 'pass'
        ci.seconds = 512
        a.outcome = 'merged'
        q.head = candidate
        end = plus(start, 15)
      }
      if (a.outcome) a.ended = end
      state.attempts.push(a)
      cursor = end
      if (a.outcome === 'failed') {
        const kind = a.reason === 'conflict' ? 'conflict' : 'red_checks'
        const agent = agentOf(store, s.ticket)
        const file = a.file ?? conflictFile(q.remote)
        state.needs.push({ id: `N-${a.n}`, ticket: s.ticket, kind, attempt: a.n, agent, person: !!s.person || !agent, open: !s.resolved, at: end, file, ...(kind === 'red_checks' ? { check: ci.name } : {}) })
        if (s.resolved && agent) state.resolutions.push({ ticket: s.ticket, attempt: a.n, kind, by: agent, at: plus(end, 8), file, diff: resolutionDiff(kind, file) })
      }
    })
  }
  state.seq = n
}

/** The normal day's specs (DEMO fixture tickets). */
const FIXTURE_SPECS: Spec[] = [
  { ticket: 'DEMO-0038', at: '2026-09-29T10:10:00Z', plan: ['clean'] },
  { ticket: 'DEMO-0042', at: '2026-10-03T08:35:00Z', plan: ['clean'] },
  { ticket: 'DEMO-0051', at: '2026-10-07T15:05:00Z', plan: ['clean'] },
  { ticket: 'DEMO-0053', at: '2026-10-09T10:41:00Z', plan: ['moves', 'conflict'], resolved: true },
  { ticket: 'DEMO-0052', at: '2026-10-09T11:20:00Z', plan: ['clean'], final: 'current' },
]

const REMOTE_ORDER = [REMOTES.dbt, REMOTES.api]
const isDemo = (store: MockStore, ws: string) => store.workspaces.find((w) => w.id === ws)?.prefix === 'DEMO'
const present = (store: MockStore, ws: string, specs: Spec[]) => specs.filter((s) => store.workspaceOf(s.ticket)?.id === ws)

export function seedLand(ws: string, store: MockStore): LandState {
  const state = emptyState()
  if (!isDemo(store, ws)) return state
  queueFor(state, REMOTE_ORDER[0], 'develop').foreign = 1 // a ticket of another workspace on this machine waits here
  build(store, state, present(store, ws, FIXTURE_SPECS), 9)
  return state
}
/** When the verdict made the ticket done (its last move to done). */
const doneAt = (store: MockStore, key: string) => [...store.eventsOf(key)].reverse().find((e) => e.type === 'status.changed' && e.to === 'done')?.at ?? ''

export function seedLandBusy(ws: string, store: MockStore, rng: Rng): LandState {
  const state = emptyState()
  if (!isDemo(store, ws)) return state
  queueFor(state, REMOTE_ORDER[0], 'develop').foreign = 2
  queueFor(state, REMOTE_ORDER[1], 'develop').foreign = 1
  const fixture = new Set(FIXTURE_SPECS.map((s) => s.ticket))
  const done = store
    .ticketKeys(ws)
    .filter((k) => !fixture.has(k) && approved(store, k))
    .map((k) => ({ k, at: doneAt(store, k) }))
    .filter((x) => x.at)
    .sort((a, b) => a.at.localeCompare(b.at))
  const today = done.filter((x) => x.at >= '2026-10-09')
  const before = done.filter((x) => x.at < '2026-10-09').slice(-18)
  const specs: Spec[] = before.map((x, i) => ({ ticket: x.k, at: plus(x.at, 1), plan: i % 5 === 2 ? ['moves', 'clean'] : ['clean'] }))
  // Today: the first ones landed (one after a rebuilt candidate), one red check, one conflict a person must resolve;
  // the rest wait — a stacked pair (parent first) and a descendant whose parent's checks were red.
  const roles: (Partial<Spec> & { plan: Script[] })[] = [
    { plan: ['clean'] },
    { plan: ['clean'] },
    { plan: ['red'] },
    { plan: ['moves', 'clean'] },
    { plan: ['conflict'], person: true },
    { plan: ['clean'] },
    { plan: ['clean'] },
  ]
  const queued = today.slice(roles.length)
  today.slice(0, roles.length).forEach((x, i) => specs.push({ ticket: x.k, at: plus(x.at, 1), ...roles[i] }))
  const red = today[2]?.k
  const enqueuedAt = (i: number) => `2026-10-09T11:${String(22 + i).padStart(2, '0')}:00Z`
  queued.forEach((x, i) => {
    const spec: Spec = { ticket: x.k, at: enqueuedAt(i), plan: [], final: 'queued' }
    // The first waits for the red parent; the last two are a stacked pair (the parent lands first).
    if (i === 0 && red) Object.assign(spec, { stacked_on: red, remote: remoteOf(store, red) })
    if (i === queued.length - 1 && i > 1) Object.assign(spec, { stacked_on: queued[i - 1].k, remote: remoteOf(store, queued[i - 1].k) })
    if (rng.chance(0.3)) spec.plan = ['moves', 'clean']
    specs.push(spec)
  })
  build(store, state, [...specs, ...present(store, ws, FIXTURE_SPECS)], 1)
  // The worker restarted once today: it found the attempt it was on (#12 or the next merge) recorded as merged and only tidied the queue.
  const resumed = state.attempts.find((a) => a.n >= 12 && a.outcome === 'merged')
  if (resumed?.ended) state.worker.resumed = { from: resumed.n, at: plus(resumed.ended, 2) }
  return state
}
