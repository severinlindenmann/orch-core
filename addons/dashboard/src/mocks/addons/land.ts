import { addonActive } from '@/api/addons'
import type { AddonDecision } from '@/api/types'
import type { MockStore } from '../store'
import { canSeeTicket, conflict, invalid, notFound, registerAddon, type AddonCtx } from './registry'
import {
  ADDON,
  agentLabel,
  approved,
  asLand,
  attemptOf,
  blockOf,
  branchOf,
  mergedNow,
  NEVER_TARGET,
  queueFor,
  remoteOf,
  resolve,
  sourceOf,
  step,
  type Attempt,
  type Check,
  type LandState,
  type Queue,
} from './land-worker'
import { seedLand, seedLandBusy } from './land-seed'

// land (Landing, D53, Phase 2 preview): the merge lane. Approved tickets land on their target branch one at a time.
//  - One queue per (canonical remote, target branch); the queue is shared by the workspaces on this machine (a file
//    lock in the host), so entries of other workspaces show as a count only.
//  - Only tickets with a standing verify approval (the verdict) enter. Approval binds to code: what was approved,
//    checked and merged is the same candidate. A conflict resolution or a fix is new code: core voids the approval
//    (store.voidApproval, gate.invalidated) and the ticket goes back to review with the resolution diff.
//  - Failures open a "needs" item for the ticket's agent, shown on Today as a core-signed decision (and as a decision
//    a person must answer when the agent could not resolve it). The queue moves on.
//  - `main` is never an allowed target (D33). The worker pushes only the ticket's own branch (--force-with-lease).
//  - The worker is simulated: "Run the worker (demo)" plays one step every few seconds (store.sim), capped.

const STEP_MS = 3000
const MAX_STEPS = 40
const TARGET_RE = /^[A-Za-z0-9._/-]{1,80}$/
const MAX_TARGETS = 10
const scriptId = (ws: string) => `land:${ws}`

type Ctx = Pick<AddonCtx, 'store' | 'ws' | 'viewer'>
type Node = Record<string, unknown>

const nameOf = (c: Ctx, person: string) => c.store.workspaces.find((w) => w.id === c.ws)?.members.find((m) => m.person === person)?.name ?? person
/** "Claude Code for Severin", or a person's name. */
const whoLabel = (c: Ctx, actor: string) => (actor.includes(':') ? `${agentLabel(actor)} for ${nameOf(c, actor.split(':')[2])}` : nameOf(c, actor))
const hhmm = (iso: string) => `${iso.slice(11, 16)} UTC`
const when = (iso?: string) => (iso ? (iso.startsWith('2026-10-09') ? hhmm(iso) : `${iso.slice(5, 10)} ${iso.slice(11, 16)}`) : '–')
const repo = (remote: string) => remote.split('/').slice(-1)[0]
const queueName = (q: Pick<Queue, 'remote' | 'target'>) => `${q.remote} → ${q.target}`
const titleOf = (c: Ctx, key: string) => c.store.ticket(key)?.title ?? ''
const navOf = (state: LandState, viewer: string) => state.nav[viewer] ?? {}
const queueOfAttempt = (state: LandState, a: Attempt) => state.queues.find((q) => q.id === a.queue)
const openNeedOf = (state: LandState, key: string) => [...state.needs].reverse().find((n) => n.open && n.ticket === key)
const lastResolution = (state: LandState, key: string) => [...state.resolutions].reverse().find((r) => r.ticket === key)
const STATUS: Record<Check['result'], string> = { pass: 'pass', fail: 'fail', timeout: 'fail', running: 'running', waiting: 'skip' }
const RESULT_WORD: Record<Check['result'], string> = { pass: 'pass', fail: 'fail', timeout: 'timed out', running: 'running', waiting: 'waits' }
const checksLine = (a: Attempt) => (a.checks.length ? a.checks.map((c) => `${c.level} ${RESULT_WORD[c.result]}`).join(' · ') : 'not run (conflict)')

