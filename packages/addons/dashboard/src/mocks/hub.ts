// A simulated hub for the mockup and its tests (V3 A7). Scenario by address: `?hub=standalone|selective|full|offline|nottold`
// (kept for the tab). Reads are fixed by the scenario. Stopping sharing (a ticket, a rule) is a plain owner click; a
// join and a leave are signed by One click with the backup key and change the scenario (see the click routes below).
import type { HubAudienceList, HubSharedTicket, HubPreview, HubRule, HubShare, HubStatus } from '@/api/hub'
import type { ProfileFields } from '@/api/profile' // type only: a runtime import would loop through api/client
import { BACKUP_KEY_LINE, CLICK_RISK, type ClickViewAnswer } from '@/api/click'
import { fail, ok, type MockRouter } from './router'

export const HUB_SCENARIOS = ['standalone', 'selective', 'full', 'offline', 'nottold', 'update'] as const
export type HubScenario = (typeof HUB_SCENARIOS)[number]
const KEY = 'orch.mock.hub'

let forced: HubScenario | null = null
/** Tickets the owner stopped sharing in the mock (reset with the scenario). */
const withdrawn = new Set<string>()
/** Tests and the dev switch set the scenario. */
export const setHubScenario = (s: HubScenario | null) => {
  forced = s
  withdrawn.clear()
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
    case 'selective': return { ...joined, mode: 'selective', hub_card: JSON.stringify({ o: { hub_id: HUB_ID }, sig: 'x' }) }
    case 'full': return { ...joined, mode: 'full', member: true }
    case 'update': return { ...joined, mode: 'full', member: true, update_target: '0.2.0' }
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
    shared: 6 - withdrawn.size, refused: [], tickets: SHARED.map((t) => t.key).filter((k) => !withdrawn.has(k)), tickets_complete: true,
  }
}

export const MOCK_SHARED_WITH_YOU: HubSharedTicket[] = [
  { workspace_id: 'a'.repeat(32), key: 'DEMO-0007', title: 'Invoice export for Ben', shared_by: 'Anna', status: 'open', type: 'feature', deadline: '2026-11-06', description: 'Export invoices as CSV.' },
]

export const MOCK_AUDIENCE: HubAudienceList = {
  people: [{ user_id: 'b'.repeat(32), name: 'Ben Test' }, { user_id: 'c'.repeat(32), name: 'Carla Test' }],
  teams: [{ group: 'finance', members: 4 }],
  note: 'from the hub, not signed',
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
  items = items.filter((i) => !withdrawn.has(i.line.replace(/^ticket ([^:]+):.*$/, '$1')))
  const fresh = items.slice(base.length + 1).map((i) => i.line)
  const aud = add && (add.kind === 'type' || add.kind === 'tag' || add.kind === 'ticket') ? add.audience : undefined
  const names = (aud?.users ?? []).map((u) => MOCK_AUDIENCE.people.find((p) => p.user_id === u)?.name ?? 'a person on the hub')
  const withText = add && (add.kind === 'type' || add.kind === 'tag' || add.kind === 'ticket') && add.description === true
  const review = {
    hub_id: 'ab12cd34ef56ab12', readers: { people: names, teams: aud?.groups ?? [], note: 'from the hub, not signed', info: null }, widened: [],
    new_count: added, new_more: 0, new_items: fresh,
    leaves: added ? [withText ? "the ticket's title, status, labels, deadline and description, but never its plan, comments or files" : "the ticket's title, status, labels and deadline, but never its description, comments or files"] : [],
    later_edits: 'Later edits are shared too, until you stop sharing.',
    leaves_short: added ? (withText ? 'the title, status, labels, deadline and description' : 'the title, status, labels and deadline') : null,
    later_edits_short: 'Later edits are shared too.',
  }
  return { count: items.length, new: added, revoked: removed, more: 0, items, rules: [], plan: 'P'.repeat(42) + String(items.length % 10), review }
}

/** The owner's profile (HUB-WS-09): kept in memory; `pushed` only when the scenario has a hub the host could reach. */
const EMPTY_PROFILE: ProfileFields = { listed: false, tags: [], handles: [], not_handled: [], accepts: [], description: '', contact: '', languages: '', hours: '' }
let mockProfile: ProfileFields = { ...EMPTY_PROFILE }
export const resetMockProfile = () => { mockProfile = { ...EMPTY_PROFILE } }
function registerHubProfileRoutes(r: MockRouter) {
  r.add('GET', '/api/w/:ws/hub/profile', () => ok({ ...mockProfile, pushed: false }))
  r.add('PUT', '/api/w/:ws/hub/profile', (_s, c) => {
    const b = c.body as Partial<ProfileFields> | null
    if (!b || typeof b.listed !== 'boolean' || typeof b.description !== 'string') return fail(400, 'invalid.input', 'Body must be a profile')
    mockProfile = { ...EMPTY_PROFILE, ...b }
    const s = scenario()
    return ok({ ...mockProfile, pushed: s === 'selective' || s === 'full' })
  })
}

