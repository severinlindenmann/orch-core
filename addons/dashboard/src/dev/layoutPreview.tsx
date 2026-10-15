// DEV ONLY (reached through the `__layout/$name` route, whose component loads only when `import.meta.env.DEV`; production redirects to /).
//
// The quick tasks page, the Settings "Records" tab and the "Dashboard execution" card are drawn only on a real host
// (features `quick`, `records`, `runner`), so the layout guard, which runs against the mock app, never sees them. This
// draws the REAL components with fixture answers: the mock `api` methods they call are replaced here, per scenario, by
// functions that return plain data. Nothing of this is in a production build and nothing in it reaches a host.
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { fingerprintOf, reviewDigestOf, type HandoffEvent, type HandoffRequest, type HandoffState } from '@/api/handoff'
import type { QuickRow, RecordsStatus } from '@/api/quickRecords'
import { ApiError } from '@/api/types'
import { SettingsFrame } from '@/app/pages/settings'
import { HostGeneral } from '@/app/pages/settings/host/General'
import { HostRecords } from '@/app/pages/settings/host/Records'
import { HostDevices } from '@/app/pages/settings/host/Devices'
import { createPairFixture, installPairFixture, type PairFixture, type PairFixtureOptions } from './pairFixture'
import type { MemberDevice, PairMember, PairOfferStatus } from '@/api/pairing'
import { HOST_TABS } from '@/app/pages/settings/tabs'
import { QuickPage } from '@/app/pages/quick'
import { useWorkspace } from '@/app/workspace'
import { runsApi, type Run, type RunList } from '@/api/runs'
import { RealDock } from '@/app/terminal/real/RealDock'
import { termTransport, type TermConnection, type TermHandlers } from '@/app/terminal/real/termProtocol'
import { agentsApi, type AgentPreview } from '@/api/agents'
import { StartAgentDialog } from '@/app/terminal/real/StartAgentDialog'

export const LAYOUT_SCENARIOS = [
  'quick-normal',
  'quick-many',
  'quick-add',
  'records-ok',
  'records-hidden-url',
  'records-first',
  'records-push-failed',
  'records-push-mismatch',
  'runner-on',
  'runner-off',
  // M7a: the real-host terminal dock (RealDock with fixture runs and a fixture socket).
  'terminal-dock',
  'terminal-preview',
  'terminal-readonly',
  'terminal-take',
  'terminal-exited',
  'terminal-stop',
  // M7b: core's Start agent dialog with fixture answers (preview, blocked reasons, the runner grant card, outcomes).
  'agent-dialog',
  'agent-blocked',
  'agent-grant',
  'agent-outcomes',
  // Pair a phone (proposed pair/1): Settings → Devices with the fixture pairing api.
  'pair-unavailable',
  'pair-blocked-convenience',
  'pair-offer',
  'pair-pending',
  'pair-cert',
  'pair-failed',
  'pair-devices',
] as const
export type LayoutScenario = (typeof LAYOUT_SCENARIOS)[number]

const LONG_URL = 'https://git.example.test/' + 'a-very-long-organisation-name/'.repeat(6) + 'records-of-the-workspace-with-an-unbreakable-name.git'
const LONG_WORD = 'U'.repeat(90)

const quickRow = (i: number, over: Partial<QuickRow> = {}): QuickRow =>
  ({
    key: `DEMO-${String(100 + i).padStart(4, '0')}`,
    uid: `U${i}`,
    title: `Quick task number ${i}`,
    type: 'chore',
    priority: null,
    size: null,
    status: 'open',
    labels: ['quick'],
    parent: null,
    owner: null,
    assignees: [],
    claim: null,
    turn: { who: 'nobody', why: '' },
    progress: null,
    open_questions: 0,
    blocking_questions: null,
    restricted: false,
    awaiting_gate: null,
    addons: {},
    updated_at: '2026-10-12T09:00:00Z',
    quick: { commits: 1, files: 2, over: false },
    ...over,
  }) as QuickRow