/** A core `gates` widget for the checks of an attempt (T2, then CI on the exact candidate). */
function checksWidget(a: Attempt): Node {
  const block = {
    type: 'gates',
    id: `land-checks-${a.n}`,
    title: `Checks on candidate ${a.candidate_sha}`,
    items: a.checks.map((c) => ({ name: c.result === 'waiting' ? `${c.name} (waits for T2)` : c.result === 'timeout' ? `${c.name} (timed out)` : c.name, status: STATUS[c.result], ...(c.seconds !== undefined ? { seconds: c.seconds } : {}) })),
  }
  return { type: 'widget', block: JSON.stringify(block) }
}

function diffWidget(n: number, file: string, diff: string): Node {
  return { type: 'widget', block: JSON.stringify({ type: 'diff', id: `land-diff-${n}`, title: 'Resolution diff', file, lines: diff }) }
}

/** Where a ticket is in landing, as the board chip says it (null: nothing to show). */
function chipOf(state: LandState, key: string): string | null {
  const cur = attemptOf(state, state.worker.current)
  if (cur?.ticket === key && !cur.outcome) return 'checking'
  for (const q of state.queues) {
    const i = q.entries.findIndex((e) => e.ticket === key)
    if (i >= 0) return blockOf(state, q.entries[i]) ? 'landing · waits' : `landing #${i + 1}`
  }
  const need = openNeedOf(state, key)
  if (need) return need.kind === 'conflict' ? 'conflict' : 'red checks'
  const merged = mergedNow(state, key)
  const res = lastResolution(state, key)
  if (res && (!merged || merged.n < res.attempt)) return 'back to review'
  if (merged && (merged.ended ?? '') >= '2026-10-09') return 'landed'
  return null
}

