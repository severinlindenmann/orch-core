// The agent side of the busy day: the session registry (rows like fixtures/me.json), the sessions each grant covers,
// and a handful of refusals including one that trips the stop rule (the third same refusal by one session).
import type { Rng } from './rng'
import { ENDED, GRANT_OF, SESSIONS, actorOf } from './roster'
import { NOW_MS, MIN, iso, type GenEvent } from './timeline'
import type { Built } from './types'

export interface AgentRow {
  id: string
  name: string
  for: string
  session: string
  grant: string
  last_seen: string
  parent: string | null
  harness: string
  model: string
  state: 'working' | 'waiting' | 'stopped'
  waiting_on?: { kind: 'question'; ticket: string; ref: string }
}

const NAME = { 'claude-code': 'Claude Code', codex: 'Codex' } as const

/** Registry rows for the generated sessions plus two that have stopped. */
export function agentRows(rng: Rng, demo: Built[]): AgentRow[] {
  const waiting = new Map<string, { ticket: string; ref: string }>()
  for (const b of demo) if (b.holder && b.waitingOn) waiting.set(b.holder, { ticket: b.definition.key, ref: b.waitingOn.question })
  const rows: AgentRow[] = SESSIONS.map((s) => {
    const w = waiting.get(s.id)
    return {
      id: s.agent,
      name: s.name,
      for: s.for,
      session: s.id,
      grant: GRANT_OF[s.for].id,
      last_seen: iso(NOW_MS - (w ? rng.int(10, 50) : rng.int(1, 7)) * MIN),
      parent: s.parent,
      harness: s.agent,
      model: s.model,
      state: w ? 'waiting' : 'working',
      ...(w ? { waiting_on: { kind: 'question' as const, ...w } } : {}),
    }
  })
  for (const e of ENDED.slice(0, 2))
    rows.push({ id: e.agent, name: NAME[e.agent], for: e.for, session: e.id, grant: GRANT_OF[e.for as 'p_sev' | 'p_mara'].id, last_seen: iso(NOW_MS - rng.int(120, 300) * MIN), parent: null, harness: e.agent, model: e.agent === 'codex' ? 'gpt-5-codex' : 'claude-opus-4-5', state: 'stopped' })
  return rows
}

/** Every generated session id (and the stopped ones) belongs to the grant of the person it works for. */
export function grantSessions(): Record<string, string[]> {
  const out: Record<string, string[]> = { [GRANT_OF.p_sev.id]: [], [GRANT_OF.p_mara.id]: [] }
  for (const s of SESSIONS) out[GRANT_OF[s.for].id].push(s.id)
  for (const e of ENDED.slice(0, 2)) out[GRANT_OF[e.for as 'p_sev' | 'p_mara'].id].push(e.id)
  return out
}

const REFUSALS = [
  { code: 'claim.held', message: (k: string, by: string) => `${k} is claimed by ${by}.`, op: 'claim', retryable: false },
  { code: 'lease.held', message: (k: string, by: string) => `A task of ${k} is leased by ${by}.`, op: 'lease', retryable: false },
  { code: 'gate.not_approved', message: (k: string) => `${k}: the plan is not approved yet.`, op: 'claim', retryable: false },
  { code: 'verify.failed', message: (k: string) => `${k}: the task check failed (exit 1).`, op: 'task.done', retryable: true },
  { code: 'grant.scope', message: () => 'This grant does not cover approvals.', op: 'approve', retryable: false },
]

/**
 * Refusals today: one session (s_b105) is refused `claim.held` three times in a row (the stop rule), and five other
 * sessions are refused once each for a different reason. Refused events are added to tickets agents work on.
 */
export function addRefusals(rng: Rng, demo: Built[]): void {
  const working = demo.filter((b) => b.arch === 'wip' && b.holder)
  const open = demo.filter((b) => b.arch === 'openReady')
  const events = (b: Built, session: (typeof SESSIONS)[number], r: (typeof REFUSALS)[number], minutesAgo: number) => {
    const holder = b.holder ?? 'another session'
    const e: GenEvent = { type: 'agent.refused', actor: actorOf(session), at: iso(NOW_MS - minutesAgo * MIN), code: r.code, message: r.message(b.definition.key, holder), retryable: r.retryable, op: r.op }
    b.events.push(e)
    b.events.sort((x, y) => x.at.localeCompare(y.at))
  }
  const stopper = SESSIONS.find((s) => s.id === 's_b105')!
  const targets = rng.sample(working, 3)
  targets.forEach((b, i) => events(b, stopper, REFUSALS[0], 55 - i * 17))
  const others = SESSIONS.filter((s) => !s.parent && s.id !== 's_b105')
  rng.sample(others, 5).forEach((s, i) => events(rng.pick([...working, ...open]), s, REFUSALS[1 + (i % (REFUSALS.length - 1))], rng.int(5, 200)))
}
