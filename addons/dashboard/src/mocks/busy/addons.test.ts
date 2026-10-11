import { bestOfFive } from '@/test/bestOf'
import { describe, expect, it } from 'vitest'
import { addonActive } from '@/api/addons'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '../store'

// The addons of the busy day: counts per addon, installed catalog addons, and the visibility rules still hold.
const make = (dataset: 'normal' | 'busy' = 'busy', viewer = 'p_sev') => {
  const store = createMockStore({ persist: false, dataset })
  store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
const s = make()
type Rows = Record<string, unknown>[]
const raw = (name: string, x = s) => x.store.addonState(x.ws, name) as Record<string, unknown>

describe('busy day: addons are installed and filled', () => {
  it('installs the catalog addons in DEMO, active, with a grant', () => {
    const ws = s.store.workspaces.find((w) => w.id === s.ws)!
    for (const n of ['activity', 'records', 'worktrees', 'quick', 'models', 'schedules', 'factory']) expect(addonActive(ws, n), n).toBe(true)
    const normal = make('normal')
    expect(addonActive(normal.store.workspaces.find((w) => w.id === normal.ws)!, 'worktrees')).toBe(false)
  })

  it('publish: 12 apps and 40 shares', () => {
    expect((raw('publish').apps as Rows).length).toBe(12)
    expect((raw('publish').shares as Rows).length).toBe(40)
  })
  it('github: 30 pull requests and 25 issues', () => {
    expect((raw('github').prs as Rows).length).toBe(30)
    expect((raw('github').issues as Rows).length).toBe(25)
  })
  it('usage grows with the busy day: more cost, busy tickets in By ticket, more sessions (R-g)', async () => {
    type Usage = { weekCents: number; cost30Cents: number; ticketRows: { ticket: string }[]; agentRows: { sessions: number }[]; budgetChf: number; budgetPct: number }
    const busy = (await s.api.getAddonState(s.ws, 'usage')) as unknown as Usage
    const n = make('normal')
    const normal = (await n.api.getAddonState(n.ws, 'usage')) as unknown as Usage
    expect(busy.weekCents).toBeGreaterThan(normal.weekCents * 3)
    expect(busy.cost30Cents).toBeGreaterThan(normal.cost30Cents * 3)
    expect(busy.ticketRows.length).toBeGreaterThan(normal.ticketRows.length + 30)
    expect(busy.ticketRows.some((r) => Number(r.ticket.slice(5)) >= 100)).toBe(true)
    const sessions = (u: Usage) => u.agentRows.reduce((t, r) => t + r.sessions, 0)
    expect(sessions(busy)).toBeGreaterThan(sessions(normal) * 3)
    // A busy month, not an overrun: under the 80 % alert.
    expect(busy.budgetPct).toBeLessThan(80)
  })
  it('wiki: 30 pages with unique slugs and titles', () => {
    const pages = raw('wiki').pages as { slug: string; title: string }[]
    expect(pages.length).toBe(30)
    expect(new Set(pages.map((p) => p.slug)).size).toBe(30)
    expect(new Set(pages.map((p) => p.title)).size).toBe(30)
  })
  it('terminals: 7 sessions, 2 shells of people and 5 agent mirrors (one ended) with long transcripts', () => {
    const sessions = raw('terminals').sessions as { kind: string; transcript?: string[] }[]
    expect(sessions.length).toBe(7)
    expect(sessions.filter((x) => x.kind === 'person').length).toBe(2)
    const mirrors = sessions.filter((x) => x.kind === 'agent')
    expect(mirrors.length).toBe(5)
    expect(mirrors.filter((m) => (m.transcript?.length ?? 0) >= 150).length).toBeGreaterThanOrEqual(3)
  })
  it('worktrees: 20', () => expect((raw('worktrees').worktrees as Rows).length).toBe(20))
  it('quick tasks: 25, several outgrew', () => {
    const items = raw('quick').items as { status: string }[]
    expect(items.length).toBe(25)
    expect(items.filter((q) => q.status === 'outgrew').length).toBeGreaterThanOrEqual(4)
    for (const st of ['open', 'claimed', 'done', 'converted']) expect(items.some((q) => q.status === st), st).toBe(true)
  })
  it('records: 200 or more events not recorded yet', async () => {
    const view = (await s.api.getAddonState(s.ws, 'records')) as { rows?: { events: number }[]; pending?: { events: number }[] }
    const state = JSON.stringify(view)
    expect(state.length).toBeGreaterThan(1000)
    const total = (view.rows ?? view.pending ?? []).reduce((n, r) => n + r.events, 0)
    expect(total).toBeGreaterThanOrEqual(200)
  })
  it('schedules: 8', () => expect((raw('schedules').schedules as Rows).length).toBe(8))
  it('factory: an epic with 20 children and 5 open permits', async () => {
    expect(s.store.ticket('DEMO-0050')!.children).toHaveLength(20)
    expect((raw('factory').permits as { state: string }[]).filter((p) => p.state === 'open').length).toBe(5)
    const view = (await s.api.getAddonState(s.ws, 'factory')) as { used: number; mode: string }
    expect(view.used).toBe(23) // 20 children + the 3 a seeded full run reserved
    expect(view.mode).toBe('running')
  })
  it('activity is busy by itself: many events, many days', async () => {
    const view = JSON.stringify(await s.api.getAddonState(s.ws, 'activity'))
    expect(view.length).toBeGreaterThan(5000)
  })
  it('gives Severin addon decisions on top of the open items', async () => {
    const d = await s.api.getAddonDecisions(s.ws)
    expect(d.length).toBeGreaterThanOrEqual(8)
  })
})

describe('busy day: every addon answers for every viewer', () => {
  const ADDONS = ['publish', 'github', 'usage', 'wiki', 'estimate', 'terminals', 'worktrees', 'quick', 'records', 'activity', 'start-agent', 'models', 'factory', 'schedules', 'widgets', 'guide']
  for (const viewer of ['p_sev', 'p_mara', 'p_tom'])
    it(`${viewer}: all addon states load, and quickly`, async () => {
      const x = make('busy', viewer)
      for (const name of ADDONS) {
        const t0 = performance.now()
        const st = await x.api.getAddonState(x.ws, name)
        expect(st, name).toBeTruthy()
        expect(performance.now() - t0, name).toBeLessThan(250)
      }
    })
})

// The visibility sweep (ticket-visibility.test.ts) against the busy day: tickets a person cannot see appear nowhere.
describe('busy day: restricted tickets stay out of addon views', () => {
  const sweep = async (viewer: string) => {
    const owner = make('busy', 'p_sev')
    const x = make('busy', viewer)
    const hidden = owner.store.listTickets(owner.ws).filter((t) => !x.store.isVisible(t.key, viewer))
    expect(hidden.length).toBeGreaterThanOrEqual(4)
    const visible = x.store.listTickets(x.ws)
    const visibleTitles = new Set(visible.map((t) => t.title))
    const hiddenTitles = hidden.map((t) => t.title).filter((t) => !visibleTitles.has(t))
    expect(hiddenTitles.length).toBeGreaterThanOrEqual(3)
    for (const name of ['publish', 'github', 'usage', 'wiki', 'estimate', 'terminals', 'worktrees', 'quick', 'records', 'activity', 'start-agent', 'models', 'factory', 'schedules']) {
      const json = JSON.stringify(await x.api.getAddonState(x.ws, name))
      for (const t of hidden) expect(json, `${name} leaks ${t.key}`).not.toContain(t.key)
      for (const title of hiddenTitles) expect(json, `${name} leaks "${title}"`).not.toContain(title)
    }
    const decisions = JSON.stringify(await x.api.getAddonDecisions(x.ws))
    for (const t of hidden) expect(decisions).not.toContain(t.key)
  }
  it('control: Severin does see restricted tickets in publish, github, worktrees and quick (so the sweep tests something)', async () => {
    const restricted = s.store.listTickets(s.ws).filter((t) => t.restricted).map((t) => t.key)
    for (const name of ['publish', 'github', 'worktrees', 'quick', 'wiki']) {
      const json = JSON.stringify(await s.api.getAddonState(s.ws, name))
      expect(restricted.some((k) => json.includes(k)), name).toBe(true)
    }
  })
  it('Mara (outside the Severin-only tickets)', () => sweep('p_mara'))
  it('Tom (outside every restricted ticket)', () => sweep('p_tom'))
})

describe('busy day: speed', () => {
  it('serves list, today and addon decisions in under 50 ms each with every addon on', async () => {
    const x = make()
    for (const f of [() => x.api.listTickets(x.ws), () => x.api.getToday(x.ws), () => x.api.getAddonDecisions(x.ws)]) {
      expect(await bestOfFive(f)).toBeLessThan(50)
    }
  })
})
