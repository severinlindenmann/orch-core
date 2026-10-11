import { codeReviewApplies } from '@/api/gates'
import { atLeast } from '@/api/permissions'
import { plain } from '@/components/sign/visible'
import type { AddonDecision } from '@/api/types'
import { fmtDateTime } from '@/lib/time'
import type { MockStore, StoreFailure } from '../store'
import { fnvHex } from '../derive'
import { canSeeTicket, conflict, refusal, type AddonCtx } from './registry'

// Factory full runs (owner decision 2026-10-10 evening, D61 option; docs/factory-full-run-proposal.md). A request
// that one person signs: the factory plans, writes requirements, builds and tests, validates and collects evidence
// for each child, reaches Preview (made and checked, visible only inside the workspace) and, when the request says so,
// Deliver (it goes out) after a hold window during which a person can Stop it.
//  - Runs live in the addon state (`runs`); their milestones are written on the factory epic as the addon
//    (`factory.run_requested`, `factory.run_step`, `factory.deliver_held`, `factory.deliver_stopped`,
//    `factory.delivered`; provisional names). The children of a run are steps in the state, not tickets, in this mock.
//  - Progress is a simulator chain per workspace (`factory:<ws>:runs`), one step every RUN_STEP_MS.
//  - The hold is a core decision with one option, Stop delivery (`factory.hold:<run>`, `hold` set so core can show it in
//    the shell). Its terms are the run, the destination and the end of the window, so the Stop is signed on exactly those.
//  - When the window ends with no Stop, the delivery happens (lazily on the next look, or on the chain's next step).

//  - One charter check (`runGate`) at admission, every step and settlement: paused holds everything (the hold clock
//    too); a charter stopped by time (or an over-committed budget) stops progress and ends a hold "not delivered".
//    A run reserves its children against the shared child budget when it is admitted (`used += RUN_CHILDREN`).
//  - D61 keeps the code gate human: when the workspace code review policy applies, each child waits at "Code review"
//    for a person (a core decision `factory.code:<run>:<n>`) before Validate; Preview needs every review.
//  - Every request has a single-use id the host issued at review (`request`): it is signed with the rest and consumed
//    with the run's creation; a replay is refused (409 `factory.request_used`).
//  - A hold also keeps a wall-clock deadline (`holdWallUntil`), so a reload (which restarts the mock clock) never
//    extends it: the mock deadline is re-derived from the wall clock when the two drift apart.

export const RUN_STEP_MS = import.meta.env.MODE === 'test' ? 40 : 2_000
export const HOLD_CHOICES = [15, 30, 60, 240] as const
export const DEFAULT_HOLD = 30
export const CHILD_STEPS = ['Requirements', 'Build and test', 'Validate', 'Evidence'] as const
/** Children a run reserves against the charter's child budget when it is admitted. */
export const RUN_CHILDREN = 3
export const MAX_CHILDREN = 25
export const MAX_HOURS = 72
const HOUR = 3_600_000
const MAX_GOAL = 200
const MAX_MEANS = 160
const MAX_RUNS_SHOWN = 4
const MIN = 60_000
/** The index in CHILD_STEPS a child cannot pass without its code review (when the gate applies): Validate. */
const REVIEW_BEFORE = 2

type Ctx = Pick<AddonCtx, 'store' | 'ws' | 'viewer'>
export type Target = 'Preview' | 'Deliver'
export interface RunChild {
  title: string
  size: string
  /** Steps done, 0 to CHILD_STEPS.length. */
  done: number
  /** Who wrote it (the run's agent): never one of its reviewers. */
  author?: string
  /** Commits pushed after the first (the mock's content revision): a new one voids the code review approvals. */
  rev?: number
  /** The code review (only when the gate applies): the approvals, each on the commit it signed; waiting until enough. */
  review?: { waiting: boolean; approvals: { by: string; at: string; sha: string }[] }
}
export interface Run {
  id: string
  /** The single-use request id the signature covered. */
  request: string
  goal: string
  goesUpTo: Target
  deliverMeans: string | null
  holdMinutes: number
  signedBy: string
  signedAt: string
  planned: boolean
  /** The workspace code review policy applies to this run's children (sticky: once on, it stays on). */
  codeReview?: boolean
  children: RunChild[]
  stage: 'working' | 'preview' | 'holding' | 'delivered' | 'stopped' | 'not_delivered'
  holdUntil?: string
  /** The same deadline on the wall clock (ms): a reload never extends a hold. */
  holdWallUntil?: number
  /** While the factory is paused: what was left of the hold (ms), frozen; resume rebuilds both deadlines from it. */
  holdRemainingMs?: number
  deliveredAt?: string
  stopped?: { at: string; by: string }
  notDelivered?: { at: string; reason: string }
}
export interface RunDraft {
  /** Issued by the host when the request is reviewed; single use. */
  request?: string
  goal: string
  goes_up_to: Target
  deliver_means?: string
  hold_minutes?: number
}

const ADDON = { kind: 'addon', id: 'factory' } as const
const scriptId = (ws: string) => `factory:${ws}:runs`
const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
const holdWord = (m: number) => (m >= 60 ? `${m / 60} h` : `${m} min`)
export const runsOf = (state: Record<string, unknown>) => ((state.runs ??= []) as Run[])
const usedRequests = (state: Record<string, unknown>) => ((state.usedRequests ??= []) as string[])
const nameOf = (c: Ctx, person: string) => c.store.workspaces.find((w) => w.id === c.ws)?.members.find((m) => m.person === person)?.name ?? person

