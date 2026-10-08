import { registerAddon } from './registry'

// usage: cost and tokens per day, model, ticket and agent. Everything derives from ONE generator (`seedDays`) so the
// page, the ticket panel and the Today card always agree. Money is whole cents. No Math.random / Date.now: the
// series is a seeded LCG, so tests and screenshots are stable.

const MODELS = ['opus', 'sonnet', 'haiku'] as const
type Model = (typeof MODELS)[number]
const MODEL_LABEL: Record<Model, string> = { opus: 'Opus', sonnet: 'Sonnet', haiku: 'Haiku' }
/** Tokens per cent: the cheaper the model, the more tokens a cent buys. */
const TOKENS_PER_CENT: Record<Model, number> = { opus: 250, sonnet: 900, haiku: 3000 }

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

/** Splits `total` over `weights` (largest remainder), so the parts add up to exactly `total`. */
function allocate(total: number, weights: number[]): number[] {
  const sum = weights.reduce((a, b) => a + b, 0)
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

function seedDays(): Day[] {
  const rand = rng(20261009)
  const days: Day[] = []
  for (let i = 0; i < DAYS; i++) {
    const t = END_DAY - (DAYS - 1 - i) * DAY_MS
    const weekend = [0, 6].includes(new Date(t).getUTCDay())
    const f = weekend ? 0.4 : 1
    const cents = {
      opus: Math.round((150 + rand() * 180) * f),
      sonnet: Math.round((90 + rand() * 100) * f),
      haiku: Math.round((20 + rand() * 40) * f),
    }
    days.push({ date: isoDay(t), cents, tokens: { opus: 0, sonnet: 0, haiku: 0 } })
  }
  // Pin the last 7 days to exactly WEEK_CENTS (scale, then put the rounding remainder on today's opus).
  const week = days.slice(-7)
  const raw = week.reduce((n, d) => n + sum(d), 0)
  for (const d of week) for (const m of MODELS) d.cents[m] = Math.round((d.cents[m] * WEEK_CENTS) / raw)
  week[6].cents.opus += WEEK_CENTS - week.reduce((n, d) => n + sum(d), 0)
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

function seedState(): Record<string, unknown> {
  const days = seedDays()
  const cents = days.reduce((n, d) => n + sum(d), 0)
  const tokens = days.reduce((n, d) => n + sumTokens(d), 0)
  const tc = allocate(cents, TICKET_SEED.map((t) => t.weight))
  const tt = allocate(tokens, TICKET_SEED.map((t) => t.weight))
  const ac = allocate(cents, AGENT_SEED.map((t) => t.weight))
  const at = allocate(tokens, AGENT_SEED.map((t) => t.weight))
  const tickets: Row[] = TICKET_SEED.map((t, i) => ({ key: t.key, cents: tc[i], tokens: tt[i], sessions: t.sessions, ms: t.ms }))
  const agents: Agent[] = AGENT_SEED.map((a, i) => ({ id: a.id, name: a.name, cents: ac[i], tokens: at[i], sessions: a.sessions }))
  return { days, tickets, agents, settings: { budget_chf: DEFAULT_BUDGET_CHF } satisfies Settings }
}

const chf = (cents: number) => `CHF ${(cents / 100).toFixed(2)}`
const hm = (ms: number) => {
  const min = Math.round(ms / 60000)
  return min >= 60 ? `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, '0')} min` : `${min} min`
}
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const dayLabel = (iso: string) => `${Number(iso.slice(8))} ${MONTHS[Number(iso.slice(5, 7)) - 1]}`

function budgetOf(state: Record<string, unknown>): number {
  const b = Number((state.settings as Partial<Settings> | undefined)?.budget_chf)
  return b > 0 ? b : DEFAULT_BUDGET_CHF
}

registerAddon({
  name: 'usage',
  seed: () => seedState(),

  view(state) {
    const days = state.days as Day[]
    const tickets = state.tickets as Row[]
    const week = days.slice(-7).reduce((n, d) => n + sum(d), 0)
    const month = days.filter((d) => d.date.startsWith(MONTH_PREFIX)).reduce((n, d) => n + sum(d), 0)
    const budget = budgetOf(state)
    const ratio = month / (budget * 100)
    const pct = Math.round(ratio * 100)
    const tokens30 = days.reduce((n, d) => n + sumTokens(d), 0)
    const byTicket: Record<string, Omit<Row, 'key'>> = {}
    for (const t of tickets) byTicket[t.key] = { cents: t.cents, tokens: t.tokens, sessions: t.sessions, ms: t.ms }
    return {
      weekCents: week,
      monthCents: month,
      tokens30,
      budgetChf: budget,
      budgetPct: pct,
      monthChf: month / 100,
      // Shown only past 80% of the monthly budget; an empty stack renders nothing otherwise.
      budgetNotice:
        ratio > ALERT_AT
          ? { type: 'alert', tone: 'warn', title: `Budget ${pct}% used`, text: `${chf(month)} of ${chf(budget * 100)} this month. Raise the budget in Settings or slow the agents down.` }
          : { type: 'stack', children: [] },
      perDay: days.map((d) => ({ day: dayLabel(d.date), chf: sum(d) / 100 })),
      perModel: MODELS.map((m) => ({ model: MODEL_LABEL[m], chf: days.reduce((n, d) => n + d.cents[m], 0) / 100 })),
      topTickets: [...tickets]
        .sort((a, b) => b.cents - a.cents)
        .slice(0, 5)
        .map((t) => ({ ticket: t.key, sessions: t.sessions, time: hm(t.ms), cost: chf(t.cents) })),
      byTicket,
    }
  },

  actions: {
    save_settings: {
      minRole: 'owner',
      run: ({ state, body }) => {
        state.settings = body.formData ?? {}
        return { ok: true, message: 'Settings saved.', changed: true }
      },
    },
  },
})