const quickRows = (scenario: LayoutScenario): QuickRow[] =>
  scenario === 'quick-many'
    ? Array.from({ length: 60 }, (_, i) => quickRow(i, { title: i % 7 === 0 ? `Fix ${LONG_URL}` : `Quick task number ${i} with a title that goes on for a while to see how a row wraps`, labels: i % 3 === 0 ? ['quick', 'docs'] : ['quick'], quick: { commits: i % 5 === 0 ? null : i, files: i % 4 === 0 ? null : i * 2, over: i % 6 === 0 ? true : i % 5 === 0 ? null : false } }))
    : [
        quickRow(1, { title: `Read ${LONG_URL} and fix the link`, labels: ['quick', 'docs', 'a-rather-long-label-name', 'web'], quick: { commits: 4, files: 7, over: true } }),
        quickRow(2, { title: LONG_WORD, status: 'in-progress' }),
        quickRow(3, { title: 'No repository is linked yet', status: 'backlog', quick: { commits: null, files: null, over: null } }),
        quickRow(4, { title: 'Fix the typo in CONTRIBUTING' }),
      ]

const records = (scenario: LayoutScenario): RecordsStatus => {
  const base: RecordsStatus = {
    available: true,
    configured: true,
    repo: { state: 'ok', problem: null, problem_hidden: false },
    pending: { events: 120, tickets: Array.from({ length: 12 }, (_, i) => ({ key: `DEMO-${String(i + 1).padStart(4, '0')}`, events: i + 3 })), more: true, workspace_events: 5 },
    last: { commit: 'c'.repeat(40), at: '2026-10-12T08:00:00Z', by: 'p_' + 'a'.repeat(32), ws_seq: 40, pushed_at: '2026-10-12T08:01:00Z' },
    last_noted: { by: 'p_' + 'a'.repeat(32), at: '2026-10-12T08:00:00Z' },
    auto: { on: true, every_min: 15, push: true },
    remote: { url: LONG_URL, url_hidden: false, private: true, export: 'ok' },
  }
  if (scenario === 'records-hidden-url')
    return { ...base, repo: { state: 'broken', problem: null, problem_hidden: true }, remote: { url: null, url_hidden: true, private: null, export: 'blocked_convenience' } }
  if (scenario === 'records-first') return { ...base, configured: false, repo: null, last: null, last_noted: null, auto: null, remote: null }
  return base
}

let serial = 0
function request(action: Record<string, unknown>): HandoffRequest {
  serial += 1
  const id = '01J9ZP00000000000000000' + String(serial).padStart(3, '0')
  const type = action.action === 'new' ? 'ticket.created' : 'ticket.updated'
  const event = action.action === 'new' ? { type, ticket: { key: 'DEMO-0199', type: 'chore', title: action.title } } : { type, ticket: action.key, labels: action.labels }
  const events = [{ v: 2, hash_v: 1, roster_v: 3, based_on: 'sha256:' + 'a'.repeat(64), id, actor: { kind: 'person', id: 'p_' + 'a'.repeat(32), device: null }, auth: null, ...event }] as unknown as HandoffEvent[]
  const { type: t, ...payload } = event
  const material = { kind: 'payload' as const, ticket: null, review: [{ event_id: id, type: t, payload, derived: {} }] }
  const digest = reviewDigestOf(material)
  const op = String(action.action)
  return {
    id: 'hr_' + '0'.repeat(22) + String(serial).padStart(4, '0'),
    code: '7F3K-Q2MA',
    expires_at: new Date(Date.now() + 600_000).toISOString().replace(/\.\d+Z$/, 'Z'),
    state: 'prepared',
    op,
    key: null,
    fingerprint: fingerprintOf(digest, events, op),
    review_digest: digest,
    gate_hash: null,
    material,
    events,
    origin: null,
  }
}