export function registerHubRoutes(r: MockRouter) {
  r.add('GET', '/api/w/:ws/hub', () => ok(mockHubStatus()))
  r.add('GET', '/api/w/:ws/hub/share', () => ok(mockShare()))
  r.add('GET', '/api/w/:ws/hub/shared', () => ok({ items: scenario() === 'selective' || scenario() === 'full' ? MOCK_SHARED_WITH_YOU : [] }))
  // Shared spaces: none in the mock (the page and its tests spy the api); the real host lists them from the hub.
  r.add('GET', '/api/w/:ws/spaces', () => ok({ spaces: [], note: 'from the hub, not signed' }))
  r.add('GET', '/api/w/:ws/spaces/:id', (_s, c) => ok({ id: c.params.id, items: [], refused: 0, note: 'from the hub, not signed' }))
  r.add('GET', '/api/w/:ws/hub/share/audience', () => ok(MOCK_AUDIENCE))
  r.add('POST', '/api/w/:ws/hub/share/preview', (_s, c) => ok(mockPreview(c.body as { add?: HubRule; remove?: HubRule } | null)))
  // Stopping sharing: a plain owner click (the server's full-session and owner checks are its own tests).
  r.add('POST', '/api/w/:ws/hub/share/revoke', (_s, c) => {
    const ref = (c.body as { ref?: unknown } | null)?.ref
    if (typeof ref !== 'string' || !ref) return fail(400, 'invalid.input', 'Body must be {ref}')
    const had = SHARED.some((t) => t.key === ref) && !withdrawn.has(ref)
    withdrawn.add(ref)
    return ok({ synced: true, pending: null, revoked: had ? 1 : 0 })
  })
  r.add('POST', '/api/w/:ws/hub/share/rule-remove', (_s, c) => {
    const b = c.body as { rule?: HubRule; expect?: unknown } | null
    if (!b?.rule || typeof b.expect !== 'string' || b.expect.length !== 43) return fail(400, 'invalid.input', 'Body must be {rule, expect}')
    return ok({ synced: true, pending: null, revoked: mockPreview({ remove: b.rule }).revoked })
  })
  r.add('POST', '/api/w/:ws/hub/share/rule-add', (_s, c) => {
    const b = c.body as { rule?: HubRule; expect?: unknown } | null
    if (!b?.rule || typeof b.expect !== 'string' || b.expect.length !== 43) return fail(400, 'invalid.input', 'Body must be {rule, expect}')
    return ok({ synced: true, pending: null, put: mockPreview({ add: b.rule }).new })
  })
  r.add('POST', '/api/w/:ws/hub/share/sync', (_s, c) => {
    if (typeof (c.body as { expect?: unknown } | null)?.expect !== 'string') return fail(400, 'invalid.input', 'Body must be {expect}')
    return ok({ synced: true, pending: null, put: 0 })
  })
  registerHubProfileRoutes(r)
  registerHubClickRoutes(r)
}

// The One click path of the hub pages (#1198 C11), for the mockup and tests: prepare, review and sign a hub join or leave
// with a backup key. The key is sealed by the page for real; this mock has no private half and never opens it.
let oneClickOn = true
let connectError: string | null = null
let confirmAfter = 0 // how many refreshes the mock hub needs before it confirms (0: the connect itself confirms)
let refreshes = 0
export const setMockConfirmAfter = (n: number) => { confirmAfter = n; refreshes = 0 }
/** Tests and the dev switch: is One click on in the mock workspace? (Off means the key is back under a passphrase.) */
export const setMockOneClick = (on: boolean) => {
  oneClickOn = on
}
/** Tests: make the hub refuse the connect after a join (the host's plain message). `null`: it works. */
export const setMockConnectError = (message: string | null) => {
  connectError = message
}
const SIGNER_PK = 'BABS9E022fzn3B-vMJGezPNuQW1z-oyas3W9A_ddxudSTZvCUQxuuTtcP1R8LE_ZJ7UrTnxyss6LZ8YgRK_YMpU' // a fixed public point
const open = new Map<string, { op: 'hub.join' | 'hub.mode' | 'hub.leave'; hubId: string; full: boolean }>()
// Run-9 feedback 6: with the person key kept (the new default) a hub step asks for no backup key. Off: the old way.
let vault = false
export const setMockVault = (on: boolean) => { vault = on }
const keptKey = () => { try { return vault || new URLSearchParams(location.search).get('vault') === '1' } catch { return vault } }

