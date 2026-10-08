import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { sha256Hex } from '@/api/sha256'
import { findTemplate, templateDigest } from '@/api/widgetTemplates'
import { resolveTicketWidgets, type Block } from '@/app/pages/ticket/widgets/parse'
import type { TicketDocument } from '@/api/types'
import { createMockStore } from '../store'
import { generateBusy } from './generate'

// Agents, artifacts and widgets of the busy day.
const busy = () => {
  const store = createMockStore({ persist: false, dataset: 'busy' })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  const api = createApi(createMockTransport(store, { latency: false }))
  return { store, ws, api }
}
const s = busy()
const docs = () => s.store.listTickets(s.ws) as TicketDocument[]
/** Generated tickets only (the fixtures have a few widgets of their own, some on purpose broken). */
const generated = () => docs().filter((t) => Number(t.key.slice(5)) >= 100)

describe('agents', () => {
  it('has 12 or more sessions working at once, some with 2 to 3 subagents', async () => {
    const agents = await s.api.getAgents(s.ws)
    const roots = agents.filter((a) => !a.parent && a.state !== 'stopped')
    expect(roots.length).toBeGreaterThanOrEqual(12)
    const subs = new Map<string, number>()
    for (const a of agents) if (a.parent && a.state !== 'stopped') subs.set(a.parent, (subs.get(a.parent) ?? 0) + 1)
    expect([...subs.values()].filter((n) => n >= 2 && n <= 3).length).toBeGreaterThanOrEqual(3)
    expect(agents.some((a) => a.state === 'waiting' && a.waiting_on)).toBe(true)
    expect(agents.some((a) => a.state === 'stopped')).toBe(true)
  })

  it('works under both people\'s grants and holds 20 or more active leases', async () => {
    const agents = await s.api.getAgents(s.ws)
    expect(new Set(agents.filter((a) => a.state !== 'stopped').map((a) => a.for))).toEqual(new Set(['p_sev', 'p_mara']))
    expect(agents.reduce((n, a) => n + a.leases.length, 0)).toBeGreaterThanOrEqual(20)
    for (const a of agents) expect(a.grant, a.session).not.toBeNull()
    expect(agents.every((a) => a.grant!.id.startsWith('gr_'))).toBe(true)
  })

  it('lists every session on the grant of the person it works for', async () => {
    const grants = await s.api.listGrants(s.ws)
    const agents = await s.api.getAgents(s.ws)
    for (const a of agents) expect(grants.find((g) => g.id === a.grant!.id)!.sessions, a.session).toContain(a.session)
  })

  it('shows several refusals, including the stop rule for s_b105 (on top of the fixture\'s s_9e3f)', async () => {
    const feed = await s.api.getAgentActivity(s.ws)
    const refused = feed.filter((i) => i.type === 'refused')
    expect(refused.length).toBeGreaterThanOrEqual(8)
    expect(refused.filter((i) => i.refusal?.stop).map((i) => i.session).sort()).toEqual(['s_9e3f', 's_b105'])
  })

  it('points a waiting agent at a question that is really open', async () => {
    const agents = await s.api.getAgents(s.ws)
    for (const a of agents.filter((x) => x.waiting_on)) {
      const t = s.store.ticket(a.waiting_on!.ticket)!
      expect(t.questions_state.find((q) => q.id === a.waiting_on!.ref)?.state, a.session).toBe('open')
    }
  })
})

const artifactEvents = () => generateBusy().tickets.DEMO.map((t) => ({ key: t.definition.key, arts: t.events.filter((e) => e.type === 'artifact.added') }))