// ------------------------------------------------------------------ the charter (one check)

export function hoursUsed(state: Record<string, unknown>, now: string): number {
  const paused = state.paused as { at: string } | null
  const end = Date.parse(paused ? paused.at : now)
  return Math.max(0, (end - Date.parse(state.startedAt as string) - ((state.pausedMs as number) ?? 0)) / HOUR)
}

/**
 * The charter for full runs, checked at admission, every step and settlement: `paused` (nothing moves, the hold clock
 * stops), `stopped` (the time budget is used up, or more children are counted than the budget allows: nothing moves and
 * a hold ends "not delivered"), or `running`. The child budget is spent at admission (reserved), so reaching it stops
 * new children (the factory page's mode) but not children already admitted.
 */
export function runGate(state: Record<string, unknown>, now: string): 'running' | 'paused' | 'stopped' {
  if (hoursUsed(state, now) >= MAX_HOURS || ((state.used as number) ?? 0) > MAX_CHILDREN) return 'stopped'
  return state.paused ? 'paused' : 'running'
}

const policyApplies = (store: MockStore, ws: string) => codeReviewApplies(store.workspaces.find((w) => w.id === ws)?.gates.code?.applies, 'feature')

// ------------------------------------------------------------------ the request

/** The args core's signing prompt shows and the host checks again: everything the request authorises. */
export function runArgs(d: RunDraft): Record<string, string | number> {
  const base = { request: d.request ?? '', goal: d.goal }
  return d.goes_up_to === 'Deliver'
    ? { ...base, goes_up_to: 'Deliver', deliver_means: d.deliver_means ?? '', hold_minutes: d.hold_minutes ?? DEFAULT_HOLD, largest_child: 'm' }
    : { ...base, goes_up_to: 'Preview', largest_child: 'm' }
}

/** The factory epic when this viewer may see it, else a 404 (`not_visible`): runs are the epic's content. */
export function visibleEpic(c: Ctx, state: Record<string, unknown>): string | StoreFailure {
  const epic = state.epic as string | null
  if (!epic) return refusal(404, 'not_found', 'There is no factory epic here.')
  if (!canSeeTicket(c, epic)) return refusal(404, 'not_visible', 'No factory epic you can see here.', 'The epic is restricted to other people.')
  return epic
}

/** Checks a request (the form's data, or a signed body): a refusal, or the draft (without its request id). */
export function checkRequest(c: Ctx, f: Record<string, unknown>): RunDraft | StoreFailure {
  const goal = typeof f.goal === 'string' ? f.goal.trim() : ''
  if (!goal) return refusal(400, 'validation', 'Say what the factory should make.', 'Fill in Goal.')
  if (goal.length > MAX_GOAL) return refusal(400, 'validation', `The goal is longer than ${MAX_GOAL} characters.`)
  const to = f.goes_up_to ?? 'Preview'
  if (to !== 'Preview' && to !== 'Deliver') return refusal(400, 'validation', 'Choose Up to Preview or All the way to Deliver.')
  if (to === 'Preview') return { goal, goes_up_to: 'Preview' }
  const means = typeof f.deliver_means === 'string' ? f.deliver_means.trim() : ''
  if (!means) return refusal(400, 'validation', 'Say what Deliver means: where it goes out (for example "Deploy to production" or "Send to finance@example.test").', 'Fill in What Deliver means.')
  if (means.length > MAX_MEANS) return refusal(400, 'validation', `What Deliver means is longer than ${MAX_MEANS} characters.`)
  const hold = Number(f.hold_minutes ?? DEFAULT_HOLD)
  if (!(HOLD_CHOICES as readonly number[]).includes(hold)) return refusal(400, 'validation', 'Choose a hold window: 15 min, 30 min, 1 h or 4 h.')
  // Deliver sends something out of the workspace: an owner signs that request (open point 1 of the proposal).
  if (c.store.roleIn(c.ws, c.viewer) !== 'owner') return conflict('factory.deliver_owner_only', 'Only an owner can sign a full run that goes all the way to Deliver.', 'Choose Up to Preview, or ask an owner.')
  return { goal, goes_up_to: 'Deliver', deliver_means: means, hold_minutes: hold }
}

/** A random nonce for one seeding of the factory state (a Reset seeds again, so ids never repeat across resets). */
export function requestNonce(): string {
  const b = new Uint8Array(4)
  globalThis.crypto.getRandomValues(b)
  return [...b].map((x) => x.toString(16).padStart(2, '0')).join('')
}

/**
 * A reviewed draft with a new single-use request id (every review issues a new one): `rq-<PREFIX>-<nonce>-<n>`, bound
 * to the workspace and this seeding of the state, so a reset never issues an id that was signed before.
 */
export function issueRequest(c: Ctx, state: Record<string, unknown>, d: RunDraft): RunDraft {
  const n = ((state.requestSeq as number) ?? 0) + 1
  state.requestSeq = n
  const nonce = ((state.requestNonce as string | undefined) ??= requestNonce())
  const prefix = c.store.workspaces.find((w) => w.id === c.ws)?.prefix ?? c.ws
  return { ...d, request: `rq-${prefix}-${nonce}-${n}` }
}

