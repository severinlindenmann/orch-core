// The agent side of the busy day: the session registry (rows like fixtures/me.json), the sessions each grant covers,
// and a handful of refusals (on the session rows, not in any ticket log) including one that trips the stop rule (the third same refusal by one session).
import type { Rng } from './rng'
import { ENDED, GRANT_OF, SESSIONS } from './roster'
import type { SessionRefusal } from '@/api/types'
import { NOW_MS, MIN, iso } from './timeline'
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
  refusals?: SessionRefusal[]
}

const NAME = { 'claude-code': 'Claude Code', codex: 'Codex' } as const

/** Registry rows for the generated sessions plus two that have stopped. */
export function agentRows(rng: Rng, demo: Built[], refusals: Map<string, SessionRefusal[]> = new Map()): AgentRow[] {
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
      ...(refusals.has(s.id) ? { refusals: refusals.get(s.id) } : {}),
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
  { code: 'claim.held', message: (k: string, by: string) => `${k} is claimed by ${by}.`, hint: 'Wait for the other session to release it, or work on another ticket.', op: 'claim', retryable: false },
  { code: 'lease.held', message: (k: string, by: string) => `A task of ${k} is leased by ${by}.`, hint: 'Pick another task, or wait until the lease is released.', op: 'lease', retryable: false },
  { code: 'gate.not_approved', message: (k: string) => `${k}: the plan is not approved yet.`, hint: 'A person approves the plan first.', op: 'claim', retryable: false },
  { code: 'verify.failed', message: (k: string) => `${k}: the task check failed (exit 1).`, hint: 'Fix the failing check and run it again.', op: 'task.done', retryable: true },
  { code: 'grant.scope', message: () => 'This grant does not cover approvals.', hint: 'Ask a person to approve, or to widen the grant.', op: 'approve', retryable: false },
]

/**
 * Refusals today (CLI error envelopes returned to the agents, kept on the session rows, never in a ticket log): one
 * session (s_b105) is refused `claim.held` three times in a row (the stop rule), and five other sessions are refused
 * once each for a different reason. The refusals sit on tickets agents work on.
 */
export function addRefusals(rng: Rng, demo: Built[]): Map<string, SessionRefusal[]> {
  const out = new Map<string, SessionRefusal[]>()
  const working = demo.filter((b) => b.arch === 'wip' && b.holder)
  const open = demo.filter((b) => b.arch === 'openReady')
  const add = (b: Built, session: (typeof SESSIONS)[number], r: (typeof REFUSALS)[number], minutesAgo: number) => {
    const holder = b.holder ?? 'another session'
    const list = out.get(session.id) ?? []
    list.push({ at: iso(NOW_MS - minutesAgo * MIN), ticket: b.definition.key, code: r.code, message: r.message(b.definition.key, holder), hint: r.hint, retryable: r.retryable, op: r.op })
    list.sort((x, y) => x.at.localeCompare(y.at))
    out.set(session.id, list)
  }
  const stopper = SESSIONS.find((s) => s.id === 's_b105')!
  const targets = rng.sample(working, 3)
  targets.forEach((b, i) => add(b, stopper, REFUSALS[0], 55 - i * 17))
  const others = SESSIONS.filter((s) => !s.parent && s.id !== 's_b105')
  rng.sample(others, 5).forEach((s, i) => add(rng.pick([...working, ...open]), s, REFUSALS[1 + (i % (REFUSALS.length - 1))], rng.int(5, 200)))
  return out
}
