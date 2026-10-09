import type { AddonActionResult, AddonDecision, NewTicketRequest } from '@/api/types'
import { briefs } from '../busy/helpers'
import type { Rng } from '../busy/rng'
import type { MockStore, StoreFailure } from '../store'
import { canSeeTicket, conflict, invalid, markDecided, notFound, registerAddon, type AddonCtx } from './registry'

// quick tasks: one-line jobs too small for a ticket (v1 docs/quick-tasks.md).
//  - Keys Q-001..; status open -> claimed -> done (with one line of proof). Past the limit (commits, files) a task is
//    `outgrew`; it then asks the owner for a decision (runtime decision, rendered by core on Today).
//  - "Make a ticket" creates a backlog chore through the store's ticket-creation path (same validation as POST tickets)
//    and marks the task `converted` with the new key.
//  - Closing needs a proof line: "Close with proof" opens a one-line field in the task's own row (list item `input`), then posts `close {id, proof}`.
//  - "Agents may add" is a setting (off). The mock's add action is a person action either way.

type Status = 'open' | 'claimed' | 'done' | 'outgrew' | 'converted'
interface Quick {
  id: string
  title: string
  status: Status
  added_by: string
  claimed_by?: string
  proof?: string
  commits?: number
  files?: number
  /** Extra files allowed on this task after the owner chose "Allow 3 more files". */
  extra_files?: number
  ticket?: string
}
interface Settings {
  agents_add: boolean
  max_commits: number
  max_files: number
}
const DEFAULTS: Settings = { agents_add: false, max_commits: 1, max_files: 3 }
const MORE_FILES = 3
const MAX_TITLE = 120

const seed = (): Quick[] => [
  { id: 'Q-001', title: 'Fix the typo in README', status: 'open', added_by: 'Severin' },
  { id: 'Q-002', title: 'Bump the dbt-core patch version', status: 'open', added_by: 'Mara' },
  { id: 'Q-003', title: 'Remove the dead import in billing_api/jobs.py', status: 'claimed', added_by: 'Severin', claimed_by: 'Claude Code' },
  { id: 'Q-004', title: 'Rename the stale seeds fixture', status: 'outgrew', added_by: 'Severin', commits: 4, files: 7 },
  { id: 'Q-005', title: 'Drop the unused logo.png', status: 'done', added_by: 'Mara', claimed_by: 'Claude Code', proof: 'removed, 4f2a91c' },
  { id: 'Q-006', title: 'Update the freshness check comment', status: 'open', added_by: 'Claude Code' },
]

