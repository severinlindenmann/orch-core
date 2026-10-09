// Tiny in-process router for the mock API: (method, path pattern) -> handler(store, ctx).
import type { HttpMethod, TransportResponse } from '@/api/transport'
import type { ActionRequest, AddonOpRequest, GateName, Role, SettingsRequest, WorkspaceIdentity, NewTicketRequest, ApiErrorBody, BodySections, OrchEvent, Priority, SavedView, Status, ViewParams, TicketDocument, TicketSummary } from '@/api/types'
import { STATUSES } from '@/api/types'
import type { MockStore } from './store'
import { atLeast, can } from '@/api/permissions'
import { APPROVER_GROUPS, unmeetablePolicy } from '@/api/gates'
import { addonActive } from '@/api/addons'
import peopleFixture from './fixtures/people.json'
import type { RelayRequest, RelaySimRequest } from '@/api/types'
import { listArtifacts } from './artifacts'
import { relayEpoch, relayRequest, relaySim, relayState } from './relay'

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
  const ws = store.workspaceOf(key)
  if (ws && !store.roleIn(ws.id, store.viewer)) return fail(403, 'forbidden', 'You are not a member of this workspace.', 'Ask an owner.')
  return store.servedTicket(key)!
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
  if (!can(role, 'ticket.act')) return fail(403, 'forbidden', 'Viewers cannot change tickets.', 'Ask an owner or maintainer.')

  const finish = (event: OrchEvent | null) => ok({ ok: true, event, ticket: store.servedTicket(key)! })

  switch (a.action) {
    case 'answer': {
      const q = t.questions_state.find((x) => x.id === a.question)
      if (!q) return fail(404, 'question.not_found', `No question ${a.question} on ${key}`)
      if (q.state === 'answered') return fail(409, 'question.already_answered', `${q.id} was already answered by ${q.answer?.by}.`, 'The first valid answer wins.')
      if (q.to !== me && !can(role, 'question.answer.any')) return fail(403, 'question.not_addressee', `${q.id} is addressed to ${q.to}.`)
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
      // D57: before a claim, the needed connections are checked; auth or identity failures refuse.
      const blocked = store.conn.precheck(ws.id, key, 'claim')
      if (blocked) return fail(blocked.status, blocked.code, blocked.message, blocked.hint)
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
      if (!can(role, 'ticket.move')) return fail(403, 'forbidden', 'Only owners and maintainers move tickets.')
      if (a.status === 'done') return fail(409, 'human_only', 'Done is reached by a verdict', 'Give the verdict on the ticket page', false)
      return finish(store.append(key, { type: 'status.changed', to: a.status }))
    }
    case 'add_label': {
      if (!can(role, 'ticket.label')) return fail(403, 'forbidden', 'Only owners and maintainers label tickets.')
      const label = a.label?.trim().toLowerCase()
      if (!label) return fail(400, 'validation', 'Write a label first.')
      if (t.labels.includes(label)) return finish(null) // already there: nothing to record
      return finish(store.append(key, { type: 'labels.changed', add: [label] }))
    }
    default:
      return fail(400, 'validation', `Unknown action ${(a as { action: string }).action}`)
  }
}

const PRIORITY_ORDER: Priority[] = ['urgent', 'high', 'medium', 'low']
const BODY_ORDER: (keyof BodySections)[] = ['summary', 'context', 'requirements', 'out_of_scope', 'plan', 'decisions', 'verification', 'current_state']

/** First body-section hit for `needle` (lower case), as a snippet of at most 140 characters with the hit in «». */
function bodyMatch(body: BodySections, needle: string): TicketSummary['match'] {
  for (const section of BODY_ORDER) {
    const text = body[section]?.replace(/\s+/g, ' ')
    const at = text ? text.toLowerCase().indexOf(needle) : -1
    if (!text || at < 0) continue
    const hit = text.slice(at, at + Math.min(needle.length, 40))
    const from = Math.max(0, at - 50)
    const to = Math.min(text.length, at + hit.length + 40)
    return { section, snippet: `${from > 0 ? '…' : ''}${text.slice(from, at)}«${hit}»${text.slice(at + hit.length, to)}${to < text.length ? '…' : ''}` }
  }
  return undefined
}