/** The agent that writes a run's children for the person who signed it. */
export const runAgent = (by: string) => `claude-code:s_factory:${by}`

const childrenFor = (goal: string, by: string): RunChild[] => {
  const g = goal.length > 60 ? `${goal.slice(0, 57)}…` : goal
  return [
    { title: `Make the first version: ${g}`, size: 's', done: 0, author: runAgent(by) },
    { title: `Check it against the goal: ${g}`, size: 'm', done: 0, author: runAgent(by) },
    { title: `Prepare what goes out: ${g}`, size: 'xs', done: 0, author: runAgent(by) },
  ]
}

// ------------------------------------------------------------------ code review of a run's child (D58/D59 semantics)

/** The commit the child's branch is at (mock sha): a new commit is a new revision. */
export const childSha = (r: Run, i: number, k: RunChild) => fnvHex(`${r.request}|${i}|${k.rev ?? 0}`, 7)
const codePolicy = (store: MockStore, ws: string) => store.workspaces.find((w) => w.id === ws)!.gates.code
/** Approvals that still stand: on the current commit, one per person. */
const standing = (r: Run, i: number, k: RunChild) => {
  const sha = childSha(r, i, k)
  const seen = new Set<string>()
  return (k.review?.approvals ?? []).filter((a) => a.sha === sha && !seen.has(a.by) && seen.add(a.by))
}
/** Has the child the code reviews the policy asks for (distinct people, on its current commit)? Off: nothing to do. */
const reviewed = (store: MockStore, ws: string, r: Run, i: number, k: RunChild) => !r.codeReview || standing(r, i, k).length >= codePolicy(store, ws).count
/** Core's eligibility rule (D59) for this child: the run's requester and the child's author count as its assignees. */
export function reviewEligibility(store: MockStore, ws: string, state: Record<string, unknown>, r: Run, i: number, k: RunChild, person: string): string | null {
  const epic = state.epic as string
  const reviewers = store.ticket(epic)?.people.reviewers ?? []
  return store.gateEligibility(ws, 'code', person, { assignees: [r.signedBy, k.author ?? runAgent(r.signedBy)], reviewers }, standing(r, i, k).map((a) => a.by))
}

/**
 * Admits a signed request as a run, in one step: the charter check, the request id consumed, the children reserved
 * against the budget, the run created and the chain started. A refusal changes nothing.
 */
export function startRun(store: MockStore, ws: string, state: Record<string, unknown>, d: RunDraft, by: string): Run | StoreFailure {
  const gate = runGate(state, store.now())
  if (gate !== 'running') return conflict('factory.not_running', gate === 'paused' ? 'The factory is paused: resume it before starting a full run.' : 'The factory is stopped: its charter is used up.')
  if (!d.request || usedRequests(state).includes(d.request)) return conflict('factory.request_used', 'This request was already used to start a run.', 'Review the request again and sign the new one.')
  const used = (state.used as number) ?? 0
  if (used + RUN_CHILDREN > MAX_CHILDREN) return conflict('factory.budget', `A full run needs ${RUN_CHILDREN} children; ${Math.max(0, MAX_CHILDREN - used)} of ${MAX_CHILDREN} are left in the charter.`)
  usedRequests(state).push(d.request)
  state.used = used + RUN_CHILDREN
  const seq = ((state.runSeq as number) ?? 0) + 1
  state.runSeq = seq
  const run: Run = {
    id: `R-${seq}`,
    request: d.request,
    goal: d.goal,
    goesUpTo: d.goes_up_to,
    deliverMeans: d.goes_up_to === 'Deliver' ? d.deliver_means! : null,
    holdMinutes: d.goes_up_to === 'Deliver' ? d.hold_minutes! : 0,
    signedBy: by,
    signedAt: store.now(),
    planned: false,
    codeReview: policyApplies(store, ws),
    children: childrenFor(d.goal, by),
    stage: 'working',
  }
  runsOf(state).unshift(run)
  const epic = state.epic as string
  store.append(epic, { type: 'factory.run_requested', actor: ADDON, run: run.id, request: run.request, goal: run.goal, goes_up_to: run.goesUpTo, children: RUN_CHILDREN, ...(run.deliverMeans ? { deliver_means: run.deliverMeans, hold_minutes: run.holdMinutes } : {}) })
  armChain(store, ws)
  return run
}

// ------------------------------------------------------------------ progress and settlement

/** Keeps a hold's mock deadline on its wall-clock deadline when the mock clock jumped (a reload restarts it). */
function syncHold(store: MockStore, r: Run) {
  // Paused: the remaining time is frozen in holdRemainingMs; resume rebuilds both deadlines from it.
  if (r.stage !== 'holding' || !r.holdUntil || r.holdWallUntil === undefined || r.holdRemainingMs !== undefined) return
  const want = Date.parse(store.now()) + (r.holdWallUntil - Date.now())
  if (Math.abs(want - Date.parse(r.holdUntil)) > 5_000) r.holdUntil = iso(want)
}

/**
 * Settles every hold: a stopped charter ends it "not delivered"; a paused one holds it; otherwise a window that ended
 * delivers to exactly the signed destination. Returns true when something changed.
 */