const list = (state: Record<string, unknown>) => state.items as Quick[]
const settingsOf = (state: Record<string, unknown>): Settings => {
  const s = (state.settings ?? {}) as Partial<Settings>
  const n = (v: unknown, d: number) => (Number.isInteger(v) && (v as number) >= 0 ? (v as number) : d)
  return { agents_add: s.agents_add === true, max_commits: n(s.max_commits, DEFAULTS.max_commits), max_files: n(s.max_files, DEFAULTS.max_files) }
}
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`
const nameOf = (c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>) => c.store.workspaces.find((w) => w.id === c.ws)?.members.find((m) => m.person === c.viewer)?.name ?? c.viewer
const find = (state: Record<string, unknown>, id: unknown) => list(state).find((q) => q.id === id)
const oneLine = (v: unknown): string | null => {
  if (typeof v !== 'string') return null
  const t = v.trim()
  return t && !/[\r\n]/.test(t) ? t : null
}
const limitText = (s: Settings, q: Quick) => `limit of ${plural(s.max_commits, 'commit', 'commits')} and ${plural(s.max_files + (q.extra_files ?? 0), 'file', 'files')}`
const outgrew = (state: Record<string, unknown>) => list(state).filter((q) => q.status === 'outgrew')
const decisionId = (q: Quick) => `dec_quick_${q.id}`

const STATUS_TONE = { open: 'idle', claimed: 'running', done: 'ok', outgrew: 'warn', converted: 'ok' } as const

function makeTicket(ctx: AddonCtx, q: Quick): AddonActionResult | StoreFailure {
  if (q.status !== 'open' && q.status !== 'outgrew') return conflict('quick.not_open', `${q.id} is ${q.status}; only an open or outgrown task becomes a ticket.`)
  const req: NewTicketRequest = {
    type: 'chore',
    title: q.title,
    priority: 'medium',
    size: null,
    labels: ['quick-task'],
    parent: null,
    due: null,
    visibility: 'workspace',
    people: { owner: null, assignees: [], reviewers: [] },
    sections: { requirements: q.title },
    acceptance: [],
  }
  const r = ctx.store.createFromRequest(ctx.ws, req)
  if (!r.ok) return r
  q.status = 'converted'
  q.ticket = r.ticket.key
  markDecided(ctx.state, decisionId(q))
  return { ok: true, message: `${q.id} is now ${r.ticket.key}, in the backlog.`, changed: true }
}

const QUICK_TITLES = [
  'Fix the typo in the loader log message', 'Bump the dbt-utils patch version', 'Delete the commented-out join in fct_usage', 'Rename the stale tariffs fixture', 'Add a newline at the end of the seeds',
  'Pin ruff in the pre-commit file', 'Remove the unused env var', 'Update the freshness warning text', 'Fix the broken link in the runbook', 'Sort the imports in billing_api',
  'Drop the old export script', 'Correct the unit in the usage column comment', 'Quote the table name in the vacuum job', 'Use UTC in the nightly log line', 'Delete the duplicate test case',
  'Add the missing index comment', 'Fix the README badge', 'Rename a variable that shadows a builtin', 'Remove the TODO that is done',
]

/** Busy day: 25 quick tasks in all: open, claimed, done, outgrew (so owners get decisions) and two made into tickets. */
function seedBusy(ws: string, store: MockStore, rng: Rng) {
  const base = { items: seed(), settings: { ...DEFAULTS }, decided: [] as string[], nav: {} }
  if (store.workspaces.find((w) => w.id === ws)?.prefix !== 'DEMO') return base
  const t = briefs(store, ws)
  const converted = [...t.filter((x) => x.restricted).slice(0, 1), ...t.filter((x) => !x.restricted).slice(0, 1)]
  const who = ['Severin', 'Mara', 'Claude Code']
  const plan: Status[] = ['outgrew', 'outgrew', 'outgrew', 'outgrew', 'converted', 'converted', 'done', 'done', 'done', 'done', 'claimed', 'claimed', 'claimed', 'claimed', 'open', 'open', 'open', 'open', 'open']
  QUICK_TITLES.forEach((title, i) => {
    const status = plan[i]
    base.items.push({
      id: `Q-${String(i + 7).padStart(3, '0')}`,
      title: i === 5 ? 'Replace the hard-coded warehouse name in the nightly job config, the dbt profile and the Dagster resource' : title,
      status,
      added_by: rng.pick(who),
      ...(status === 'claimed' || status === 'done' ? { claimed_by: rng.pick(['Claude Code', 'Codex']) } : {}),
      ...(status === 'done' ? { proof: `done, ${rng.int(1000000, 9999999).toString(16).slice(0, 7)}` } : {}),
      ...(status === 'outgrew' ? { commits: rng.int(2, 6), files: rng.int(4, 12) } : {}),
      ...(status === 'converted' ? { ticket: converted[i - 4]?.key } : {}),
    })
  })
  return base
}

registerAddon({
  name: 'quick',
  seed: () => ({ items: seed(), settings: { ...DEFAULTS }, decided: [], nav: {} }),
  seedBusy,

  view(state, c) {
    const s = settingsOf(state)
    /** A converted task shows its ticket key only to people who can see that ticket. */
    const items = list(state).map((q) => (q.ticket && !canSeeTicket(c, q.ticket) ? { ...q, ticket: undefined } : q))
    const row = (q: Quick) => {
      const actions =
        q.status === 'open'
          ? [
              // No Claim button: agents claim a task (the `claim` action stays for them); a person makes a ticket or waits.
              // An ordinary task is for an agent to pick up; making a ticket of it is the quiet way out.
              { label: 'Make a ticket', action: 'make_ticket', args: { id: q.id }, variant: 'ghost' as const },
            ]
          : q.status === 'claimed'
            ? [{ label: 'Close with proof', action: 'close', args: { id: q.id }, variant: 'secondary' as const, input: { name: 'proof', label: 'Proof (one line)', placeholder: 'e.g. removed, 4f2a91c', submitLabel: 'Close task', maxLength: 200 } }]
            : q.status === 'outgrew'
              ? [{ label: 'Make a ticket', action: 'make_ticket', args: { id: q.id }, variant: 'secondary' as const, primary: true }]
              : undefined
      const detail =
        q.status === 'outgrew'
          ? `${plural(q.commits ?? 0, 'commit', 'commits')}, ${plural(q.files ?? 0, 'file', 'files')}: over the ${limitText(s, q)}`
          : q.status === 'claimed'
            ? `claimed by ${q.claimed_by}`
            : q.status === 'done'
              ? `done by ${q.claimed_by ?? 'someone'}: ${q.proof}`
              : q.status === 'converted'
                ? q.ticket
                  ? `made into ${q.ticket}`
                  : 'made into a ticket'
                : `added by ${q.added_by}`
      return { title: `${q.id} ${q.title}`, subtitle: detail, badge: q.status, status: STATUS_TONE[q.status], actions }
    }
    const count = (st: Status) => items.filter((q) => q.status === st).length
    return {
      items,
      settings: s,
      list: items.map(row),
      // The tabs: Open = open, claimed and outgrew; Done = done and made into a ticket.
      // The oversized one first: it is the one that needs a person.
      openList: [...items.filter((q) => q.status === 'outgrew'), ...items.filter((q) => q.status === 'open' || q.status === 'claimed')].map(row),
      summaryLine: `${count('open')} waiting for an agent · ${count('claimed')} in progress · ${count('outgrew')} should become a ticket`,
      doneList: items.filter((q) => q.status === 'done' || q.status === 'converted').map(row),
      openTotal: items.filter((q) => q.status === 'open' || q.status === 'claimed' || q.status === 'outgrew').length,
      doneTotal: items.filter((q) => q.status === 'done' || q.status === 'converted').length,
      open: count('open'),
      claimed: count('claimed'),
      done: count('done'),
      outgrew: count('outgrew'),
      addUi: { 'ui:options': { layout: 'row' }, 'ui:globalOptions': { layout: 'row' }, title: { 'ui:placeholder': 'Fix the typo in README' } },
      addSchema: { type: 'object', required: ['title'], properties: { title: { type: 'string', title: 'Quick task (one line)', maxLength: MAX_TITLE } } },
    }
  },

  // One decision per outgrown task, for as long as it stays outgrown. Nothing is added to the package's decisions.
  decisions(state, pkg): AddonDecision[] {
    const done = (state.decided as string[] | undefined) ?? []
    const mine = pkg.filter((d) => !done.includes(d.id))
    const s = settingsOf(state)
    return [
      ...mine,
      ...outgrew(state)
        .filter((q) => !done.includes(decisionId(q)))
        .map(
          (q): AddonDecision => ({
            kind: 'decision',
            id: decisionId(q),
            addon: 'quick',
            title: `${q.id} outgrew its limit`,
            question: `${q.id} outgrew its limit: make it a ticket, or allow ${MORE_FILES} more files?`,
            detail: `${q.title}. ${plural(q.commits ?? 0, 'commit', 'commits')} and ${plural(q.files ?? 0, 'file', 'files')} against a ${limitText(s, q)}. The agent stopped.`,
            options: [
              { key: 'ticket', label: 'Make it a ticket', primary: true },
              { key: 'more', label: `Allow ${MORE_FILES} more files` },
            ],
            action: 'decide',
          }),
        ),
    ]
  },

  actions: {
    add(ctx) {
      const title = oneLine((ctx.body.formData as { title?: unknown } | undefined)?.title)
      if (!title) return invalid('Write the task as one line.')
      if (title.length > MAX_TITLE) return invalid(`Keep it under ${MAX_TITLE} characters, or make a ticket.`)
      const n = Math.max(0, ...list(ctx.state).map((q) => Number(q.id.slice(2)))) + 1
      const id = `Q-${String(n).padStart(3, '0')}`
      list(ctx.state).push({ id, title, status: 'open', added_by: nameOf(ctx) })
      return { ok: true, message: `Added ${id}.`, changed: true }
    },
    claim(ctx) {
      const q = find(ctx.state, ctx.body.id)
      if (!q) return notFound('No such quick task.')
      if (q.status !== 'open') return conflict('quick.not_open', `${q.id} is ${q.status}; only an open task can be claimed.`)
      q.status = 'claimed'
      q.claimed_by = nameOf(ctx)
      return { ok: true, message: `${q.id} claimed.`, changed: true }
    },
    close(ctx) {
      const q = find(ctx.state, ctx.body.id)
      if (!q) return notFound('No such quick task.')
      if (q.status !== 'claimed') return conflict('quick.not_claimed', 'Pick a claimed task to close first.')
      const proof = oneLine(ctx.body.proof)
      if (!proof) return invalid('Write one line of proof.')
      q.status = 'done'
      q.proof = proof.slice(0, 200)
      return { ok: true, message: `${q.id} closed.`, changed: true }
    },
    make_ticket(ctx) {
      const q = find(ctx.state, ctx.body.id)
      if (!q) return notFound('No such quick task.')
      return makeTicket(ctx, q)
    },
    // A decision action: core checked who decides, that it is open and that the option is one of its options.
    decide(ctx) {
      const id = ctx.decision?.id
      const q = id ? outgrew(ctx.state).find((x) => decisionId(x) === id) : undefined
      if (!id || !q) return conflict('decision.closed', 'That decision is closed.') // only when the manifest lacks `decision: true`
      if (ctx.body.option === 'ticket') return makeTicket(ctx, q)
      q.extra_files = (q.extra_files ?? 0) + MORE_FILES
      q.status = 'open'
      delete q.claimed_by
      markDecided(ctx.state, id)
      return { ok: true, message: `${q.id} may change ${MORE_FILES} more files. It is open again.`, changed: true }
    },
    save_settings: ({ state, body }) => {
      state.settings = body.formData ?? {}
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})