const RUN_BASE = 'r_01M4Y46B485AXD4CBEJZ15Z1'
const fixtureRun = (id: string, over: Partial<Run> = {}): Run => ({
  run: RUN_BASE + id, rev: 1, kind: 'shell', ticket: null, harness: 'shell', mode: null, model: null, as_person: true, state: 'running',
  started_at: '2026-10-12T09:00:00Z', viewers: 1, writer: false, cols: 100, rows: 30, replay_until: null, label: null, label_hidden: false,
  cwd: { kind: 'workspace' }, exit: null, ...over,
})
const ENDED = { code: 1, signal: null, at: '2026-10-12T09:30:00Z', reason: 'stopped' as const, cleanup: 'complete' as const }
const runList = (scenario: LayoutScenario): RunList => {
  const runner = { connected: true, enabled: true, terminals: true }
  if (scenario === 'terminal-exited')
    return { runner, truncated: false, runs: [fixtureRun('EX', { state: 'exited', replay_until: '2026-01-01T00:00:00Z', exit: ENDED, label: `Build ${LONG_WORD}` })] }
  if (scenario === 'terminal-stop')
    return { runner, truncated: false, runs: [fixtureRun('ST', { state: 'exited', replay_until: '2099-01-01T00:00:00Z', exit: { ...ENDED, cleanup: 'incomplete' } })] }
  return {
    runner,
    truncated: false,
    runs: [
      fixtureRun('AA', { writer: scenario === 'terminal-take', label: `Release notes for ${LONG_URL}` }),
      fixtureRun('BB', { ticket: 'DEMO-0043', label: null }),
      ...Array.from({ length: 6 }, (_, i) => fixtureRun(`C${i}`, { label: `Shell ${i + 3}` })),
      fixtureRun('DD', { state: 'exited', replay_until: '2099-01-01T00:00:00Z', exit: ENDED }),
    ],
  }
}
const AGENT_GRANT = 'gr_01M4Y46B485AXD4CBEJZ15Z1TV'
const agentPreview = (scenario: LayoutScenario): AgentPreview => {
  const base: AgentPreview = {
    spec: 'f'.repeat(64), ticket: 'DEMO-0043', display: `claude --model opus 'Run orch status, then work on ticket DEMO-0043. Follow AGENTS.orch.md.' ${LONG_WORD}`, display_hidden: false,
    cwd: { kind: 'worktree', repo: 'billing', ticket: 'DEMO-0043' }, cwd_path: `/Users/demo/${'deeply-nested-folder/'.repeat(6)}billing.worktrees/0a1b2c3d-DEMO-0043`, cwd_path_hidden: false,
    will_create_worktree: true, login_shell: true, env: ['TERM', 'COLORTERM', 'ORCH_GRANT', 'ORCH_SESSION', 'ORCH_TICKET', 'ORCH_HARNESS', 'CLAUDE_CODE_SUBAGENT_MODEL'],
    grant: { id: AGENT_GRANT, until: '2026-10-15T20:00:00Z', scope: 'workable' }, blocked: null,
    warnings: ['the program is found and started through your login shell; your shell profile runs first and can change what runs', `ORCH_WORKTREES leaves out ${LONG_URL}: its path contains ':'`],
  }
  if (scenario === 'agent-blocked') return { ...base, grant: null, blocked: { code: 'runner.disabled', message: `dashboard execution is off for this workspace on this machine ${LONG_WORD}`, detail: { missing: 'machine' } } }
  if (scenario === 'agent-grant' || scenario === 'agent-outcomes') return { ...base, grant: null, blocked: { code: 'launch.no_grant', message: 'no usable runner grant', detail: { reason: 'expiring' } } }
  return base
}
/** A fixture socket: `ready` at once (never the lease), then a screenful of output. Nothing leaves this page. */
function fixtureConnect(_ticket: string, h: TermHandlers): TermConnection {
  const timer = setTimeout(() => {
    h.ready({ start: 0, offset: 0, cols: 100, rows: 30, writer: false, state: current === 'terminal-stop' ? 'exited' : 'running' })
    const text = Array.from({ length: 40 }, (_, i) => `demo $ echo line ${i} ${'-'.repeat(i % 90)}\r\n`).join('')
    h.output(0, new TextEncoder().encode(text))
    if (current === 'terminal-stop') h.exit(1, null)
  }, 50)
  return { sendInput() {}, resize() {}, close: () => clearTimeout(timer) }
}