export function settleRuns(store: MockStore, state: Record<string, unknown>): boolean {
  const now = store.now()
  const gate = runGate(state, now)
  let changed = false
  for (const r of runsOf(state)) {
    if (r.stage !== 'holding') continue
    syncHold(store, r)
    if (gate === 'paused') continue
    if (gate === 'stopped') {
      r.stage = 'not_delivered'
      r.notDelivered = { at: now, reason: 'charter stopped' }
      store.append(state.epic as string, { type: 'factory.deliver_cancelled', actor: ADDON, run: r.id, reason: 'charter_stopped' })
      changed = true
      continue
    }
    if (r.holdUntil && Date.parse(now) >= Date.parse(r.holdUntil)) {
      r.stage = 'delivered'
      r.deliveredAt = now
      store.append(state.epic as string, { type: 'factory.delivered', actor: ADDON, run: r.id, deliver_means: r.deliverMeans })
      changed = true
    }
  }
  return changed
}


/** One step of every working run (only while the charter runs): plan, one child step, Preview, then the hold. */
function stepRuns(store: MockStore, ws: string, state: Record<string, unknown>) {
  if (runGate(state, store.now()) !== 'running') return
  const epic = state.epic as string
  const now = store.now()
  const policy = policyApplies(store, ws)
  for (const r of runsOf(state)) {
    if (r.stage === 'working') {
      if (policy) r.codeReview = true // a policy turned on mid-run applies to what has not passed Preview
      if (!r.planned) {
        r.planned = true
        store.append(epic, { type: 'factory.run_step', actor: ADDON, run: r.id, step: 'Plan' })
        continue
      }
      // A child at Validate (or later) without its reviews waits for people; it never passes on its own.
      const blocked = (k: RunChild, i: number) => k.done >= REVIEW_BEFORE && !reviewed(store, ws, r, i, k)
      r.children.forEach((k, i) => {
        if (blocked(k, i)) k.review = { waiting: true, approvals: k.review?.approvals ?? [] }
        else if (k.review) k.review.waiting = false
      })
      const next = r.children.map((k, i) => ({ k, i })).filter(({ k, i }) => k.done < CHILD_STEPS.length && !blocked(k, i)).sort((a, b) => a.k.done - b.k.done)[0]
      if (next) {
        next.k.done += 1
        if (blocked(next.k, next.i)) next.k.review = { waiting: true, approvals: next.k.review?.approvals ?? [] }
        continue
      }
      if (r.children.some((k, i) => k.done < CHILD_STEPS.length || blocked(k, i))) continue // waiting for code reviews
      r.stage = 'preview'
      store.append(epic, { type: 'factory.run_step', actor: ADDON, run: r.id, step: 'Preview' })
      continue
    }
    if (r.stage === 'preview' && r.goesUpTo === 'Deliver') {
      r.stage = 'holding'
      r.holdUntil = iso(Date.parse(now) + r.holdMinutes * MIN)
      r.holdWallUntil = Date.now() + r.holdMinutes * MIN
      store.append(epic, { type: 'factory.deliver_held', actor: ADDON, run: r.id, deliver_means: r.deliverMeans, until: r.holdUntil })
    }
  }
}

const busy = (state: Record<string, unknown>) => runsOf(state).some((r) => r.stage === 'working' || r.stage === 'holding' || (r.stage === 'preview' && r.goesUpTo === 'Deliver'))

/** The chain re-arms itself while a run works or holds; it stops when none does. */
export function armChain(store: MockStore, ws: string) {
  const step = {
    afterMs: RUN_STEP_MS,
    run: (st: MockStore) => {
      const state = st.addonState(ws, 'factory')
      stepRuns(st, ws, state)
      settleRuns(st, state)
      if (busy(state)) st.sim.play(scriptId(ws), [step])
      else st.sim.stop(scriptId(ws))
    },
  }
  store.sim.play(scriptId(ws), [step])
}

/** Re-arms the chain after a reload (timers do not survive one) when a run still works or holds. */
export function ensureChain(store: MockStore, ws: string, state: Record<string, unknown>) {
  if (busy(state) && !store.sim.running().includes(scriptId(ws))) armChain(store, ws)
}

/** Runs on hold before Deliver: core refuses to turn the factory off (disable, update, uninstall) while one holds. */
export function holdBlocksOff(state: Record<string, unknown>): string | null {
  const r = runsOf(state).find((x) => x.stage === 'holding')
  return r ? `Full run ${r.id} "${plain(r.goal)}" is on hold before Deliver (${plain(r.deliverMeans ?? '')}). Stop it, or let the hold end, before you turn the AI Factory off or change it.` : null
}