/** The ticket rail panel: state, candidate, checks, and the conflict or red checks with what they did to the approval. */
function ticketPanel(c: Ctx, state: LandState, key: string): Node | null {
  const t = c.store.ticket(key)
  if (!t || (t.status !== 'testing' && t.status !== 'done')) return null
  const cur = attemptOf(state, state.worker.current)
  const merged = mergedNow(state, key)
  const need = openNeedOf(state, key)
  const res = lastResolution(state, key)
  const voided = res && (!merged || merged.n < res.attempt) && t.gates.verify.state !== 'approved'
  const remote = remoteOf(c.store, key)
  const target = state.settings.targets[0] ?? 'develop'
  const queued = state.queues.flatMap((q) => q.entries.map((e, i) => ({ q, e, pos: i + 1 }))).find((x) => x.e.ticket === key)
  const children: Node[] = []
  const kv = (pairs: { label: string; value: string | number; mono?: boolean }[]) => children.push({ type: 'kv', pairs })
  const button = (label: string, action: string, variant = 'secondary') => children.push({ type: 'button', label, action, variant })

  if (cur && cur.ticket === key && !cur.outcome) {
    kv([
      { label: 'State', value: `Checking (attempt #${cur.n})` },
      { label: 'Queue', value: `${repo(cur.remote)} → ${cur.target}` },
      { label: 'Candidate', value: cur.candidate_sha, mono: true },
      { label: 'Source', value: cur.source_sha, mono: true },
      { label: 'Target', value: cur.target_sha, mono: true },
      { label: 'Rebase', value: 'Clean: the approval stands' },
      { label: 'Times out', value: hhmm(cur.timeout_at) },
    ])
    children.push(checksWidget(cur))
  } else if (queued) {
    const block = blockOf(state, queued.e)
    kv([
      { label: 'State', value: `Queued #${queued.pos}` },
      { label: 'Queue', value: `${repo(queued.q.remote)} → ${queued.q.target}` },
      { label: 'Source', value: queued.e.source_sha, mono: true },
      ...(queued.e.stacked_on ? [{ label: 'Stacked on', value: queued.e.stacked_on }] : []),
    ])
    if (block) children.push({ type: 'alert', tone: block.why === 'failed' ? 'warn' : 'info', title: `Waits for ${block.parent}`, text: block.why === 'failed' ? `${block.parent} failed to land. A stacked ticket never lands without its parent.` : `${block.parent} is ahead in the queue and lands first.` })
    button('Take off the queue', 'dequeue', 'ghost')
  } else if (need) {
    const a = attemptOf(state, need.attempt)
    kv([
      { label: 'State', value: need.kind === 'conflict' ? 'Failed: conflict' : 'Failed: red checks' },
      { label: 'Queue', value: `${repo(remote)} → ${a?.target ?? target}` },
      ...(a ? [{ label: 'Candidate', value: a.candidate_sha, mono: true }] : []),
      { label: 'Needs', value: need.person ? 'A person to resolve it' : `${whoLabel(c, need.agent!)} picks it up` },
    ])
    if (need.kind === 'conflict')
      children.push({ type: 'alert', tone: 'error', title: `Conflict in ${need.file}`, text: `The rebase onto ${a?.target ?? target} did not apply cleanly (attempt #${need.attempt}). Any resolution voids the approval: the ticket goes back to review with the resolution diff.` })
    else {
      children.push({ type: 'alert', tone: 'error', title: `Red checks: ${need.check ?? 'CI'} failed`, text: `On candidate ${a?.candidate_sha ?? ''}. The approval stands until the code changes; a fix voids it.` })
      if (a) children.push(checksWidget(a))
    }
  } else if (voided && res) {
    kv([
      { label: 'State', value: 'Back to review' },
      { label: 'Gate', value: `Verify: ${t.gates.verify.state === 'invalidated' ? 'invalidated by orch' : t.gates.verify.state}` },
      { label: 'Resolved by', value: whoLabel(c, res.by) },
    ])
    children.push({ type: 'alert', tone: 'warn', title: `${res.kind === 'conflict' ? 'Conflict resolution' : 'A fix'} voids the approval — back to review`, text: t.gates.verify.reason ?? `Attempt #${res.attempt} changed the code the approval covered.` })
    children.push(diffWidget(res.attempt, res.file, res.diff))
    children.push({ type: 'markdown', text: 'A new verdict approves the resolved code; then it can enter the queue again.' })
  } else if (merged) {
    kv([
      { label: 'State', value: `Merged into ${merged.target}` },
      { label: 'Queue', value: `${repo(merged.remote)} → ${merged.target}` },
      { label: 'Attempt', value: `#${merged.n} · ${when(merged.ended)}` },
      { label: 'Checks', value: checksLine(merged) },
    ])
    children.push({ type: 'alert', tone: 'success', title: `Approved, tested and merged: the same candidate ${merged.candidate_sha}`, text: `Source ${merged.source_sha} rebased cleanly onto ${merged.target_sha}; the checks ran on ${merged.candidate_sha} and that commit is what merged.` })
  } else {
    kv([
      { label: 'State', value: 'Not queued' },
      { label: 'Queue', value: `${repo(remote)} → ${target}` },
    ])
    if (approved(c.store, key)) button('Add to the landing queue', 'enqueue', 'primary')
    else children.push({ type: 'markdown', text: 'Enters the queue after the verdict: only approved tickets land.' })
  }
  return { type: 'stack', children }
}

function entryRow(c: Ctx, state: LandState, q: Queue, i: number) {
  const e = q.entries[i]
  const cur = attemptOf(state, state.worker.current)
  const checking = cur?.ticket === e.ticket && !cur.outcome
  const block = blockOf(state, e)
  const notes = [
    e.branch,
    `source ${e.source_sha}`,
    e.stacked_on && !block ? `stacked on ${e.stacked_on}` : '',
    block ? `waits for ${block.parent}${block.why === 'failed' ? ' (its landing failed)' : ' (ahead in the queue)'}` : '',
    checking ? `checking now (attempt #${cur!.n})` : `queued ${when(e.enqueued)} by ${nameOf(c, e.by)}`,
  ].filter(Boolean)
  return {
    id: `entry:${e.ticket}`,
    title: `#${i + 1} ${e.ticket} · ${titleOf(c, e.ticket)}`,
    subtitle: notes.join(' · '),
    status: checking ? 'running' : block ? 'warn' : 'idle',
    ...(checking ? {} : { actions: [{ label: 'Take off', action: 'dequeue', args: { ticket: e.ticket }, variant: 'ghost' }] }),
  }
}