/** `_` and `-` read as spaces, so "tariff code" finds tariff_code. */
const plainText = (s: string) => s.toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim()

/** A key typed without its dash (demo0041) still finds DEMO-0041. */
const squash = (s: string) => plainText(s).replace(/ /g, '')

function searchTickets(s: MockStore, ws: string, query: URLSearchParams): TicketSummary[] {
  const list = (k: string) => query.get(k)?.split(',').filter(Boolean)
  const statuses = list('status')
  const priorities = list('priority')
  const q = query.get('q')?.trim().toLowerCase()
  const type = query.get('type')
  const parent = query.get('parent')
  const label = query.get('label')
  const person = query.get('person')
  const needs = query.get('needs')
  const restricted = query.get('restricted')
  const sort = query.get('sort') ?? 'updated'
  const rows = s
    .listTickets(ws)
    .filter((t) => !statuses?.length || statuses.includes(t.status))
    .filter((t) => !priorities?.length || priorities.includes(t.priority))
    .filter((t) => !type || t.type === type)
    .filter((t) => !parent || t.parent === parent)
    .filter((t) => !label || t.labels.includes(label))
    .filter((t) => !person || t.people.owner === person || t.people.assignees.includes(person) || t.claim?.for === person)
    .filter((t) => !needs || (needs === 'me' ? t.turn.who === s.viewer : needs === 'agent' ? t.turn.who.startsWith('agent:') : t.turn.who === 'nobody'))
    .filter((t) => restricted === null || t.restricted === (restricted === 'true'))
    .map((t) => ({
      t,
      direct: !q || [t.key, t.title, ...t.labels].some((v) => plainText(v).includes(plainText(q))) || squash(t.key).includes(squash(q)),
      match: q ? bodyMatch(t.body, q) : undefined,
    }))
    .filter((r) => r.direct || r.match)
  const byUpdated = (a: TicketDocument, b: TicketDocument) => b.updated_at.localeCompare(a.updated_at)
  rows.sort((a, b) => {
    switch (sort) {
      case 'priority':
        return PRIORITY_ORDER.indexOf(a.t.priority) - PRIORITY_ORDER.indexOf(b.t.priority) || byUpdated(a.t, b.t)
      case 'key':
        return a.t.key.localeCompare(b.t.key)
      case 'status':
        return STATUSES.indexOf(a.t.status) - STATUSES.indexOf(b.t.status) || byUpdated(a.t, b.t)
      default:
        return byUpdated(a.t, b.t)
    }
  })
  const blocks = s.blockingCheck(ws)
  return rows.map(({ t, match }) => ({ ...s.summary(t, blocks), ...(match ? { match } : {}) }))
}

const ROLES: Role[] = ['owner', 'maintainer', 'member', 'viewer']
const GATES: GateName[] = ['requirements', 'plan', 'verify']

/** A stable, fake SHA256-style fingerprint derived from the workspace id. */
function fingerprint(id: string): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
  let h = 2166136261
  let out = ''
  for (let i = 0; i < 43; i++) {
    h = Math.imul(h ^ id.charCodeAt(i % id.length), 16777619) >>> 0
    out += alphabet[(h >>> 7) % 64]
  }
  return 'SHA256:' + out
}