/** One person's code review approval on the child's current commit; enough distinct people and the child goes on. */
export function approveReview(store: MockStore, ws: string, state: Record<string, unknown>, id: string, by: string, terms: unknown): StoreFailure | { run: Run; child: RunChild; done: boolean } {
  const m = /^factory\.code:(R-\d+):(\d+)$/.exec(id)
  const run = m && runsOf(state).find((r) => r.id === m[1])
  const i = m ? Number(m[2]) - 1 : -1
  const child = run && run.children[i]
  if (!run || !child || run.stage !== 'working' || !child.review?.waiting) return conflict('decision.closed', 'That code review is no longer waiting.')
  // The approval signs exactly the commit shown; a new commit since the prompt opened is refused.
  const sha = childSha(run, i, child)
  if ((terms as { commit?: unknown } | undefined)?.commit !== sha) return conflict('decision.closed', 'The child has a new commit since you opened the review.', 'Reopen it and review the current commit.')
  const why = reviewEligibility(store, ws, state, run, i, child, by)
  if (why) return refusal(403, 'forbidden', why)
  child.review.approvals.push({ by, at: store.now(), sha })
  const done = reviewed(store, ws, run, i, child)
  if (done) child.review.waiting = false
  store.append(state.epic as string, { type: 'factory.code_reviewed', actor: ADDON, run: run.id, child: i + 1, commit: sha, approvals: standing(run, i, child).length, needed: codePolicy(store, ws).count })
  return { run, child, done }
}

/**
 * A new commit on a run's child (the agent pushed again): its code review approvals no longer stand (they signed the
 * previous commit, D58/D59) and a child already past Validate goes back to wait for the review again.
 */
export function pushChildCommit(store: MockStore, state: Record<string, unknown>, runId: string, n: number): string | null {
  const run = runsOf(state).find((r) => r.id === runId)
  const child = run?.children[n - 1]
  if (!run || !child || run.stage !== 'working') return null
  child.rev = (child.rev ?? 0) + 1
  if (run.codeReview && child.done > REVIEW_BEFORE) child.done = REVIEW_BEFORE
  if (run.codeReview && child.done >= REVIEW_BEFORE) child.review = { waiting: true, approvals: child.review?.approvals ?? [] }
  const sha = childSha(run, n - 1, child)
  store.append(state.epic as string, { type: 'factory.child_pushed', actor: ADDON, run: run.id, child: n, commit: sha })
  return sha
}

// ------------------------------------------------------------------ what core draws

/** "via the factory full run you signed on 10 Oct 2026 14:02 UTC — no person reviewed this step" */
export function stepLabel(c: Ctx, r: Run) {
  const who = r.signedBy === c.viewer ? 'you' : nameOf(c, r.signedBy)
  return `via the factory full run ${who} signed on ${fmtDateTime(r.signedAt)} — no person reviewed this step`
}

export const minutesLeft = (r: Run, now: string) => Math.max(0, Math.ceil((Date.parse(r.holdUntil ?? now) - Date.parse(now)) / MIN))

/** The run as core draws it: a heading line, the request, its state and the steps with their labels. Values via visible.tsx. */
function runNode(c: Ctx, r: Run, now: string, gate: ReturnType<typeof runGate>) {
  const label = stepLabel(c, r)
  const goal = plain(r.goal)
  const means = plain(r.deliverMeans ?? '')
  const doneAll = r.children.every((k, i) => k.done >= CHILD_STEPS.length && reviewed(c.store, c.ws, r, i, k))
  const need = codePolicy(c.store, c.ws).count
  const childRow = (k: RunChild, i: number) => {
    const waiting = !!k.review?.waiting
    const ok = standing(r, i, k)
    const reviewedBy = ok.length ? `; code review (commit ${childSha(r, i, k)}) approved by ${ok.map((a) => nameOf(c, a.by)).join(', ')}${waiting ? ` (${ok.length} of ${need})` : ''}` : ''
    return {
      step: waiting ? `Code review (waits for ${need === 1 ? 'a person' : `${need} people`})` : k.done >= CHILD_STEPS.length ? 'Evidence collected' : `${CHILD_STEPS[k.done]} (${k.done + 1} of ${CHILD_STEPS.length})`,
      item: `${plain(k.title)} · ${k.size}`,
      state: waiting ? 'waiting' : k.done >= CHILD_STEPS.length ? 'done' : r.planned ? 'working' : 'waiting',
      decided: (k.done > 0 ? label : '–') + reviewedBy,
    }
  }
  const rows = [
    { step: 'Plan', item: `Split the goal into ${r.children.length} children (size m or smaller)`, state: r.planned ? 'done' : 'working', decided: r.planned ? label : '–' },
    ...r.children.map(childRow),
    { step: 'Preview', item: 'Made and checked, visible only inside this workspace', state: r.stage === 'working' ? (doneAll ? 'working' : 'waiting') : 'done', decided: r.stage === 'working' ? '–' : label },
    ...(r.goesUpTo === 'Deliver'
      ? [
          {
            step: 'Deliver',
            item: means,
            state: r.stage === 'holding' ? 'holding' : r.stage === 'delivered' ? 'done' : r.stage === 'stopped' || r.stage === 'not_delivered' ? 'stopped' : 'waiting',
            decided:
              r.stage === 'holding'
                ? `automatic after the hold window (${holdWord(r.holdMinutes)}) unless a person stops it`
                : r.stage === 'delivered'
                  ? label
                  : r.stage === 'stopped'
                    ? `stopped by ${nameOf(c, r.stopped!.by)}`
                    : r.stage === 'not_delivered'
                      ? 'not delivered: the charter stopped'
                      : '–',
          },
        ]
      : []),
  ]
  const halted = r.stage === 'working' && gate !== 'running'
  const status =
    r.stage === 'holding'
      ? { type: 'alert', tone: 'info', title: `Delivering in ${minutesLeft(r, now)} min · ${means}`, text: gate === 'paused' ? 'The factory is paused: the hold clock does not run.' : `Goes out at ${fmtDateTime(r.holdUntil!)} unless a person stops it. Stop is above, with the other things that wait for you.` }
      : r.stage === 'delivered'
        ? { type: 'alert', tone: 'success', title: `Delivered: ${means} at ${fmtDateTime(r.deliveredAt!)}` }
        : r.stage === 'stopped'
          ? { type: 'alert', tone: 'info', title: `Stopped by ${nameOf(c, r.stopped!.by)} at ${fmtDateTime(r.stopped!.at)}: nothing went out. The run stays at Preview.` }
          : r.stage === 'not_delivered'
            ? { type: 'alert', tone: 'warn', title: `Not delivered: the charter stopped (${fmtDateTime(r.notDelivered!.at)}). Nothing went out; the run stays at Preview.` }
            : halted
              ? { type: 'alert', tone: 'info', title: gate === 'paused' ? 'The factory is paused: this run waits.' : 'The charter is stopped: this run goes no further.' }
              : r.stage === 'working' && r.children.some((k) => k.review?.waiting)
                ? { type: 'alert', tone: 'info', title: 'Waiting for a code review by a person (the code review gate applies). Approve it above.' }
                : r.stage === 'preview'
                  ? { type: 'alert', tone: 'success', title: r.goesUpTo === 'Deliver' ? 'At Preview. The hold before Deliver starts next.' : 'At Preview: made and checked, visible only inside this workspace. It goes no further on its own.' }
                  : { type: 'stack', children: [] }
  return {
    type: 'stack',
    children: [
      { type: 'markdown', text: `#### ${r.id} · ${mdText(goal)}` },
      {
        type: 'kv',
        pairs: [
          { label: 'Goes up to', value: r.goesUpTo === 'Deliver' ? 'Deliver (it goes out)' : 'Preview (made and checked, visible only inside this workspace)' },
          ...(r.goesUpTo === 'Deliver' ? [{ label: 'Deliver means', value: means }, { label: 'Hold window', value: holdWord(r.holdMinutes) }] : []),
          { label: 'Signed', value: `by ${nameOf(c, r.signedBy)} on ${fmtDateTime(r.signedAt)} (request ${r.request})` },
        ],
      },
      status,
      {
        type: 'table',
        columns: [
          { key: 'step', label: 'Step' },
          { key: 'item', label: 'What' },
          { key: 'state', label: 'State', cell: 'state' },
          { key: 'decided', label: 'How it was decided' },
        ],
        rows,
      },
    ],
  }
}