const LONG_LABEL = `Severin’s iPhone 17 Pro Max ${LONG_WORD}`.slice(0, 80)
const pairOffer = (state: PairOfferStatus['state'], over: Partial<PairOfferStatus> = {}): PairOfferStatus => ({
  offer_id: '7'.repeat(32), state, created_ms: Date.now() - 60_000, expires_ms: Date.now() + 240_000, scopes: ['look', 'decide'], label: LONG_LABEL, ...over,
})
const pairDevice = (i: number, over: Partial<MemberDevice> = {}): MemberDevice => ({
  id: 'd_' + String(i).repeat(32).slice(0, 32), auth: null, scopes: ['look', 'decide'], valid: true, label: `Phone ${i}`, paired_ms: Date.parse('2026-10-12T09:00:00Z') - i * 86_400_000, relay: true,
  cert_expires: '2027-10-12T09:00:00Z', reader: 1, ...over,
})
const pairOptions = (scenario: LayoutScenario): PairFixtureOptions => {
  const member = (devices: MemberDevice[]): PairMember[] => [
    // The Mac's own device has no pairing fields (no `relay`): it is not listed as a paired device.
    { person: 'p_' + 'a'.repeat(32), name: 'Severin', role: 'owner', devices: [{ id: 'd_mac', auth: null, scopes: ['look', 'decide', 'operate'], valid: true }, ...devices] },
  ]
  const members = member([pairDevice(1, { label: LONG_LABEL }), pairDevice(2, { relay: false, reader: 0, label: 'Work phone\nsecond line' }), pairDevice(9, { label: '' })])
  switch (scenario) {
    case 'pair-unavailable':
      return { served: false }
    case 'pair-blocked-convenience':
      return {
        preview: { blocked: { code: 'sign.convenience_readers', message: `click-to-sign is on for this workspace; a phone cannot be added while it is on (${LONG_WORD})`, cause: 'flag' } },
        members: member([]),
      }
    case 'pair-pending':
      return { offers: [pairOffer('pending', { sas: 'K7Q2MA' })], members }
    case 'pair-cert':
      return { offers: [pairOffer('cert_pending', { cert_code: 'H4WZ7P' })], members }
    case 'pair-failed':
      // A failed offer is not live: the scenario follows it as the page would right after the failure.
      return { offers: [pairOffer('failed', { reason: `the certificate did not verify: ${LONG_URL}` })], preview: { live_offer: '7'.repeat(32) }, members }
    case 'pair-devices':
      return { members: member([pairDevice(1, { label: LONG_LABEL }), pairDevice(2, { relay: false, reader: 0, label: 'Work phone\nsecond line' }), ...Array.from({ length: 8 }, (_, i) => pairDevice(i + 3, { label: i % 3 === 0 ? `${LONG_WORD} ${i}` : `Phone ${i + 3}` }))]) }
    default:
      return { members }
  }
}
let undoPair: (() => void) | null = null
let pairOverride: PairFixtureOptions | null = null
let pairFixture: PairFixture | null = null
/** Tests: options merged into the next pair scenario, and the fixture it installed (to drive `pair` items). */
export const pairPreviewForTests = {
  set: (o: PairFixtureOptions | null) => void (pairOverride = o),
  fixture: () => pairFixture,
}

let current: LayoutScenario = 'quick-normal'
let installed = false
const originals = new Map<string, unknown>()

/** Test cleanup: undo every mock api swap before another test uses the shared client. */
export function resetLayoutPreview() {
  const a = api as unknown as Record<string, unknown>
  const r = runsApi as unknown as Record<string, unknown>
  for (const [name, fn] of originals) {
    if (name === 'runs.connect') termTransport.connect = fn as typeof termTransport.connect
    else if (name.startsWith('runs.')) r[name.slice(5)] = fn
    else if (name.startsWith('agents.')) (agentsApi as unknown as Record<string, unknown>)[name.slice(7)] = fn
    else a[name] = fn
  }
  originals.clear()
  undoPair?.()
  undoPair = null
  pairOverride = null
  pairFixture = null
  installed = false
  current = 'quick-normal'
  serial = 0
}

