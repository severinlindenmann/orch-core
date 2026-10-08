// Tiny in-process router for the mock API: (method, path pattern) -> handler(store, ctx).
import type { HttpMethod, TransportResponse } from '@/api/transport'
import type { ActionRequest, ApiErrorBody, OrchEvent, Status, TicketDocument } from '@/api/types'
import { STATUSES } from '@/api/types'
import type { MockStore } from './store'

export interface RouteContext {
  params: Record<string, string>
  query: URLSearchParams
  body: unknown
}
export type Handler = (store: MockStore, ctx: RouteContext) => TransportResponse

interface Route {
  method: HttpMethod
  pattern: string
  regex: RegExp
  names: string[]
  handler: Handler
}

export const ok = (json: unknown, status = 200): TransportResponse => ({ status, json })
export const fail = (status: number, code: string, message: string, hint?: string, retryable = false): TransportResponse => ({
  status,
  json: { ok: false, error: { code, message, hint, retryable } } satisfies ApiErrorBody,
})

function compile(pattern: string): { regex: RegExp; names: string[] } {
  const names: string[] = []
  const src = pattern.replace(/:([a-zA-Z_]+)/g, (_, n: string) => {
    names.push(n)
    return '([^/]+)'
  })
  return { regex: new RegExp('^' + src + '$'), names }
}

export class MockRouter {
  private routes: Route[] = []

  add(method: HttpMethod, pattern: string, handler: Handler) {
    this.routes.push({ method, pattern, handler, ...compile(pattern) })
    return this
  }

  match(method: HttpMethod, path: string, body?: unknown): { handler: Handler; ctx: RouteContext } | null {
    const url = new URL(path, 'http://mock.local')
    for (const r of this.routes) {
      if (r.method !== method) continue
      const m = r.regex.exec(url.pathname)
      if (!m) continue
      const params: Record<string, string> = {}
      r.names.forEach((n, i) => (params[n] = decodeURIComponent(m[i + 1])))
      return { handler: r.handler, ctx: { params, query: url.searchParams, body } }
    }
    return null
  }
}

// ------------------------------------------------------------------ routes

function visibleTicket(store: MockStore, key: string): TicketDocument | TransportResponse {
  if (!store.hasTicket(key)) return fail(404, 'not_found', `No ticket ${key}`)
  if (!store.isVisible(key)) return fail(404, 'not_visible', `No ticket ${key}`, 'The ticket is restricted to other people.')
  return store.ticket(key)!
}
const isResponse = (x: unknown): x is TransportResponse => typeof x === 'object' && x !== null && 'status' in x && 'json' in x