/** Markdown would read `*`, `_`, `#` … in a value as formatting: escape them (the value is shown, never interpreted). */
const mdText = (s: string) => s.replace(/[\\`*_{}\[\]()#+\-.!|<>~]/g, (ch) => `\\${ch}`)

/** The Full runs tab, the hold and review notices above the tabs and the counts. */
export function runsView(c: Ctx, state: Record<string, unknown>, opts: { epic: string | null; canRequest: boolean; canAnswer: boolean; isOwner: boolean }) {
  const now = c.store.now()
  const gate = runGate(state, now)
  const runs = opts.epic ? runsOf(state) : []
  const nav = (((state.nav ?? {}) as Record<string, { runDraft?: RunDraft }>)[c.viewer] ?? {}) as { runDraft?: RunDraft }
  const draft = nav.runDraft
  const holding = runs.filter((r) => r.stage === 'holding')
  const reviews = runs.flatMap((r) => (r.stage === 'working' ? r.children.map((k, i) => ({ r, k, i })).filter((x) => x.k.review?.waiting && !reviewEligibility(c.store, c.ws, state, x.r, x.i, x.k, c.viewer)) : []))
  const policy = policyApplies(c.store, c.ws)
  const form = {
    type: 'form',
    schema: {
      type: 'object',
      required: ['goal', 'goes_up_to'],
      properties: {
        goal: { type: 'string', title: 'Goal', maxLength: MAX_GOAL },
        goes_up_to: { type: 'string', title: 'How far may the factory go on its own?', enum: ['Preview', 'Deliver'], default: 'Preview' },
        deliver_means: { type: 'string', title: 'What Deliver means', description: 'Only for Deliver: where it goes out, for example "Deploy to production", "Publish campaign" or "Send to finance@example.test (boss)".', maxLength: MAX_MEANS },
        hold_minutes: { type: 'integer', title: 'Hold window before Deliver', enum: [...HOLD_CHOICES], default: DEFAULT_HOLD },
      },
      allOf: [{ if: { properties: { goes_up_to: { const: 'Deliver' } } }, then: { required: ['deliver_means'] } }],
    },
    uiSchema: {
      goes_up_to: { 'ui:widget': 'radio', 'ui:enumNames': ['Up to Preview: made and checked, visible only in this workspace', `All the way to Deliver: it goes out after the hold window${opts.isOwner ? '' : ' (owners only)'}`] },
      hold_minutes: { 'ui:enumNames': ['15 min', '30 min (default)', '1 h', '4 h'] },
      deliver_means: { 'ui:placeholder': 'Deploy to production' },
    },
    formData: draft ? { goal: draft.goal, goes_up_to: draft.goes_up_to, ...(draft.deliver_means ? { deliver_means: draft.deliver_means } : {}), hold_minutes: draft.hold_minutes ?? DEFAULT_HOLD } : { goes_up_to: 'Preview', hold_minutes: DEFAULT_HOLD },
    action: 'prepare_run',
    submitLabel: 'Review request',
  }
  const left = Math.max(0, MAX_CHILDREN - ((state.used as number) ?? 0))
  const request = !opts.epic
    ? []
    : !opts.canRequest
      ? [{ type: 'markdown', text: 'Owners and maintainers request full runs.' }]
      : [
          {
            type: 'markdown',
            text: `### New full run\nOne request, one signature: the factory plans, writes requirements, builds and tests, validates and collects evidence for each child, then stops at Preview, or goes on to Deliver after a hold window you can stop. A run takes ${RUN_CHILDREN} of the charter's children (${left} of ${MAX_CHILDREN} left), size m or smaller. ${policy ? 'The code review gate applies here (Settings → Gates): each child waits at Code review for a person before Validate.' : 'When the code review gate applies (Settings → Gates), each child waits at Code review for a person before Validate; the factory never signs it.'}`,
          },
          form,
          ...(draft
            ? [
                {
                  type: 'alert',
                  tone: 'info',
                  title: draft.goes_up_to === 'Deliver' ? `Ready to sign: ${plain(draft.goal)}, all the way to Deliver (${plain(draft.deliver_means ?? '')})` : `Ready to sign: ${plain(draft.goal)}, up to Preview`,
                  text: `Request ${draft.request}: it starts one run, once. ${draft.goes_up_to === 'Deliver' ? `The signing prompt lists every value. After Preview it holds for ${holdWord(draft.hold_minutes ?? DEFAULT_HOLD)} with a notice and Stop, then goes out to exactly that destination; anything else is refused.` : 'The signing prompt lists every value. The run ends at Preview and waits for you.'}`,
                },
                {
                  type: 'stack',
                  direction: 'row',
                  fit: true,
                  children: [
                    { type: 'button', label: 'Sign and start', action: 'start_run', variant: 'primary', args: runArgs(draft) },
                    { type: 'button', label: 'Cancel', action: 'clear_run', variant: 'ghost' },
                  ],
                },
              ]
            : [{ type: 'button', label: 'Fill in a demo request', action: 'demo_run', variant: 'ghost' }]),
        ]
  const list = runs.slice(0, MAX_RUNS_SHOWN).map((r) => runNode(c, r, now, gate))
  const holdNotice =
    holding.length && opts.canAnswer
      ? holding.flatMap((r) => [
          { type: 'markdown', text: `**Full run ${r.id} · ${mdText(plain(r.goal))}: on hold before Deliver**` },
          { type: 'decision', id: `factory.hold:${r.id}` },
          ...(opts.isOwner ? [{ type: 'button', label: 'Simulate: let the hold time pass (demo)', action: 'simulate_time', variant: 'ghost', args: { run: r.id } }] : []),
        ])
      : holding.map((r) => ({ type: 'alert', tone: 'info', title: `Delivering in ${minutesLeft(r, now)} min · ${plain(r.deliverMeans ?? '')}`, text: 'An owner or maintainer can stop it until then.' }))
  const reviewNotice =
    reviews.length && opts.canAnswer
      ? [{ type: 'markdown', text: `**${reviews.length === 1 ? '1 code review waits' : `${reviews.length} code reviews wait`} for a person** (full runs; the factory never signs it)` }, ...reviews.map((x) => ({ type: 'decision', id: `factory.code:${x.r.id}:${x.i + 1}` }))]
      : []
  return {
    runCount: runs.length,
    runsNode: {
      type: 'stack',
      children: [...request, ...(list.length ? [{ type: 'markdown', text: '### Runs' }, ...list] : opts.epic ? [{ type: 'markdown', text: 'No full runs yet.' }] : [])],
    },
    holdNode: { type: 'stack', children: [...holdNotice, ...reviewNotice] },
    holding: holding.length + reviewNotice.length,
  }
}

