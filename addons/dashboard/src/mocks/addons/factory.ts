import { addonActive } from '@/api/addons'
import { atLeast } from '@/api/permissions'
import type { AddonDecision } from '@/api/types'
import type { MockStore } from '../store'
import type { Rng } from '../busy/rng'
import { canSeeTicket, conflict, notFound, refusal, registerAddon, withinCharterSize, type AddonCtx } from './registry'
import { plain } from '@/components/sign/visible'
import { fmtDateTime, fmtWhen } from '@/lib/time'
import { approveReview, checkRequest, visibleEpic, DEMO_DATASETS, DEMO_REQUEST, ensureChain, holdBlocksOff, holdDecisions, hoursUsed, issueRequest, migrateState, MAX_CHILDREN, RUN_CHILDREN, requestNonce, MAX_HOURS, runsOf, runsView, seedRuns, settleRuns, staleRunKeys, startRun, type RunDraft } from './factory-runs'

// factory (AI Factory, Phase 2 preview; v1 docs/factory.md): one factory epic, DEMO-0050 "Monthly billing v2".
//  - The charter (25 children or 72 hours, children of size m or smaller) was signed when the epic started. The
//    children are real tickets with `parent` = the epic; "auto-approved by agent" is read from their gate events.
//  - The budget (`used`) is a counter kept in the addon state, like v1's markers beside the ledger, so the number
//    does not depend on which children a viewer may see. Rows, permits and the Ready report list only visible tickets.
//  - Permits are core-rendered decisions (`decisions()`): agents ask, a maintainer answers "Grant once / Grant for this
//    epic / Refuse". Core records the answer (addon.decided); the addon logs factory.permit_granted / _refused on the
//    epic as itself. Children are auto-approved through core (store.autoApprove, the charter hook). A grant for the epic is standing:
//    the same command asked again is answered at once and never reaches Today.
//  - Pause and Resume are declared `confirm: 'sign'` (core's signing prompt) and maintainer-only.
//  - "Run demo activity" (action `watch`) plays a simulator script (one child and one permit request about every 20 s). Nodes cannot tell that
//    someone has the page open, so watching is an explicit per-viewer action; the script stops when the last watcher
//    stops, when the factory is paused or stopped, and after WATCH_STEPS steps, so the demo never floods.
//  - Open permits are answered in place: the page carries `decision` nodes for them (core draws and signs), as well as Today.

const EPIC_TITLE = 'Monthly billing v2'
const MAX_SIZE = 'm' as const
const WATCH_STEPS = 10 // simulated steps per workspace per hour
const STEP_MS = 20_000
const MAX_PERMITS_SHOWN = 5
const SIZES = ['xs', 's', 'm', 'l', 'xl']
const WATCH_WINDOW_MS = 3_600_000 // the cap below counts per workspace over this window

type Ctx = Pick<AddonCtx, 'store' | 'ws' | 'viewer'>
type Mode = 'running' | 'paused' | 'stopped'
interface Permit {
  id: string
  command: string
  reason: string
  ticket: string
  state: 'open' | 'granted once' | 'granted for this epic' | 'refused'
  at: string
}
interface Nav {
  watching?: boolean
  /** A full-run request reviewed and waiting for the signature (per viewer). */
  runDraft?: RunDraft
}

const COMMANDS = [
  { command: 'dbt build --select state:modified+', reason: 'The build is the check for the task, and the sandbox denied it.' },
  { command: 'uv run pytest tests/billing -q', reason: 'Runs the billing unit tests the task needs.' },
  { command: 'psql -c "select count(*) from billing_runs"', reason: 'Counts rows in the billing runs table to check the backfill.' },
  { command: 'git push origin HEAD', reason: 'Pushes the child branch so its pull request can open.' },
  { command: 'curl -s https://api.acme-energy.example/tariffs', reason: 'Fetches the current tariff list to compare with the seeds.' },
]
const TITLES = [
  ['Round invoice lines to cents', 's'],
  ['Backfill billing_run_id for September', 'xs'],
  ['Add a tariff version column to the preview', 's'],
  ['Check proration across a leap day', 'm'],
  ['Document the billing run in the dbt docs', 'xs'],
  ['Handle a tariff that ends mid-period', 'm'],
  ['Add a dry-run flag to the billing job', 's'],
  ['Log the billing run summary', 'xs'],
  ['Reconcile credit notes with invoice lines', 'm'],
  ['Retry a failed billing run step', 's'],
] as const