/** Replaces the mock api's methods that these pages call. Done once; the answers follow `current`. */
function install() {
  if (installed) return
  installed = true
  const a = api as unknown as Record<string, (...args: never[]) => unknown>
  const original = { getIdentity: a.getIdentity, getTicket: a.getTicket }
  const set = (name: string, fn: (...args: never[]) => unknown) => {
    originals.set(name, a[name])
    a[name] = fn
  }
  set('getQuick', async (_ws: string, p: { status?: string[] } = {}) => {
    const done = (p.status ?? []).includes('done')
    const rows = quickRows(current)
    return { available: true, rows: done ? [quickRow(900, { status: 'done', title: 'Drop the old logo' })] : rows, next_page: current === 'quick-many' ? 'page-2' : null, total: done ? 1 : rows.length }
  })
  set('getRecords', async () => records(current))
  set('getRecordsCurrent', async () => ({ ...records(current), remote: { ...records(current).remote!, current_url: LONG_URL + (current === 'records-push-mismatch' ? '/changed' : ''), current_url_hidden: false, current_private: true, matches_last: current !== 'records-push-mismatch' } }))
  // M7a: the real-host dock's routes and its socket answer from fixtures.
  const r = runsApi as unknown as Record<string, unknown>
  for (const name of ['list', 'preview', 'open', 'stop', 'ticket']) originals.set(`runs.${name}`, r[name])
  originals.set('runs.connect', termTransport.connect)
  r.list = async () => runList(current)
  r.preview = async () => ({
    spec: 'f'.repeat(64), display: `$SHELL -l -c 'exec "$0" "$@"' ${LONG_WORD}`, display_hidden: false, cwd_path: `/Users/demo/${'deeply-nested-folder/'.repeat(6)}billing`,
    cwd_path_hidden: false, notice: 'acts as you, not as an agent', as_person: true, login_shell: true, env: ['PATH', 'HOME', 'LANG', 'TERM', 'ORCH_TICKET', 'SHELL', 'USER', 'LOGNAME'],
    blocked: null, warnings: [`The working copy ${LONG_URL} has uncommitted changes.`],
  })
  r.ticket = async () => ({ ticket: 'layout-preview', expires_in_ms: 30000 })
  r.stop = async () => ({ state: 'exited', cleanup: 'incomplete' })
  termTransport.connect = fixtureConnect
  // M7b: the Start agent dialog's routes answer from fixtures.
  const ag = agentsApi as unknown as Record<string, unknown>
  for (const name of ['preview', 'runner', 'start', 'depositStatus', 'forget']) originals.set(`agents.${name}`, ag[name])
  ag.preview = async () => agentPreview(current)
  ag.runner = async () => ({ runner: { connected: true, enabled: current !== 'agent-blocked', terminals: false, grant: current === 'agent-dialog' ? { id: AGENT_GRANT, until: '2026-10-15T20:00:00Z', scope: 'workable' } : null } })
  ag.start = async () => { throw new ApiError(409, { code: 'run.limit', message: `8 agent runs at most ${LONG_WORD}`, retryable: true }) }
  ag.depositStatus = async () => ({ deposit: 'unknown' })
  ag.forget = async () => ({ forgotten: true })
  // "Make a normal ticket" reads the ticket fresh: answer with the quick row's labels and status.
  set('getTicket', async (...args: never[]) => {
    const key = args[0] as string
    const row = quickRows(current).find((r) => r.key === key)
    const base = (await (original.getTicket as (...a: never[]) => Promise<object>)(...args).catch(() => ({ key }))) as object
    return row ? { ...base, key, labels: row.labels, status: row.status } : base
  })
  set('recordsCommit', async (_ws: string, push: boolean) => {
    if (current === 'records-push-failed' && push) throw new ApiError(502, { code: 'records.push_failed', message: `push to ${LONG_URL} was refused`, retryable: true, commit: 'c'.repeat(40) })
    return { ok: true, commit: 'd'.repeat(40), pushed: push, url: push ? LONG_URL : null }
  })
  set('recordsAutoOff', async () => ({ ok: true }))
  set('getIdentity', async (...args: never[]) => ({ ...((await (original.getIdentity as (...a: never[]) => Promise<object>)(...args)) as object), runner: { connected: true, enabled: current === 'runner-on', terminals: true } }))
  set('prepareHandoff', async (_ws: string, action: Record<string, unknown>) => request(action))
  set('getHostSettings', async () => ({
    available: true,
    grant_hours: 8,
    claim_ttl_min: 120,
    lease_ttl_min: 60,
    defaults: ['claim_ttl_min', 'lease_ttl_min'],
    gates: {},
    repos: [{ name: 'code', default_branch: 'main', remote: LONG_URL }],
    repos_more: false,
    settings_at: null,
    policy_at: null,
  }))
  set('getHandoff', async (_ws: string, id: string): Promise<HandoffState> => ({ id, state: 'prepared', op: 'new', key: null, expires_at: new Date(Date.now() + 600_000).toISOString(), event_ids: [], seq: null }))
  set('cancelHandoff', async (_ws: string, id: string) => ({ id, state: 'cancelled' }))
}