/** Core decisions for full runs: Stop delivery while a run holds, and code reviews that wait for a person. */
export function holdDecisions(c: Ctx, state: Record<string, unknown>, epic: string | null): AddonDecision[] {
  // Runs belong to the factory epic: a viewer who cannot see it gets no decision at all (nothing of the run).
  if (!epic || !canSeeTicket(c, epic)) return []
  const runs = runsOf(state)
  // Stop delivery: owners and maintainers. Code reviews: whoever core's gate eligibility admits (below).
  const holds = !atLeast(c.store.roleIn(c.ws, c.viewer), 'maintainer') ? [] : runs
    .filter((r) => r.stage === 'holding')
    .map((r) => ({
      kind: 'decision' as const,
      id: `factory.hold:${r.id}`,
      addon: 'factory',
      ticket: epic,
      title: 'AI Factory full run: delivery on hold',
      // Stable signed text (the digest covers it): the destination and the deadline, never a countdown. Core draws the
      // minutes left from `hold.until`, outside what is signed (Codex integration review #1).
      question: `Delivery at ${fmtDateTime(r.holdUntil!)}: ${plain(r.deliverMeans!)}`,
      detail: `Full run ${r.id} "${plain(r.goal)}" reached Preview and goes out at ${fmtDateTime(r.holdUntil!)} unless you stop it. Stop cancels the delivery; the run stays at Preview.`,
      options: [{ key: 'stop', label: 'Stop delivery', primary: true }],
      terms: { run: r.id, deliver_means: r.deliverMeans!, hold_until: r.holdUntil! },
      hold: { until: r.holdUntil!, deliver_means: r.deliverMeans! },
      action: 'hold',
    }))
  const reviews = runs
    .filter((r) => r.stage === 'working')
    .flatMap((r) =>
      r.children.flatMap((k, i) =>
        k.review?.waiting && !reviewEligibility(c.store, c.ws, state, r, i, k, c.viewer)
          ? [
              {
                kind: 'decision' as const,
                id: `factory.code:${r.id}:${i + 1}`,
                addon: 'factory',
                ticket: epic,
                title: 'AI Factory full run: code review',
                question: `Code review: ${plain(k.title)} (commit ${childSha(r, i, k)})`,
                detail: `Full run ${r.id} "${plain(r.goal)}". The code review gate applies (${standing(r, i, k).length} of ${codePolicy(c.store, c.ws).count} approvals on this commit), so people review this child before it is validated; the factory never signs it. A new commit voids the approvals.`,
                options: [{ key: 'approve', label: 'Approve code review', primary: true }],
                terms: { run: r.id, child: i + 1, child_title: k.title, commit: childSha(r, i, k) },
                action: 'code_review',
              },
            ]
          : [],
      ),
    )
  return [...holds, ...reviews]
}

