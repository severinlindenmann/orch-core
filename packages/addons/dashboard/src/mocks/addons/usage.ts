import type { MockStore } from '../store'
import { briefs, isDemo } from '../busy/helpers'
import type { Rng } from '../busy/rng'
import { SESSIONS } from '../busy/roster'
import { canSeeTicket, registerAddon } from './registry'

// usage: cost and tokens per day, model, ticket and agent. Everything derives from ONE generator (`seedDays`) so the
// page, the ticket panel and the Today card always agree. Money is whole cents. No Math.random / Date.now: the
// series is a seeded LCG, so tests and screenshots are stable.

const MODELS = ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-4-5', 'gpt-5-codex'] as const
type Model = (typeof MODELS)[number]
const MODEL_LABEL: Record<Model, string> = { 'claude-opus-5-5': 'claude-opus-5-5', 'claude-sonnet-5-5': 'claude-sonnet-5-5', 'claude-haiku-4-5': 'claude-haiku-4-5', 'gpt-5-codex': 'gpt-5-codex' }
/** Tokens per cent: the cheaper the model, the more tokens a cent buys. */
const TOKENS_PER_CENT: Record<Model, number> = { 'claude-opus-5-5': 250, 'claude-sonnet-5-5': 900, 'claude-haiku-4-5': 3000, 'gpt-5-codex': 700 }
/** Share of a model's tokens that are output, and cache-read tokens per token in + out (cache reads are extra, never in the total). */
const OUT_SHARE: Record<Model, number> = { 'claude-opus-5-5': 0.16, 'claude-sonnet-5-5': 0.19, 'claude-haiku-4-5': 0.22, 'gpt-5-codex': 0.14 }
const CACHE_PER_TOKEN: Record<Model, number> = { 'claude-opus-5-5': 3.1, 'claude-sonnet-5-5': 2.6, 'claude-haiku-4-5': 1.4, 'gpt-5-codex': 1.9 }

const END_DAY = Date.UTC(2026, 9, 9) // mock "now" is 2026-10-09
const DAY_MS = 86_400_000
const DAYS = 30
const WEEK_CENTS = 3140 // "This week CHF 31.40" (last 7 days, incl. today)
const MONTH_PREFIX = '2026-10'
const DEFAULT_BUDGET_CHF = 150
const ALERT_AT = 0.8

interface Day {
  date: string
  cents: Record<Model, number>
  tokens: Record<Model, number>
}
interface Row {
  key: string
  cents: number
  tokens: number
  sessions: number
  ms: number
}
interface Agent {
  id: string
  name: string
  cents: number
  tokens: number
  sessions: number
}
/** A person whose grants ran agent sessions; `weight` is their share of the work (seed only). */
interface Person {
  id: string
  name: string
  weight: number
}
interface Settings {
  budget_chf: number
}

/** Seeded LCG in [0, 1). */
function rng(seed: number): () => number {
  let x = seed
  return () => {
    x = (x * 1103515245 + 12345) % 2147483648
    return x / 2147483648
  }
}

/**
 * Splits `total` over `weights` (largest remainder), so the parts add up to exactly `total`. With no weight at all
 * (every weight 0, e.g. a month without cost) every part is 0: nothing to split, never NaN.
 */
export function allocate(total: number, weights: number[]): number[] {
  const sum = weights.reduce((a, b) => a + b, 0)
  if (!(sum > 0)) return weights.map(() => 0)
  const exact = weights.map((w) => (total * w) / sum)
  const out = exact.map(Math.floor)
  let left = total - out.reduce((a, b) => a + b, 0)
  const order = exact.map((e, i) => [e - Math.floor(e), i] as const).sort((a, b) => b[0] - a[0] || a[1] - b[1])
  for (const [, i] of order) {
    if (left-- <= 0) break
    out[i]++
  }
  return out
}

const isoDay = (t: number) => new Date(t).toISOString().slice(0, 10)