function postAction(store: MockStore, ctx: RouteContext): TransportResponse {
  const t0 = visibleTicket(store, ctx.params.key)
  if (isResponse(t0)) return t0
  const t = t0
  const key = t.key
  const me = store.viewer
  const ws = store.workspaceOf(key)!
  const role = store.roleIn(ws.id, me)
  const a = ctx.body as ActionRequest | null
  if (!a || typeof a !== 'object' || !('action' in a)) return fail(400, 'validation', 'Body must be {action, ...}')
  if (!role || role === 'viewer') return fail(403, 'forbidden', 'Viewers cannot change tickets.', 'Ask an owner or maintainer.')

  const finish = (event: OrchEvent) => ok({ ok: true, event, ticket: store.ticket(key)! })

  switch (a.action) {
    case 'answer': {
      const q = t.questions_state.find((x) => x.id === a.question)
      if (!q) return fail(404, 'question.not_found', `No question ${a.question} on ${key}`)
      if (q.state === 'answered') return fail(409, 'question.already_answered', `${q.id} was already answered by ${q.answer?.by}.`, 'The first valid answer wins.')
      if (q.to !== me && role !== 'owner') return fail(403, 'question.not_addressee', `${q.id} is addressed to ${q.to}.`)
      if (a.option && !q.options?.some((o) => o.key === a.option)) return fail(400, 'validation', `Unknown option ${a.option}`)
      if (!a.option && !a.text?.trim()) return fail(400, 'validation', 'Pick an option or write an answer.')
      const event = store.append(key, { type: 'question.answered', question: q.id, option: a.option, text: a.text?.trim() || undefined })
      const stillBlocked = store.ticket(key)!.questions_state.some((x) => x.state === 'open' && x.blocking)
      if (!stillBlocked && t.status === 'waiting') store.append(key, { type: 'status.changed', actor: 'host', to: 'open' })
      return finish(event)
    }
    case 'approve': {
      const why = store.canApprove(t, a.gate, me)
      if (why) return fail(403, 'gate.not_eligible', why, 'See the gate policy in the workspace settings.')
      const event = store.append(key, { type: 'gate.approved', gate: a.gate, presence: 'touchid' })
      if (a.gate === 'plan' && t.status === 'backlog') store.append(key, { type: 'status.changed', actor: 'host', to: 'open' })
      return finish(event)
    }
    case 'request_changes': {
      if (!a.text?.trim()) return fail(400, 'validation', 'Say what should change.')
      const why = store.canApprove(t, a.gate, me)
      if (why && !/already/.test(why)) return fail(403, 'gate.not_eligible', why)
      const event = store.append(key, { type: 'gate.changes_requested', gate: a.gate, text: a.text.trim() })
      if (a.gate === 'verify' && t.status === 'testing') store.append(key, { type: 'status.changed', actor: 'host', to: 'in-progress' })
      return finish(event)
    }
    case 'verdict': {
      if (t.status !== 'testing') return fail(409, 'transition.not_allowed', `${key} is ${t.status}, not testing.`)
      if (t.verdict) return fail(409, 'verdict.exists', 'A verdict was already given.')
      const why = store.canApprove(t, 'verify', me)
      if (why) return fail(403, 'gate.not_eligible', why)
      const event = store.append(key, { type: 'verdict.given', result: a.result, text: a.text?.trim() || undefined })
      if (a.result === 'pass') {
        store.append(key, { type: 'gate.approved', gate: 'verify', presence: 'touchid' })
        store.append(key, { type: 'status.changed', actor: 'host', to: 'done' })
      } else {
        store.append(key, { type: 'gate.changes_requested', gate: 'verify', text: a.text?.trim() })
        store.append(key, { type: 'status.changed', actor: 'host', to: 'in-progress' })
      }
      return finish(event)
    }
    case 'comment': {
      if (!a.text?.trim()) return fail(400, 'validation', 'Write something first.')
      return finish(store.append(key, { type: 'log.added', text: a.text.trim() }))
    }
    case 'ask': {
      if (!a.text?.trim()) return fail(400, 'validation', 'Write the question first.')
      const n = t.questions_state.length + 1
      const def = { id: `Q${n}`, to: a.to, text: a.text.trim(), options: a.options, blocking: a.blocking ?? false }
      return finish(store.append(key, { type: 'question.asked', question: def.id, def }))
    }
    case 'claim': {
      if (t.claim) return fail(409, 'claim.held', `${key} is claimed by ${t.claim.agent}.`, 'Use takeover with a reason.', false)
      const event = store.append(key, {
        type: 'claim.taken',
        actor: `claude-code:s_${key.slice(-4)}:${me}`,
        expires: '2026-10-09T18:00:00Z',
      })
      if (t.status === 'open' || t.status === 'backlog') store.append(key, { type: 'status.changed', actor: 'host', to: 'in-progress' })
      return finish(event)
    }
    case 'release': {
      if (!t.claim) return fail(409, 'claim.none', `${key} has no claim.`)
      return finish(store.append(key, { type: 'claim.released', actor: `${t.claim.agent}:${t.claim.session}:${t.claim.for}` }))
    }
    case 'set_status': {
      if (!STATUSES.includes(a.status as Status)) return fail(400, 'validation', `Unknown status ${a.status}`)
      if (role !== 'owner' && role !== 'maintainer') return fail(403, 'forbidden', 'Only owners and maintainers move tickets.')
      if (a.status === 'done') return fail(409, 'human_only', 'Done is reached by a verdict', 'Give the verdict on the ticket page', false)
      return finish(store.append(key, { type: 'status.changed', to: a.status }))
    }
    default:
      return fail(400, 'validation', `Unknown action ${(a as { action: string }).action}`)
  }
}

