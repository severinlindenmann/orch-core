import type { AddonDecision, NewTicketRequest } from '@/api/types'
import type { StoreFailure } from '../store'
import type { Rng } from '../busy/rng'
import { canSeeTicket, getAddon, openDecisions, registerAddon } from './registry'

// schedules (later, preview; v1 docs/schedules.md): agent work that starts without anyone typing.
//  - Three kinds. `schedule`: a skill on a clock. `listener`: a skill when an orch event happens. `recurring`: no
//    session, a ticket template whose finding lands on Today, where the person files it with one click.
//  - Times are UTC and computed from the mock clock (store.now()); nothing runs by itself in the mockup. "Run now" is
//    the only thing that starts a run, and adds one with a short markdown report (shown through SafeMarkdown).
//  - Arm and Disarm are `confirm: 'sign'` (core's signing prompt), maintainer-only. Run now needs an armed schedule.
//  - A recurring finding is a core decision on Today ("Dependency update · Monday: file it?"). Filing creates a backlog
//    ticket through store.createFromRequest, as the person who decides. Dismiss closes it.
//  - A filed ticket is named in the run history only to people who can see it.

type Kind = 'schedule' | 'listener' | 'recurring'
type Result = 'quiet' | 'finding' | 'failed' | 'skipped'
interface Template {
  label: string
  title: string
  ask: string
  type: 'chore' | 'feature' | 'bug'
  priority: 'low' | 'medium' | 'high' | 'urgent'
}
interface Schedule {
  id: string
  name: string
  kind: Kind
  skill: string | null
  every?: number // minutes
  between?: [string, string]
  days?: number[] // 0 = Sunday
  weekly?: number
  at?: string
  event?: string
  to?: string
  cooldown?: number // minutes
  template?: Template
  armed: boolean
  armedBy?: string
  armedAt?: string
  disarmedBy?: string
  disarmedAt?: string
}
interface Run {
  id: string
  schedule: string
  at: string
  trigger: 'clock' | 'manual' | 'event'
  result: Result
  summary: string
  report: string
  finding?: { title: string; ask: string; type: Template['type']; priority: Template['priority']; label: string }
  findingState?: 'open' | 'filed' | 'dismissed'
  filed?: string
}

const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const DOW_LONG = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const KIND_LABEL: Record<Kind, string> = { schedule: 'schedule', listener: 'listener', recurring: 'recurring ticket' }
const KEEP_RUNS = 200
const SHOWN_RUNS = 8
const pad = (n: number) => String(n).padStart(2, '0')
const minutes = (hhmm: string) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5))

const stamp = (iso: string) => {
  const d = new Date(iso)
  return `${DOW[d.getUTCDay()]} ${pad(d.getUTCDate())} ${MON[d.getUTCMonth()]} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`
}
/** ISO week number of a date. */
function isoWeek(iso: string): number {
  const d = new Date(iso)
  const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()))
  t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7))
  return Math.ceil(((t.getTime() - Date.UTC(t.getUTCFullYear(), 0, 1)) / 86_400_000 + 1) / 7)
}

function triggerText(s: Schedule): string {
  if (s.kind === 'listener') return `when a ticket moves to ${s.to} (cooldown ${s.cooldown} min)`
  if (s.weekly !== undefined) return `weekly on ${DOW_LONG[s.weekly]} at ${s.at}`
  const every = s.every! % 60 === 0 ? `${s.every! / 60} h` : `${s.every} min`
  const days = s.days!.join() === '1,2,3,4,5' ? 'Mon-Fri' : s.days!.map((d) => DOW[d]).join(', ')
  return `every ${every}, ${s.between![0]}-${s.between![1]}, ${days}`
}

