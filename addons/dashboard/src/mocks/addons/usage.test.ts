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
  it('has 30 days ending 2026-10-09 for opus, sonnet and haiku', async () => {
    const st = await state(setup())
    expect(st.days).toHaveLength(30)
    expect(st.days[0].date).toBe('2026-09-10')
    expect(st.days[29].date).toBe('2026-10-09')
    expect(Object.keys(st.days[0].cents).sort()).toEqual(['haiku', 'opus', 'sonnet'])
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
    expect(st.perModel.map((m) => m.model)).toEqual(['Opus', 'Sonnet', 'Haiku'])
    expect(st.topTickets.length).toBeGreaterThanOrEqual(3)
    expect(st.topTickets[0].cost).toMatch(/^CHF \d+\.\d\d$/)
    expect(st.byTicket['DEMO-0043']).toMatchObject({ sessions: 3 })
    expect(st.byTicket['DEMO-0043'].cents).toBeGreaterThan(0)
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