/** Busy day: one run holds before Deliver (28 min left; its 3 children are reserved in the budget by the caller). */
export function seedRuns(store: MockStore, by: string, ws: string): { runs: Run[]; runSeq: number; requestSeq: number; requestNonce: string; usedRequests: string[] } {
  const now = Date.parse(store.now())
  const nonce = requestNonce()
  const prefix = store.workspaces.find((w) => w.id === ws)?.prefix ?? ws
  const request = `rq-${prefix}-${nonce}-1`
  const done = (titles: [string, string][]) => titles.map(([title, size]) => ({ title, size, done: CHILD_STEPS.length, author: runAgent(by) }))
  return {
    runSeq: 1,
    requestSeq: 1,
    requestNonce: nonce,
    usedRequests: [request],
    runs: [
      {
        id: 'R-1',
        request,
        goal: 'Autumn tariff campaign',
        goesUpTo: 'Deliver',
        deliverMeans: 'Publish campaign to the newsletter list',
        holdMinutes: 30,
        signedBy: by,
        signedAt: iso(now - 95 * MIN),
        planned: true,
        children: done([['Write the campaign text', 's'], ['Make the tariff comparison images', 'm'], ['Check prices against the tariff table', 's']]),
        stage: 'holding',
        holdUntil: iso(now + 28 * MIN),
        holdWallUntil: Date.now() + 28 * MIN,
      },
    ],
  }
}

/**
 * Factory state saved under an older version (2: full runs without request ids or wall deadlines; 3: single review
 * per child): runs, holds and counters are kept. A hold without a wall-clock deadline gets its full window again from
 * now: conservative, never shorter than what was left (the old deadline cannot be trusted after a reload).
 */
export function migrateState(state: Record<string, unknown>, from: number): Record<string, unknown> | null {
  if (from >= 4) return state
  const runs = runsOf(state)
  state.requestNonce ??= requestNonce()
  state.requestSeq ??= 0
  for (const r of runs) {
    r.request ??= `rq-legacy-${r.id}`
    for (const k of r.children ?? []) {
      k.author ??= runAgent(r.signedBy)
      const old = k.review as unknown as { state?: string; by?: string; at?: string } | undefined
      if (old?.state === 'approved') k.review = { waiting: false, approvals: [{ by: old.by!, at: old.at!, sha: childSha(r, r.children.indexOf(k), k) }] }
      else if (old?.state === 'waiting') k.review = { waiting: true, approvals: [] }
    }
    if (r.stage === 'holding' && r.holdWallUntil === undefined) r.holdWallUntil = Date.now() + r.holdMinutes * MIN
  }
  state.usedRequests = [...new Set([...(((state.usedRequests as string[]) ?? [])), ...runs.map((r) => r.request)])]
  return state
}

/** The mock's demo datasets: the only place the hold simulator (`simulate_time`) answers. */
export const DEMO_DATASETS = ['normal', 'busy'] as const

export const DEMO_REQUEST: RunDraft = { goal: 'Release monthly billing v2', goes_up_to: 'Deliver', deliver_means: 'Deploy to production', hold_minutes: DEFAULT_HOLD }

/** The keys whose signed value differs from the draft (empty: the signature still matches). */
export const staleRunKeys = (body: Record<string, unknown>, d: RunDraft) => {
  const want = runArgs(d)
  const keys = new Set([...Object.keys(want), ...['request', 'goal', 'goes_up_to', 'deliver_means', 'hold_minutes', 'largest_child'].filter((k) => body[k] !== undefined)])
  return [...keys].filter((k) => String(body[k] ?? '') !== String(want[k] ?? ''))
}
