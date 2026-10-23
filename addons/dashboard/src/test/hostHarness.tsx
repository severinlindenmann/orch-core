// A fake dashboard server for the production app (M4): the fetch the app makes is answered here from plain data, so a
// test reads exactly what the page asked for and what it posted. The hand-off requests it returns have hashes that
// recompute, like the real server's (`api/handoff.ts`), so the app accepts them.
import { QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'
import { setFeatures } from '@/api/features'
import { fingerprintOf, reviewDigestOf, type HandoffEvent, type HandoffRequest } from '@/api/handoff'
import type { HostGrant, HostSettings, RosterMember } from '@/api/settings'
import type { Role } from '@/api/types'
import board from '@/test/fixtures/server-m2/board-page.json'
import changes from '@/test/fixtures/server-m2/changes.json'
import events from '@/test/fixtures/server-m2/events.json'
import ticket from '@/test/fixtures/server-m2/ticket.json'
import today from '@/test/fixtures/server-m2/today.json'
import { queryClient, Shell } from '@/production/Shell'

export const OWNER = 'p_' + 'a'.repeat(32)
export const MARA = 'p_' + 'b'.repeat(32)
export const TOM = 'p_' + 'c'.repeat(32)
export const CODE = '7F3K-Q2MA'
export const READS = ['identity', 'today', 'tickets', 'board', 'ticket', 'changes', 'evidence', 'settings', 'members', 'settings_read', 'grant_list', 'read_ext2']
export const DO = ['do:settings.set', 'do:policy.set', 'do:member.add', 'do:member.role', 'do:member.remove', 'do:grant.revoke']

export const policy = (over: Partial<HostSettings['gates'] extends infer G ? (G extends Record<string, infer P> ? P : never) : never> = {}) => ({
  approvers: ['owner'],
  count: 1,
  not: [] as string[],
  applies: 'all' as 'all' | 'off' | string[],
  independent: false,
  ...over,
})

export interface FakeWs {
  id: string
  prefix: string
  name: string
  host_state: string
  /** The host's reason it cannot serve this workspace (the dashboard server sends it with a non-ok host_state). */
  host_message?: string
  features: string[]
  /** The signed-in person (this dashboard server's person) in this workspace. */
  me: string
  role: Role
  settings: HostSettings
  roster: RosterMember[]
  grants: HostGrant[]
  /** The title the workspace's DEMO-0001 has (two workspaces may share a key and differ in everything else). */
  title: string
  /** What the signer says about the workspace (the server adds the `click` feature when it can sign). */
  click?: { custody: 'convenience' | 'passphrase' | 'none'; enabled: boolean; available: boolean; problem?: string } | null
  /** `whoami.runner` as the server sends it (null: the host says nothing). */
  runner?: { connected: boolean; enabled: boolean; terminals: boolean } | null
  /** What `GET /quick` answers (M6); absent: the route is not served. */
  quick?: unknown | ((q: URLSearchParams) => unknown)
  /** What `GET /records` answers (M6); absent: the route is not served. */
  records?: unknown
  /** What `GET /add-ons` answers (R2-2). */
  addOns?: import('@/api/addOnSwitches').AddOnsData
  /** What `GET /versions` answers (R3-A1); absent: the route is not served. */
  versions?: unknown
}

export function settingsOf(over: Partial<HostSettings> = {}): HostSettings {
  return {
    available: true,
    grant_hours: 8,
    claim_ttl_min: 120,
    lease_ttl_min: 60,
    defaults: ['claim_ttl_min', 'lease_ttl_min'],
    gates: {
      requirements: policy(),
      plan: policy(),
      verify: policy({ approvers: ['reviewers'], not: ['assignees'] }),
      code: policy({ approvers: ['maintainer', 'owner'], not: ['assignees'], applies: 'off', independent: true }),
    },
    repos: [{ name: 'code', default_branch: 'main', remote: 'https://git.example.test/a/code', path: '/srv/code', check: { state: 'present', branch: 'main' } }],
    repos_more: false,
    settings_at: null,
    policy_at: null,
    ...over,
  }
}

export const device = (id: string) => ({ id, auth: null, scopes: ['look', 'decide', 'operate', 'type'], valid: true })
export const roster = (): RosterMember[] => [
  { person: OWNER, name: 'Sev', role: 'owner', devices: [device('d_' + '1'.repeat(32))] },
  { person: MARA, name: 'Mara', role: 'maintainer', devices: [device('d_' + '2'.repeat(32))] },
  { person: TOM, name: 'Tom', role: 'member', devices: [device('d_' + '3'.repeat(32))] },
]

export const grant = (id: string, person: string, over: Partial<HostGrant> = {}): HostGrant => ({
  id,
  person,
  scope: 'all',
  verbs: 'agent',
  issued_at: '2026-10-12T08:00:00Z',
  expires_at: '2099-10-12T16:00:00Z',
  revoked: null,
  sessions: 0,
  session_ids: [],
  sessions_more: null,
  ...over,
})
export const GRANT_A = 'gr_' + '0'.repeat(25) + 'A'
export const GRANT_B = 'gr_' + '0'.repeat(25) + 'B'

export function workspace(over: Partial<FakeWs> & Pick<FakeWs, 'id'>): FakeWs {
  return {
    prefix: 'DEMO',
    name: 'Dev workspace',
    host_state: 'ok',
    features: [...READS, ...DO],
    me: OWNER,
    role: 'owner',
    settings: settingsOf(),
    roster: roster(),
    grants: [grant(GRANT_A, OWNER), grant(GRANT_B, TOM, { scope: 'workable' })],
    title: 'Real P1 ticket',
    ...over,
  }
}

const ENVELOPE = { v: 2, hash_v: 1, roster_v: 3, based_on: 'sha256:' + 'a'.repeat(64) }
let serial = 0

/** A hand-off request for a workspace event, as the dashboard server would send it (hashes recompute). */
export function workspaceRequest(op: string, event: Record<string, unknown>, opts: { derived?: Record<string, unknown>; before?: Record<string, unknown>; person?: string } = {}): HandoffRequest {
  serial += 1
  const id = '01J9ZP00000000000000000' + String(serial).padStart(3, '0')
  const events: HandoffEvent[] = [{ ...ENVELOPE, id, actor: { kind: 'person', id: opts.person ?? OWNER, device: null }, auth: null, ...event } as unknown as HandoffEvent]
  const { type, ...payload } = event
  const material = { kind: 'payload' as const, ticket: null, review: [{ event_id: id, type: type as string, payload, derived: opts.derived ?? {} }], ...(opts.before ? { before: opts.before } : {}) }
  const digest = reviewDigestOf(material)
  return {
    id: 'hr_' + '0'.repeat(22) + String(serial).padStart(4, '0'),
    code: CODE,
    expires_at: new Date(Date.now() + 600_000).toISOString().replace(/\.\d+Z$/, 'Z'),
    state: 'prepared',
    op,
    key: null,
    fingerprint: fingerprintOf(digest, events, op),
    review_digest: digest,
    gate_hash: null,
    material,
    events,
    origin: { uid: 501, pid: 4312, client: 'orch-dashboard', created_at: new Date().toISOString() },
  }
}

export interface Call {
  method: string
  path: string
  body?: Record<string, unknown>
}

/** What the fake host makes of a posted action: the event the real host would prepare for it. */
function prepare(body: Record<string, unknown>): HandoffRequest {
  switch (body.action) {
    case 'settings_set': {
      const set = Object.fromEntries(['grant_hours', 'claim_ttl_min', 'lease_ttl_min'].filter((k) => k in body).map((k) => [k, body[k]]))
      return workspaceRequest('settings.set', { type: 'settings.changed', set }, { before: Object.fromEntries(Object.keys(set).map((k) => [k, 'HOST-BEFORE-VALUE'])) })
    }
    case 'repo_set': {
      const { path, remote, default_branch } = body as Record<string, string | undefined>
      const entry = { path, ...(remote ? { remote } : {}), ...(default_branch ? { default_branch } : {}) }
      return workspaceRequest('settings.set', { type: 'settings.changed', set: { repos: { [body.name as string]: entry } } })
    }
    case 'repo_remove':
      return workspaceRequest('settings.set', { type: 'settings.changed', set: { repos: { [body.name as string]: null } } })
    case 'policy_set':
      return workspaceRequest('policy.set', { type: 'policy.changed', gates: { [body.gate as string]: body.policy } })
    case 'member_add':
      return workspaceRequest(
        'member.add',
        { type: 'member.added', person: 'p_' + 'd'.repeat(32), name: body.name, role: body.role, pk_pub: body.pk, device_cert: JSON.parse(body.cert as string) },
        { derived: { cert_device: 'd_' + '4'.repeat(32), cert_person: 'p_' + 'd'.repeat(32), cert_scopes_max: ['look', 'decide', 'operate', 'type'], cert_expires: null } },
      )
    case 'new':
      return workspaceRequest('new', { type: 'ticket.created', ticket: { key: 'DEMO-0099', type: body.type ?? 'feature', title: body.title } })
    case 'quick_start':
    case 'new_sequence':
      return workspaceRequest('new', { type: 'ticket.created', ticket: { key: 'DEMO-0099', type: body.action === 'quick_start' ? 'chore' : body.type, title: body.title } })
    case 'details':
      return workspaceRequest('set', { type: 'ticket.updated', ticket: body.key, labels: body.labels })
    case 'close':
      return workspaceRequest('close', { type: 'ticket.closed', ticket: body.key })
    case 'move':
      return workspaceRequest('move', { type: 'ticket.moved', from: 'open', to: body.to, ...(body.reason ? { reason: body.reason } : {}) })
    case 'member_role':
      return workspaceRequest('member.role', { type: 'role.changed', person: body.person, role: body.role })
    case 'member_remove':
      return workspaceRequest('member.remove', { type: 'member.removed', person: body.person })
    case 'icon_set':
      return workspaceRequest('settings.set', { type: 'settings.changed', set: { icon: body.icon } })
    case 'models_set':
      return workspaceRequest('settings.set', { type: 'settings.changed', set: { models: Object.keys(body.models as object).length ? body.models : null } })
    case 'publish_setup':
      return workspaceRequest('settings.set', { type: 'settings.changed', set: { publish: { server: body.server, share: body.share } } })
    case 'publish_share':
      return workspaceRequest('publish.share', { type: 'log.added', text: `Publish approval: share files outside this computer\nStaged copy: ${String(body.staged_id)}` })
    case 'addon_grant':
      return workspaceRequest('addon.grant', { type: 'addon.granted', name: body.name })
    case 'addon_disable':
      return workspaceRequest('addon.disable', { type: 'addon.disabled', name: body.name })
    case 'grant_revoke':
      return workspaceRequest('grant.revoke', { type: 'grant.revoked', grant: body.grant, ...(body.reason ? { reason: body.reason } : {}) })
    default:
      throw new Error(`the fake host does not prepare ${String(body.action)}`)
  }
}

export class FakeHost {
  calls: Call[] = []
  workspaces: FakeWs[]
  requests = new Map<string, HandoffRequest>()
  cancelled: string[] = []
  /** Click-to-sign: what the fake signer was asked, and the Views it shows (by request id). */
  clickRequests = new Map<string, { op: string; confirm: 'click' | 'word' }>()
  clickCancelled: string[] = []
  clickSigned: Record<string, unknown>[] = []
  clickCount = 0
  /** Hand-off request ids the person has signed in the terminal: the state read answers `submitted` with this key. */
  submitted = new Map<string, string>()
  /** A route a test overrides: return undefined to fall through. */
  override?: (path: string, method: string, body?: Record<string, unknown>) => Response | undefined

  constructor(workspaces: FakeWs[]) {
    this.workspaces = workspaces
  }

  running = () => this.workspaces.filter((w) => w.host_state === 'ok')
  find = (id: string) => this.workspaces.find((w) => w.id === id)

  features() {
    const union = new Set(this.running().flatMap((w) => w.features))
    return { features: [...union], workspaces: Object.fromEntries(this.workspaces.map((w) => [w.id, w.host_state === 'ok' ? w.features : []])) }
  }

  fetch = async (url: string, init?: RequestInit): Promise<Response> => {
    const method = init?.method ?? 'GET'
    const [path, query = ''] = url.split('?')
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined
    this.calls.push({ method, path: query ? `${path}?${query}` : path, body })
    const over = this.override?.(path, method, body)
    if (over) return over
    const json = (value: unknown, status = 200) => Response.json(value, { status })
    const missing = () => json({ ok: false, error: { code: 'not_found', message: 'No such route in the test', retryable: false } }, 404)
    if (path === '/api/session') return json(this.features())
    if (path === '/api/workspaces')
      return json(
        this.workspaces.map((w) =>
          w.host_state === 'ok'
            ? { id: w.id, prefix: w.prefix, name: w.name, members: w.roster.map((m) => ({ person: m.person, name: m.name, role: m.role, devices: m.devices.length })), needs_you: 0, host_state: 'ok', features: w.features, click: w.click ?? null, ...(w.runner !== undefined ? { runner: w.runner } : {}) }
            : { id: w.id, prefix: w.host_message ? w.prefix : '', name: w.id, members: [], needs_you: null, host_state: w.host_state, ...(w.host_message ? { host_message: w.host_message } : {}), features: [] },
        ),
      )
    if (path === '/api/me') return json(this.running().map((w) => ({ person: w.me, name: w.roster.find((m) => m.person === w.me)?.name ?? 'Sev', role: w.role, tier: 'A', workspace: w.id })))
    const m = /^\/api\/w\/([0-9a-f]{32})\/(.*)$/.exec(path)
    const ws = m && this.find(m[1])
    if (!m || !ws) return missing()
    if (ws.host_state !== 'ok') return json({ ok: false, error: { code: 'host.unavailable', message: 'host.unavailable', retryable: true } }, 503)
    const rest = m[2]
    if (rest === 'today') return json(today)
    if (rest === 'board' || rest === 'tickets') return json({ ...board, next_page: null })
    if (rest === 'tickets/DEMO-0001') return json({ ...ticket, title: ws.title })
    if (rest === 'tickets/DEMO-0001/events') return json(events)
    if (rest === 'tickets/DEMO-0001/changes') return json(changes)
    if (rest === 'identity') return json({ uuid: ws.id, prefix: ws.prefix, genesis: 'sha256:' + 'f'.repeat(64), epoch: 3, key_fingerprint: 'AB12 CD34', created_at: null, tier: 'A', me: { person: ws.me, name: 'Sev', role: ws.role, tier: 'A', workspace: ws.id }, ...(ws.runner !== undefined ? { runner: ws.runner } : {}) })
    if (rest === 'quick') return ws.quick === undefined ? missing() : json(typeof ws.quick === 'function' ? ws.quick(new URLSearchParams(query)) : ws.quick)
    if (rest === 'records') return ws.records === undefined ? missing() : json(ws.records)
    if (rest === 'settings') return json(ws.settings)
    if (rest === 'members') return json(ws.roster)
    if (rest === 'versions') return ws.versions === undefined ? missing() : json(ws.versions)
    if (rest === 'add-ons') return ws.addOns === undefined ? missing() : json(ws.addOns)
    if (rest === 'grants') return json(new URLSearchParams(query).get('active') === 'false' ? ws.grants : ws.grants.filter((g) => !g.revoked))
    if (rest === 'repos/check' && method === 'POST') {
      return String(body?.path).startsWith('/refused')
        ? json({ ok: false, error: { code: 'invalid.input', message: 'This folder is not a git repository.', retryable: false } }, 400)
        : json({ ok: true })
    }
    if (rest === 'handoff' && method === 'POST') {
      const request = prepare(body!)
      this.requests.set(request.id, request)
      return json(request)
    }
    if (rest === 'click/prepare' && method === 'POST') {
      const request = prepare(body!)
      const id = 'hr_' + String(++this.clickCount).padStart(26, '0')
      const confirm = request.op === 'policy.set' || request.op.startsWith('member.') ? 'word' : 'click'
      this.clickRequests.set(id, { op: request.op, confirm })
      return json({ request_id: id, expires_at: request.expires_at, op: request.op, confirm })
    }
    if (rest === 'click/view' && method === 'POST') {
      const got = this.clickRequests.get(String(body?.request_id))
      if (!got) return missing()
      return json({
        view_digest: 'sha256:' + '9'.repeat(64),
        view: {
          risk: 'Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.',
          op: got.op,
          workspace: { id: ws.id, name: ws.name, prefix: ws.prefix },
          fingerprint: '0123456789ab',
          lines: [`FROM-THE-SIGNER ${got.op}`],
          checks: [],
          confirm: got.confirm === 'word' ? { kind: 'word', word: `${ws.prefix}~${ws.id.slice(-8)}/POLICY` } : { kind: 'click' },
          expires_at: new Date(Date.now() + 600_000).toISOString(),
          auth: 'convenience',
          device: 'd_' + '1'.repeat(32),
          artifacts: null,
        },
      })
    }
    if (rest === 'click/sign' && method === 'POST') {
      this.clickSigned.push(body!)
      return json({ state: 'submitted', duplicate: false, key: 'DEMO-0001', seq: 5 })
    }
    if (rest === 'click/cancel' && method === 'POST') {
      this.clickCancelled.push(`${ws.id}:${String(body?.request_id)}`)
      return json({ id: body?.request_id, state: 'cancelled' })
    }
    const h = /^handoff\/(hr_[0-9A-Z]+)$/.exec(rest)
    if (h && method === 'GET') {
      const request = this.requests.get(h[1])
      const signedKey = this.submitted.get(h[1])
      return request ? json({ id: request.id, state: signedKey ? 'submitted' : 'prepared', op: request.op, key: signedKey ?? null, expires_at: request.expires_at, event_ids: signedKey ? ['01J9ZP0000000000000000SIGN'] : [], seq: signedKey ? 7 : null }) : missing()
    }
    if (h && method === 'DELETE') {
      this.cancelled.push(`${ws.id}:${h[1]}`)
      return json({ id: h[1], state: 'cancelled' })
    }
    return missing()
  }

  posts = () => this.calls.filter((c) => c.method === 'POST' && c.path.endsWith('/handoff'))
  deletes = () => this.calls.filter((c) => c.method === 'DELETE')
}

/** Installs the fake host as `fetch` for the production app, and cleans up after each test. */
export function useFakeHost(make: () => FakeWs[]) {
  const state: { host: FakeHost } = { host: new FakeHost([]) }
  beforeEach(() => {
    state.host = new FakeHost(make())
    vi.stubGlobal('fetch', vi.fn(state.host.fetch))
    localStorage.clear()
  })
  afterEach(() => {
    setFeatures(null)
    queryClient.clear()
    vi.unstubAllGlobals()
  })
  return state
}

export function show(path: string) {
  history.replaceState(null, '', path)
  return render(
    <QueryClientProvider client={queryClient}>
      <Shell />
    </QueryClientProvider>,
  )
}