/** `weekCents`: what the last 7 days add up to; `scale` multiplies every day before that pin (the Busy day). */
function seedDays(weekCents = WEEK_CENTS, scale = 1): Day[] {
  const rand = rng(20261009)
  const days: Day[] = []
  for (let i = 0; i < DAYS; i++) {
    const t = END_DAY - (DAYS - 1 - i) * DAY_MS
    const weekend = [0, 6].includes(new Date(t).getUTCDay())
    const f = (weekend ? 0.4 : 1) * scale
    const cents = {
      'claude-opus-5-5': Math.round((120 + rand() * 140) * f),
      'claude-sonnet-5-5': Math.round((70 + rand() * 80) * f),
      'claude-haiku-4-5': Math.round((15 + rand() * 30) * f),
      'gpt-5-codex': Math.round((50 + rand() * 70) * f),
    }
    days.push({ date: isoDay(t), cents, tokens: { 'claude-opus-5-5': 0, 'claude-sonnet-5-5': 0, 'claude-haiku-4-5': 0, 'gpt-5-codex': 0 } })
  }
  // Pin the last 7 days to exactly WEEK_CENTS (scale, then put the rounding remainder on today's opus).
  const week = days.slice(-7)
  const raw = week.reduce((n, d) => n + sum(d), 0)
  for (const d of week) for (const m of MODELS) d.cents[m] = Math.round((d.cents[m] * weekCents) / raw)
  week[6].cents['claude-opus-5-5'] += weekCents - week.reduce((n, d) => n + sum(d), 0)
  for (const d of days) for (const m of MODELS) d.tokens[m] = d.cents[m] * TOKENS_PER_CENT[m]
  return days
}

const sum = (d: Day) => MODELS.reduce((n, m) => n + d.cents[m], 0)
const sumTokens = (d: Day) => MODELS.reduce((n, m) => n + d.tokens[m], 0)

/** Weights/sessions/time per ticket (the demo tickets that already carry agent work). */
const TICKET_SEED: { key: string; weight: number; sessions: number; ms: number }[] = [
  { key: 'DEMO-0040', weight: 1280, sessions: 9, ms: 17_400_000 },
  { key: 'DEMO-0043', weight: 412, sessions: 3, ms: 5_460_000 },
  { key: 'DEMO-0041', weight: 298, sessions: 2, ms: 3_900_000 },
  { key: 'DEMO-0042', weight: 187, sessions: 1, ms: 2_100_000 },
  { key: 'DEMO-0037', weight: 143, sessions: 1, ms: 1_500_000 },
  { key: 'DEMO-0046', weight: 96, sessions: 1, ms: 1_100_000 },
  { key: 'DEMO-0038', weight: 74, sessions: 1, ms: 900_000 },
]
const AGENT_SEED = [
  { id: 'claude-code', name: 'Claude Code', weight: 58, sessions: 11 },
  { id: 'codex', name: 'Codex', weight: 27, sessions: 5 },
  { id: 'claude-code-sub', name: 'Claude Code (subagents)', weight: 15, sessions: 2 },
]

/** Who ran the agents: every agent session runs under one person's grant (the owner most of the time). */
const PERSON_SEED: Person[] = [
  { id: 'p_sev', name: 'Severin', weight: 64 },
  { id: 'p_mara', name: 'Mara', weight: 36 },
]