/** Owner-only workspace settings. Every op is checked here, whatever the UI shows. */
function postSettings(store: MockStore, ctx: RouteContext): TransportResponse {
  const wsId = ctx.params.ws
  const ws = store.workspaces.find((w) => w.id === wsId)
  if (!ws) return fail(404, 'not_found', 'No such workspace')
  if (!can(store.roleIn(wsId, store.viewer), 'settings')) return fail(403, 'forbidden', 'Only owners change settings.', 'Ask an owner.')
  const b = ctx.body as SettingsRequest | null
  if (!b || typeof b !== 'object' || !('op' in b)) return fail(400, 'validation', 'Body must be {op, ...}')
  const done = () => ok({ ok: true, workspace: store.workspaceList().find((w) => w.id === wsId)! })
  const member = (person: string) => ws.members.find((m) => m.person === person)
  switch (b.op) {
    case 'rename': {
      const name = String(b.name ?? '').trim()
      if (name.length < 1 || name.length > 60) return fail(400, 'validation.name', 'The name needs 1 to 60 characters.')
      store.appendWs(wsId, { type: 'workspace.renamed', name })
      return done()
    }
    case 'member.add': {
      const person = String(b.person ?? '').trim()
      const name = String(b.name ?? '').trim()
      if (!person || !name) return fail(400, 'validation', 'Give the person an id and a name.')
      // A person id is `p_` plus lowercase letters, digits or _: never an actor spelling such as `addon:land` or `agent:s:p`.
      if (!/^p_[a-z0-9_]{1,40}$/.test(person)) return fail(400, 'validation.person', 'A person id is p_ followed by lowercase letters, digits or _ (at most 40).', 'For example p_ida.')
      if (!ROLES.includes(b.role) || b.role === 'owner') return fail(400, 'validation.role', 'New members can be maintainer, member or viewer.', 'Promote to owner afterwards.')
      if (member(person)) return fail(409, 'member.exists', `${person} is already a member.`)
      store.appendWs(wsId, { type: 'member.added', person, name, role: b.role })
      return done()
    }
    case 'member.role': {
      const m = member(b.person)
      if (!m) return fail(404, 'not_found', `No member ${b.person}`)
      if (!ROLES.includes(b.role)) return fail(400, 'validation.role', `Unknown role ${String(b.role)}`)
      if (m.role === 'owner' && b.role !== 'owner' && ws.members.filter((x) => x.role === 'owner').length === 1)
        return fail(409, 'member.last_owner', 'You cannot demote the last owner.', 'Make someone else an owner first.')
      if (m.role !== b.role) store.appendWs(wsId, { type: 'member.role_changed', person: b.person, role: b.role, from: m.role })
      return done()
    }
    case 'member.remove': {
      if (b.person === store.viewer) return fail(409, 'member.self', 'You cannot remove yourself.', 'Ask another owner.')
      const m = member(b.person)
      if (!m) return fail(404, 'not_found', `No member ${b.person}`)
      store.appendWs(wsId, { type: 'member.removed', person: b.person })
      return done()
    }
    case 'gate.policy': {
      if (!GATES.includes(b.gate)) return fail(400, 'validation.gate', `Unknown gate ${String(b.gate)}`)
      if (!APPROVER_GROUPS.some((a) => a.value === b.approvers)) return fail(400, 'validation.approvers', `Unknown approvers ${String(b.approvers)}`)
      if (!Number.isInteger(b.count) || b.count < 1 || b.count > 3) return fail(400, 'validation.count', 'A gate needs 1 to 3 approvals.')
      if (b.not != null && b.not !== 'assignees') return fail(400, 'validation', 'Only "assignees" can be excluded.')
      const why = unmeetablePolicy(ws, { approvers: b.approvers, count: b.count })
      if (why) return fail(409, 'gate.unmeetable', why, 'Add people to that group first, or lower the count.')
      store.appendWs(wsId, { type: 'gate.policy_set', gate: b.gate, approvers: b.approvers, count: b.count, not: b.not ?? null })
      return done()
    }
    case 'archive':
      if (b.prefix !== ws.prefix) return fail(400, 'validation.prefix', `Type ${ws.prefix} to confirm.`)
      return fail(409, 'cli_only', 'Archiving is CLI-only: `orch workspace archive`', 'Run it in a terminal; the dashboard cannot archive.')
    default:
      return fail(400, 'validation', `Unknown op ${(b as { op: string }).op}`)
  }
}

