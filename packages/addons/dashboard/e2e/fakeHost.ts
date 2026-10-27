// A fake dashboard server for the browser spec of the Add-ons page (A10): it serves the production build (`dist/`) and answers
// the dashboard server's /api routes from plain data, including the marketplace host ops (mocked: the Python side is G3's).
// Every request is recorded in `calls`, so the spec can prove what the page asked for (the staged id in the hand-off) and that
// nothing was signed by the page. The hand-off requests carry hashes that recompute, like the real server's.
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fingerprintOf, reviewDigestOf, type HandoffEvent, type HandoffRequest } from '../src/api/handoff'

export const WS = 'a'.repeat(32)
export const OWNER = 'p_' + 'a'.repeat(32)
export const STAGE = 'ms_' + '1'.repeat(32)
export const STAGE_UPDATE = 'ms_' + '2'.repeat(32)
export const STAGE_WIDE = 'ms_' + '3'.repeat(32)
export const CODE = '7F3K-Q2MA'

export interface Call { method: string; path: string; body?: Record<string, any> }

const fixture = (n: string) => JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, '../src/test/fixtures/server-m2', n), 'utf8'))
const MIME: Record<string, string> = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.json': 'application/json', '.woff2': 'font/woff2', '.png': 'image/png' }
const FEATURES = ['identity', 'today', 'tickets', 'board', 'ticket', 'changes', 'evidence', 'settings', 'members', 'settings_read', 'grant_list', 'read_ext2', 'actions', 'market']

const entry = (id: string, version: string, over: Record<string, unknown> = {}) => ({
  id, version, capabilities: [], revoked: false, publisher: null, from_marketplace: { summary: `${id} summary` }, installed: null, update: false, ...over,
})
const OFFICIAL = 'https://github.com/severinlindenmann/orch-marketplace'
const ACME = 'https://github.com/acme/orch-market'

export function marketList() {
  return {
    markets: [
      {
        url: OFFICIAL, class: 'official', pinned: false, index_commit: 'a'.repeat(40), last_fetch: 1_790_000_000, cache: 'ok', version: 7, verified: true, stale: false, expires_at: null,
        entries: 3, revoked: 0, from_marketplace: { name: 'orch-marketplace', title: 'Official' }, allowed_by: 'orch', publisher_key: 'known',
        addons: [
          entry('jira', '1.2.0', { capabilities: ['network'], from_marketplace: { summary: 'Link tickets to Jira issues and show their status.' }, publisher: 'SHA256:7F3Aexample' }),
          entry('github', '1.2.0', { capabilities: ['network'], from_marketplace: { summary: 'Read pull requests and checks on tickets.' }, installed: { version: '1.1.0', state: 'active' }, update: true }),
          entry('changelog', '0.3.1', { from_marketplace: { summary: 'Writes a changelog line on every finished feature ticket.' }, installed: { version: '0.3.1', state: 'disabled' } }),
        ],
      },
      {
        url: ACME, class: 'community', pinned: true, index_commit: 'b'.repeat(40), last_fetch: 1_790_000_000, cache: 'ok', version: 2, verified: false, stale: false, expires_at: null,
        entries: 1, revoked: 0, from_marketplace: { name: 'acme-market', title: 'ACME internal' }, allowed_by: 'you', added_by: 'Sev', publisher_key: 'new',
        addons: [entry('deploy-preview', '1.0.0', { capabilities: ['network', 'git_push'], from_marketplace: { summary: 'Pushes a preview branch and posts the link.' } })],
      },
    ],
  }
}

export function installed() {
  return {
    addons: [
      { name: 'github', version: '1.1.0', package_sha256: 'sha256:' + '1'.repeat(64), origin: OFFICIAL, commit: 'c'.repeat(40), installed_at: 1_789_000_000, state: 'active', publisher: 'SHA256:7F3Aexample', available: { version: '1.2.0', package_sha256: 'sha256:' + '2'.repeat(64), capabilities_added: [], revoked: false, stale: false } },
      { name: 'changelog', version: '0.3.1', package_sha256: 'sha256:' + '3'.repeat(64), origin: OFFICIAL, commit: 'c'.repeat(40), installed_at: 1_789_000_000, state: 'disabled', publisher: null, available: null },
      { name: 'schedules', version: '2.0.0', package_sha256: 'sha256:' + '4'.repeat(64), origin: OFFICIAL, commit: 'c'.repeat(40), installed_at: 1_789_000_000, state: 'active', publisher: null, available: { version: '2.1.0', package_sha256: 'sha256:' + '5'.repeat(64), capabilities_added: ['spawn_agent'], revoked: false, stale: false } },
    ],
  }
}