function seedState(days = seedDays(), ticketSeed = TICKET_SEED, agentSeed = AGENT_SEED, budget = DEFAULT_BUDGET_CHF): Record<string, unknown> {
  const cents = days.reduce((n, d) => n + sum(d), 0)
  const tokens = days.reduce((n, d) => n + sumTokens(d), 0)
  const tc = allocate(cents, ticketSeed.map((t) => t.weight))
  const tt = allocate(tokens, ticketSeed.map((t) => t.weight))
  const ac = allocate(cents, agentSeed.map((t) => t.weight))
  const at = allocate(tokens, agentSeed.map((t) => t.weight))
  const tickets: Row[] = ticketSeed.map((t, i) => ({ key: t.key, cents: tc[i], tokens: tt[i], sessions: t.sessions, ms: t.ms }))
  const agents: Agent[] = agentSeed.map((a, i) => ({ id: a.id, name: a.name, cents: ac[i], tokens: at[i], sessions: a.sessions }))
  return { days, tickets, agents, people: PERSON_SEED, settings: { budget_chf: budget } satisfies Settings }
}

/**
 * The Busy day (R-g): usage grows with the work. The normal demo has 18 agent sessions over 30 days; the busy day adds
 * the generated sessions (19 working now, with their subagents) and the generated tickets agents worked on, so cost
 * and tokens scale by the share of extra sessions, the busy tickets join By ticket and the sessions join By agent.
 * The monthly budget is raised so the page reads as a busy month, not an overrun (the alert stays for real overruns).
 */
export const BUSY_BUDGET_CHF = 600

function seedBusyState(ws: string, store: MockStore, r: Rng): Record<string, unknown> {
  const normalSessions = AGENT_SEED.reduce((n, a) => n + a.sessions, 0)
  const demo = isDemo(store, ws)
  // DEMO has the generated agent sessions; the other workspaces get a share of the work by their generated tickets.
  const extraSessions = demo ? SESSIONS.length * 3 : 6
  const scale = (normalSessions + extraSessions) / normalSessions
  const days = seedDays(Math.round(WEEK_CENTS * scale), scale)
  const worked = briefs(store, ws).filter((b) => !b.restricted && (b.claimed || ['in-progress', 'waiting', 'testing', 'done'].includes(b.status)))
  const busyTickets = r.sample(worked, Math.min(worked.length, demo ? 36 : 8)).map((b) => {
    const sessions = r.int(1, 6)
    return { key: b.key, weight: r.int(30, 420), sessions, ms: sessions * r.int(12, 55) * 60_000 }
  })
  const bySession = (agent: 'claude-code' | 'codex', sub: boolean) => SESSIONS.filter((x) => x.agent === agent && !!x.parent === sub).length * 3
  const agents = demo
    ? [
        { id: 'claude-code', name: 'Claude Code', weight: 58, sessions: AGENT_SEED[0].sessions + bySession('claude-code', false) },
        { id: 'codex', name: 'Codex', weight: 31, sessions: AGENT_SEED[1].sessions + bySession('codex', false) + bySession('codex', true) },
        { id: 'claude-code-sub', name: 'Claude Code (subagents)', weight: 21, sessions: AGENT_SEED[2].sessions + bySession('claude-code', true) },
      ]
    : AGENT_SEED.map((a) => ({ ...a, sessions: a.sessions + Math.round((extraSessions * a.weight) / 100) }))
  return seedState(days, [...TICKET_SEED, ...busyTickets], agents, BUSY_BUDGET_CHF)
}

const chf = (cents: number) => `CHF ${(cents / 100).toFixed(2)}`
const hm = (ms: number) => {
  const min = Math.round(ms / 60000)
  return min >= 60 ? `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, '0')} min` : `${min} min`
}
/** Millions with two decimals, the one unit of every token column on the page. */
const mtok = (n: number) => (n / 1_000_000).toFixed(2)
/** The same, from whole 10k-token units. */
const mUnits = (u: number) => (u / 100).toFixed(2)
/** Whole cents as a plain amount; the column header carries the unit (CHF). */
const cents2 = (cents: number) => (cents / 100).toFixed(2)
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const dayLabel = (iso: string) => `${Number(iso.slice(8))} ${MONTHS[Number(iso.slice(5, 7)) - 1]}`