/** The next slot after `now` for a clock schedule (UTC), or null. Scans minute by minute for 8 days. */
function nextSlot(s: Schedule, now: string): string | null {
  const start = Math.floor(Date.parse(now) / 60_000) * 60_000 + 60_000
  for (let i = 0; i < 8 * 24 * 60; i++) {
    const d = new Date(start + i * 60_000)
    const m = d.getUTCHours() * 60 + d.getUTCMinutes()
    const dow = d.getUTCDay()
    const hit =
      s.weekly !== undefined
        ? dow === s.weekly && m === minutes(s.at!)
        : s.days!.includes(dow) && m >= minutes(s.between![0]) && m <= minutes(s.between![1]) && (m - minutes(s.between![0])) % s.every! === 0
    if (hit) return d.toISOString()
  }
  return null
}
const nextText = (s: Schedule, now: string): string => {
  if (!s.armed) return 'not armed'
  if (s.kind === 'listener') return `on the next ticket moved to ${s.to}`
  const n = nextSlot(s, now)
  return n ? `${DOW[new Date(n).getUTCDay()]} ${n.slice(11, 16)} UTC` : 'no slot in the next 8 days'
}

const schedulesOf = (state: Record<string, unknown>) => state.schedules as Schedule[]
const runsOf = (state: Record<string, unknown>) => state.runs as Run[] // newest first
const selectedOf = (state: Record<string, unknown>, viewer: string): string | undefined => ((state.nav ?? {}) as Record<string, { selected?: string }>)[viewer]?.selected
const fail = (status: number, code: string, message: string): StoreFailure => ({ ok: false, status, code, message })
const lastRun = (state: Record<string, unknown>, id: string) => runsOf(state).find((r) => r.schedule === id)
const lastText = (state: Record<string, unknown>, id: string) => {
  const r = lastRun(state, id)
  return r ? `${stamp(r.at)}, ${r.result}` : 'never'
}

const TEMPLATE_DEPS: Template = { label: 'Dependency update', title: 'Update dependencies, week {week}', ask: 'Bump minor versions and run the tests.', type: 'chore', priority: 'medium' }

function findingFor(s: Schedule, at: string): Run['finding'] {
  const t = s.template!
  return { title: t.title.replace('{week}', String(isoWeek(at))), ask: t.ask, type: t.type, priority: t.priority, label: t.label }
}

/** The report a run writes. Deterministic from the schedule and the run number. */
function reportFor(s: Schedule, at: string, n: number): Pick<Run, 'result' | 'summary' | 'report' | 'finding' | 'findingState'> {
  if (s.kind === 'recurring') {
    const finding = findingFor(s, at)!
    return {
      result: 'finding',
      summary: `${s.name}: 1 finding`,
      report: `Prepared the ticket for **week ${isoWeek(at)}**.\n\n- Title: ${finding.title}\n- Ask: ${finding.ask}\n\nFile it from Today, or dismiss it.`,
      finding,
      findingState: 'open',
    }
  }
  if (s.kind === 'listener') {
    return { result: 'quiet', summary: 'Smoke test passed: 14 of 14 checks.', report: `Ran the smoke test on the staging build.\n\n**14 of 14 checks passed.** Nothing needs you.` }
  }
  const mails = 2 + (n % 5)
  return { result: 'quiet', summary: `${mails} mails, none need work.`, report: `**${mails} mails** since the last run, none need work.\n\nChecked the support inbox. Nothing was filed.` }
}

const seedSchedules = (): Schedule[] => [
  { id: 'check-inbox', name: 'Check inbox', kind: 'schedule', skill: 'triage-inbox', every: 60, between: ['08:00', '19:00'], days: [1, 2, 3, 4, 5], armed: true, armedBy: 'p_sev', armedAt: '2026-10-05T07:00:00Z' },
  { id: 'smoke-on-testing', name: 'Smoke test on testing', kind: 'listener', skill: 'smoke-test', event: 'ticket.moved', to: 'testing', cooldown: 30, armed: false },
  { id: 'deps-weekly', name: 'Weekly dependency update', kind: 'recurring', skill: null, weekly: 1, at: '07:00', template: TEMPLATE_DEPS, armed: true, armedBy: 'p_sev', armedAt: '2026-10-05T07:00:00Z' },
]

const decisionId = (r: Run) => `schedules.finding:${r.id}`