const nameOf = (c: Ctx, person: string) => c.store.workspaces.find((w) => w.id === c.ws)?.members.find((m) => m.person === person)?.name ?? person
const utc = (iso: string) => fmtDateTime(iso)
const scriptId = (ws: string) => `factory:${ws}`
/** The addon writes its own events (`factory.*`) as itself; who answered or signed is in core's addon.decided / addon.action_signed. */
const ADDON = { kind: 'addon', id: 'factory' } as const
const navOf = (state: Record<string, unknown>, viewer: string): Nav => ((state.nav ?? {}) as Record<string, Nav>)[viewer] ?? {}
const setNav = (state: Record<string, unknown>, viewer: string, nav: Nav) => {
  ;((state.nav ??= {}) as Record<string, Nav>)[viewer] = nav
}
const permitsOf = (state: Record<string, unknown>) => state.permits as Permit[]
const sizeRank = (s: string | null) => (s ? SIZES.indexOf(s) : -1)

/** Child tickets of the epic in this workspace (all of them: the caller filters what a viewer may see). */
const childKeys = (store: MockStore, ws: string, epic: string) =>
  store
    .ticketKeys(ws)
    .filter((k) => store.ticket(k)?.parent === epic)
    .sort()

/** The epic, only when it exists in this workspace and the viewer may see it. */
const epicOf = (c: Ctx, state: Record<string, unknown>): string | null => {
  const key = state.epic as string | null
  return key && canSeeTicket(c, key) ? key : null
}

function modeOf(state: Record<string, unknown>, now: string): Mode {
  if ((state.used as number) >= MAX_CHILDREN || hoursUsed(state, now) >= MAX_HOURS) return 'stopped'
  return state.paused ? 'paused' : 'running'
}

/** How a child was approved, from its gate events (an agent's approval is "auto-approved by agent"). */
function approvalOf(store: MockStore, c: Ctx, key: string): string {
  const gates = store.eventsOf(key).filter((e) => e.type === 'gate.approved' && (e.gate === 'requirements' || e.gate === 'plan'))
  if (gates.some((e) => e.actor.kind === 'agent')) return 'auto-approved by agent'
  const person = gates.find((e) => e.actor.kind === 'person')
  if (person) return `approved by ${nameOf(c, (person.actor as { id: string }).id)}`
  return sizeRank(store.ticket(key)?.size ?? null) > sizeRank(MAX_SIZE) ? 'waits for you (above size m)' : 'waiting for approval'
}

/** The words core uses for a verdict nobody reviewed (the ticket page says the same). */
export const CHARTER_VERDICT = 'via the factory charter — no person reviewed this'

/** How a child's verdict was given, from core's verdict record: under the charter, by a person, or not yet. */
function verdictOf(store: MockStore, c: Ctx, key: string): string {
  const t = store.ticket(key)
  const v = t?.verdict
  if (!t || !v) return t?.status === 'testing' ? 'waiting' : '–'
  const code = t.gates.code.required && t.gates.code.state !== 'approved' && v.result === 'pass' ? '; waits for a code review by a person' : ''
  if (v.via === 'factory_charter') return `${CHARTER_VERDICT} (commit ${v.source_sha})${code}`
  return `${v.result === 'pass' ? 'pass' : 'sent back'} by ${nameOf(c, v.by)}${code}`
}

function readyReport(c: Ctx, epic: string, keys: string[]): string {
  if (!keys.length) return ''
  const statuses = keys.map((k) => c.store.ticket(k)!.status)
  if (!statuses.every((s) => s === 'testing' || s === 'done') || !statuses.includes('testing')) return ''
  const seen = keys.filter((k) => canSeeTicket(c, k))
  const lines = seen.map((k) => {
    const t = c.store.ticket(k)!
    const n = t.acceptance_state.length
    return `- **${k}** ${t.title}: ${approvalOf(c.store, c, k)}, ${n} acceptance ${n === 1 ? 'criterion' : 'criteria'}, ${t.status}${t.verdict ? `; verdict ${verdictOf(c.store, c, k)}` : ''}`
  })
  return [
    '### Ready for your verdict',
    '',
    `Every child of ${epic} is in testing or done. The evidence is what the agents wrote, not a check.`,
    '',
    ...lines,
    '',
    `Accept the epic and close its children by giving the verdict on ${epic}.`,
  ].join('\n')
}

