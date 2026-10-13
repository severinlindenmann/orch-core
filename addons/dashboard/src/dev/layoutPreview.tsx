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
import { HOST_TABS } from '@/app/pages/settings/tabs'
import { QuickPage } from '@/app/pages/quick'
import { useWorkspace } from '@/app/workspace'

export const LAYOUT_SCENARIOS = [
  'quick-normal',
  'quick-many',
  'quick-add',
  'records-ok',
  'records-hidden-url',
  'records-first',
  'records-push-failed',
  'runner-on',
  'runner-off',
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

let current: LayoutScenario = 'quick-normal'
let installed = false
const originals = new Map<string, unknown>()

/** Test cleanup: undo every mock api swap before another test uses the shared client. */
export function resetLayoutPreview() {
  const a = api as unknown as Record<string, unknown>
  for (const [name, fn] of originals) a[name] = fn
  originals.clear()
  installed = false
  current = 'quick-normal'
  serial = 0
}

/** Replaces the mock api's methods that these pages call. Done once; the answers follow `current`. */
function install() {
  if (installed) return
  installed = true
  const a = api as unknown as Record<string, (...args: never[]) => unknown>
  const original = { getIdentity: a.getIdentity }
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
    // Nothing of an earlier scenario stays in the cache.
    for (const key of ['quick', 'records', 'identity']) qc.removeQueries({ queryKey: [key] })
    return current
  })
  if (!scenario) return <p role="alert">Unknown layout scenario {name}.</p>
  if (!workspace) return null
  if (scenario.startsWith('quick-')) return <QuickPage />
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