function monthRangeOf(days: Day[]): string {
  const inMonth = days.filter((d) => d.date.startsWith(MONTH_PREFIX))
  if (inMonth.length === 0) return ''
  const first = Number(inMonth[0].date.slice(8))
  const last = dayLabel(inMonth[inMonth.length - 1].date)
  return first === Number(last.split(' ')[0]) ? last : `${first}–${last}`
}

function budgetOf(state: Record<string, unknown>): number {
  const b = Number((state.settings as Partial<Settings> | undefined)?.budget_chf)
  return b > 0 ? b : DEFAULT_BUDGET_CHF
}

registerAddon({
  name: 'usage',
  // 2: per-day cents and tokens keyed by the full model ids (was opus/sonnet/haiku before N3).
  stateVersion: 2,
  seed: () => seedState(),
  seedBusy: (ws, store, r) => seedBusyState(ws, store, r),

  view(state, c) {
    const days = state.days as Day[]
    const tickets = (state.tickets as Row[]).filter((t) => canSeeTicket(c, t.key)) // per-ticket rows: visible tickets of this workspace only
    const week = days.slice(-7).reduce((n, d) => n + sum(d), 0)
    const month = days.filter((d) => d.date.startsWith(MONTH_PREFIX)).reduce((n, d) => n + sum(d), 0)
    const budget = budgetOf(state)
    const ratio = month / (budget * 100)
    const pct = Math.round(ratio * 100)
    const tokens30 = days.reduce((n, d) => n + sumTokens(d), 0)
    const sessionsTotal = (state.agents as Agent[]).reduce((n, a) => n + a.sessions, 0)
    const modelCents = MODELS.map((m) => days.reduce((n, d) => n + d.cents[m], 0))
    const modelTokens = MODELS.map((m) => days.reduce((n, d) => n + d.tokens[m], 0))
    const modelSessions = allocate(sessionsTotal, modelCents)
    const modelShare = allocate(1000, modelCents)
    const cost30 = modelCents.reduce((a, b) => a + b, 0)
    const pctText = (tenths: number) => `${(tenths / 10).toFixed(1)} %`
    const byModel = MODELS.map((m, i) => {
      const out = Math.round(modelTokens[i] * OUT_SHARE[m])
      return { model: MODEL_LABEL[m], sessions: modelSessions[i], tokensIn: modelTokens[i] - out, tokensOut: out, cacheRead: Math.round(modelTokens[i] * CACHE_PER_TOKEN[m]), cents: modelCents[i], share: modelShare[i] }
    }).sort((a, b) => b.cents - a.cents)
    // The table's token cells are rounded so the rows add up to the Total row (largest remainder, in 10k-token units).
    const unitsOf = (xs: number[]) => allocate(Math.round(xs.reduce((a, b) => a + b, 0) / 10_000), xs)
    const inU = unitsOf(byModel.map((r) => r.tokensIn))
    const outU = unitsOf(byModel.map((r) => r.tokensOut))
    const cacheU = unitsOf(byModel.map((r) => r.cacheRead))
    const sum1 = (xs: number[]) => xs.reduce((a, b) => a + b, 0)
    const agents = state.agents as Agent[]
    const agentShare = allocate(1000, agents.map((a) => a.cents))
    // By person: the same 30-day totals split by whose grant ran the sessions (largest remainder, so each column adds
    // up to the Overview and the By model Total exactly: cost in cents, sessions, tokens in 10k units, share in tenths).
    const people = (state.people as Person[] | undefined) ?? PERSON_SEED
    const weights = people.map((p) => p.weight)
    const tokenUnits = sum1(inU) + sum1(outU)
    const personCents = allocate(cost30, weights)
    const personSessions = allocate(sessionsTotal, weights)
    const personUnits = allocate(tokenUnits, weights)
    const personShare = allocate(1000, personCents)
    const personTotals = people
      .map((p, i) => ({ person: p.id, name: p.name, cents: personCents[i], sessions: personSessions[i], tokenUnits: personUnits[i], shareTenths: personShare[i] }))
      .sort((a, b) => b.cents - a.cents || (a.name < b.name ? -1 : 1))
    const byTicket: Record<string, Omit<Row, 'key'>> = {}
    for (const t of tickets) byTicket[t.key] = { cents: t.cents, tokens: t.tokens, sessions: t.sessions, ms: t.ms }
    return {
      tickets, // overrides the raw list
      weekCents: week,
      monthCents: month,
      tokens30,
      // The Overview stat reads the same rounded number as the By model Total (Input + Output).
      tokens30Text: `${mUnits(sum1(inU) + sum1(outU))} M`,
      budgetChf: budget,
      budgetPct: pct,
      monthChf: month / 100,
      // The one period of the "this month" tile, in words: "1–9 Oct".
      monthRange: monthRangeOf(days),
      // Shown only past 80% of the monthly budget; an empty stack renders nothing otherwise.
      budgetNotice:
        ratio > ALERT_AT
          ? { type: 'alert', tone: 'warn', title: `Budget ${pct}% used`, text: `${chf(month)} of ${chf(budget * 100)} this month. Raise the budget in Settings or slow the agents down.` }
          : { type: 'stack', children: [] },
      // The Today glance's sparkline: cost per day of the last 7 days (CHF), the days that add up to weekCents.
      weekTrend: days.slice(-7).map((d) => sum(d) / 100),
      perDay: days.map((d) => ({ day: dayLabel(d.date), chf: sum(d) / 100 })),
      cost30Cents: cost30,
      perModel: [...MODELS.map((m, i) => ({ model: MODEL_LABEL[m], chf: modelCents[i] / 100 }))].sort((a, b) => b.chf - a.chf),
      // The By model tab: last 30 days, so its totals are the Overview's "Last 30 days" cost and Tokens (in + out).
      // Cache reads are extra and not part of "Tokens".
      modelRows: [
        ...byModel.map((r, i) => ({ model: r.model, cost: cents2(r.cents), share: pctText(r.share), sessions: r.sessions, input: mUnits(inU[i]), output: mUnits(outU[i]), cache: mUnits(cacheU[i]) })),
        { model: 'Total', cost: cents2(cost30), share: '100.0 %', sessions: sessionsTotal, input: mUnits(sum1(inU)), output: mUnits(sum1(outU)), cache: mUnits(sum1(cacheU)) },
      ],
      // Raw numbers behind the By model rows (the formatted cells round).
      modelTotals: byModel.map((r) => ({ model: r.model, sessions: r.sessions, tokensIn: r.tokensIn, tokensOut: r.tokensOut, cacheRead: r.cacheRead, cents: r.cents, shareTenths: r.share })),
      ticketRows: [...tickets].sort((a, b) => b.cents - a.cents).map((t) => ({ ticket: t.key, cost: cents2(t.cents), tokens: mtok(t.tokens), sessions: t.sessions, time: hm(t.ms) })),
      personTotals,
      personRows: [
        ...personTotals.map((p) => ({ person: p.name, sessions: p.sessions, tokens: mUnits(p.tokenUnits), cost: cents2(p.cents), share: pctText(p.shareTenths) })),
        { person: 'Total', sessions: sessionsTotal, tokens: mUnits(tokenUnits), cost: cents2(cost30), share: '100.0 %' },
      ],
      agentRows: agents.map((a, i) => ({ agent: a.name, cost: cents2(a.cents), share: pctText(agentShare[i]), sessions: a.sessions, tokens: mtok(a.tokens) })),
      topTickets: [...tickets]
        .sort((a, b) => b.cents - a.cents)
        .slice(0, 5)
        .map((t) => ({ ticket: t.key, sessions: t.sessions, time: hm(t.ms), cost: chf(t.cents) })),
      byTicket,
    }
  },

  actions: {
    save_settings: ({ state, body }) => {
      state.settings = body.formData ?? {}
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})