/** Simulated steps in the last hour (the cap is per workspace, so pressing Run demo activity again cannot go past it). */
const recentSteps = (state: Record<string, unknown>, now: string) => ((state.simTimes as string[]) ?? []).filter((t) => Date.parse(now) - Date.parse(t) < WATCH_WINDOW_MS)

/**
 * One simulated step: an agent working for the person who pressed Run demo activity writes a child (through core's
 * createFromRequest, so ticket.create applies), auto-approves it, and asks for a permission.
 * Returns false when nothing was added (addon off, factory not running, cap used, or the person may no longer create).
 */
function simulateStep(store: MockStore, ws: string): boolean {
  const w = store.workspaces.find((x) => x.id === ws)
  if (!addonActive(w, 'factory')) return false
  const state = store.addonState(ws, 'factory')
  const epic = state.epic as string | null
  const now = store.now()
  if (!epic || modeOf(state, now) !== 'running' || recentSteps(state, now).length >= WATCH_STEPS) return false
  const person = state.simBy as string
  const agent = `claude-code:s_demo:${person}` // a simulated agent that belongs to the person who pressed Run demo activity
  const n = (state.simSteps as number) ?? 0
  const [title, size] = TITLES[n % TITLES.length]
  const made = store.createFromRequest(
    ws,
    {
      type: 'feature',
      title,
      priority: 'medium',
      size,
      labels: ['billing'],
      parent: epic,
      due: null,
      visibility: 'workspace',
      people: { owner: null, assignees: [], reviewers: ['p_mara'] },
      sections: { summary: title, requirements: `- ${title}.\n- Covered by a test.` },
      acceptance: [`${title}: done and covered by a test`],
    },
    { actor: agent, person },
  )
  if (!made.ok) return false
  const child = made.ticket
  // Auto-approval is core's (store.autoApprove checks the charter is in force); the addon never writes gate events.
  // A refused approval is not a step: the budget (`used`) counts only children core approved under the charter.
  if (!store.autoApprove(child.key, 'requirements', { charter: 'factory', by: agent }).ok) return false
  if (!store.autoApprove(child.key, 'plan', { charter: 'factory', by: agent }).ok) return false
  // The charter approves everything, verdicts included (owner decision 2026-10-10): a child waiting in testing gets
  // its verdict through core, on its branch head. A code review stays human (core refuses it under a charter).
  const waiting = childKeys(store, ws, epic).find((k) => {
    const t = store.ticket(k)
    return t?.status === 'testing' && !t.verdict && withinCharterSize(t.size, MAX_SIZE)
  })
  if (waiting) store.autoApprove(waiting, 'verify', { charter: 'factory', by: agent })
  state.used = (state.used as number) + 1
  state.simSteps = n + 1
  state.simTimes = [...recentSteps(state, now), now]
  const seq = ((state.seq as number) ?? 0) + 1
  state.seq = seq
  const ask = COMMANDS[seq % COMMANDS.length]
  const standing = (state.epicGrants as string[]).includes(ask.command)
  permitsOf(state).unshift({ id: `P-${seq}`, command: ask.command, reason: ask.reason, ticket: child.key, state: standing ? 'granted for this epic' : 'open', at: now })
  // A standing grant answers at once; it is still logged on the epic, marked as standing.
  if (standing) store.append(epic, { type: 'factory.permit_granted', actor: ADDON, permit: `P-${seq}`, scope: 'epic', standing: true, child: child.key, command: ask.command })
  return true
}

/** Nobody is watching any more: forget the flags and stop the script. */
function endWatching(store: MockStore, ws: string, state: Record<string, unknown>) {
  for (const nav of Object.values((state.nav ?? {}) as Record<string, Nav>)) nav.watching = false
  store.sim.stop(scriptId(ws))
}

function startScript(store: MockStore, ws: string) {
  const steps = Array.from({ length: WATCH_STEPS }, (_, i) => ({
    afterMs: STEP_MS,
    run: (st: MockStore) => {
      const state = st.addonState(ws, 'factory')
      if (!simulateStep(st, ws) || i === WATCH_STEPS - 1) endWatching(st, ws, state)
    },
  }))
  store.sim.play(scriptId(ws), steps)
}