function queueSection(c: Ctx, state: LandState, q: Queue): Node[] {
  const cur = attemptOf(state, state.worker.current)
  const mine = cur && !cur.outcome && cur.queue === q.id ? cur : undefined
  const visible = q.entries.map((_, i) => i).filter((i) => canSeeTicket(c, q.entries[i].ticket))
  const hidden = q.entries.length - visible.length
  const allowed = state.settings.targets.includes(q.target)
  const out: Node[] = [
    { type: 'markdown', text: `### ${queueName(q)}` },
    {
      type: 'kv',
      pairs: [
        { label: 'Target head', value: q.head, mono: true },
        { label: 'Waiting', value: `${q.entries.length + q.foreign} ${q.entries.length + q.foreign === 1 ? 'ticket' : 'tickets'}` },
        { label: 'Shared', value: q.foreign ? `With other workspaces on this machine: ${q.foreign} of theirs waiting (one queue per remote and target)` : 'One queue per remote and target, for every workspace on this machine' },
      ],
    },
  ]
  if (!allowed) out.push({ type: 'alert', tone: 'warn', title: `${q.target} is not an allowed target`, text: 'Nothing lands here until an owner allows it again in Settings > Addons > Landing.' })
  if (mine) {
    if (canSeeTicket(c, mine.ticket)) {
      out.push({
        type: 'kv',
        pairs: [
          { label: 'Now', value: `Attempt #${mine.n} · ${mine.ticket} ${titleOf(c, mine.ticket)}` },
          { label: 'Candidate', value: `${mine.candidate_sha} (source ${mine.source_sha} on target ${mine.target_sha})`, mono: true },
          { label: 'Rebase', value: 'Clean: the approval stands' },
          { label: 'Times out', value: hhmm(mine.timeout_at) },
        ],
      })
      out.push(checksWidget(mine))
    } else out.push({ type: 'markdown', text: `Attempt #${mine.n} is checking a ticket you cannot see.` })
  }
  const items = visible.map((i) => entryRow(c, state, q, i)).filter((r) => !(mine && r.id === `entry:${mine.ticket}`))
  if (hidden) items.push({ id: `hidden:${q.id}`, title: `${hidden} ${hidden === 1 ? 'ticket' : 'tickets'} you cannot see`, subtitle: 'Restricted to other people.', status: 'idle' })
  if (q.foreign) items.push({ id: `foreign:${q.id}`, title: `${q.foreign} from another workspace on this machine`, subtitle: 'Same remote and target, so the same queue.', status: 'idle' })
  out.push({ type: 'list', items, empty: mine ? 'Nothing else waiting.' : 'Nothing waiting.' })
  return out
}

function needRows(c: Ctx, state: LandState) {
  return state.needs
    .filter((n) => n.open && canSeeTicket(c, n.ticket))
    .map((n) => ({
      id: n.id,
      title: `${n.ticket} needs: ${n.kind === 'conflict' ? 'conflict' : 'red checks'}`,
      subtitle: `${n.kind === 'conflict' ? `Conflict in ${n.file}` : `${n.check ?? 'CI'} failed`} (attempt #${n.attempt}) · ${n.person ? 'a person must resolve it' : `for ${whoLabel(c, n.agent!)}`}`,
      status: 'error',
    }))
}

function historyRows(c: Ctx, state: LandState) {
  return [...state.attempts]
    .reverse()
    .filter((a) => canSeeTicket(c, a.ticket))
    .map((a) => ({
      n: a.n,
      ticket: a.ticket,
      target: `${repo(a.remote)} → ${a.target}`,
      source: a.source_sha,
      target_sha: a.target_sha,
      candidate: a.candidate_sha,
      checks: checksLine(a),
      outcome: a.outcome === 'failed' ? `failed: ${a.reason === 'conflict' ? 'conflict' : a.reason === 'timeout' ? 'timed out' : 'red checks'}` : a.outcome === 'requeued' ? 'requeued: target moved' : (a.outcome ?? 'checking'),
      when: when(a.ended ?? a.started),
      has_checks: a.checks.length > 0,
    }))
}

