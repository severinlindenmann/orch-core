import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import type { TicketSummary } from '@/api/types'
import { fnvHex } from '../derive'
import { createMockStore, MOCK_EPOCH } from '../store'
import { BUSY_SEED, generateBusy } from './generate'

// The busy-day dataset: deterministic, fixture-shaped and big enough to stress every page.
const NOW = Date.parse(MOCK_EPOCH)
const DAY = 86_400_000
const hash = (v: unknown) => fnvHex(JSON.stringify(v), 16)

const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false, dataset: 'busy' })
  store.setViewer(viewer)
  const api = createApi(createMockTransport(store, { latency: false }))
  const id = (prefix: string) => store.workspaces.find((w) => w.prefix === prefix)!.id
  return { store, api, id }
}

afterEach(() => vi.restoreAllMocks())

describe('generateBusy: a pure, deterministic generator', () => {
  it('the same seed gives the same JSON, another seed gives other data', () => {
    expect(hash(generateBusy(BUSY_SEED))).toBe(hash(generateBusy(BUSY_SEED)))
    expect(hash(generateBusy(BUSY_SEED + 1))).not.toBe(hash(generateBusy(BUSY_SEED)))
  })

  it('uses neither Math.random nor the real clock', () => {
    const random = vi.spyOn(Math, 'random')
    const now = vi.spyOn(Date, 'now')
    generateBusy(BUSY_SEED)
    expect(random).not.toHaveBeenCalled()
    expect(now).not.toHaveBeenCalled()
  })

  it('takes under 500 ms', () => {
    const t0 = performance.now()
    generateBusy(BUSY_SEED)
    expect(performance.now() - t0).toBeLessThan(500)
  })

  it('gives every ticket a unique key and uid, in fixture shape', () => {
    const d = generateBusy(BUSY_SEED)
    const all = [...d.tickets.DEMO, ...d.tickets.INT, ...d.tickets.CLI]
    expect(new Set(all.map((t) => t.definition.key)).size).toBe(all.length)
    expect(new Set(all.map((t) => t.definition.uid)).size).toBe(all.length)
    for (const t of all) {
      expect(t.definition.uid).toHaveLength(26)
      expect(Array.isArray(t.events)).toBe(true)
    }
  })

  it('writes every event inside the last 21 days and never in the future', () => {
    const d = generateBusy(BUSY_SEED)
    for (const t of [...d.tickets.DEMO, ...d.tickets.INT, ...d.tickets.CLI])
      for (const e of t.events) {
        const at = Date.parse(e.at)
        expect(at).toBeLessThan(NOW)
        expect(at).toBeGreaterThan(NOW - 22 * DAY)
      }
  })
})

describe('busy store: counts and mix', () => {
  const s = setup()
  const demo = () => s.store.listTickets(s.id('DEMO'))

  it('has about 150 / 60 / 30 tickets per workspace', () => {
    expect(demo().length).toBeGreaterThanOrEqual(140)
    expect(demo().length).toBeLessThanOrEqual(165)
    const int = s.store.listTickets(s.id('INT')).length
    const cli = s.store.listTickets(s.id('CLI')).length
    expect(int).toBeGreaterThanOrEqual(55)
    expect(int).toBeLessThanOrEqual(70)
    expect(cli).toBeGreaterThanOrEqual(28)
    expect(cli).toBeLessThanOrEqual(36)
  })

  it('covers every type, priority, size and status', () => {
    const t = demo()
    for (const type of ['feature', 'bug', 'chore', 'spike', 'epic']) expect(t.some((x) => x.type === type), type).toBe(true)
    for (const p of ['low', 'medium', 'high', 'urgent']) expect(t.some((x) => x.priority === p), p).toBe(true)
    for (const z of ['xs', 's', 'm', 'l', 'xl', null]) expect(t.some((x) => x.size === z), String(z)).toBe(true)
    for (const st of ['backlog', 'open', 'in-progress', 'waiting', 'testing', 'done']) expect(t.some((x) => x.status === st), st).toBe(true)
  })

  it('has about 15 tickets in testing awaiting a verdict and about 20 waiting on a question', () => {
    const t = demo()
    const awaiting = t.filter((x) => x.status === 'testing' && !x.verdict).length
    const asking = t.filter((x) => x.questions_state.some((q) => q.state === 'open')).length
    expect(awaiting).toBeGreaterThanOrEqual(13)
    expect(awaiting).toBeLessThanOrEqual(19)
    expect(asking).toBeGreaterThanOrEqual(16)
    expect(asking).toBeLessThanOrEqual(26)
  })

  it('uses 10 to 15 label names, some of them long', () => {
    const generated = new Set(generateBusy(BUSY_SEED).tickets.DEMO.flatMap((t) => t.definition.labels ?? []))
    expect(generated.size).toBeGreaterThanOrEqual(10)
    expect(generated.size).toBeLessThanOrEqual(15)
    expect([...generated].filter((l) => l.length >= 30).length).toBeGreaterThanOrEqual(2)
  })

  it('has 3 to 4 epics of 8 to 20 children, one big epic of 40, and the factory epic has 20', () => {
    const t = demo()
    const epics = t.filter((x) => x.type === 'epic' && x.key !== 'DEMO-0040' && x.key !== 'DEMO-0050')
    expect(epics.length).toBeGreaterThanOrEqual(3)
    expect(epics.length).toBeLessThanOrEqual(4)
    for (const e of epics) {
      const n = s.store.ticket(e.key)!.children!.length
      expect(n, e.key).toBeGreaterThanOrEqual(8)
      expect(n, e.key).toBeLessThanOrEqual(e.key === 'DEMO-0100' ? 40 : 20)
    }
    // The big one shadows the board unless it is grouped (N2): 40 children.
    expect(s.store.ticket('DEMO-0100')!.children).toHaveLength(40)
    expect(s.store.ticket('DEMO-0050')!.children).toHaveLength(20)
  })

  it('gives about a tenth of the tickets a very long title (90 to 120 characters)', () => {
    const t = generateBusy(BUSY_SEED).tickets.DEMO
    const long = t.filter((x) => x.definition.title.length >= 90)
    expect(long.length / t.length).toBeGreaterThan(0.07)
    expect(long.length / t.length).toBeLessThan(0.16)
    for (const x of long) expect(x.definition.title.length).toBeLessThanOrEqual(120)
  })

  it('has restricted tickets that an outside member does not see', () => {
    const restricted = demo().filter((x) => x.restricted)
    expect(restricted.length).toBeGreaterThanOrEqual(6)
    s.store.setViewer('p_tom')
    const seen = new Set(s.store.listTickets(s.id('DEMO')).map((x) => x.key))
    s.store.setViewer('p_sev')
    for (const r of restricted.filter((x) => (x.visibility as { restricted: string[] }).restricted.length)) {
      if (!(r.visibility as { restricted: string[] }).restricted.includes('p_tom')) expect(seen.has(r.key)).toBe(false)
    }
  })
})