export function LayoutPreview({ name }: { name: string }) {
  const qc = useQueryClient()
  const { workspace } = useWorkspace()
  const [scenario] = useState<LayoutScenario | null>(() => {
    if (!(LAYOUT_SCENARIOS as readonly string[]).includes(name)) return null
    install()
    current = name as LayoutScenario
    if (current.startsWith('pair-')) {
      undoPair?.()
      pairFixture = createPairFixture({ ...pairOptions(current), ...pairOverride })
      undoPair = installPairFixture(pairFixture)
    }
    // Nothing of an earlier scenario stays in the cache.
    for (const key of ['quick', 'records', 'identity', 'runs', 'pair', 'runner']) qc.removeQueries({ queryKey: [key] })
    return current
  })
  if (!scenario) return <p role="alert">Unknown layout scenario {name}.</p>
  if (!workspace) return null
  if (scenario.startsWith('quick-')) return <QuickPage />
  if (scenario.startsWith('terminal-')) return <TerminalPreview scenario={scenario} />
  if (scenario.startsWith('agent-'))
    return (
      <StartAgentDialog ws={workspace.id} ticket="DEMO-0043" open onClose={() => {}}
        grantStep={scenario === 'agent-outcomes' ? { kind: 'unknown', grant: AGENT_GRANT } : undefined} />
    )
  if (scenario.startsWith('pair-'))
    return (
      <SettingsFrame current="devices" addon={undefined} ws={workspace.id} tabs={HOST_TABS}>
        <HostDevices workspace={workspace} />
      </SettingsFrame>
    )
  if (scenario.startsWith('records-'))
    return (
      <SettingsFrame current="records" addon={undefined} ws={workspace.id} tabs={HOST_TABS}>
        <HostRecords workspace={workspace} />
      </SettingsFrame>
    )
  return (
    <SettingsFrame current="general" addon={undefined} ws={workspace.id} tabs={HOST_TABS}>
      <HostGeneral workspace={workspace} />
    </SettingsFrame>
  )
}

/** The real-host dock at its default bottom size, under a stand-in page area (the guard checks it at 720 px too). */
function TerminalPreview({ scenario }: { scenario: LayoutScenario }) {
  const view = { width: typeof window === 'undefined' ? 1440 : window.innerWidth, height: typeof window === 'undefined' ? 900 : window.innerHeight }
  return (
    <div className="flex h-[640px] min-w-0 flex-col">
      <p className="p-3 text-xs text-text-muted">Real-host terminal dock ({scenario}): fixture runs and a fixture socket.</p>
      <div className="flex-1" />
      <RealDock prefs={{ open: true, side: 'bottom', bottom: 360, right: 480, harness: 'shell' } as never} side="bottom" size={360} view={view} area={view.width}
        rightFits={false} setPrefs={() => {}} focus={{ current: null }} memory={new Map()} collapse={() => {}} autoPreview={scenario === 'terminal-preview'} />
    </div>
  )
}
