import { atLeast } from '@/api/permissions'
import type { AddonDecision } from '@/api/types'
import { fmtDateTime } from '@/lib/time'
import type { MockStore, StoreFailure } from '../store'
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

export const RUN_STEP_MS = import.meta.env.MODE === 'test' ? 40 : 2_000
export const HOLD_CHOICES = [15, 30, 60, 240] as const
export const DEFAULT_HOLD = 30
export const CHILD_STEPS = ['Requirements', 'Build and test', 'Validate', 'Evidence'] as const
const MAX_GOAL = 200
const MAX_MEANS = 160
const MAX_RUNS_SHOWN = 4
const MIN = 60_000

type Ctx = Pick<AddonCtx, 'store' | 'ws' | 'viewer'>
export type Target = 'Preview' | 'Deliver'
export interface RunChild {
  title: string
  size: string
  /** Steps done, 0 to CHILD_STEPS.length. */
  done: number
}
export interface Run {
  id: string
  goal: string
  goesUpTo: Target
  deliverMeans: string | null
  holdMinutes: number
  signedBy: string
  signedAt: string
  planned: boolean
  children: RunChild[]
  stage: 'working' | 'preview' | 'holding' | 'delivered' | 'stopped'
  holdUntil?: string
  deliveredAt?: string
  stopped?: { at: string; by: string }
}
export interface RunDraft {
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
const nameOf = (c: Ctx, person: string) => c.store.workspaces.find((w) => w.id === c.ws)?.members.find((m) => m.person === person)?.name ?? person

/** The args core's signing prompt shows and the host checks again: everything the request authorises. */
export function runArgs(d: RunDraft): Record<string, string | number> {
  return d.goes_up_to === 'Deliver'
    ? { goal: d.goal, goes_up_to: 'Deliver', deliver_means: d.deliver_means ?? '', hold_minutes: d.hold_minutes ?? DEFAULT_HOLD, largest_child: 'm' }
    : { goal: d.goal, goes_up_to: 'Preview', largest_child: 'm' }
}

/** Checks a request (the form's data, or a signed body): a refusal, or the draft. */
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

const childrenFor = (goal: string): RunChild[] => {
  const g = goal.length > 60 ? `${goal.slice(0, 57)}…` : goal
  return [
    { title: `Make the first version: ${g}`, size: 's', done: 0 },
    { title: `Check it against the goal: ${g}`, size: 'm', done: 0 },
    { title: `Prepare what goes out: ${g}`, size: 'xs', done: 0 },
  ]
}

/** A new run from a signed request; the chain starts. */
export function startRun(store: MockStore, ws: string, state: Record<string, unknown>, d: RunDraft, by: string): Run {
  const seq = ((state.runSeq as number) ?? 0) + 1
  state.runSeq = seq
  const run: Run = {
    id: `R-${seq}`,
    goal: d.goal,
    goesUpTo: d.goes_up_to,
    deliverMeans: d.goes_up_to === 'Deliver' ? d.deliver_means! : null,
    holdMinutes: d.goes_up_to === 'Deliver' ? d.hold_minutes! : 0,
    signedBy: by,
    signedAt: store.now(),
    planned: false,
    children: childrenFor(d.goal),
    stage: 'working',
  }
  runsOf(state).unshift(run)
  const epic = state.epic as string
  store.append(epic, { type: 'factory.run_requested', actor: ADDON, run: run.id, goal: run.goal, goes_up_to: run.goesUpTo, ...(run.deliverMeans ? { deliver_means: run.deliverMeans, hold_minutes: run.holdMinutes } : {}) })
  armChain(store, ws)
  return run
}

/** Delivers every run whose window has ended (not while the factory is paused). Returns true when something changed. */
export function settleRuns(store: MockStore, state: Record<string, unknown>, paused: boolean): boolean {
  if (paused) return false
  const now = store.now()
  let changed = false
  for (const r of runsOf(state)) {
    if (r.stage === 'holding' && r.holdUntil && Date.parse(now) >= Date.parse(r.holdUntil)) {
      r.stage = 'delivered'
      r.deliveredAt = now
      store.append(state.epic as string, { type: 'factory.delivered', actor: ADDON, run: r.id, deliver_means: r.deliverMeans })
      changed = true
    }
  }
  return changed
}

/** One step of every working run: plan, then one child step, then Preview, then the hold before Deliver. */
function stepRuns(store: MockStore, state: Record<string, unknown>) {
  const epic = state.epic as string
  const now = store.now()
  for (const r of runsOf(state)) {
    if (r.stage === 'working') {
      if (!r.planned) {
        r.planned = true
        store.append(epic, { type: 'factory.run_step', actor: ADDON, run: r.id, step: 'Plan' })
        continue
      }
      const next = [...r.children].sort((a, b) => a.done - b.done)[0]
      if (next && next.done < CHILD_STEPS.length) {
        next.done += 1
        continue
      }
      r.stage = 'preview'
      store.append(epic, { type: 'factory.run_step', actor: ADDON, run: r.id, step: 'Preview' })
      continue
    }
    if (r.stage === 'preview' && r.goesUpTo === 'Deliver') {
      r.stage = 'holding'
      r.holdUntil = iso(Date.parse(now) + r.holdMinutes * MIN)
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
      const paused = !!state.paused
      if (!paused) {
        stepRuns(st, state)
        settleRuns(st, state, false)
      }
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

/** "via the factory full run you signed on 10 Oct 2026 14:02 UTC — no person reviewed this step" */
export function stepLabel(c: Ctx, r: Run) {
  const who = r.signedBy === c.viewer ? 'you' : nameOf(c, r.signedBy)
  return `via the factory full run ${who} signed on ${fmtDateTime(r.signedAt)} — no person reviewed this step`
}

export const minutesLeft = (r: Run, now: string) => Math.max(0, Math.ceil((Date.parse(r.holdUntil ?? now) - Date.parse(now)) / MIN))

/** The run as core draws it: a heading line, the request, its state and the steps with their labels. */
function runNode(c: Ctx, r: Run, now: string) {
  const label = stepLabel(c, r)
  const doneAll = r.children.every((k) => k.done >= CHILD_STEPS.length)
  const rows = [
    { step: 'Plan', item: `Split the goal into ${r.children.length} children (size m or smaller)`, state: r.planned ? 'done' : 'working', decided: r.planned ? label : '–' },
    ...r.children.map((k) => ({
      step: k.done >= CHILD_STEPS.length ? 'Evidence collected' : `${CHILD_STEPS[k.done]} (${k.done + 1} of ${CHILD_STEPS.length})`,
      item: `${k.title} · ${k.size}`,
      state: k.done >= CHILD_STEPS.length ? 'done' : r.planned ? 'working' : 'waiting',
      decided: k.done > 0 ? label : '–',
    })),
    { step: 'Preview', item: 'Made and checked, visible only inside this workspace', state: r.stage === 'working' ? (doneAll ? 'working' : 'waiting') : 'done', decided: r.stage === 'working' ? '–' : label },
    ...(r.goesUpTo === 'Deliver'
      ? [
          {
            step: 'Deliver',
            item: r.deliverMeans!,
            state: r.stage === 'holding' ? 'holding' : r.stage === 'delivered' ? 'done' : r.stage === 'stopped' ? 'stopped' : 'waiting',
            decided:
              r.stage === 'holding'
                ? `automatic after the hold window (${holdWord(r.holdMinutes)}) unless a person stops it`
                : r.stage === 'delivered'
                  ? label
                  : r.stage === 'stopped'
                    ? `stopped by ${nameOf(c, r.stopped!.by)}`
                    : '–',
          },
        ]
      : []),
  ]
  const status =
    r.stage === 'holding'
      ? { type: 'alert', tone: 'info', title: `Delivering in ${minutesLeft(r, now)} min · ${r.deliverMeans}`, text: `Goes out at ${fmtDateTime(r.holdUntil!)} unless a person stops it. Stop is above, with the other things that wait for you.` }
      : r.stage === 'delivered'
        ? { type: 'alert', tone: 'success', title: `Delivered: ${r.deliverMeans} at ${fmtDateTime(r.deliveredAt!)}` }
        : r.stage === 'stopped'
          ? { type: 'alert', tone: 'info', title: `Stopped by ${nameOf(c, r.stopped!.by)} at ${fmtDateTime(r.stopped!.at)}: nothing went out. The run stays at Preview.` }
          : r.stage === 'preview'
            ? { type: 'alert', tone: 'success', title: r.goesUpTo === 'Deliver' ? 'At Preview. The hold before Deliver starts next.' : 'At Preview: made and checked, visible only inside this workspace. It goes no further on its own.' }
            : { type: 'stack', children: [] }
  return {
    type: 'stack',
    children: [
      { type: 'markdown', text: `#### ${r.id} · ${r.goal}` },
      {
        type: 'kv',
        pairs: [
          { label: 'Goes up to', value: r.goesUpTo === 'Deliver' ? 'Deliver (it goes out)' : 'Preview (made and checked, visible only inside this workspace)' },
          ...(r.goesUpTo === 'Deliver' ? [{ label: 'Deliver means', value: r.deliverMeans! }, { label: 'Hold window', value: holdWord(r.holdMinutes) }] : []),
          { label: 'Signed', value: `by ${nameOf(c, r.signedBy)} on ${fmtDateTime(r.signedAt)}` },
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

/** The Full runs tab, the hold notices above the tabs and the counts. */
export function runsView(c: Ctx, state: Record<string, unknown>, opts: { epic: string | null; canRequest: boolean; canAnswer: boolean; isOwner: boolean }) {
  const now = c.store.now()
  const runs = opts.epic ? runsOf(state) : []
  const nav = (((state.nav ?? {}) as Record<string, { runDraft?: RunDraft }>)[c.viewer] ?? {}) as { runDraft?: RunDraft }
  const draft = nav.runDraft
  const holding = runs.filter((r) => r.stage === 'holding')
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
    formData: draft ?? { goes_up_to: 'Preview', hold_minutes: DEFAULT_HOLD },
    action: 'prepare_run',
    submitLabel: 'Review request',
  }
  const request = !opts.epic
    ? []
    : !opts.canRequest
      ? [{ type: 'markdown', text: 'Owners and maintainers request full runs.' }]
      : [
          { type: 'markdown', text: '### New full run\nOne request, one signature: the factory plans, writes requirements, builds and tests, validates and collects evidence for each child, then stops at Preview, or goes on to Deliver after a hold window you can stop. Children of size m or smaller; the code review gate stays with a person.' },
          form,
          ...(draft
            ? [
                {
                  type: 'alert',
                  tone: 'info',
                  title: draft.goes_up_to === 'Deliver' ? `Ready to sign: ${draft.goal}, all the way to Deliver (${draft.deliver_means})` : `Ready to sign: ${draft.goal}, up to Preview`,
                  text: draft.goes_up_to === 'Deliver' ? `The signing prompt lists every value. After Preview it holds for ${holdWord(draft.hold_minutes ?? DEFAULT_HOLD)} with a notice and Stop, then goes out to exactly that destination; anything else is refused.` : 'The signing prompt lists every value. The run ends at Preview and waits for you.',
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
  const list = runs.slice(0, MAX_RUNS_SHOWN).map((r) => runNode(c, r, now))
  const holdNotice =
    holding.length && opts.canAnswer
      ? holding.flatMap((r) => [
          { type: 'markdown', text: `**Full run ${r.id} · ${r.goal}: on hold before Deliver**` },
          { type: 'decision', id: `factory.hold:${r.id}` },
          ...(opts.isOwner ? [{ type: 'button', label: 'Simulate: let the hold time pass (demo)', action: 'simulate_time', variant: 'ghost', args: { run: r.id } }] : []),
        ])
      : holding.map((r) => ({ type: 'alert', tone: 'info', title: `Delivering in ${minutesLeft(r, now)} min · ${r.deliverMeans}`, text: 'An owner or maintainer can stop it until then.' }))
  return {
    runCount: runs.length,
    runsNode: {
      type: 'stack',
      children: [...request, ...(list.length ? [{ type: 'markdown', text: '### Runs' }, ...list] : opts.epic ? [{ type: 'markdown', text: 'No full runs yet.' }] : [])],
    },
    holdNode: { type: 'stack', children: holdNotice },
    holding: holding.length,
  }
}

/** Core decisions for runs that hold before Deliver: one option, Stop delivery (owners and maintainers decide). */
export function holdDecisions(c: Ctx, state: Record<string, unknown>, epic: string | null): AddonDecision[] {
  if (!atLeast(c.store.roleIn(c.ws, c.viewer), 'maintainer')) return []
  const now = c.store.now()
  return runsOf(state)
    .filter((r) => r.stage === 'holding')
    .map((r) => ({
      kind: 'decision' as const,
      id: `factory.hold:${r.id}`,
      addon: 'factory',
      ...(epic && canSeeTicket(c, epic) ? { ticket: epic } : {}),
      title: 'AI Factory full run: delivery on hold',
      question: `Delivering in ${minutesLeft(r, now)} min: ${r.deliverMeans}`,
      detail: `Full run ${r.id} "${r.goal}" reached Preview and goes out at ${fmtDateTime(r.holdUntil!)} unless you stop it. Stop cancels the delivery; the run stays at Preview.`,
      options: [{ key: 'stop', label: 'Stop delivery', primary: true }],
      terms: { run: r.id, deliver_means: r.deliverMeans!, hold_until: r.holdUntil! },
      hold: { until: r.holdUntil!, deliver_means: r.deliverMeans! },
      action: 'hold',
    }))
}

/** Busy day: one run holds before Deliver (28 min left) and an earlier one was delivered. */
export function seedRuns(store: MockStore, by: string): { runs: Run[]; runSeq: number } {
  const now = Date.parse(store.now())
  const done = (titles: [string, string][]) => titles.map(([title, size]) => ({ title, size, done: CHILD_STEPS.length }))
  return {
    runSeq: 2,
    runs: [
      {
        id: 'R-2',
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
      },
      {
        id: 'R-1',
        goal: 'September cost report for my boss',
        goesUpTo: 'Deliver',
        deliverMeans: 'Send to finance@example.test (boss)',
        holdMinutes: 30,
        signedBy: by,
        signedAt: iso(now - 26 * 60 * MIN),
        planned: true,
        children: done([['Pull September costs by model', 's'], ['Write the summary', 'xs'], ['Check the totals against usage', 's']]),
        stage: 'delivered',
        holdUntil: iso(now - 24 * 60 * MIN),
        deliveredAt: iso(now - 24 * 60 * MIN),
      },
    ],
  }
}

/** The mock's demo datasets: the only place the hold simulator (`simulate_time`) answers. */
export const DEMO_DATASETS = ['normal', 'busy'] as const

export const DEMO_REQUEST: RunDraft = { goal: 'Release monthly billing v2', goes_up_to: 'Deliver', deliver_means: 'Deploy to production', hold_minutes: DEFAULT_HOLD }

/** The keys whose signed value differs from the draft (empty: the signature still matches). */
export const staleRunKeys = (body: Record<string, unknown>, d: RunDraft) => {
  const want = runArgs(d)
  const keys = new Set([...Object.keys(want), ...['goal', 'goes_up_to', 'deliver_means', 'hold_minutes', 'largest_child'].filter((k) => body[k] !== undefined)])
  return [...keys].filter((k) => String(body[k] ?? '') !== String(want[k] ?? ''))
}