/** Busy day: the epic has 20 children (the store holds them), 5 permits wait for an answer and older ones are decided. */
function seedBusy(ws: string, store: MockStore, rng: Rng) {
  const state = seedBase(ws, store) as { epic: string | null; permits: Permit[]; seq: number }
  if (!state.epic) return state
  const kids = childKeys(store, ws, state.epic)
  const generated = kids.filter((k) => Number(k.slice(k.lastIndexOf('-') + 1)) >= 100)
  const permits = state.permits
  const states: Permit['state'][] = ['open', 'open', 'open', 'open', 'granted once', 'granted for this epic', 'refused', 'granted once', 'granted once', 'refused']
  states.forEach((st, i) => {
    const ask = COMMANDS[(i + 1) % COMMANDS.length]
    const day = st === 'open' ? 9 : 8 - (i % 3)
    permits.unshift({ id: `P-${i + 3}`, ...ask, ticket: generated[i % Math.max(1, generated.length)] ?? kids[0], state: st, at: `2026-10-0${day}T${String(rng.int(7, 11)).padStart(2, '0')}:${String(rng.int(10, 59))}:00Z` })
  })
  permits.sort((a, b) => b.at.localeCompare(a.at))
  state.seq = 2 + states.length
  // Full runs (D61 option): one holds before Deliver, an earlier one was delivered.
  Object.assign(state, seedRuns(store, 'p_sev', ws))
  // A valid charter state: the run's children are reserved in the child budget like any admitted run's.
  ;(state as unknown as { used: number }).used += RUN_CHILDREN
  return state
}

function seedBase(ws: string, store: MockStore) {
  const epic = store.ticketKeys(ws).find((k) => k === 'DEMO-0050' && store.ticket(k)?.type === 'epic') ?? null
  const kids = epic ? childKeys(store, ws, epic) : []
  return {
    epic,
    startedAt: '2026-10-07T08:00:00Z',
    startedBy: 'p_sev',
    paused: null,
    pausedMs: 0,
    used: kids.length,
    seq: 2,
    simSteps: 0,
    simTimes: [],
    simBy: null,
    epicGrants: [],
    runs: [],
    runSeq: 0,
    requestSeq: 0,
    requestNonce: requestNonce(),
    usedRequests: [],
    permits: epic
      ? ([
          { id: 'P-2', ...COMMANDS[2], ticket: 'DEMO-0052', state: 'open', at: '2026-10-09T10:40:00Z' },
          { id: 'P-1', ...COMMANDS[0], ticket: 'DEMO-0051', state: 'granted once', at: '2026-10-07T12:10:00Z' },
        ] satisfies Permit[])
      : [],
    nav: {},
  }
}

