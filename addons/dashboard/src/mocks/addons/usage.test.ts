import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { FORMATTERS } from '@/addon-ui/bindings'

const setup = () => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
type Day = { date: string; cents: Record<string, number>; tokens: Record<string, number> }
type Row = { key: string; cents: number; tokens: number; sessions: number; ms: number }
type Agent = { id: string; cents: number; tokens: number }
type State = {
  days: Day[]
  tickets: Row[]
  agents: Agent[]
  settings: { budget_chf: number }
  weekCents: number
  monthCents: number
  budgetPct: number
  byTicket: Record<string, { cents: number; tokens: number; sessions: number; ms: number }>
  budgetNotice: { type: string; tone?: string }
  topTickets: { ticket: string; cost: string }[]
  perDay: { day: string; chf: number }[]
  perModel: { model: string; chf: number }[]
}
const state = async (s: ReturnType<typeof setup>) => (await s.api.getAddonState(s.ws, 'usage')) as unknown as State
const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0)
const dayCents = (d: Day) => sum(Object.values(d.cents))
const dayTokens = (d: Day) => sum(Object.values(d.tokens))

describe('usage seed', () => {
  it('has 30 days ending 2026-10-09 for the four models', async () => {
    const st = await state(setup())
    expect(st.days).toHaveLength(30)
    expect(st.days[0].date).toBe('2026-09-10')
    expect(st.days[29].date).toBe('2026-10-09')
    expect(Object.keys(st.days[0].cents).sort()).toEqual(['claude-haiku-4-5', 'claude-opus-5-5', 'claude-sonnet-5-5', 'gpt-5-codex'])
    expect(st.settings.budget_chf).toBe(150)
  })
  it('is deterministic across stores', async () => {
    const a = await state(setup())
    const b = await state(setup())
    expect(a.days).toEqual(b.days)
    expect(a.tickets).toEqual(b.tickets)
  })
  it('the last 7 days are exactly CHF 31.40, and month to date is Oct 1 to 9', async () => {
    const st = await state(setup())
    expect(sum(st.days.slice(-7).map(dayCents))).toBe(3140)
    expect(st.weekCents).toBe(3140)
    expect(st.monthCents).toBe(sum(st.days.filter((d) => d.date >= '2026-10-01').map(dayCents)))
  })
  it('model, ticket and agent totals agree', async () => {
    const st = await state(setup())
    const cents = sum(st.days.map(dayCents))
    const tokens = sum(st.days.map(dayTokens))
    expect(sum(st.tickets.map((t) => t.cents))).toBe(cents)
    expect(sum(st.agents.map((t) => t.cents))).toBe(cents)
    expect(sum(st.tickets.map((t) => t.tokens))).toBe(tokens)
    expect(sum(st.agents.map((t) => t.tokens))).toBe(tokens)
    expect(sum(st.perModel.map((m) => m.chf))).toBeCloseTo(cents / 100, 2)
  })
  it('exposes chart points, top tickets and a per-ticket map', async () => {
    const st = await state(setup())
    expect(st.perDay).toHaveLength(30)
    expect(st.perModel.map((m) => m.model)).toEqual(['claude-opus-5-5', 'claude-sonnet-5-5', 'gpt-5-codex', 'claude-haiku-4-5']) // biggest first
    expect(st.perModel.map((m) => m.chf)).toEqual([...st.perModel.map((m) => m.chf)].sort((a, b) => b - a))
    expect(st.topTickets.length).toBeGreaterThanOrEqual(3)
    expect(st.topTickets[0].cost).toMatch(/^CHF \d+\.\d\d$/)
    expect(st.byTicket['DEMO-0043']).toMatchObject({ sessions: 3 })
    expect(st.byTicket['DEMO-0043'].cents).toBeGreaterThan(0)
  })
})