describe('artifacts', () => {
  it('covers every kind and about 40 tickets, two of them with 15 or more', () => {
    const per = artifactEvents()
    const kinds = new Set(per.flatMap((p) => p.arts.map((a) => a.kind)))
    for (const k of ['screenshot', 'log', 'report', 'link', 'dataset', 'build', 'diagram', 'receipt', 'feedback']) expect(kinds.has(k), k).toBe(true)
    const withArts = per.filter((p) => p.arts.length)
    expect(withArts.length).toBeGreaterThanOrEqual(35)
    expect(withArts.length).toBeLessThanOrEqual(50)
    expect(per.filter((p) => p.arts.length >= 15).length).toBeGreaterThanOrEqual(2)
  })

  it('has a log of about 2,000 lines and a dataset of about 500 rows', () => {
    const all = artifactEvents().flatMap((p) => p.arts)
    const log = all.find((a) => a.kind === 'log' && String(a.preview).split('\n').length > 1500)!
    expect(String(log.preview).split('\n').length).toBeGreaterThan(1900)
    expect(String(log.preview).split('\n').length).toBeLessThan(2100)
    const csv = all.find((a) => a.kind === 'dataset' && String(a.preview).split('\n').length > 400)!
    expect(String(csv.preview).split('\n').length).toBeGreaterThan(480)
    expect(String(csv.preview).split('\n').length).toBeLessThan(520)
  })

  it('names artifacts uniquely within a ticket, and link artifacts carry an https url', () => {
    for (const p of artifactEvents()) expect(new Set(p.arts.map((a) => a.name)).size, p.key).toBe(p.arts.length)
    for (const a of artifactEvents().flatMap((p) => p.arts).filter((a) => a.kind === 'link')) expect(String(a.url)).toMatch(/^https:\/\//)
  })
})

describe('widgets', () => {
  const blocksOf = (t: TicketDocument) => {
    const seg = resolveTicketWidgets(t.body)
    return Object.values(seg).flatMap((list) => list.flatMap((x) => (x.kind === 'widget' ? [x.block] : [])))
  }
  const heavy = () => generated().map((t) => ({ t, blocks: blocksOf(t) })).filter((x) => x.blocks.length >= 4)

  it('puts 4 to 10 widgets on at least 6 tickets', () => {
    const h = heavy()
    expect(h.length).toBeGreaterThanOrEqual(6)
    for (const { t, blocks } of h) expect(blocks.length, t.key).toBeLessThanOrEqual(10)
  })

  it('draws every kind of block across them: bars, table, checks, kv, templates and html pages', () => {
    const all = heavy().flatMap((x) => x.blocks).filter((b) => b.spec)
    const types = new Set(all.map((b) => b.spec!.type ?? b.spec!.widget?.split('@')[0] ?? 'html'))
    for (const k of ['bars', 'table', 'checks', 'kv', 'before-after', 'line-chart', 'option-prototype', 'html']) expect(types.has(k), k).toBe(true)
    const bars = all.filter((b) => b.spec!.type === 'bars').map((b) => Object.keys((b.spec!.fields.data as object) ?? {}).length)
    expect(Math.max(...bars)).toBeGreaterThanOrEqual(30)
    expect(Math.max(...all.filter((b) => b.spec!.type === 'table').map((b) => (b.spec!.fields.rows as unknown[]).length))).toBeGreaterThanOrEqual(60)
    expect(Math.max(...all.filter((b) => b.spec!.type === 'checks').map((b) => (b.spec!.fields.rows as unknown[]).length))).toBeGreaterThanOrEqual(30)
  })

  it('pins templates to their digest and html pages to the sha256 of the artifact they name', () => {
    let good = 0
    let drifted = 0
    for (const { t, blocks } of heavy()) {
      for (const b of blocks.filter((x: Block) => x.spec && x.spec.layer !== 'type')) {
        const spec = b.spec!
        if (spec.layer === 'widget') {
          expect(spec.sha256, `${t.key} ${spec.widget}`).toBe(templateDigest(findTemplate(spec.widget!)!))
          good++
        } else {
          const art = t.artifacts.find((a) => a.name === spec.artifact)!
          expect(art, `${t.key} ${spec.artifact}`).toBeTruthy()
          if (sha256Hex(art.preview!) === spec.sha256) good++
          else drifted++
        }
      }
    }
    expect(good).toBeGreaterThanOrEqual(10)
    expect(drifted).toBe(1) // exactly one page is on purpose out of date
  })

  it('has a few blocks the page must refuse, each with a reason', () => {
    const refused = heavy().flatMap((x) => x.blocks).filter((b) => b.reason)
    expect(refused.length).toBeGreaterThanOrEqual(3)
    const reasons = refused.map((b) => b.reason!).join(' | ')
    expect(reasons).toMatch(/negative/)
    expect(reasons).toMatch(/unknown widget type|is not drawn/)
  })

  it('keeps every ticket under the 40-block cap', () => {
    for (const t of docs()) expect(blocksOf(t).length, t.key).toBeLessThanOrEqual(40)
  })
})

describe('speed with the biggest ticket', () => {
  it('opens the ticket with the 2,000-line log in under 50 ms', async () => {
    const t = docs().find((d) => d.artifacts.some((a) => a.kind === 'log' && (a.preview ?? '').length > 100_000))!
    await s.api.getTicket(t.key)
    const t0 = performance.now()
    await s.api.getTicket(t.key)
    expect(performance.now() - t0).toBeLessThan(50)
  })
})