function workerAlert(c: Ctx, state: LandState): Node {
  const cur = attemptOf(state, state.worker.current)
  const resumed = state.worker.resumed ? ` Restarted at ${when(state.worker.resumed.at)}: resumed from attempt #${state.worker.resumed.from}, no repeat merge.` : ''
  const demo = c.store.sim.running().includes(scriptId(c.ws)) ? ` Demo worker running: a step every ${STEP_MS / 1000} s.` : ''
  if (cur && !cur.outcome) {
    const q = queueOfAttempt(state, cur)
    const what = canSeeTicket(c, cur.ticket) ? cur.ticket : 'a ticket you cannot see'
    return { type: 'alert', tone: 'info', title: `Worker: checking ${what} (attempt #${cur.n}) on ${q ? queueName(q) : cur.target}`, text: `One attempt at a time, across every queue.${resumed}${demo}` }
  }
  return { type: 'alert', tone: 'info', title: 'Worker: idle', text: `Approved tickets land one at a time, across every queue.${resumed}${demo}` }
}

function view(state0: Record<string, unknown>, c: Ctx & { ticket?: string }): Record<string, unknown> {
  const state = asLand(state0)
  const hide = { queues: undefined, attempts: undefined, needs: undefined, resolutions: undefined, worker: undefined, seq: undefined }
  const settings = { targets: state.settings.targets.join(', '), timeout_minutes: state.settings.timeout_minutes }
  // A ticket panel asks for its own ticket only.
  if (c.ticket) {
    const panel = canSeeTicket(c, c.ticket) ? ticketPanel(c, state, c.ticket) : null
    return { ...hide, settings, byTicket: panel ? { [c.ticket]: panel } : {} }
  }
  const cards: Record<string, string> = {}
  const keys = new Set([...state.queues.flatMap((q) => q.entries.map((e) => e.ticket)), ...state.attempts.map((a) => a.ticket), ...state.needs.map((n) => n.ticket)])
  for (const k of keys) {
    if (!canSeeTicket(c, k)) continue
    const chip = chipOf(state, k)
    if (chip) cards[k] = chip
  }
  const tab = navOf(state, c.viewer).view ?? 'queues'
  const running = c.store.sim.running().includes(scriptId(c.ws))
  const tabButton = (label: string, action: string, on: boolean) => ({ type: 'button', label, action, variant: on ? 'primary' : 'ghost' })
  const controls = {
    type: 'stack',
    direction: 'row',
    children: [tabButton('Queues', 'view_queues', tab === 'queues'), tabButton('History', 'view_history', tab === 'history'), running ? { type: 'button', label: 'Stop the worker (demo)', action: 'stop_worker', variant: 'ghost' } : { type: 'button', label: 'Run the worker (demo)', action: 'run_worker', variant: 'secondary' }],
  }
  const needs = needRows(c, state)
  const queues = [...state.queues].sort((a, b) => queueName(a).localeCompare(queueName(b)))
  const body =
    tab === 'history'
      ? {
          type: 'stack',
          children: [
            { type: 'markdown', text: 'Every attempt is a `land.attempt` record: what was approved (source), what it was tested on (candidate on target) and what merged are the same commit.' },
            {
              type: 'table',
              columns: [
                { key: 'n', label: '#' },
                { key: 'ticket', label: 'Ticket' },
                { key: 'target', label: 'Queue' },
                { key: 'source', label: 'Source' },
                { key: 'target_sha', label: 'Target' },
                { key: 'candidate', label: 'Candidate' },
                { key: 'checks', label: 'Checks' },
                { key: 'outcome', label: 'Outcome' },
                { key: 'when', label: 'When' },
              ],
              rows: historyRows(c, state),
              rowActions: [{ label: 'Checks', action: 'open_checks', args: { n: '$row.n' }, when: '$row.has_checks' }],
              empty: 'No landing attempts yet.',
            },
          ],
        }
      : {
          type: 'stack',
          children: [
            ...queues.flatMap((q) => queueSection(c, state, q)),
            { type: 'markdown', text: '### Needs' },
            { type: 'list', items: needs, empty: 'Nothing failed. A conflict or red checks shows here, for the ticket\'s agent, and on Today.' },
            { type: 'markdown', text: '### Allowed targets' },
            {
              type: 'kv',
              pairs: [
                { label: 'Lands on', value: state.settings.targets.join(', ') || 'nothing (no target allowed)' },
                { label: 'Never', value: `${NEVER_TARGET} (D33: people merge to main by hand)` },
                { label: 'Check timeout', value: `${state.settings.timeout_minutes} minutes` },
                { label: 'Pushes', value: "Only the ticket's own branch, with --force-with-lease" },
              ],
            },
          ],
        }
  return { ...hide, settings, cards, workerAlert: workerAlert(c, state), controls, body, needsCount: needs.length }
}