export function buildRouter(): MockRouter {
  const r = new MockRouter()
  // Workspace reads are for members only: 404 for an unknown workspace, 403 for someone who is not in it.
  const readOf = (path: string, h: (s: MockStore, c: Parameters<Parameters<MockRouter['add']>[2]>[1]) => TransportResponse) =>
    r.add('GET', path, (s, c) => {
      if (!s.workspaces.some((w) => w.id === c.params.ws)) return fail(404, 'not_found', 'No such workspace')
      if (!s.roleIn(c.params.ws, s.viewer)) return fail(403, 'forbidden', 'You are not a member of this workspace.', 'Ask an owner.')
      return h(s, c)
    })
  r.add('GET', '/api/me', (s) => ok(s.me()))
  r.add('GET', '/api/workspaces', (s) => ok(s.workspaceList()))
  // Who an owner can add: the registry plus the people already in the caller's workspaces. Owners only (names and emails).
  readOf('/api/workspaces/:ws/people', (s, c) => {
    if (!can(s.roleIn(c.params.ws, s.viewer), 'settings')) return fail(403, 'forbidden', 'Only owners see the people directory.', 'Ask an owner.')
    const people = [...peopleFixture]
    for (const w of s.workspaces) if (s.roleIn(w.id, s.viewer)) for (const m of w.members) if (!people.some((p) => p.person === m.person)) people.push({ person: m.person, name: m.name, email: '' })
    return ok(people)
  })
  readOf('/api/workspaces/:ws/today', (s, c) =>
    ok(s.today(c.params.ws)),
  )
  readOf('/api/workspaces/:ws/tickets', (s, c) => {
    return ok(searchTickets(s, c.params.ws, c.query))
  })
  r.add('POST', '/api/workspaces/:ws/tickets', (s, c) => {
    const wsId = c.params.ws
    const res = s.createFromRequest(wsId, c.body as NewTicketRequest | null)
    if (!res.ok) return fail(res.status, res.code, res.message, res.hint)
    return ok({ ok: true, ticket: res.ticket }, 201)
  })
  r.add('POST', '/api/workspaces/:ws/tickets/:key/undo-create', (s, c) => {
    const res = s.undoCreate(c.params.ws, c.params.key)
    if (!res.ok) return fail(res.status, res.code, res.message, res.hint)
    return ok({ ok: true })
  })
  readOf('/api/workspaces/:ws/views', (s, c) =>
    ok(s.views(c.params.ws)),
  )
  r.add('POST', '/api/workspaces/:ws/views', (s, c) => {
    const ws = c.params.ws
    if (!s.workspaces.some((w) => w.id === ws)) return fail(404, 'not_found', 'No such workspace')
    const role = s.roleIn(ws, s.viewer)
    if (!role) return fail(403, 'forbidden', 'Not a member of this workspace.')
    const b = c.body as { name?: string; shared?: boolean; params?: ViewParams } | null
    const name = b?.name?.trim()
    if (!name) return fail(400, 'validation', 'Name the view.')
    if (b?.shared && !can(role, 'view.share')) return fail(403, 'forbidden', 'Viewers can only save personal views.', 'Uncheck "Share with the workspace".')
    const n = s.wsEventsOf(ws).length + 1
    const view = `v_${n}`
    s.appendWs(ws, { type: 'view.saved', view, name, shared: !!b?.shared, params: b?.params ?? {} })
    return ok(s.views(ws).find((v) => v.id === view) satisfies SavedView | undefined)
  })
  r.add('POST', '/api/workspaces/:ws/views/:id/delete', (s, c) => {
    const ws = c.params.ws
    if (!s.workspaces.some((w) => w.id === ws)) return fail(404, 'not_found', 'No such workspace')
    if (!s.roleIn(ws, s.viewer)) return fail(403, 'forbidden', 'Not a member of this workspace.')
    const v = s.views(ws).find((x) => x.id === c.params.id)
    if (!v) return fail(404, 'not_found', 'No such view')
    if (v.owner !== s.viewer) return fail(403, 'forbidden', 'Only the owner of a view can delete it.')
    s.appendWs(ws, { type: 'view.deleted', view: v.id })
    return ok({ ok: true })
  })
  readOf('/api/workspaces/:ws/identity', (s, c) => {
    const ws = s.workspaces.find((w) => w.id === c.params.ws)!
    return ok({ uuid: ws.id, prefix: ws.prefix, created_at: '2026-08-14T07:42:10Z', key_fingerprint: fingerprint(ws.id), epoch: relayEpoch(s, ws.id) } satisfies WorkspaceIdentity)
  })
  r.add('POST', '/api/workspaces/:ws/settings', postSettings)
  // Every artifact of the tickets the viewer can see (visibility decided here, never by the client).
  readOf('/api/workspaces/:ws/artifacts', (s, c) => {
    const q = (k: string) => c.query.get(k) ?? undefined
    const num = (k: string) => (q(k) !== undefined && Number.isFinite(Number(q(k))) ? Number(q(k)) : undefined)
    return ok(listArtifacts(s, c.params.ws, { kind: q('kind'), ticket: q('ticket'), by: q('by'), since: q('since'), q: q('q'), page: num('page'), per: num('per') }))
  })
  // Relay & devices (simulated, see mocks/relay.ts). Members read; owners change (signed in the dashboard).
  readOf('/api/workspaces/:ws/relay', (s, c) => ok(relayState(s, c.params.ws)))
  r.add('POST', '/api/workspaces/:ws/relay', (s, c) => {
    const res = relayRequest(s, c.params.ws, c.body as RelayRequest | null)
    return res.ok ? ok(res.relay) : fail(res.status, res.code, res.message, res.hint)
  })
  readOf('/api/workspaces/:ws/cursor', (s, c) =>
    ok({ cursor: s.cursor(c.params.ws) }),
  )
  readOf('/api/workspaces/:ws/agents', (s, c) => ok(s.agents(c.params.ws)))
  readOf('/api/workspaces/:ws/agents/activity', (s, c) =>
    ok(s.agentActivity(c.params.ws)),
  )
  readOf('/api/workspaces/:ws/agents/launch', (s, c) => {
    const q = (k: string) => c.query.get(k) ?? undefined
    const res = s.launchPreview(c.params.ws, { ticket: q('ticket'), mode: q('mode'), harness: q('harness'), where: q('where') })
    return res.ok ? ok(res.launch) : fail(res.status, res.code, res.message, res.hint)
  })
  readOf('/api/workspaces/:ws/grants', (s, c) =>
    ok(s.grants(c.params.ws)),
  )
  // Issuing and revoking are human-only: the actor is always the viewer, a person.
  const person = (s: MockStore) => ({ kind: 'person', id: s.viewer, device: 'd_mac' }) as const
  r.add('POST', '/api/workspaces/:ws/grants', (s, c) => {
    if (!s.workspaces.some((w) => w.id === c.params.ws)) return fail(404, 'not_found', 'No such workspace')
    const b = c.body as { hours?: number; scope?: 'all' } | null
    const res = s.issueGrant(c.params.ws, { hours: Number(b?.hours), scope: b?.scope ?? 'all' }, person(s))
    return res.ok ? ok(res.grant, 201) : fail(res.status, res.code, res.message, res.hint)
  })
  r.add('POST', '/api/workspaces/:ws/grants/:id/revoke', (s, c) => {
    if (!s.workspaces.some((w) => w.id === c.params.ws)) return fail(404, 'not_found', 'No such workspace')
    const res = s.revokeGrant(c.params.ws, c.params.id, person(s))
    return res.ok ? ok(res.grant) : fail(res.status, res.code, res.message, res.hint)
  })
  // Skills, connections and the secrets file (D55–D57). Answers carry names only, never a secret value.
  readOf('/api/workspaces/:ws/skills', (s, c) => ok(s.conn.skills(c.params.ws)))
  readOf('/api/workspaces/:ws/connections', (s, c) => ok(s.conn.connections(c.params.ws)))
  readOf('/api/workspaces/:ws/secrets', (s, c) => {
    if (!atLeast(s.roleIn(c.params.ws, s.viewer), 'maintainer')) return fail(403, 'forbidden', 'Only owners and maintainers see the secrets file.', 'Names only; ask an owner.')
    return ok(s.conn.secretsFile(c.params.ws))
  })
  const wsExists = (s: MockStore, ws: string) => s.workspaces.some((w) => w.id === ws)
  r.add('POST', '/api/workspaces/:ws/connections/:name/check', (s, c) => {
    if (!wsExists(s, c.params.ws)) return fail(404, 'not_found', 'No such workspace')
    const t = (c.body as { trigger?: unknown } | null)?.trigger ?? 'on_demand'
    if (t !== 'on_demand' && t !== 'relogin') return fail(400, 'validation', 'trigger must be "on_demand" or "relogin"')
    const res = s.conn.check(c.params.ws, c.params.name, t, person(s))
    return res.ok ? ok(res.connection) : fail(res.status, res.code, res.message, res.hint)
  })
  r.add('POST', '/api/workspaces/:ws/doctor', (s, c) => {
    if (!wsExists(s, c.params.ws)) return fail(404, 'not_found', 'No such workspace')
    const res = s.conn.doctor(c.params.ws, person(s))
    return res.ok ? ok(res.report) : fail(res.status, res.code, res.message, res.hint)
  })
  r.add('POST', '/api/workspaces/:ws/skills/:name/grant', (s, c) => {
    if (!wsExists(s, c.params.ws)) return fail(404, 'not_found', 'No such workspace')
    const res = s.conn.grant(c.params.ws, c.params.name, c.body, person(s))
    return res.ok ? ok(res.skill) : fail(res.status, res.code, res.message, res.hint)
  })
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
  readOf('/api/workspaces/:ws/addons', (s, c) =>
    ok(s.workspaceAddons(c.params.ws)),
  )
  // Decisions and the catalog sit outside /addons/:name, so no addon name can collide with them.
  readOf('/api/workspaces/:ws/addon-catalog', (s, c) =>
    ok(s.workspaceCatalog(c.params.ws)),
  )
  r.add('POST', '/api/workspaces/:ws/addons/:name', (s, c) => {
    const b = c.body as AddonOpRequest | null
    if (!b || typeof b !== 'object' || !('op' in b)) return fail(400, 'validation', 'Body must be {op, ...}')
    const res = s.addonOp(c.params.ws, c.params.name, b, person(s))
    return res.ok ? ok(res.addon) : fail(res.status, res.code, res.message, res.hint)
  })
  readOf('/api/workspaces/:ws/addon-decisions', (s, c) =>
    ok(s.addonDecisions(c.params.ws)),
  )
  readOf('/api/workspaces/:ws/addons/:name/state', (s, c) => {
    // `?ticket=KEY`: per-ticket data for that ticket only (ticket panels). Same answers as the ticket routes.
    const ticket = c.query.get('ticket') ?? undefined
    if (ticket !== undefined) {
      if (!s.hasTicket(ticket) || s.workspaceOf(ticket)?.id !== c.params.ws) return fail(404, 'not_found', `No ticket ${ticket}`)
      if (!s.isVisible(ticket)) return fail(404, 'not_visible', `No ticket ${ticket}`, 'The ticket is restricted to other people.')
    }
    if (!s.addons.some((a) => a.name === c.params.name)) return fail(404, 'not_found', 'No such addon')
    if (!addonActive(s.workspaces.find((w) => w.id === c.params.ws), c.params.name))
      return fail(409, 'addon.inactive', `${c.params.name} is not active in this workspace.`, 'Enable it, or grant its capabilities, in Settings > Addons.')
    const v = s.addonStateView(c.params.ws, c.params.name, ticket)
    return v ? ok(v) : fail(404, 'not_found', 'No such addon')
  })
  r.add('POST', '/api/workspaces/:ws/addons/:name/actions/:id', (s, c) => {
    const addon = s.addons.find((a) => a.name === c.params.name)
    if (!addon) return fail(404, 'not_found', 'No such addon')
    const res = s.runAddon(c.params.ws, addon.name, c.params.id, (c.body ?? {}) as Record<string, unknown>)
    if (res && !res.ok) return fail(res.status, res.code, res.message, res.hint)
    return res ? ok(res) : fail(404, 'not_found', `Addon ${addon.name} has no action ${c.params.id}`)
  })
  r.add('POST', '/api/dev/reset', (s, c) => {
    const dataset = (c.body as { dataset?: unknown } | null)?.dataset
    if (dataset !== undefined && dataset !== 'normal' && dataset !== 'busy') return fail(400, 'validation', 'dataset must be "normal" or "busy"')
    s.reset(dataset, true)
    return ok({ ok: true })
  })
  r.add('GET', '/api/dev/dataset', (s) => ok({ dataset: s.dataset }))
  r.add('POST', '/api/dev/relay/:ws', (s, c) => {
    const res = relaySim(s, c.params.ws, c.body as RelaySimRequest | null)
    return res.ok ? ok(res.relay) : fail(res.status, res.code, res.message, res.hint)
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