function mockView(ws: string, id: string): ClickViewAnswer | null {
  const r = open.get(id)
  if (!r) return null
  const join = r.op !== 'hub.leave'
  return {
    view_digest: 'sha256:' + '1'.repeat(64),
    view: {
      risk: CLICK_RISK, op: r.op, workspace: { id: ws, name: 'billing', prefix: 'DEMO' }, fingerprint: '0123456789ab',
      lines: [join ? `join hub ${r.hubId} in ${r.full ? 'full' : 'selective'} mode: ${r.full ? 'the hub will be able to read everything in this workspace' : 'the hub sees only what you share, plus this workspace\'s name, and never gets the workspace key'}` : `leave hub ${r.hubId} and tell it that this workspace is no longer bound`, '', BACKUP_KEY_LINE],
      checks: [], confirm: { kind: 'click' }, expires_at: new Date(Date.now() + 600_000).toISOString(), auth: 'convenience', device: 'd_' + 'a'.repeat(32), artifacts: null,
      ...(keptKey() ? {} : { seal: { v: 1, alg: 'P256-HKDF-SHA256-A256GCM', pk: SIGNER_PK, purposes: ['hub.backup_key'] }, backup_key_line: BACKUP_KEY_LINE }),
    },
  }
}

export function registerHubClickRoutes(r: MockRouter) {
  r.add('GET', '/api/w/:ws/click/status', () => ok({ custody: oneClickOn ? 'convenience' : 'passphrase', enabled: oneClickOn, available: true, switchable: true }))
  r.add('POST', '/api/w/:ws/click/prepare', (_s, c) => {
    const b = c.body as { action?: string; hub_id?: string; mode?: string } | null
    if (!oneClickOn) return fail(409, 'human.click_refused', 'One click is off in this workspace.')
    if (b?.action !== 'hub_join' && b?.action !== 'hub_leave' && b?.action !== 'hub_mode_full') return fail(400, 'invalid.input', 'The mock signs only hub steps.')
    const id = 'hr_01M5PVRB46WS3X8H5X2RQK' + String(open.size).padStart(2, '0') + 'AB'
    open.set(id, { op: b.action === 'hub_join' ? 'hub.join' : b.action === 'hub_mode_full' ? 'hub.mode' : 'hub.leave', hubId: b.hub_id ?? mockHubStatus().hub_id ?? HUB_ID, full: b.action === 'hub_mode_full' || b.mode === 'full' })
    return ok({ request_id: id, expires_at: new Date(Date.now() + 600_000).toISOString(), op: open.get(id)!.op, confirm: 'click' })
  })
  r.add('POST', '/api/w/:ws/click/view', (_s, c) => {
    const got = mockView(c.params.ws, (c.body as { request_id?: string } | null)?.request_id ?? '')
    return got ? ok(got) : fail(404, 'not_found', 'No such request.')
  })
  r.add('POST', '/api/w/:ws/click/sign', (_s, c) => {
    const b = c.body as { request_id?: string; sealed?: Record<string, unknown> } | null
    const req = open.get(b?.request_id ?? '')
    if (!req) return fail(404, 'not_found', 'No such request.')
    if (!keptKey() && !b?.sealed?.['hub.backup_key']) return fail(400, 'invalid.input', 'A hub step needs your backup key.')
    setHubScenario(req.op === 'hub.leave' ? 'standalone' : req.full ? 'full' : 'selective')
    open.delete(b!.request_id!)
    return ok({ state: 'submitted', duplicate: false })
  })
  r.add('POST', '/api/w/:ws/click/cancel', (_s, c) => {
    const id = (c.body as { request_id?: string } | null)?.request_id ?? ''
    open.delete(id)
    return ok({ id, state: 'cancelled' })
  })
  r.add('POST', '/api/w/:ws/hub/link', (_s, c) => {
    const link = (c.body as { link?: string } | null)?.link ?? ''
    const m = /^https?:\/\/([^/]+)\/c#h1\.([0-9a-f]{32})\.([A-Za-z0-9_-]{1,64})(?:\.(.+))?$/.exec(link)
    if (!m) return fail(400, 'invalid.input', 'That is not a connect link.')
    const card = JSON.stringify({ o: { hub_id: m[2] }, sig: 'x' })
    return ok({ hub_id: m[2], name: m[1], code: m[3], endpoint: m[4] ?? null, dev: true, hub_card: card })
  })
  r.add('POST', '/api/w/:ws/hub/connect', (_s, c) => {
    const b = c.body as { invite?: string; hub_card?: string } | null
    if (!b?.hub_card) return fail(400, 'invalid.input', 'Needs an invite and a card.')
    if (connectError) return fail(502, 'hub.unreachable', connectError, undefined, true)
    refreshes = 0
    return ok({ connected: true, mode: 'selective', confirmed: confirmAfter === 0 })
  })
  r.add('POST', '/api/w/:ws/hub/refresh', () => { refreshes += 1; return ok({ confirmed: refreshes >= confirmAfter }) })
  r.add('POST', '/api/w/:ws/click/state', (_s, c) => ok({ id: (c.body as { request_id?: string } | null)?.request_id ?? '', state: 'prepared' }))
}