/** Open needs as core decisions: the agent's item (a person may redirect it) and the ones a person must resolve. */
function decisions(state0: Record<string, unknown>, _pkg: AddonDecision[], c: Ctx): AddonDecision[] {
  const state = asLand(state0)
  return state.needs
    .filter((n) => n.open && !n.decided && canSeeTicket(c, n.ticket))
    .map((n) => {
      const a = attemptOf(state, n.attempt)
      const target = a?.target ?? 'the target'
      const conflictText = `${n.ticket} did not rebase cleanly onto ${target} (attempt #${n.attempt}): conflict in ${n.file}.`
      const redText = `${n.check ?? 'CI'} failed on candidate ${a?.candidate_sha ?? ''} for ${n.ticket} (${target}).`
      const who = n.person ? (n.agent ? 'Its agent could not resolve it, so a person must.' : 'No agent works on it, so a person must resolve it.') : `${whoLabel(c, n.agent!)} picks this up unless you say otherwise.`
      const options =
        n.kind === 'red_checks'
          ? [
              { key: 'agent', label: 'Leave it to the agent', primary: true },
              { key: 'rerun', label: 'Re-run the checks' },
              { key: 'drop', label: 'Take it off the queue' },
            ]
          : n.person
            ? [
                { key: 'self', label: 'I will resolve it', primary: true },
                { key: 'agent', label: 'Hand it to an agent' },
                { key: 'drop', label: 'Take it off the queue' },
              ]
            : [
                { key: 'agent', label: 'Leave it to the agent', primary: true },
                { key: 'self', label: 'I will resolve it' },
                { key: 'drop', label: 'Take it off the queue' },
              ]
      const title = n.kind === 'conflict' ? (n.person ? 'Needs: conflict, a person must resolve' : 'Needs: conflict') : 'Needs: red checks'
      return {
        kind: 'decision' as const,
        id: `land.need:${n.id}`,
        addon: 'land',
        ticket: n.ticket,
        title,
        question: `${title}. ${n.kind === 'conflict' ? conflictText : redText}`,
        detail: `${who} A resolution or fix is new code: it voids the approval and the ticket goes back to review. The queue moved on.`,
        options,
        action: 'resolve',
      }
    })
}

/** Play the demo worker: one step every STEP_MS; it stops when idle, when the addon is off, or after MAX_STEPS. */
function startWorker(store: MockStore, ws: string) {
  const steps = Array.from({ length: MAX_STEPS }, (_, i) => ({
    afterMs: STEP_MS,
    run: (st: MockStore) => {
      const w = st.workspaces.find((x) => x.id === ws)
      if (!addonActive(w, 'land')) return st.sim.stop(scriptId(ws))
      const did = step(st, asLand(st.addonState(ws, 'land')))
      st.addonChanged(ws)
      if (did === null || i === MAX_STEPS - 1) st.sim.stop(scriptId(ws))
    },
  }))
  store.sim.play(scriptId(ws), steps)
}