registerAddon({
  name: 'factory',
  stateVersion: 4, // full runs: request ids with a nonce, code review approvals per commit, holds on the wall clock
  migrate: migrateState,
  saveOnSeed: true,
  seed: seedBase,
  seedBusy,

  view(state, c) {
    const now = c.store.now()
    if (state.epic) {
      settleRuns(c.store, state)
      ensureChain(c.store, c.ws, state)
    }
    const epic = epicOf(c, state)
    const mode = modeOf(state, now)
    const used = state.used as number
    const hours = hoursUsed(state, now)
    const visible = epic ? childKeys(c.store, c.ws, epic).filter((k) => canSeeTicket(c, k)) : []
    const children = visible.map((k) => {
      const t = c.store.ticket(k)!
      return { ticket: k, title: t.title, status: t.status, approval: approvalOf(c.store, c, k), verdict: verdictOf(c.store, c, k), size: t.size ?? '–' }
    })
    const permits = permitsOf(state).filter((p) => canSeeTicket(c, p.ticket))
    const open = permits.filter((p) => p.state === 'open').length
    const canAnswer = atLeast(c.store.roleIn(c.ws, c.viewer), 'maintainer')
    const paused = state.paused as { at: string; by: string } | null
    const watching = !!navOf(state, c.viewer).watching && c.store.sim.running().includes(scriptId(c.ws))
    const runs = runsView(c, state, { epic, canRequest: canAnswer, canAnswer, isOwner: c.store.roleIn(c.ws, c.viewer) === 'owner' })

    const stateAlert =
      !epic
        ? { type: 'alert', tone: 'info', title: 'No factory epic here', text: 'This workspace has no epic that was started as an AI Factory.' }
        : mode === 'stopped'
          ? {
              type: 'alert',
              tone: 'warn',
              title: 'Stopped: the budget is used up',
              text:
                used >= MAX_CHILDREN
                  ? `The child budget is used up (${used} of ${MAX_CHILDREN}). Agents start no new children and no grant answers anything until you decide again.`
                  : `The time budget is used up (${MAX_HOURS} hours). Agents make no new auto-approvals, start no tasks and no grant answers anything until you decide again.`,
            }
          : mode === 'paused'
            ? { type: 'alert', tone: 'info', title: `Paused by ${nameOf(c, paused!.by)} ${fmtWhen(paused!.at, c.store.now())}`, text: 'Agents hold their work and nothing new starts. The time budget stops while paused. Resume when you are ready.' }
            : { type: 'stack', children: [] }

    // Everyone sees that demo activity is running, not only the person who started it.
    const demoAlert = c.store.sim.running().includes(scriptId(c.ws))
      ? { type: 'alert', tone: 'info', title: `Demo activity is running: a new child and permit about every ${STEP_MS / 1000} s, at most ${WATCH_STEPS} an hour.` }
      : { type: 'stack', children: [] }
    // Near the child budget: say so before the factory stops (a stopped factory has its own alert).
    const budgetAlert =
      epic && mode !== 'stopped' && used >= Math.ceil(MAX_CHILDREN * 0.8)
        ? { type: 'alert', tone: 'warn', title: `${used} of ${MAX_CHILDREN} children used. The factory stops at ${MAX_CHILDREN}.` }
        : { type: 'stack', children: [] }
    const button = (label: string, action: string, variant: 'primary' | 'secondary' | 'ghost') => ({ type: 'button', label, action, variant })
    const controls = {
      type: 'stack',
      direction: 'row',
      children: !epic
        ? []
        : mode === 'stopped'
          ? []
          : mode === 'paused'
            ? [button('Resume', 'resume', 'primary')]
            : [button('Pause factory', 'pause', 'secondary'), watching ? button('Stop demo activity', 'stop_watching', 'ghost') : button('Run demo activity', 'watch', 'ghost')],
    }
    const report = epic ? readyReport(c, epic, childKeys(c.store, c.ws, epic)) : ''
    const permitRows = permits.slice(0, 6).map((p) => ({ id: p.id, command: p.command, child: p.ticket, state: p.state }))
    const startedBy = nameOf(c, state.startedBy as string)

    return {
      epic: epic ? { key: epic, title: EPIC_TITLE } : null,
      epicLine: epic ? `${epic} · ${EPIC_TITLE}` : 'none',
      mode,
      stateAlert,
      charter: `Signed by ${startedBy} on ${utc(state.startedAt as string)}: up to ${MAX_CHILDREN} children or ${MAX_HOURS} hours, whichever comes first. Children of size ${MAX_SIZE} or smaller only; larger ones wait for you. The charter approves everything on them, verdicts included: no person reviews that work unless the code review gate is on for them (Settings → Gates).`,
      maxChildren: MAX_CHILDREN,
      maxHours: MAX_HOURS,
      used,
      hoursUsed: Math.round(hours * 10) / 10,
      usedProgress: { type: 'progress', label: 'Children used', value: Math.min(used, MAX_CHILDREN), max: MAX_CHILDREN },
      timeProgress: { type: 'progress', label: 'Hours used', value: Math.min(Math.round(hours), MAX_HOURS), max: MAX_HOURS },
      controls,
      children,
      permits,
      permitRows,
      openPermits: open,
      childCount: children.length,
      permitsLine: open
        ? canAnswer
          ? `${open} waiting for you: Grant once, Grant for this epic or Refuse.`
          : `${open} waiting for an owner or maintainer to answer.`
        : 'Nothing waiting. Agents ask here when they need a permission they do not hold.',
      // Core draws and signs each open permit in place (decision nodes); Today lists the same decisions.
      permitNodes: { type: 'stack', children: canAnswer ? permits.filter((p) => p.state === 'open').slice(0, MAX_PERMITS_SHOWN).map((p) => ({ type: 'decision', id: `factory.permit:${p.id}` })) : [] },
      // What waits for a person sits above the tabs, never behind one: open permits as core's decision rows (core signs).
      // A full run holding before Deliver comes first: it goes out on its own unless someone stops it.
      attentionNode: {
        type: 'stack',
        children: [
          ...(runs.holding ? [runs.holdNode] : []),
          ...(open > 0 && canAnswer
            ? [
                { type: 'markdown', text: `**${open === 1 ? '1 permission request needs' : `${open} permission requests need`} your decision**` },
                ...permits.filter((p) => p.state === 'open').slice(0, MAX_PERMITS_SHOWN).map((p) => ({ type: 'decision', id: `factory.permit:${p.id}` })),
              ]
            : []),
        ],
      },
      runCount: runs.runCount,
      runsNode: runs.runsNode,
      runs: undefined,
      runSeq: undefined,
      permitCount: permits.length,
      demoAlert,
      budgetAlert,
      report,
      readyNode: report ? { type: 'markdown', text: report } : { type: 'markdown', text: 'The Ready report appears here once every child is in testing or done.' },
      watching,
      simSteps: undefined,
      simTimes: undefined,
      simBy: undefined,
      epicGrants: undefined,
      startedBy: undefined,
      seq: undefined,
      pausedMs: undefined,
    }
  },

  // Core refuses to turn the factory off or change it while a delivery is on hold (its Stop would go with it).
  offBlocked(state, c) {
    // Settled first: a hold whose window already ended is delivered (or not), not counted as on hold.
    if (state.epic) settleRuns(c.store, state)
    return holdBlocksOff(state)
  },

  // The charter signed when the epic started; in force while the factory runs (not paused, not stopped).
  charter(state, c) {
    const epic = state.epic as string | null
    return epic ? { epic, signedBy: state.startedBy as string, active: modeOf(state, c.store.now()) === 'running', maxSize: MAX_SIZE } : null
  },

  decisions(state, _pkg, c): AddonDecision[] {
    // Answering a permit lets a command run: owners and maintainers only.
    if (!atLeast(c.store.roleIn(c.ws, c.viewer), 'maintainer')) return []
    // The epic is named only to people who can see it (a child can be visible while its epic is not).
    const epic = epicOf(c, state)
    if (state.epic) settleRuns(c.store, state)
    const holds = state.epic ? holdDecisions(c, state, epic) : []
    return holds.concat(permitsOf(state)
      .filter((p) => p.state === 'open' && canSeeTicket(c, p.ticket))
      .map((p) => ({
        kind: 'decision' as const,
        id: `factory.permit:${p.id}`,
        addon: 'factory',
        ticket: p.ticket,
        title: 'AI Factory permit',
        question: `${p.ticket} asks to run: ${p.command}`,
        detail: `Reason: ${p.reason}${epic ? ` Epic ${epic}.` : ''} A grant for the epic answers this exact command until the epic ends.`,
        options: [
          { key: 'once', label: 'Grant once', primary: true },
          { key: 'epic', label: 'Grant for this epic' },
          { key: 'refuse', label: 'Refuse' },
        ],
        action: 'permit',
      })))
  },

  actions: {
    permit(ctx) {
      // A decision action: core checked who decides, that it is open and that the option is one of its options.
      const { state, store, body } = ctx
      const permit = ctx.decision && permitsOf(state).find((p) => `factory.permit:${p.id}` === ctx.decision!.id)
      if (!permit) return conflict('decision.closed', 'That decision is closed.') // only when the manifest lacks `decision: true`
      const epic = state.epic as string
      if (body.option === 'refuse') {
        permit.state = 'refused'
        store.append(epic, { type: 'factory.permit_refused', actor: ADDON, permit: permit.id, child: permit.ticket, command: permit.command })
        return { ok: true, message: `Refused ${permit.id}.`, changed: true }
      }
      const scope = body.option
      permit.state = scope === 'epic' ? 'granted for this epic' : 'granted once'
      if (scope === 'epic') (state.epicGrants as string[]).push(permit.command)
      store.append(epic, { type: 'factory.permit_granted', actor: ADDON, permit: permit.id, scope, child: permit.ticket, command: permit.command })
      return { ok: true, message: scope === 'epic' ? `Granted ${permit.id} for this epic.` : `Granted ${permit.id} once.`, changed: true }
    },

    // ---- full runs (D61 option): review the request, then sign it in core's prompt
    prepare_run(c) {
      const epic = visibleEpic(c, c.state)
      if (typeof epic !== 'string') return epic
      const d = checkRequest(c, (c.body.formData ?? {}) as Record<string, unknown>)
      if ('ok' in d) return d
      // Every review issues a new single-use request id: the signature binds it, the start consumes it.
      setNav(c.state, c.viewer, { ...navOf(c.state, c.viewer), runDraft: issueRequest(c, c.state, d) })
      return { ok: true, message: 'Review and sign the request.' }
    },
    clear_run(c) {
      setNav(c.state, c.viewer, { ...navOf(c.state, c.viewer), runDraft: undefined })
      return { ok: true, message: 'Request discarded.' }
    },
    demo_run(c) {
      const epic = visibleEpic(c, c.state)
      if (typeof epic !== 'string') return epic
      const d = checkRequest(c, { ...DEMO_REQUEST })
      if ('ok' in d) return d
      setNav(c.state, c.viewer, { ...navOf(c.state, c.viewer), runDraft: issueRequest(c, c.state, d) })
      return { ok: true, message: 'Demo request filled in: review and sign it.' }
    },
    // Signed in core's prompt (`confirm: 'sign'`): the covers list the goal, how far it goes, what Deliver means and the hold.
    start_run(c) {
      const { state, store, ws, viewer, body } = c
      const epic = visibleEpic(c, state)
      if (typeof epic !== 'string') return epic
      const d = checkRequest(c, body)
      if ('ok' in d) return d
      // A request id is single use: a replayed signature is refused, whatever the draft holds now.
      if (typeof body.request === 'string' && ((state.usedRequests as string[] | undefined) ?? []).includes(body.request))
        return conflict('factory.request_used', `Request ${body.request} was already used to start a run.`, 'Review the request again and sign the new one.')
      const draft = navOf(state, viewer).runDraft
      if (!draft) return conflict('factory.no_request', 'Review the request first.', 'Fill in the form and press Review request.')
      const off = staleRunKeys(body, draft)
      if (off.length) return conflict('factory.stale', `What you signed no longer matches the request: ${off.join(', ')}.`, 'Review it again and sign.')
      // One step: the charter check, the request consumed, the children reserved, the run created (or nothing).
      const run = startRun(store, ws, state, draft, viewer)
      if ('ok' in run) return run
      setNav(state, viewer, { ...navOf(state, viewer), runDraft: undefined })
      return { ok: true, message: run.goesUpTo === 'Deliver' ? `Full run ${run.id} started. It holds before Deliver (${run.holdMinutes} min) with a notice and Stop.` : `Full run ${run.id} started. It ends at Preview.`, changed: true }
    },
    // A decision (Stop delivery): core checked who decides, that it is open and its terms; the addon cancels the delivery.
    hold(ctx) {
      const { state, store, body, viewer } = ctx
      const epic = visibleEpic(ctx, state)
      if (typeof epic !== 'string') return epic
      settleRuns(store, state)
      const run = ctx.decision && runsOf(state).find((r) => `factory.hold:${r.id}` === ctx.decision!.id)
      if (!run || run.stage !== 'holding') return conflict('decision.closed', 'That delivery is no longer on hold.')
      if (body.option !== 'stop') return conflict('decision.closed', 'That decision is closed.')
      run.stage = 'stopped'
      run.stopped = { at: store.now(), by: viewer }
      store.append(state.epic as string, { type: 'factory.deliver_stopped', actor: ADDON, run: run.id })
      return { ok: true, message: `Stopped the delivery of ${run.id}. Nothing went out; the run stays at Preview.`, changed: true }
    },
    // A code review on a full run's child (a decision, signed by a person in core's prompt; core records addon.decided).
    code_review(ctx) {
      const { state, store, viewer } = ctx
      const epic = visibleEpic(ctx, state)
      if (typeof epic !== 'string') return epic
      if (!ctx.decision || ctx.body.option !== 'approve') return conflict('decision.closed', 'That decision is closed.')
      const done = approveReview(store, ctx.ws, state, ctx.decision.id, viewer, ctx.body.terms)
      if ('ok' in done) return done
      return { ok: true, message: done.done ? `Code review approved for ${done.run.id}. The child goes on to Validate.` : `Code review approval recorded for ${done.run.id}; more approvals are needed.`, changed: true }
    },
    // A SIMULATOR control of the mock, not an operation: no host operation shortens a Deliver hold (the only human
    // act during the hold is a signed Stop). The mock clock runs in real time, so the demo lets the simulated time of
    // one hold pass. Owners only, and only on the demo datasets.
    simulate_time(c) {
      const { state, store, body, viewer, ws } = c
      const epic = visibleEpic(c, state)
      if (typeof epic !== 'string') return epic
      if (store.roleIn(ws, viewer) !== 'owner') return refusal(403, 'forbidden', 'Only an owner can use the demo simulator.')
      if (!(DEMO_DATASETS as readonly string[]).includes(store.dataset)) return conflict('factory.not_demo', 'This simulator control exists only in the demo data. Nothing shortens a hold.')
      const run = runsOf(state).find((r) => r.id === body.run)
      if (!run || run.stage !== 'holding') return conflict('factory.not_holding', 'That run is not on hold.')
      if (state.paused) return conflict('factory.paused', 'The factory is paused: the hold clock does not run.')
      run.holdUntil = store.now() // the simulated time of the hold has passed
      run.holdWallUntil = Date.now()
      settleRuns(store, state)
      if ((run.stage as string) !== 'delivered') return conflict('factory.not_running', 'The charter is stopped: the hold ended without delivery.')
      return { ok: true, message: `Simulated: the hold time of ${run.id} passed. Delivered: ${plain(run.deliverMeans ?? '')}.`, changed: true }
    },

    pause({ state, store, viewer, ws }) {
      const epic = state.epic as string | null
      if (!epic) return notFound('There is no factory epic here.')
      const mode = modeOf(state, store.now())
      if (mode === 'paused') return { ok: true, message: 'The factory is already paused.' }
      if (mode === 'stopped') return conflict('factory.stopped', 'The factory is stopped; there is nothing to pause.')
      // A hold freezes: what is left of it is kept (wall clock), so a reload while paused never shortens or ends it.
      settleRuns(store, state)
      for (const r of runsOf(state))
        if (r.stage === 'holding') r.holdRemainingMs = Math.max(0, r.holdWallUntil !== undefined ? r.holdWallUntil - Date.now() : Date.parse(r.holdUntil!) - Date.parse(store.now()))
      state.paused = { at: store.now(), by: viewer }
      endWatching(store, ws, state)
      store.append(epic, { type: 'factory.paused', actor: ADDON })
      return { ok: true, message: 'Factory paused.', changed: true }
    },

    resume({ state, store }) {
      const epic = state.epic as string | null
      const paused = state.paused as { at: string } | null
      if (!epic || !paused) return conflict('factory.not_paused', 'The factory is not paused.')
      const pausedFor = Date.parse(store.now()) - Date.parse(paused.at)
      state.pausedMs = (state.pausedMs as number) + pausedFor
      state.paused = null
      // The hold clock did not run while paused: both deadlines are rebuilt from the remaining time kept at pause.
      for (const r of runsOf(state))
        if (r.stage === 'holding' && r.holdUntil) {
          const left = r.holdRemainingMs ?? Date.parse(r.holdUntil) + pausedFor - Date.parse(store.now())
          r.holdUntil = new Date(Date.parse(store.now()) + left).toISOString().replace(/\.\d{3}Z$/, 'Z')
          r.holdWallUntil = Date.now() + left
          delete r.holdRemainingMs
        }
      store.append(epic, { type: 'factory.resumed', actor: ADDON })
      return { ok: true, message: 'Factory resumed.', changed: true }
    },

    watch({ state, store, viewer, ws }) {
      if (!state.epic) return notFound('There is no factory epic to watch here.')
      if (modeOf(state, store.now()) !== 'running') return conflict('factory.not_running', 'The factory is not running; there is nothing to watch.')
      if (recentSteps(state, store.now()).length >= WATCH_STEPS) return conflict('factory.demo_limit', `Demo limit reached: ${WATCH_STEPS} simulated steps per hour in this workspace.`, 'Try again later.')
      setNav(state, viewer, { watching: true })
      if (!store.sim.running().includes(scriptId(ws))) {
        state.simBy = viewer
        startScript(store, ws)
      }
      return { ok: true, message: `Demo activity started: a new child and permit request about every ${STEP_MS / 1000} s, ${WATCH_STEPS} at most per hour.`, changed: true }
    },

    stop_watching({ state, store, viewer, ws }) {
      setNav(state, viewer, { watching: false })
      const others = Object.values((state.nav ?? {}) as Record<string, Nav>).some((n) => n.watching)
      if (!others) store.sim.stop(scriptId(ws))
      return { ok: true, message: 'Demo activity stopped.', changed: true }
    },
  },
})