export function buildRouter(): MockRouter {
  const r = new MockRouter()
  r.add('GET', '/api/me', (s) => ok(s.me()))
  r.add('GET', '/api/workspaces', (s) => ok(s.workspaceList()))
  r.add('GET', '/api/workspaces/:ws/today', (s, c) =>
    s.workspaces.some((w) => w.id === c.params.ws) ? ok(s.today(c.params.ws)) : fail(404, 'not_found', 'No such workspace'),
  )
  r.add('GET', '/api/workspaces/:ws/tickets', (s, c) => {
    if (!s.workspaces.some((w) => w.id === c.params.ws)) return fail(404, 'not_found', 'No such workspace')
    const statuses = c.query.get('status')?.split(',').filter(Boolean)
    const q = c.query.get('q')?.toLowerCase()
    const type = c.query.get('type')
    const parent = c.query.get('parent')
    const list = s
      .listTickets(c.params.ws)
      .filter((t) => !statuses?.length || statuses.includes(t.status))
      .filter((t) => !type || t.type === type)
      .filter((t) => !parent || t.parent === parent)
      .filter((t) => !q || t.key.toLowerCase().includes(q) || t.title.toLowerCase().includes(q) || t.labels.some((l) => l.includes(q)))
      .map((t) => s.summary(t))
    return ok(list)
  })
  r.add('GET', '/api/workspaces/:ws/cursor', (s, c) =>
    s.workspaces.some((w) => w.id === c.params.ws) ? ok({ cursor: s.cursor(c.params.ws) }) : fail(404, 'not_found', 'No such workspace'),
  )
  r.add('GET', '/api/workspaces/:ws/agents', (s, c) => ok(s.agents(c.params.ws)))
  r.add('GET', '/api/tickets/:key', (s, c) => {
    const t = visibleTicket(s, c.params.key)
    return isResponse(t) ? t : ok(t)
  })
  r.add('GET', '/api/tickets/:key/events', (s, c) => {
    const t = visibleTicket(s, c.params.key)
    if (isResponse(t)) return t
    const since = Number(c.query.get('since') ?? 0)
    return ok(s.eventsOf(c.params.key).filter((e) => e.seq > since))
  })
  r.add('POST', '/api/tickets/:key/actions', postAction)
  r.add('GET', '/api/addons', (s) => ok(s.addons))
  r.add('GET', '/api/addons/decisions', (s) => ok(s.canDecide() ? s.addons.filter((a) => a.enabled).flatMap((a) => a.decisions ?? []) : []))
  r.add('GET', '/api/workspaces/:ws/addons/:name/state', (s, c) => {
    if (!s.workspaces.some((w) => w.id === c.params.ws)) return fail(404, 'not_found', 'No such workspace')
    const v = s.addonStateView(c.params.ws, c.params.name)
    return v ? ok(v) : fail(404, 'not_found', 'Addon is not enabled in this workspace')
  })
  r.add('POST', '/api/addons/:name/actions/:id', (s, c) => {
    const addon = s.addons.find((a) => a.name === c.params.name && a.enabled)
    if (!addon) return fail(404, 'not_found', 'No such addon')
    const res = s.runAddon(addon.name, c.params.id, (c.body ?? {}) as Record<string, unknown>)
    return res ? ok(res) : fail(404, 'not_found', `Addon ${addon.name} has no action ${c.params.id}`)
  })
  r.add('POST', '/api/dev/reset', (s) => {
    s.reset()
    return ok({ ok: true })
  })
  r.add('POST', '/api/dev/viewer', (s, c) => {
    const person = (c.body as { person?: string } | null)?.person
    if (!person || !s.workspaces.some((w) => w.members.some((m) => m.person === person))) return fail(400, 'validation', 'Unknown person')
    s.setViewer(person)
    return ok({ ok: true })
  })
  return r
}

/** Handler with simulated latency (120-300 ms). Pass { latency: false } in tests. */
export function createMockHandler(store: MockStore, opts: { latency?: boolean } = {}) {
  const router = buildRouter()
  const latency = opts.latency ?? true
  return async (method: HttpMethod, path: string, body?: unknown): Promise<TransportResponse> => {
    if (latency) await new Promise((res) => setTimeout(res, 120 + Math.random() * 180))
    const m = router.match(method, path, body)
    if (!m) return fail(404, 'not_found', `No route for ${method} ${path}`)
    try {
      return m.handler(store, m.ctx)
    } catch (err) {
      return fail(500, 'internal', err instanceof Error ? err.message : 'Internal error', undefined, true)
    }
  }
}