const market = (url: string) => ({ url, class: url === OFFICIAL ? 'official' : 'community', commit: 'd'.repeat(40), source_note: 'github:example/source @ 9f2c1e7', publisher: { fingerprint: 'SHA256:7F3Aexample', tofu: url === OFFICIAL ? 'known' : 'new' }, index_version: 7, stale: false, summary: 'summary' })

export function stage(id: string, update: boolean) {
  const base = { expires_at: new Date(Date.now() + 600_000).toISOString(), package_sha256: 'sha256:' + (update ? '2' : '4').repeat(64), market: market(OFFICIAL) }
  if (id === 'schedules') {
    return { ...base, stage: STAGE_WIDE, name: 'schedules', version: '2.1.0', review: ['Starts agents on a timetable (NEW)', 'Adds a field "Next run" to tickets', 'Reads your tickets'], deps: { count: 2, verified: true },
      update: { from: { version: '2.0.0', package_sha256: 'sha256:' + '4'.repeat(64), commit: 'c'.repeat(40) }, changes: ['version 2.0.0 -> 2.1.0', 'capability + spawn_agent'], added: { capabilities: ['spawn_agent'], fields: ['Next run'], sections: [], needs: [], artifact_kinds: [] }, removed: { capabilities: [], fields: [], sections: [], needs: [], artifact_kinds: [] }, widened: true, publisher: 'same' } }
  }
  if (update) {
    return { ...base, stage: STAGE_UPDATE, name: id, version: '1.2.0', review: ['Connects to github.com', 'Uses your GitHub token'], deps: { count: 0, verified: true },
      update: { from: { version: '1.1.0', package_sha256: 'sha256:' + '1'.repeat(64), commit: 'c'.repeat(40) }, changes: ['version 1.1.0 -> 1.2.0', 'section "Checks" renamed'], added: { capabilities: [], fields: [], sections: [], needs: [], artifact_kinds: [] }, removed: { capabilities: [], fields: ['Old field'], sections: [], needs: [], artifact_kinds: [] }, widened: false, publisher: 'same' } }
  }
  return { ...base, stage: STAGE, name: id, version: '1.2.0', review: ['Connects to your Jira site', 'Uses your Jira token', 'Adds fields to tickets'], deps: { count: 3, verified: true }, update: null }
}

/** A hand-off request for `addon.grant` of a staged package, as the dashboard server would send it. */
export function grantRequest(name: string, staged: string, serial: number): HandoffRequest {
  const id = '01J9ZP00000000000000000' + String(serial).padStart(3, '0')
  const event = { type: 'addon.granted', name, staged }
  const events: HandoffEvent[] = [{ v: 2, hash_v: 1, roster_v: 3, based_on: 'sha256:' + 'a'.repeat(64), id, actor: { kind: 'person', id: OWNER, device: null }, auth: null, ...event } as unknown as HandoffEvent]
  const { type, ...payload } = event
  const material = { kind: 'payload' as const, ticket: null, review: [{ event_id: id, type, payload, derived: {} }] }
  const digest = reviewDigestOf(material)
  return {
    id: 'hr_' + '0'.repeat(22) + String(serial).padStart(4, '0'), code: CODE, expires_at: new Date(Date.now() + 600_000).toISOString().replace(/\.\d+Z$/, 'Z'), state: 'prepared', op: 'addon.grant', key: null,
    fingerprint: fingerprintOf(digest, events, 'addon.grant'), review_digest: digest, gate_hash: null, material, events,
    origin: { uid: 501, pid: 4312, client: 'orch-dashboard', created_at: new Date().toISOString() },
  } as unknown as HandoffRequest
}

/** The signer's View of a market grant, as core renders it: the signed event line by line, then the marketplace block. */
function clickView(name: string) {
  return {
    risk: 'Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.', op: 'addon.grant',
    workspace: { id: WS, name: 'Dev workspace', prefix: 'DEMO' }, fingerprint: '5b9f4bcf5cca',
    lines: [
      `workspace: Dev workspace (DEMO) ${WS}`, `op: addon.grant: grant addon ${name}`, '', '--- event 1 of 1: addon.granted ---', 'artifact_kinds: []', 'capabilities: ["network"]', 'fields: {}',
      `name: ${name}`, 'package_sha256: sha256:' + '4'.repeat(64), 'sections: []', 'version: 1.2.0',
      'marketplace (per host, not signed): https://github.com/severinlindenmann/orch-marketplace (official)', 'marketplace commit (per host, not signed): ' + 'd'.repeat(40),
      'source (per host, not signed): source as claimed by the marketplace', 'publisher (per host, not signed): SHA256:7F3Aexample, first use: remembered when you sign', 'index version (per host, not signed): 7',
    ],
    checks: [], confirm: { kind: 'click' }, expires_at: new Date(Date.now() + 600_000).toISOString().replace(/\.\d+Z$/, 'Z'), auth: 'convenience', device: 'd_' + '1'.repeat(32), artifacts: null,
  }
}

