// A simulated hub for the mockup and its tests (V3 A7). Scenario by address: `?hub=standalone|selective|full|offline|nottold`
// (kept for the tab). Everything is read-only here: like the real pages, changes are Terminal commands, never done in the page.
import type { HubCommand, HubCommandRequest, HubPreview, HubRule, HubShare, HubStatus } from '@/api/hub'
import { fail, ok, type MockRouter } from './router'

export const HUB_SCENARIOS = ['standalone', 'selective', 'full', 'offline', 'nottold'] as const
export type HubScenario = (typeof HUB_SCENARIOS)[number]
const KEY = 'orch.mock.hub'

let forced: HubScenario | null = null
/** Tests and the dev switch set the scenario. */
export const setHubScenario = (s: HubScenario | null) => {
  forced = s
}

function scenario(): HubScenario {
  if (forced) return forced
  try {
    const q = new URLSearchParams(typeof location === 'undefined' ? '' : location.search).get('hub')
    if (q && (HUB_SCENARIOS as readonly string[]).includes(q)) sessionStorage.setItem(KEY, q)
    const v = sessionStorage.getItem(KEY)
    if (v && (HUB_SCENARIOS as readonly string[]).includes(v)) return v as HubScenario
  } catch {
    /* storage unavailable */
  }
  return 'standalone'
}

const BASE: HubStatus = {
  mode: 'standalone', joined: false, hub_id: null, member: false, link: null, reachable: null, not_told: false, owed: 0,
  reseal_needed: false, key_change_pending: false, log_sync_stuck: false, groups: null, snapshot_version: null,
}
const HUB_ID = '9f3c1ab277de40c8'

export function mockHubStatus(s: HubScenario = scenario()): HubStatus {
  const joined = { ...BASE, joined: true, hub_id: HUB_ID, link: 'bound' as const, groups: { read: 3, edit: 8, admin: 2 }, snapshot_version: 4 }
  switch (s) {
    case 'selective': return { ...joined, mode: 'selective' }
    case 'full': return { ...joined, mode: 'full', member: true }
    case 'offline': return { ...joined, mode: 'selective', reachable: false }
    case 'nottold': return { ...BASE, hub_id: HUB_ID, link: 'unbound', not_told: true, owed: 1 }
    default: return BASE
  }
}

const SHARED = [
  { key: 'DEMO-0043', title: 'Export report as PDF' },
  { key: 'DEMO-0045', title: 'Pricing page copy' },
]

function mockShare(): HubShare {
  const joined = scenario() === 'selective' || scenario() === 'offline'
  if (!joined) return { available: true, rules: [], shared: 0, refused: [], tickets: [], tickets_complete: true }
  return {
    available: true,
    rules: [
      { kind: 'needs_you', value: null, count: 3, describes: 'needs-you summaries (type, a one-line title and the deadline)' },
      { kind: 'questions', value: null, count: 1, describes: 'every open question addressed to you (the full question)' },
      { kind: 'type', value: 'feature', count: 1, describes: "every ticket of type 'feature' (title, status, labels, deadline)" },
      { kind: 'ticket', value: 'DEMO-0045', count: 1, describes: "the ticket 'DEMO-0045' (title, status, labels, deadline)" },
    ],
    shared: 6, refused: [], tickets: SHARED.map((t) => t.key), tickets_complete: true,
  }
}

function mockPreview(body: { add?: HubRule; remove?: HubRule } | null): HubPreview {
  const base = SHARED.map((t) => ({ kind: 'ticket' as const, line: `ticket ${t.key}: ${t.title}` }))
  const notes = { kind: 'summary' as const, line: 'needs you: question: Which greeting?' }
  const add = body?.add
  const rm = body?.remove
  let items = [...base, notes]
  let added = 0
  let removed = 0
  if (add && (add.kind === 'type' || add.kind === 'tag')) {
    items = [...items, { kind: 'ticket', line: `ticket DEMO-0042: Fix login redirect` }]
    added = 1
  } else if (add && add.kind === 'ticket' && !SHARED.some((t) => t.key === add.value)) {
    items = [...items, { kind: 'ticket', line: `ticket ${add.value}: (a ticket you pick)` }]
    added = 1
  }
  if (rm && (rm.kind === 'ticket' || rm.kind === 'type')) {
    items = items.filter((i) => !(rm.kind === 'ticket' ? i.line.startsWith(`ticket ${rm.value}:`) : i.kind === 'ticket' && i.line.includes('Export')))
    removed = 1
  }
  return { count: items.length, new: added, revoked: removed, more: 0, items, rules: [] }
}

/** The same command lines the server builds (tests for the page only; the server's tests are the contract). */
function mockCommand(b: HubCommandRequest): HubCommand {
  const q = (v: string) => (/^[A-Za-z0-9._:/-]+$/.test(v) ? v : `'${v.replace(/'/g, `'\\''`)}'`)
  const flag = (r: HubRule) => (r.kind === 'needs_you' ? '--needs-you' : r.kind === 'questions' ? '--questions' : `--${r.kind} ${q(r.value ?? '')}`)
  const line = (() => {
    switch (b.kind) {
      case 'join': return `orch hub join ${q(b.invite)} --hub-card ${q(b.card)} --mode ${b.mode}`
      case 'mode_full': return `orch hub mode full --hub-card ${q(b.card)}`
      case 'leave':
      case 'leave_hub_down': return 'orch hub leave'
      case 'stop_telling': return 'orch hub unbind'
      case 'share_add': return `orch hub share rule add ${flag(b.rule)}`
      case 'share_remove': return `orch hub share rule remove ${flag(b.rule)}`
      case 'share_revoke': return `orch hub share revoke ${q(b.ref)}`
      default: return 'orch hub share sync'
    }
  })()
  return { command: line, kind: b.kind }
}

export function registerHubRoutes(r: MockRouter) {
  r.add('GET', '/api/w/:ws/hub', () => ok(mockHubStatus()))
  r.add('GET', '/api/w/:ws/hub/share', () => ok(mockShare()))
  r.add('POST', '/api/w/:ws/hub/share/preview', (_s, c) => ok(mockPreview(c.body as { add?: HubRule; remove?: HubRule } | null)))
  r.add('POST', '/api/w/:ws/hub/command', (_s, c) => {
    const b = c.body as HubCommandRequest | null
    if (!b || typeof b !== 'object' || !('kind' in b)) return fail(400, 'invalid.input', 'Body must be {kind, ...}')
    return ok(mockCommand(b))
  })
}