describe('usage by model', () => {
  type Totals = { model: string; sessions: number; tokensIn: number; tokensOut: number; cacheRead: number; cents: number; shareTenths: number }
  type Full = State & { modelTotals: Totals[]; modelRows: Record<string, string | number>[]; cost30Cents: number; tokens30: number; ticketRows: unknown[]; agentRows: unknown[] }
  const full = async () => {
    const s = setup()
    return (await s.api.getAddonState(s.ws, 'usage')) as unknown as Full
  }
  it('names the current models', async () => {
    const st = await full()
    expect(st.modelTotals.map((m) => m.model).sort()).toEqual(['claude-haiku-4-5', 'claude-opus-5-5', 'claude-sonnet-5-5', 'gpt-5-codex'])
  })
  it('cost, tokens in + out, sessions and share add up to the Overview totals', async () => {
    const st = await full()
    expect(sum(st.modelTotals.map((m) => m.cents))).toBe(st.cost30Cents)
    expect(st.cost30Cents).toBe(sum(st.days.map(dayCents)))
    expect(sum(st.modelTotals.map((m) => m.tokensIn + m.tokensOut))).toBe(st.tokens30)
    expect(sum(st.modelTotals.map((m) => m.sessions))).toBe(sum(st.agents.map((a) => (a as unknown as { sessions: number }).sessions)))
    expect(sum(st.modelTotals.map((m) => m.shareTenths))).toBe(1000)
    for (const m of st.modelTotals) expect(m.tokensOut).toBeLessThan(m.tokensIn)
  })
  it('the Overview tokens stat is the By model Total (input + output), printed the same way', async () => {
    const st = (await full()) as Full & { tokens30Text: string }
    const total = st.modelRows[4]
    expect(st.tokens30Text).toBe(`${(Math.round(Number(total.input) * 100) / 100 + Math.round(Number(total.output) * 100) / 100).toFixed(2)} M`)
  })
  it('the table has one row per model, biggest cost first, and a Total row with the same cost as the Overview', async () => {
    const st = await full()
    expect(st.modelRows).toHaveLength(5)
    const costs = st.modelTotals.map((m) => m.cents)
    expect(costs).toEqual([...costs].sort((a, b) => b - a))
    const total = st.modelRows[4]
    expect(total).toMatchObject({ model: 'Total', cost: (st.cost30Cents / 100).toFixed(2), share: '100.0 %' })
    expect(total.sessions).toBe(18)
    // What is printed adds up: every token column's rows sum to the Total cell.
    for (const col of ['cost', 'input', 'output', 'cache']) {
      const rows = st.modelRows.slice(0, 4).map((r) => Math.round(Number(r[col]) * 100))
      expect(rows.reduce((a, b) => a + b, 0), col).toBe(Math.round(Number(total[col]) * 100))
    }
    expect(st.ticketRows.length).toBeGreaterThan(0)
    expect(st.agentRows).toHaveLength(3)
  })
})

describe('usage budget', () => {
  it('has no alert at the seed (well under 80%)', async () => {
    const st = await state(setup())
    expect(st.budgetPct).toBeLessThan(80)
    expect(st.budgetNotice.type).not.toBe('alert')
  })
  it('save_settings with budget_chf 30 raises a warn alert', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'usage', 'save_settings', { formData: { budget_chf: 30 } })
    const st = await state(s)
    expect(st.settings.budget_chf).toBe(30)
    expect(st.budgetNotice).toMatchObject({ type: 'alert', tone: 'warn' })
  })
})

describe('chf formatter', () => {
  it('formats cents as CHF with two decimals', () => {
    expect(FORMATTERS.chf(3140)).toBe('CHF 31.40')
    expect(FORMATTERS.chf(5)).toBe('CHF 0.05')
  })
  it('formats tokens compactly', () => {
    expect(FORMATTERS.ktok(2_400_000)).toBe('2.4 M')
    expect(FORMATTERS.ktok(84_300)).toBe('84k')
  })
})