export class FakeServer {
  calls: Call[] = []
  requests = new Map<string, HandoffRequest>()
  /** Hand-off ids "signed in the terminal": the test flips this (the mocked human.submit). */
  submitted = new Set<string>()
  installedNames = new Set<string>()
  server: http.Server
  port = 0
  /** One click switched on for the workspace (V3-D38): the page signs through /click/* instead of the terminal hand-off. */
  clickOn = false
  clickSigned = new Set<string>()
  /** Settings switches (run 13 UE): Claude Code folder trust and the records timer. */
  trustOn = false
  historyOn = false
  trustParent = ''
  clickRequests = new Map<string, { name: string; staged: string }>()
  constructor(private dist: string) {
    this.server = http.createServer((req, res) => void this.handle(req, res))
  }
  async start() {
    await new Promise<void>((r) => this.server.listen(0, '127.0.0.1', r))
    this.port = (this.server.address() as { port: number }).port
    return `http://127.0.0.1:${this.port}`
  }
  stop() { this.server.close() }
  /** The mocked `human.submit`: the person approved in the terminal. */
  signLast() { const id = [...this.requests.keys()].pop(); if (id) this.submitted.add(id) }
  posts = (suffix: string) => this.calls.filter((c) => c.method === 'POST' && c.path.endsWith(suffix))

