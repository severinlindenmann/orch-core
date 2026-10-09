import { addonActive } from '@/api/addons'
import { briefs, scaled, scaleOf } from '../busy/helpers'
import type { Rng } from '../busy/rng'
import type { MockStore } from '../store'
import { canSeeTicket, conflict, invalid, notFound, registerAddon, type AddonCtx } from './registry'

// worktrees: one git worktree per ticket and repo (the git commands run in the host; here it is plain state).
//  - Path `wt/<ticket>-<repo>` (repo = the name after the owner), branch `feat/<ticket-slug>`; the id is the path.
//  - The page shows one table per repo; its rowActions come from view() so "Open terminal here" is bound to terminals being active.
//  - Seeded for DEMO tickets only; other workspaces start empty.
//  - "Open terminal here" runs the terminals addon's own `open_ticket` action through the registry (no access to its
//    state internals) and is offered only while terminals is active (view() exposes `terminalsActive`).
//  - Remove is refused while the worktree has changed files (the row shows why, in place); a clean one is confirmed first (confirm: 'destructive').

interface Worktree {
  id: string
  path: string
  repo: string
  ticket: string
  branch: string
  base: string
  dirty: number
  ahead: number
  behind: number
  created_by: string
}

const REPOS = ['acme-energy/energy-dbt', 'acme-energy/billing-api', 'acme-energy/ingest']
const short = (repo: string) => repo.split('/').pop() ?? repo

/** Lowercase, dashes, at most 40 characters, cut at a word boundary. */
const slugOf = (title: string) => {
  const s = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  if (s.length <= 40) return s
  const cut = s.slice(0, 41)
  return (cut[40] === '-' ? cut.slice(0, 40) : cut.slice(0, cut.lastIndexOf('-'))).replace(/-+$/, '')
}

const wt = (ticket: string, repo: string, title: string, d: Partial<Worktree>): Worktree => ({
  id: `wt/${ticket}-${short(repo)}`,
  path: `wt/${ticket}-${short(repo)}`,
  repo,
  ticket,
  branch: `feat/${slugOf(title)}`,
  base: 'main',
  dirty: 0,
  ahead: 0,
  behind: 0,
  created_by: 'Claude Code',
  ...d,
})
const seedDemo = (): Worktree[] => [
  wt('DEMO-0043', REPOS[0], 'Load tariff tables as dbt seeds', { dirty: 3, ahead: 2 }),
  wt('DEMO-0041', REPOS[0], 'Add billing reconciliation tests', { ahead: 1, behind: 3, created_by: 'Mara' }),
  wt('DEMO-0046', REPOS[1], 'Fix duplicate meter ids in dim_meter', { created_by: 'Severin' }),
  wt('DEMO-0037', REPOS[2], 'Add freshness checks to sources', { ahead: 4, behind: 1 }),
]

const list = (state: Record<string, unknown>) => state.worktrees as Worktree[]
const plural = (n: number) => `${n} changed file${n === 1 ? '' : 's'}`
/** A worktree is shown and acted on only when its ticket is in this workspace and visible to the caller. */
/** A worktree is shown and acted on only when its ticket is one the viewer can see in this workspace (core's rule). */
const canSee = (c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>, w: Worktree) => canSeeTicket(c, w.ticket)
const nameOf = (c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>) => c.store.workspaces.find((w) => w.id === c.ws)?.members.find((m) => m.person === c.viewer)?.name ?? c.viewer

/** Busy day: 20 worktrees in DEMO (16 more, from tickets in progress, in testing or just finished; a third of that elsewhere). */
function seedBusy(ws: string, store: MockStore, rng: Rng) {
  const state = { worktrees: store.workspaces.find((w) => w.id === ws)?.prefix === 'DEMO' ? seedDemo() : [] }
  const have = new Set(state.worktrees.map((w) => w.id))
  const pool = briefs(store, ws).filter((t) => ['in-progress', 'testing', 'waiting', 'open'].includes(t.status) || t.restricted)
  for (const t of rng.shuffle(pool).slice(0, scaled(16, scaleOf(store, ws)) + 4)) {
    const repo = REPOS.find((r) => r.endsWith(t.repo.replace('acme-energy-', ''))) ?? REPOS[0]
    const w = wt(t.key, repo, t.title, { dirty: rng.chance(0.5) ? rng.int(1, 12) : 0, ahead: rng.int(0, 6), behind: rng.int(0, 5), created_by: rng.pick(['Claude Code', 'Codex', 'Mara', 'Severin']) })
    if (have.has(w.id)) continue
    have.add(w.id)
    state.worktrees.push(w)
  }
  if (store.workspaces.find((w) => w.id === ws)?.prefix === 'DEMO') state.worktrees.length = Math.min(state.worktrees.length, 20)
  return state
}