/** Busy day: 8 schedules in all (5 more) and a long run history; two recurring findings wait to be filed. */
function seedBusy(_ws: string, _store: unknown, rng: Rng) {
  const state = seedBase() as { schedules: Schedule[]; runs: Run[]; seq: number }
  const extra: Schedule[] = [
    { id: 'nightly-dbt', name: 'Nightly dbt build check', kind: 'schedule', skill: 'check-dbt-run', every: 240, between: ['00:00', '08:00'], days: [0, 1, 2, 3, 4, 5, 6], armed: true, armedBy: 'p_sev', armedAt: '2026-10-05T07:00:00Z' },
    { id: 'cost-weekly', name: 'Weekly cost report', kind: 'recurring', skill: null, weekly: 5, at: '16:00', template: { label: 'Cost report', title: 'Review the cost report, week {week}', ask: 'Read the usage report and flag anything above budget.', type: 'chore', priority: 'low' }, armed: true, armedBy: 'p_mara', armedAt: '2026-10-05T07:00:00Z' },
    { id: 'smoke-on-open', name: 'Plan check when a ticket opens', kind: 'listener', skill: 'review-plan', event: 'ticket.moved', to: 'open', cooldown: 15, armed: true, armedBy: 'p_sev', armedAt: '2026-10-06T07:00:00Z' },
    { id: 'stale-sweep', name: 'Stale ticket sweep', kind: 'schedule', skill: 'sweep-stale', every: 1440, between: ['07:00', '08:00'], days: [1, 2, 3, 4, 5], armed: false },
    { id: 'freshness-weekly', name: 'Source freshness review', kind: 'recurring', skill: null, weekly: 3, at: '09:00', template: { label: 'Freshness review', title: 'Review source freshness, week {week}', ask: 'Check which sources were late this week and file what to fix.', type: 'chore', priority: 'medium' }, armed: true, armedBy: 'p_sev', armedAt: '2026-10-05T07:00:00Z' },
  ]
  state.schedules.push(...extra)
  let n = state.seq
  const add = (s: Schedule, at: string, trigger: Run['trigger']) => {
    n++
    const run: Run = { id: `R-${n}`, schedule: s.id, at, trigger, ...reportFor(s, at, n) }
    if (run.findingState === 'open') run.findingState = n % 3 === 0 ? 'open' : 'dismissed'
    state.runs.unshift(run)
  }
  for (let d = 8; d >= 1; d--) {
    for (const s of extra.filter((x) => x.armed && x.kind === 'schedule')) add(s, `2026-10-${String(9 - d).padStart(2, '0')}T0${rng.int(1, 7)}:${String(rng.int(0, 5) * 10).padStart(2, '0')}:00Z`, 'clock')
    if (d % 3 === 0) for (const s of extra.filter((x) => x.kind === 'recurring')) add(s, `2026-10-${String(9 - d).padStart(2, '0')}T07:00:00Z`, 'clock')
    if (d % 2 === 0) add(extra[2], `2026-10-${String(9 - d).padStart(2, '0')}T10:${String(rng.int(10, 59))}:00Z`, 'event')
  }
  state.runs.sort((a, b) => b.at.localeCompare(a.at))
  state.seq = n
  return state
}

function seedBase() {
  const inbox = (id: string, at: string, mails: number): Run => ({
    id,
    schedule: 'check-inbox',
    at,
    trigger: 'clock',
    result: 'quiet',
    summary: `${mails} mails, none need work.`,
    report: `**${mails} mails** since the last run, none need work.\n\nChecked the support inbox. Nothing was filed.`,
  })
  const deps = seedSchedules()[2]
  const depsAt = '2026-10-05T07:00:00Z'
  return {
    schedules: seedSchedules(),
    runs: [
      inbox('R-4', '2026-10-09T11:00:00Z', 3),
      inbox('R-3', '2026-10-09T10:00:00Z', 5),
      inbox('R-2', '2026-10-09T09:00:00Z', 2),
      { id: 'R-1', schedule: 'deps-weekly', at: depsAt, trigger: 'clock', ...reportFor(deps, depsAt, 1) },
    ] satisfies Run[],
    seq: 4,
    nav: {},
  }
}