function parseTargets(raw: unknown): string[] | string {
  const list = Array.isArray(raw) ? raw : typeof raw === 'string' ? raw.split(',') : null
  if (!list) return 'Write the allowed targets, separated by commas.'
  const out: string[] = []
  for (const x of list) {
    const t = typeof x === 'string' ? x.trim() : ''
    if (!t) continue
    if (!TARGET_RE.test(t)) return `"${String(x).slice(0, 40)}" is not a branch name.`
    if (!out.includes(t)) out.push(t)
  }
  if (out.length > MAX_TARGETS) return `At most ${MAX_TARGETS} targets.`
  return out
}

registerAddon({
  name: 'land',
  seed: (ws, store) => seedLand(ws, store),
  seedBusy: (ws, store, rng) => seedLandBusy(ws, store, rng),
  view,
  decisions,
  actions: {
    save_settings({ state: s0, body }) {
      const state = asLand(s0)
      const targets = parseTargets(body.targets)
      if (typeof targets === 'string') return invalid(targets)
      if (targets.includes(NEVER_TARGET)) return conflict('land.target_refused', '`main` is never a landing target (D33).', 'People merge to main by hand. Allow develop or a release branch instead.')
      const busy = state.queues.find((q) => q.entries.length > 0 && state.settings.targets.includes(q.target) && !targets.includes(q.target))
      if (busy) return conflict('land.target_busy', `${busy.target} still has tickets waiting in ${queueName(busy)}.`, 'Take them off the queue first, or keep the target.')
      const minutes = body.timeout_minutes === undefined ? state.settings.timeout_minutes : Number(body.timeout_minutes)
      if (!Number.isInteger(minutes) || minutes < 5 || minutes > 120) return invalid('The check timeout is 5 to 120 minutes.')
      const same = targets.join() === state.settings.targets.join() && minutes === state.settings.timeout_minutes
      state.settings = { targets, timeout_minutes: minutes }
      return { ok: true, message: same ? 'Nothing changed.' : 'Landing settings saved.', changed: !same }
    },

    enqueue({ state: s0, store, ws, viewer, ticket }) {
      const state = asLand(s0)
      const c = { store, ws, viewer }
      if (!ticket || !canSeeTicket(c, ticket)) return notFound('Open a ticket to queue it.')
      if (!approved(store, ticket)) return conflict('land.not_approved', `Only approved tickets enter the queue: ${ticket} has no standing verdict.`, 'Give the verdict first; a voided approval needs a new one.')
      const cur = attemptOf(state, state.worker.current)
      if (cur?.ticket === ticket && !cur.outcome) return { ok: true, message: `${ticket} is being checked now (attempt #${cur.n}).` }
      const at = state.queues.find((q) => q.entries.some((e) => e.ticket === ticket))
      if (at) return { ok: true, message: `${ticket} is already queued (#${at.entries.findIndex((e) => e.ticket === ticket) + 1}).` }
      if (mergedNow(state, ticket)) return conflict('land.already_merged', `${ticket} already landed.`, 'A new change needs a new ticket.')
      const target = state.settings.targets[0]
      if (!target) return conflict('land.no_target', 'No landing target is allowed.', 'An owner allows one in Settings > Addons > Landing.')
      const q = queueFor(state, remoteOf(store, ticket), target)
      for (const n of state.needs) if (n.ticket === ticket) n.open = false // a re-run: the old failure is answered
      q.entries.push({ ticket, branch: branchOf(store, ticket), source_sha: sourceOf(store, ticket), enqueued: store.now(), by: viewer })
      store.append(ticket, { type: 'land.queued', actor: ADDON, remote: q.remote, target: q.target, source_sha: sourceOf(store, ticket), by: viewer })
      return { ok: true, message: `${ticket} queued #${q.entries.length} on ${queueName(q)}.`, changed: true }
    },

    dequeue({ state: s0, store, ws, viewer, ticket }) {
      const state = asLand(s0)
      if (!ticket || !canSeeTicket({ store, ws, viewer }, ticket)) return notFound('No such ticket in the queue.')
      const cur = attemptOf(state, state.worker.current)
      if (cur?.ticket === ticket && !cur.outcome) return conflict('land.checking', `${ticket} is being checked now (attempt #${cur.n}).`, 'Wait for the attempt to end.')
      const q = state.queues.find((x) => x.entries.some((e) => e.ticket === ticket))
      if (!q) return { ok: true, message: `${ticket} was not queued.` }
      q.entries = q.entries.filter((e) => e.ticket !== ticket)
      store.append(ticket, { type: 'land.dequeued', actor: ADDON, by: viewer })
      return { ok: true, message: `${ticket} taken off the queue.`, changed: true }
    },

    // Decision action: core checked who decides, that the decision is open and the option one of its options.
    resolve({ state: s0, store, ws, viewer, body, decision }) {
      const state = asLand(s0)
      const need = decision && state.needs.find((n) => `land.need:${n.id}` === decision.id)
      if (!need) return conflict('decision.closed', 'That decision is closed.')
      const c = { store, ws, viewer }
      switch (body.option) {
        case 'agent': {
          need.person = false
          need.decided = true
          need.agent ??= `claude-code:s_land:${viewer}`
          return { ok: true, message: `Handed to ${whoLabel(c, need.agent)}. Its resolution will void the approval.`, changed: true }
        }
        case 'self': {
          resolve(store, state, need, viewer, nameOf(c, viewer))
          return { ok: true, message: `Resolution recorded. The approval is void: ${need.ticket} is back in review.`, changed: true }
        }
        case 'rerun': {
          need.open = false
          const a = attemptOf(state, need.attempt)
          const q = a ? queueOfAttempt(state, a) : undefined
          if (!q || !approved(store, need.ticket)) return { ok: true, message: `${need.ticket} is no longer approved; it needs a new verdict first.`, changed: true }
          if (!q.entries.some((e) => e.ticket === need.ticket)) q.entries.push({ ticket: need.ticket, branch: branchOf(store, need.ticket), source_sha: a!.source_sha, enqueued: store.now(), by: viewer })
          store.append(need.ticket, { type: 'land.queued', actor: ADDON, remote: q.remote, target: q.target, source_sha: a!.source_sha, by: viewer, rerun: true })
          return { ok: true, message: `${need.ticket} queued again with the same code; the checks run once more.`, changed: true }
        }
        default: {
          need.open = false
          return { ok: true, message: `${need.ticket} stays off the queue.`, changed: true }
        }
      }
    },

    run_worker({ store, ws }) {
      if (store.sim.running().includes(scriptId(ws))) return { ok: true, message: 'The worker is already running.' }
      startWorker(store, ws)
      return { ok: true, message: `The worker runs: one step every ${STEP_MS / 1000} s, one attempt at a time.`, changed: true }
    },

    stop_worker({ store, ws }) {
      if (!store.sim.running().includes(scriptId(ws))) return { ok: true, message: 'The worker was already stopped.' }
      store.sim.stop(scriptId(ws))
      return { ok: true, message: 'Worker stopped.', changed: true }
    },

    view_queues({ state, viewer }) {
      asLand(state).nav[viewer] = { view: 'queues' }
      return { ok: true, message: 'Queues.' }
    },

    view_history({ state, viewer }) {
      asLand(state).nav[viewer] = { view: 'history' }
      return { ok: true, message: 'History.' }
    },

    open_checks({ state: s0, store, ws, viewer, body }) {
      const a = asLand(s0).attempts.find((x) => x.n === Number(body.n))
      if (!a || !canSeeTicket({ store, ws, viewer }, a.ticket) || !a.checks.length) return notFound('No checks for that attempt.')
      const ci = a.checks.find((c) => c.level === 'CI') ?? a.checks[0]
      return { ok: true, message: `Checks of attempt #${a.n}.`, url: ci.url }
    },
  },
})