registerAddon({
  name: 'worktrees',
  seed: (ws, store) => ({ worktrees: store.workspaces.find((w) => w.id === ws)?.prefix === 'DEMO' ? seedDemo() : [] }),
  seedBusy,
  view(state, c) {
    const terminalsActive = addonActive(c.store.workspaces.find((w) => w.id === c.ws), 'terminals')
    const item = (w: Worktree) => ({
      id: w.id,
      title: w.path,
      subtitle: `${w.branch} from ${w.base} · ${w.ticket} · ${plural(w.dirty)} · ahead ${w.ahead}, behind ${w.behind} · by ${w.created_by}`,
      badge: w.dirty ? 'dirty' : 'clean',
      status: w.dirty ? ('warn' as const) : ('ok' as const),
      actions: [
        ...(terminalsActive ? [{ label: 'Open terminal here', action: 'open_terminal', args: { id: w.id }, variant: 'secondary' as const }] : []),
        { label: 'Remove', action: 'remove', args: { id: w.id }, variant: 'danger' as const },
      ],
    })
    const all = list(state).filter((w) => canSee(c, w))
    const row = (w: Worktree) => ({ id: w.id, path: w.path, branch: w.branch, ticket: w.ticket, changes: plural(w.dirty), sync: `ahead ${w.ahead}, behind ${w.behind}`, by: w.created_by })
    // The table's rowActions: "Open terminal here" is included only while terminals is active.
    const rowActions = [
      ...(terminalsActive ? [{ label: 'Open terminal here', action: 'open_terminal', args: { id: '$row.id' }, variant: 'secondary' as const }] : []),
      { label: 'Remove', action: 'remove', args: { id: '$row.id' }, variant: 'danger' as const },
    ]
    const rowsByRepo: Record<string, ReturnType<typeof row>[]> = {}
    const byTicket: Record<string, ReturnType<typeof item>[]> = {}
    for (const r of REPOS) rowsByRepo[short(r)] = all.filter((w) => w.repo === r).map(row)
    for (const w of all) (byTicket[w.ticket] ??= []).push(item(w))
    const open = c.store.listTickets(c.ws).filter((t) => t.status !== 'done')
    return {
      worktrees: all, // overrides the raw list: only what this viewer may see
      terminalsActive,
      rowsByRepo,
      rowActions,
      byTicket,
      total: all.length,
      dirty: all.filter((w) => w.dirty).length,
      // Labels for the Ticket select, in the order of its enum (the node's uiSchema binds them): "ID · title".
      ticketNames: open.map((t) => `${t.key} · ${t.title}`),
      addSchema: {
        type: 'object',
        required: ['ticket', 'repo'],
        properties: {
          // "ID · title" so the person picks by name, not by number.
          ticket: { type: 'string', title: 'Ticket', enum: open.map((t) => t.key) },
          repo: { type: 'string', title: 'Repository', enum: REPOS },
          base: { type: 'string', title: 'Base branch', default: 'main' },
        },
      },
    }
  },
  actions: {
    add(ctx) {
      const { store, state } = ctx
      const f = (ctx.body.formData ?? {}) as { ticket?: unknown; repo?: unknown; base?: unknown }
      const ticket = ctx.ticket ?? (typeof f.ticket === 'string' ? f.ticket : undefined)
      const repo = typeof f.repo === 'string' ? f.repo : ''
      const base = typeof f.base === 'string' && f.base.trim() ? f.base.trim() : 'main'
      if (!ticket || !repo) return invalid('Pick a ticket and a repository.')
      if (!REPOS.includes(repo)) return invalid(`${repo} is not a repository of this workspace.`)
      if (!/^[A-Za-z0-9._/-]{1,60}$/.test(base)) return invalid('That is not a valid branch name.')
      const doc = canSeeTicket(ctx, ticket) ? store.ticket(ticket) : undefined
      if (!doc) return notFound(`No ticket ${ticket}`)
      const path = `wt/${ticket}-${short(repo)}`
      if (list(state).some((w) => w.id === path)) return conflict('worktrees.exists', `${ticket} already has a worktree in ${short(repo)}.`)
      const branch = `feat/${slugOf(doc.title)}`
      list(state).push({ id: path, path, repo, ticket, branch, base, dirty: 0, ahead: 0, behind: 0, created_by: nameOf(ctx) })
      return { ok: true, message: `Created ${path} on ${branch}.`, changed: true }
    },
    remove(ctx) {
      const { state, body } = ctx
      const w = list(state).find((x) => x.id === body.id)
      if (!w || !canSee(ctx, w)) return notFound('No such worktree.')
      if (w.dirty > 0) return conflict('worktrees.dirty', `Worktree ${w.ticket} has ${plural(w.dirty)}.`, 'Commit or stash them first.')
      state.worktrees = list(state).filter((x) => x !== w)
      return { ok: true, message: `Removed ${w.path}.`, changed: true }
    },
    open_terminal(ctx) {
      const { store, ws, state, body } = ctx
      const w = list(state).find((x) => x.id === body.id)
      if (!w || !canSee(ctx, w)) return notFound('No such worktree.')
      // Same authorized path as a direct call: terminals' activity, grant, minRole and visibility checks all apply.
      const r = store.runAddon(ws, 'terminals', 'open_ticket', { ticket: w.ticket })
      if (!r) return conflict('addon.inactive', 'Terminals is not available.')
      if (!r.ok) return r.code === 'addon.inactive' ? conflict('addon.inactive', 'Terminals is not active in this workspace.', 'Turn it on in Settings > Addons.') : r
      return { ...r, message: `${r.message} Find it under Terminals.` }
    },
  },
})