registerAddon({
  name: 'schedules',
  seed: seedBase,
  seedBusy,

  view(state, c) {
    const now = c.store.now()
    const schedules = schedulesOf(state)
    const runs = runsOf(state)
    const rows = schedules.map((s) => ({
      id: s.id,
      name: s.name,
      kind: s.kind,
      trigger: triggerText(s),
      skill: s.skill ?? 'none (files a ticket)',
      armed: s.armed,
      last: lastText(state, s.id),
      next: nextText(s, now),
    }))
    const items = rows.map((r) => {
      const s = schedules.find((x) => x.id === r.id)!
      return {
        title: r.name,
        subtitle: `${KIND_LABEL[s.kind]} · ${r.trigger}${s.skill ? ` · skill ${s.skill}` : ''} · last: ${r.last}`,
        badge: s.armed ? `next: ${r.next}` : 'not armed',
        status: s.armed ? ('ok' as const) : ('idle' as const),
        actions: [
          ...(s.armed ? [{ label: 'Run now', action: 'run_now', args: { id: s.id }, variant: 'ghost' as const }] : []),
          s.armed ? { label: 'Disarm', action: 'disarm', args: { id: s.id }, variant: 'ghost' as const } : { label: 'Arm', action: 'arm', args: { id: s.id }, variant: 'secondary' as const },
        ],
      }
    })
    const nameFor = (id: string) => schedules.find((s) => s.id === id)?.name ?? id
    const shown = runs.slice(0, SHOWN_RUNS)
    const selected = runs.find((r) => r.id === selectedOf(state, c.viewer)) ?? runs[0]
    const reportTitle = selected ? `Report: ${nameFor(selected.schedule)} · ${selected.id} · ${stamp(selected.at)}` : ''
    const report = selected?.report ?? ''
    const open = runs.filter((r) => r.findingState === 'open').length
    return {
      rows,
      items,
      // The raw runs carry the filed ticket's key: replace them with this viewer's version.
      runs: runs.map((r) => ({ id: r.id, schedule: r.schedule, at: r.at, result: r.result, summary: r.summary })),
      runItems: shown.map((r) => ({
        title: `${stamp(r.at)} · ${nameFor(r.schedule)}`,
        subtitle: r.filed && canSeeTicket(c, r.filed) ? `${r.summary} Filed as ${r.filed}.` : r.findingState === 'dismissed' ? `${r.summary} Dismissed.` : r.summary,
        badge: r.result,
        status: r.result === 'failed' ? ('error' as const) : r.result === 'finding' ? ('warn' as const) : ('ok' as const),
        actions: [{ label: 'Open', action: 'open_run', args: { run: r.id }, variant: 'ghost' as const }],
      })),
      report,
      reportTitle,
      reportNode: selected ? { type: 'markdown', text: `### ${reportTitle}\n\n${report}` } : { type: 'markdown', text: 'No runs yet. Arm a schedule, or press Run now.' },
      openFindings: open,
      armedCount: schedules.filter((s) => s.armed).length,
      findingsLine: open ? `${open} finding${open === 1 ? '' : 's'} waiting for you on Today.` : 'No findings waiting.',
      schedules: undefined,
      seq: undefined,
    }
  },

  decisions(state, _pkg): AddonDecision[] {
    const schedules = schedulesOf(state)
    return runsOf(state)
      .filter((r) => r.findingState === 'open' && r.finding)
      .map((r) => ({
        kind: 'decision' as const,
        id: decisionId(r),
        addon: 'schedules',
        title: schedules.find((s) => s.id === r.schedule)?.name ?? 'Schedule',
        question: `${r.finding!.label} · ${DOW_LONG[new Date(r.at).getUTCDay()]}: file it?`,
        detail: `Ticket to file in the backlog: "${r.finding!.title}". ${r.finding!.ask}`,
        options: [
          { key: 'file', label: 'File ticket in backlog', primary: true },
          { key: 'dismiss', label: 'Dismiss' },
        ],
        action: 'finding',
      }))
  },

  actions: {
    arm(ctx) {
      const s = schedulesOf(ctx.state).find((x) => x.id === ctx.body.id)
      if (!s) return fail(404, 'not_found', 'No such schedule.')
      if (s.armed) return { ok: true, message: `${s.name} is already armed.` }
      Object.assign(s, { armed: true, armedBy: ctx.viewer, armedAt: ctx.store.now() })
      return { ok: true, message: `Armed ${s.name}. Next run: ${nextText(s, ctx.store.now())}.`, changed: true }
    },

    disarm(ctx) {
      const s = schedulesOf(ctx.state).find((x) => x.id === ctx.body.id)
      if (!s) return fail(404, 'not_found', 'No such schedule.')
      if (!s.armed) return { ok: true, message: `${s.name} was already disarmed.` }
      Object.assign(s, { armed: false, disarmedBy: ctx.viewer, disarmedAt: ctx.store.now() })
      return { ok: true, message: `Disarmed ${s.name}.`, changed: true }
    },

    run_now(ctx) {
      const { state, store, viewer } = ctx
      const s = schedulesOf(state).find((x) => x.id === ctx.body.id)
      if (!s) return fail(404, 'not_found', 'No such schedule.')
      if (!s.armed) return fail(409, 'schedule.not_armed', `${s.name} is not armed.`)
      const seq = ((state.seq as number) ?? 0) + 1
      state.seq = seq
      const at = store.now()
      const run: Run = { id: `R-${seq}`, schedule: s.id, at, trigger: 'manual', ...reportFor(s, at, seq) }
      const runs = runsOf(state)
      runs.unshift(run)
      // Keep the newest runs, plus any with an open finding.
      state.runs = runs.filter((r, i) => i < KEEP_RUNS || r.findingState === 'open')
      ;((state.nav ??= {}) as Record<string, { selected?: string }>)[viewer] = { selected: run.id }
      return { ok: true, message: `Ran ${s.name}: ${run.summary}`, changed: true }
    },

    open_run({ state, viewer, body }) {
      const run = runsOf(state).find((r) => r.id === body.run)
      if (!run) return fail(404, 'not_found', 'No such run.')
      ;((state.nav ??= {}) as Record<string, { selected?: string }>)[viewer] = { selected: run.id }
      return { ok: true, message: `Opened ${run.id}.`, changed: true }
    },

    finding(ctx) {
      const { state, store, ws, body } = ctx
      const id = String(body.id ?? '')
      const open = openDecisions(getAddon('schedules'), state, [], ctx).find((d) => d.id === id)
      const run = open && runsOf(state).find((r) => decisionId(r) === id)
      if (!open || !run?.finding) return fail(409, 'decision.closed', 'That decision is closed.')
      if (body.option !== 'file' && body.option !== 'dismiss') return fail(400, 'validation.option', 'Choose File ticket in backlog or Dismiss.')
      if (body.option === 'dismiss') {
        run.findingState = 'dismissed'
        return { ok: true, message: 'Dismissed.', changed: true }
      }
      const f = run.finding
      const schedule = schedulesOf(state).find((s) => s.id === run.schedule)
      const req: NewTicketRequest = {
        type: f.type,
        title: f.title,
        priority: f.priority,
        size: null,
        labels: ['schedule'],
        parent: null,
        due: null,
        visibility: 'workspace',
        people: { owner: null, assignees: [], reviewers: [] },
        sections: { requirements: `${f.ask}\n\nFiled from the schedule "${schedule?.name ?? run.schedule}", run ${run.id}.` },
        acceptance: [],
      }
      const made = store.createFromRequest(ws, req)
      if (!made.ok) return made
      run.findingState = 'filed'
      run.filed = made.ticket.key
      return { ok: true, message: `Filed ${made.ticket.key} in the backlog.`, changed: true }
    },
  },
})