  private async handle(req: http.IncomingMessage, res: http.ServerResponse) {
    const url = new URL(req.url ?? '/', 'http://x')
    const method = req.method ?? 'GET'
    const raw = await new Promise<string>((r) => { let s = ''; req.on('data', (c) => (s += c)); req.on('end', () => r(s)) })
    const body = raw ? (JSON.parse(raw) as Record<string, any>) : undefined
    const json = (v: unknown, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(v)) }
    const p = url.pathname
    if (p === '/auth/session') return json({ token: 'demo-not-a-real-token' })
    if (!p.startsWith('/api/')) {
      let f = path.join(this.dist, p)
      if (!f.startsWith(this.dist) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(this.dist, 'index.html')
      res.writeHead(200, { 'content-type': MIME[path.extname(f)] ?? 'application/octet-stream' })
      return res.end(fs.readFileSync(f))
    }
    this.calls.push({ method, path: p + url.search, body })
    if (p === '/api/events') { // the live stream: ready, then silence (heartbeat comments keep it open)
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' })
      res.write('event: ready\ndata: []\n\n')
      const t = setInterval(() => res.write(': hb\n\n'), 5000)
      res.on('close', () => clearInterval(t))
      return
    }
    if (p === '/api/host') return json({})
    const features = this.clickOn ? [...FEATURES, 'click'] : FEATURES
    if (p === '/api/session') return json({ features, workspaces: { [WS]: features } })
    if (p === '/api/workspaces') return json([{ id: WS, prefix: 'DEMO', name: 'Dev workspace', members: [{ person: OWNER, name: 'Sev', role: 'owner', devices: 1 }], needs_you: 0, host_state: 'ok', features, click: this.clickOn ? { custody: 'convenience', enabled: true, available: true, switchable: true } : null }])
    if (p === '/api/me') return json([{ person: OWNER, name: 'Sev', role: 'owner', tier: 'A', workspace: WS }])
    const m = /^\/api\/w\/[0-9a-f]{32}\/(.*)$/.exec(p)
    const rest = m?.[1] ?? ''
    if (rest === 'market' && method === 'GET') {
      const l = marketList() // a package whose grant was signed shows as installed (Off until turned on)
      for (const m of l.markets) for (const a of m.addons) if (this.installedNames.has(a.id) && !a.installed) a.installed = { version: a.version, state: 'disabled' }
      return json(l)
    }
    if (rest === 'market/installed' && method === 'GET') return json(installed())
    if (rest === 'market/stage' && method === 'POST') return json(stage(String(body?.id), !!body?.update))
    if (rest.startsWith('market/stage/') && method === 'DELETE') return json({ removed: true })
    if (rest === 'market/uninstall' && method === 'POST') return json({ name: body?.name, removed: true })
    if (rest === 'handoff' && method === 'POST') {
      const r = grantRequest(String(body?.name), String(body?.staged), this.requests.size + 1)
      this.requests.set(r.id, r)
      return json(r)
    }
    const h = /^handoff\/(hr_[0-9A-Z]+)$/.exec(rest)
    if (h && method === 'GET') {
      const r = this.requests.get(h[1]); const signed = this.submitted.has(h[1])
      if (signed) this.installedNames.add(String((r?.events[0] as any)?.name))
      return r ? json({ id: r.id, state: signed ? 'submitted' : 'prepared', op: r.op, key: null, expires_at: r.expires_at, event_ids: signed ? ['01J9ZP0000000000000000SIGN'] : [], seq: signed ? 7 : null }) : json({ ok: false, error: { code: 'not_found', message: 'x', retryable: false } }, 404)
    }
    if (h && method === 'DELETE') return json({ id: h[1], state: 'cancelled' })
    if (rest === 'click/prepare' && method === 'POST' && this.clickOn) {
      const id = 'hr_' + '0'.repeat(22) + String(this.clickRequests.size + 1).padStart(4, '0')
      this.clickRequests.set(id, { name: String(body?.name), staged: String(body?.staged) })
      return json({ request_id: id, expires_at: new Date(Date.now() + 600_000).toISOString().replace(/\.\d+Z$/, 'Z'), op: 'addon.grant', confirm: 'click' })
    }
    const c = /^click\/(view|sign|state|cancel)$/.exec(rest)
    if (c && method === 'POST' && this.clickOn) {
      const rid = String(body?.request_id); const r = this.clickRequests.get(rid)
      if (!r) return json({ ok: false, error: { code: 'not_found', message: 'x', retryable: false } }, 404)
      if (c[1] === 'view') return json({ view_digest: 'sha256:' + 'e'.repeat(64), view: clickView(r.name) })
      if (c[1] === 'sign') { this.clickSigned.add(rid); this.installedNames.add(r.name); return json({ state: 'submitted', duplicate: false, seq: 7 }) }
      if (c[1] === 'cancel') return json({ id: rid, state: 'cancelled' })
      return json({ id: rid, state: this.clickSigned.has(rid) ? 'submitted' : 'prepared' })
    }
    if (rest === 'me/sign_default') return json(this.clickOn ? { method: 'click', is_default: true, allowed: ['terminal', 'click'] } : { method: 'terminal', is_default: true, allowed: ['terminal'] })
    if (rest === 'identity') return json({ uuid: WS, prefix: 'DEMO', genesis: 'sha256:' + 'f'.repeat(64), epoch: 3, key_fingerprint: 'AB12 CD34', created_at: null, tier: 'A', me: { person: OWNER, name: 'Sev', role: 'owner', tier: 'A', workspace: WS } })
    if (rest === 'settings') return json({ gates: {}, sign_methods: this.clickOn ? ['terminal', 'click'] : ['terminal'], grant_hours: 8, claim_ttl_min: 60, lease_ttl_min: 60 })
    if (rest === 'claude-trust' && method === 'GET') return json({ available: true, on: this.trustOn, offered: !this.trustParent, why_not: this.trustParent ? 'A folder above this one has its own Claude Code settings.' : '', parent_config: this.trustParent })
    if (rest === 'claude-trust' && method === 'POST') { this.trustOn = true; return json({ on: true }) }
    if (rest === 'records' && method === 'GET') return json({ available: true, configured: true, repo: { state: 'ok', problem: null, problem_hidden: false }, pending: null, last: null, last_noted: null, auto: this.historyOn ? { on: true, every_min: 15, push: false } : null, remote: null })
    if (rest === 'records/auto-on' && method === 'POST') { this.historyOn = true; return json({ ok: true }) }
    if (rest === 'records/auto-off' && method === 'POST') { this.historyOn = false; return json({ ok: true }) }
    if (rest === 'members') return json([{ person: OWNER, name: 'Sev', role: 'owner', devices: [] }])
    if (rest === 'today') return json(fixture('today.json'))
    if (rest === 'board' || rest === 'tickets') return json({ ...fixture('board-page.json'), next_page: null })
    return json({ ok: false, error: { code: 'not_found', message: 'No such route in the fake', retryable: false } }, 404)
  }
}