describe('busy store: histories', () => {
  it('gives each generated ticket 5 to 40 events spread over 21 days, denser today', () => {
    const d = generateBusy(BUSY_SEED)
    const all = [...d.tickets.DEMO, ...d.tickets.INT, ...d.tickets.CLI]
    // Artifacts and refusals are added on top of the history (a ticket may carry 15 or more artifacts).
    for (const t of all) {
      const n = t.events.filter((e) => e.type !== 'artifact.added' && e.type !== 'agent.refused').length
      expect(n, t.definition.key).toBeGreaterThanOrEqual(5)
      expect(n, t.definition.key).toBeLessThanOrEqual(40)
    }
    const ats = all.flatMap((t) => t.events.map((e) => Date.parse(e.at)))
    const oldest = Math.min(...ats)
    expect(NOW - oldest).toBeGreaterThan(18 * DAY)
    const perDay = new Map<number, number>()
    for (const a of ats) perDay.set(Math.floor((NOW - a) / DAY), (perDay.get(Math.floor((NOW - a) / DAY)) ?? 0) + 1)
    const today = [...perDay].filter(([k]) => k === 0).reduce((n, [, v]) => n + v, 0)
    const avgOther = [...perDay].filter(([k]) => k !== 0).reduce((n, [, v]) => n + v, 0) / 20
    expect(today).toBeGreaterThan(avgOther * 1.5)
  })

  it('is plausible: created first, tasks with receipts, questions answered or asked, approvals', () => {
    const d = generateBusy(BUSY_SEED)
    const types = new Set(d.tickets.DEMO.flatMap((t) => t.events.map((e) => e.type)))
    for (const ty of ['ticket.created', 'people.set', 'section.edited', 'claim.taken', 'lease.taken', 'task.done', 'task.run', 'question.asked', 'question.answered', 'gate.approved', 'status.changed', 'verdict.given', 'log.added', 'labels.changed'])
      expect(types.has(ty), ty).toBe(true)
    for (const t of d.tickets.DEMO) expect(t.events[0].type, t.definition.key).toBe('ticket.created')
  })
})

describe('busy store: Today under pressure', () => {
  it('Severin has at least 20 open items and Mara about 10', () => {
    const sev = setup('p_sev')
    const today = sev.store.today(sev.id('DEMO'))
    expect(today.needs_you.length).toBeGreaterThanOrEqual(20)
    expect(today.needs_you.length).toBeLessThanOrEqual(45)
    const kinds = new Set(today.needs_you.map((i) => i.kind))
    for (const k of ['question', 'approval', 'verdict']) expect(kinds.has(k as 'question'), k).toBe(true)
    const mara = setup('p_mara')
    const n = mara.store.today(mara.id('DEMO')).needs_you.length
    expect(n).toBeGreaterThanOrEqual(7)
    expect(n).toBeLessThanOrEqual(18)
  })

  it('holds claims and leases of the agents that are working', () => {
    const { store, id } = setup()
    const docs = store.listTickets(id('DEMO'))
    expect(docs.filter((t) => t.claim).length).toBeGreaterThanOrEqual(12)
    const leases = docs.flatMap((t) => t.tasks_state.filter((x) => x.lease))
    expect(leases.length).toBeGreaterThanOrEqual(20)
    for (const t of docs) if (t.claim) expect(t.claim.expires > store.now()).toBe(true)
  })
})

describe('busy store: speed', () => {
  it('answers list, today and ticket queries in under 50 ms each', async () => {
    const { api, id } = setup()
    const ws = id('DEMO')
    const timed = async (f: () => Promise<unknown>) => {
      const t0 = performance.now()
      await f()
      return performance.now() - t0
    }
    await api.listTickets(ws) // warm up
    const list = await timed(() => api.listTickets(ws))
    const today = await timed(() => api.getToday(ws))
    const rows = (await api.listTickets(ws)) as TicketSummary[]
    const ticket = await timed(() => api.getTicket(rows[0].key))
    const agents = await timed(() => api.getAgents(ws))
    expect(list).toBeLessThan(50)
    expect(today).toBeLessThan(50)
    expect(ticket).toBeLessThan(50)
    expect(agents).toBeLessThan(50)
  })

  it('seeds the whole busy store in well under a second', () => {
    const t0 = performance.now()
    createMockStore({ persist: false, dataset: 'busy' })
    expect(performance.now() - t0).toBeLessThan(1000)
  })
})
