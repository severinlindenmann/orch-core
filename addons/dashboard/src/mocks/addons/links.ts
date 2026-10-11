import { can } from '@/api/permissions'
import type { AddonActionResult, AddonDecision } from '@/api/types'
import { fmtWhen } from '@/lib/time'
import type { Rng } from '../busy/rng'
import { briefs } from '../busy/helpers'
import { fingerprintOf, relayEpoch, relayState, RELAY_URL } from '../relay'
import type { MockStore, StoreFailure } from '../store'
import { canSeeTicket, conflict, invalid, notFound, registerAddon, type AddonCtx } from './registry'

// links (Preview; owner feedback 2026-10-10 B, docs/workspace-links-proposal.md; orch v2 §9 linked workspaces, D11-D13):
// pinned, mutual trust between two workspaces and what may cross it.
//  - A link has a peer, a carrier (same machine `spool:` or the relay; no direct LAN path, D11), what WE accept from
//    them (`accepts`) and what THEY accept from us (`theyAccept`, learned at pairing), an expiry and a log.
//  - Pairing is owner-only and mutual: a pairing code for the other side, a 6-character comparison code on both
//    screens, then each owner signs (core's signing prompt). An incoming pairing request is a decision for owners.
//  - Incoming handoffs and questions are requests (core-signed decisions on Today); widening a scope is an owner's.
//  - Restricted tickets never cross (409 links.restricted). Log rows tied to a ticket follow its visibility.
//  - The log is the addon's (`links.<verb>`, actor addon:links, with the person who signed or decided). Live actions
//    also append the same record to the workspace log, so Activity shows it.
//  - Everything is simulated: nothing leaves this machine; the other side is played by "Simulate" buttons.

export type Kind = 'handoff' | 'question' | 'status' | 'drop'
export const KINDS: Kind[] = ['handoff', 'question', 'status', 'drop']
const KIND_LABEL: Record<Kind, string> = { handoff: 'ticket handoffs', question: 'questions', status: 'status updates', drop: 'Drop files' }
type Carrier = 'local' | 'relay'

interface Peer {
  name: string
  /** One of the person's own workspaces (by prefix), or absent for another person's or organisation's. */
  ws?: string
  owner: string
  org?: string
}
interface Link {
  id: string
  peer: Peer
  carrier: Carrier
  accepts: Kind[]
  theyAccept: Kind[]
  fingerprint: string
  paired_at: string
  paired_by: string
  expires_at: string
  revoked?: { at: string; by: string }
  last_at: string
}
interface Request {
  id: string
  kind: 'pairing' | 'handoff' | 'question' | 'scope'
  /** The link it came through (pairing requests have none yet). */
  link?: string
  from: Peer
  at: string
  expires_at: string
  title: string
  text: string
  state: 'open' | 'accepted' | 'answered' | 'denied' | 'closed'
  decided?: { at: string; by: string; option: string }
  // pairing
  carrier?: Carrier
  /** What the peer asked for: the upper bound of the terms (we never accept more). */
  wants?: Kind[]
  offers?: Kind[]
  /** The terms the owner signs (narrowed from wants/offers, expiry chosen); default: as asked, 90 days. */
  terms?: Terms
  /** Bumped by "Cancel changes" so the terms form starts over from the saved terms. */
  termsRev?: number
  // question
  options?: { key: string; label: string }[]
  // scope
  scope?: Kind
  // handoff: the ticket made from it
  ticket?: string
}
interface Terms {
  /** They may send us. */
  recv: Kind[]
  /** We may send them. */
  send: Kind[]
  days: number
}
interface Sent {
  id: string
  link: string
  ticket: string
  title: string
  at: string
  by: string
  deadline: string
  state: 'waiting' | 'accepted' | 'denied' | 'done' | 'cancelled'
}
interface LogEntry {
  id: string
  at: string
  link: string | null
  type: string
  /** The person who signed or decided, when one did; the record itself is the addon's. */
  by?: string
  ticket?: string
  epoch: number
  text: string
}
interface Pending {
  id: string
  carrier: Carrier
  peer: Peer
  accepts: Kind[]
  days: number
  pairing_code: string
  started_at: string
  started_by: string
  /** Set once the other owner entered the code and signed on their side (simulated). */
  joined?: { at: string; fingerprint: string; theyAccept: Kind[] }
}
interface Nav {
  link?: string
  logLink?: string
  draft?: { link: string; ticket: string }
}

const ADDON = { kind: 'addon', id: 'links' } as const
const DAY = 86_400_000
const PAIRING_MS = 10 * 60_000
const NOW = '2026-10-09T11:30:00Z'
/** Which of the person's workspaces run on this machine (the rest are elsewhere: the relay is their carrier). */
const MACHINE: Record<string, string> = { DEMO: "Severin's MacBook Pro", INT: "Severin's MacBook Pro", CLI: 'the client VM' }
const WS_NAME: Record<string, string> = { DEMO: 'DEMO · Acme energy data', INT: 'INT · Internal', CLI: 'CLI · Client VM' }
const OWNER_KINDS: Request['kind'][] = ['pairing', 'scope']

const at = (iso: string, deltaDays: number) => new Date(Date.parse(iso) + deltaDays * DAY).toISOString().replace(/\.\d{3}Z$/, 'Z')
const plus = (iso: string, ms: number) => new Date(Date.parse(iso) + ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
const kindsText = (k: Kind[]) => (k.length ? k.map((x) => KIND_LABEL[x]).join(', ') : 'nothing')
const ownWs = (prefix: string): Peer => ({ name: WS_NAME[prefix] ?? prefix, ws: prefix, owner: 'Severin' })
const carrierOf = (from: string, to: string): Carrier => (MACHINE[from] && MACHINE[from] === MACHINE[to] ? 'local' : 'relay')
/** A pairing code for the other owner: short-lived, one use, never a credential (it only starts the comparison). */
const pairingCode = (n: number) => {
  const f = fingerprintOf(`pairing-code-${n}`)
  return `${f.slice(0, 3)}-${f.slice(3)}`
}

const NORTHWIND: Peer = { name: 'Northwind Grid · OPS', owner: 'Lena Brandt', org: 'Northwind Grid AG' }
const CONTOSO: Peer = { name: 'Contoso Analytics · BI', owner: 'Ravi Menon', org: 'Contoso Analytics' }
const FABRIKAM: Peer = { name: 'Fabrikam Energy · DATA', owner: 'Jonas Weber', org: 'Fabrikam Energy GmbH' }

function link(id: string, peer: Peer, carrier: Carrier, accepts: Kind[], theyAccept: Kind[], paired_at: string, days: number, last_at: string, extra: Partial<Link> = {}): Link {
  return { id, peer, carrier, accepts, theyAccept, fingerprint: fingerprintOf(`link|${id}`), paired_at, paired_by: 'p_sev', expires_at: at(paired_at, days), last_at, ...extra }
}
function log(id: string, atIso: string, linkId: string | null, verb: string, text: string, extra: Partial<LogEntry> = {}): LogEntry {
  return { id, at: atIso, link: linkId, type: `links.${verb}`, epoch: 1, text, ...extra }
}

function seedState(ws: string, store: MockStore) {
  const prefix = store.workspaces.find((w) => w.id === ws)?.prefix ?? ''
  const empty = { links: [] as Link[], requests: [] as Request[], sent: [] as Sent[], log: [] as LogEntry[], next: 1 }
  if (prefix === 'INT') {
    return {
      ...empty,
      links: [link('ln_demo', ownWs('DEMO'), 'local', ['handoff', 'status', 'drop'], ['handoff', 'question', 'status'], '2026-09-12T08:10:00Z', 365, '2026-10-09T10:20:00Z')],
      sent: [{ id: 'out_int_1', link: 'ln_demo', ticket: 'INT-0007', title: 'Meter schema v3: update the DEMO import', at: '2026-10-09T10:20:00Z', by: 'p_sev', deadline: at('2026-10-09T10:20:00Z', 3), state: 'waiting' }] as Sent[],
      log: [
        log('lg_int_1', '2026-09-12T08:10:00Z', 'ln_demo', 'paired', 'Linked with DEMO · Acme energy data on this machine.', { by: 'p_sev' }),
        log('lg_int_2', '2026-10-09T10:20:00Z', 'ln_demo', 'sent', 'Handed off "Meter schema v3: update the DEMO import"; waiting for DEMO to accept.', { by: 'p_sev' }),
      ],
    }
  }
  if (prefix !== 'DEMO') return empty
  const links: Link[] = [
    link('ln_int', ownWs('INT'), 'local', ['handoff', 'question', 'status'], ['handoff', 'status', 'drop'], '2026-09-12T08:10:00Z', 365, '2026-10-09T10:20:00Z'),
    link('ln_north', NORTHWIND, 'relay', ['question', 'status'], ['question', 'drop'], '2026-07-16T13:00:00Z', 90, '2026-10-09T09:05:00Z'),
    link('ln_contoso', CONTOSO, 'relay', ['drop'], ['drop'], '2026-06-02T09:30:00Z', 365, '2026-09-30T15:12:00Z', { revoked: { at: '2026-09-30T15:12:00Z', by: 'p_sev' } }),
  ]
  const requests: Request[] = [
    {
      id: 'rq_fab', kind: 'pairing', from: FABRIKAM, at: '2026-10-09T10:52:00Z', expires_at: at('2026-10-09T10:52:00Z', 1), carrier: 'relay', wants: ['question', 'drop'], offers: ['handoff', 'question'],
      title: 'Link request from Fabrikam Energy · DATA', text: 'Jonas Weber (Fabrikam Energy GmbH) wants to link through the relay. They would send questions and Drop files; they accept ticket handoffs and questions from you.', state: 'open',
    },
    { id: 'rq_int_meter', kind: 'handoff', link: 'ln_int', from: ownWs('INT'), at: '2026-10-09T10:20:00Z', expires_at: at('2026-10-09T10:20:00Z', 3), title: 'Meter schema v3: update the DEMO import', text: 'INT changed the meter schema: v3 adds register_id and renames read_at to read_ts. Update the DEMO import and its tests before Monday.', state: 'open' },
    {
      id: 'rq_north_csv', kind: 'question', link: 'ln_north', from: NORTHWIND, at: '2026-10-09T09:05:00Z', expires_at: at('2026-10-09T09:05:00Z', 5), title: 'Tariff export format', text: 'Can you send the Q3 tariff reconciliation as CSV instead of XLSX?', state: 'open',
      options: [{ key: 'csv', label: 'Yes, CSV from now on' }, { key: 'both', label: 'Both this quarter' }, { key: 'no', label: 'No, keep XLSX' }],
    },
    { id: 'rq_int_drop', kind: 'scope', link: 'ln_int', from: ownWs('INT'), at: '2026-10-08T16:40:00Z', expires_at: at('2026-10-08T16:40:00Z', 7), scope: 'drop', title: 'INT asks to send Drop files', text: 'INT wants to drop schema exports into this workspace\'s inbox.', state: 'open' },
    { id: 'rq_north_q2', kind: 'question', link: 'ln_north', from: NORTHWIND, at: '2026-10-06T08:00:00Z', expires_at: at('2026-10-06T08:00:00Z', 5), title: 'Reading cut-off', text: 'Is the monthly reading cut-off still the 3rd at 06:00 UTC?', state: 'answered', options: [{ key: 'yes', label: 'Yes' }, { key: 'no', label: 'No' }], decided: { at: '2026-10-06T09:14:00Z', by: 'p_mara', option: 'yes' } },
  ]
  const sent: Sent[] = [
    { id: 'out_1', link: 'ln_int', ticket: 'DEMO-0041', title: 'Add billing reconciliation tests', at: '2026-10-07T14:00:00Z', by: 'p_mara', deadline: at('2026-10-07T14:00:00Z', 3), state: 'accepted' },
    { id: 'out_2', link: 'ln_int', ticket: 'DEMO-0042', title: 'Normalize meter reading timestamps to UTC', at: '2026-10-09T08:45:00Z', by: 'p_sev', deadline: at('2026-10-09T08:45:00Z', 3), state: 'waiting' },
  ]
  const entries: LogEntry[] = [
    log('lg_1', '2026-06-02T09:30:00Z', 'ln_contoso', 'paired', 'Linked with Contoso Analytics · BI through the relay. Comparison codes matched.', { by: 'p_sev' }),
    log('lg_2', '2026-07-16T13:00:00Z', 'ln_north', 'paired', 'Linked with Northwind Grid · OPS through the relay. Comparison codes matched.', { by: 'p_sev' }),
    log('lg_3', '2026-09-12T08:10:00Z', 'ln_int', 'paired', 'Linked with INT · Internal on this machine.', { by: 'p_sev' }),
    log('lg_4', '2026-09-30T15:12:00Z', 'ln_contoso', 'revoked', 'Revoked the link. Contoso was told with a signed revoke envelope.', { by: 'p_sev' }),
    log('lg_5', '2026-10-06T08:00:00Z', 'ln_north', 'request_received', 'Question received: "Reading cut-off".'),
    log('lg_6', '2026-10-06T09:14:00Z', 'ln_north', 'answered', 'Answered "Reading cut-off": Yes. Sent back through the relay.', { by: 'p_mara' }),
    log('lg_7', '2026-10-07T14:00:00Z', 'ln_int', 'sent', 'Handed off DEMO-0041 to INT.', { by: 'p_mara', ticket: 'DEMO-0041' }),
    log('lg_8', '2026-10-07T16:30:00Z', 'ln_int', 'status', 'INT accepted the handoff of DEMO-0041.', { ticket: 'DEMO-0041' }),
    log('lg_9', '2026-10-08T07:20:00Z', 'ln_north', 'sent', 'Dropped tariff-q3.xlsx to Northwind (sealed to their exchange key).', { by: 'p_sev' }),
    log('lg_10', '2026-10-08T11:02:00Z', 'ln_int', 'refused', 'An agent tried to hand off DEMO-0044 to INT. Refused: restricted tickets never leave this workspace.', { ticket: 'DEMO-0044' }),
    log('lg_11', '2026-10-08T16:40:00Z', 'ln_int', 'request_received', 'INT asks to also send Drop files.'),
    log('lg_12', '2026-10-09T08:45:00Z', 'ln_int', 'sent', 'Handed off DEMO-0042 to INT.', { by: 'p_sev', ticket: 'DEMO-0042' }),
    log('lg_13', '2026-10-09T09:05:00Z', 'ln_north', 'request_received', 'Question received: "Tariff export format".'),
    log('lg_14', '2026-10-09T10:20:00Z', 'ln_int', 'request_received', 'Ticket handoff received: "Meter schema v3: update the DEMO import".'),
    log('lg_15', '2026-10-09T10:52:00Z', null, 'request_received', 'Link request from Fabrikam Energy · DATA (through the relay).'),
  ]
  return { links, requests, sent, log: entries, next: 1 }
}

const ORGS: Peer[] = [
  { name: 'Northwind Grid · METER', owner: 'Ole Sund', org: 'Northwind Grid AG' },
  { name: 'Tailspin Solar · OPS', owner: 'Mia Kovac', org: 'Tailspin Solar' },
  { name: 'Wingtip Water · DATA', owner: 'Ana Ruiz', org: 'Wingtip Water' },
  { name: 'Litware Labs · ML', owner: 'Kenji Ito', org: 'Litware Labs' },
  { name: 'Adatum Audit · FIN', owner: 'Paula Stein', org: 'Adatum Audit' },
]
const HANDOFF_TITLES = ['Backfill September readings', 'Add the new tariff zone to the seed', 'Check the outage export for gaps', 'Rename the region codes in the forecast', 'Fix the late meters report', 'Add a freshness test to the grid feed', 'Map the new substation ids']
const QUESTIONS = ['Can you share the reconciliation rules for credit notes?', 'Is the forecast in local time or UTC?', 'Who owns the meter master data on your side?', 'May we reuse your dbt macros for proration?', 'When does the September close finish?']

function seedBusy(ws: string, store: MockStore, rng: Rng) {
  const state = seedState(ws, store)
  const prefix = store.workspaces.find((w) => w.id === ws)?.prefix ?? ''
  if (prefix === 'CLI') {
    state.links.push(link('ln_demo', ownWs('DEMO'), 'relay', ['handoff', 'status'], ['handoff', 'question', 'status', 'drop'], '2026-09-20T10:00:00Z', 90, '2026-10-09T07:40:00Z'))
    return state
  }
  if (prefix !== 'DEMO') return state
  state.links.push(link('ln_cli', ownWs('CLI'), 'relay', ['handoff', 'question', 'status', 'drop'], ['handoff', 'status'], '2026-09-20T10:00:00Z', 90, '2026-10-09T07:40:00Z'))
  ORGS.forEach((p, i) => {
    const paired = at(NOW, -rng.int(20, 300))
    state.links.push(link(`ln_b${i + 1}`, p, 'relay', rng.sample(KINDS, rng.int(1, 3)), rng.sample(KINDS, rng.int(1, 3)), paired, rng.pick([90, 365]), at(NOW, -rng.next() * 9), i === 4 ? { revoked: { at: at(NOW, -12), by: 'p_sev' } } : {}))
  })
  // Only usable links carry traffic: requests and handoffs never go over a revoked or expired link.
  const live = state.links.filter((l) => usable(l, NOW))
  const from = (l: Link) => l.peer
  // More open requests between DEMO, INT and CLI, and from the organisations.
  for (let i = 0; i < 7; i++) {
    const l = i < 4 ? live.find((x) => x.id === (i % 2 ? 'ln_cli' : 'ln_int'))! : rng.pick(live)
    const when = at(NOW, -rng.next() * 2)
    const handoff = i < 4 || rng.chance(0.5)
    state.requests.push(
      handoff
        ? { id: `rq_b${i + 1}`, kind: 'handoff', link: l.id, from: from(l), at: when, expires_at: at(when, 3), title: HANDOFF_TITLES[i % HANDOFF_TITLES.length], text: `${l.peer.name} hands this over: ${HANDOFF_TITLES[i % HANDOFF_TITLES.length].toLowerCase()}.`, state: 'open' }
        : { id: `rq_b${i + 1}`, kind: 'question', link: l.id, from: from(l), at: when, expires_at: at(when, 5), title: 'Question', text: rng.pick(QUESTIONS), options: [{ key: 'yes', label: 'Yes' }, { key: 'no', label: 'No' }, { key: 'later', label: 'Ask us next week' }], state: 'open' },
    )
    state.log.push(log(`lg_b_rq${i + 1}`, when, l.id, 'request_received', handoff ? `Ticket handoff received: "${HANDOFF_TITLES[i % HANDOFF_TITLES.length]}".` : 'Question received.'))
  }
  // Handoffs this workspace sent: never a restricted ticket.
  const open = briefs(store, ws).filter((b) => !b.restricted)
  const targets = live.filter((l) => l.theyAccept.includes('handoff'))
  for (let i = 0; i < 14 && open.length && targets.length; i++) {
    const t = rng.pick(open)
    const l = rng.pick(targets)
    const when = at(NOW, -rng.next() * 12)
    const st = rng.pick(['waiting', 'accepted', 'accepted', 'done', 'denied'] as const)
    state.sent.push({ id: `out_b${i + 1}`, link: l.id, ticket: t.key, title: t.title, at: when, by: rng.pick(['p_sev', 'p_mara']), deadline: at(when, 3), state: st })
    state.log.push(log(`lg_b_out${i + 1}`, when, l.id, 'sent', `Handed off ${t.key} to ${l.peer.name}.`, { by: 'p_sev', ticket: t.key }))
  }
  // A long tail of traffic for the log: status, files, answers, and the refusals the host wrote for agents.
  const restricted = briefs(store, ws).filter((b) => b.restricted)
  for (let i = 0; i < 90; i++) {
    const l = rng.pick(state.links)
    const when = at(NOW, -rng.next() * 30)
    const k = rng.int(0, 3)
    const r = restricted.length && k === 3 ? rng.pick(restricted) : null
    state.log.push(
      r
        ? log(`lg_b${i + 1}`, when, l.id, 'refused', `An agent tried to hand off ${r.key} to ${l.peer.name}. Refused: restricted tickets never leave this workspace.`, { ticket: r.key })
        : k === 0
          ? log(`lg_b${i + 1}`, when, l.id, 'status', `${l.peer.name} reports progress on a handed-off ticket.`)
          : k === 1
            ? log(`lg_b${i + 1}`, when, l.id, 'received', `Received export-${100 + i}.csv into the Drop inbox.`)
            : log(`lg_b${i + 1}`, when, l.id, 'answered', 'Answered a question. Sent back.', { by: rng.pick(['p_sev', 'p_mara']) }),
    )
  }
  return state
}

// ------------------------------------------------------------------ state access

const linksOf = (s: Record<string, unknown>) => s.links as Link[]
const requestsOf = (s: Record<string, unknown>) => s.requests as Request[]
const sentOf = (s: Record<string, unknown>) => s.sent as Sent[]
const logOf = (s: Record<string, unknown>) => s.log as LogEntry[]
const navOf = (s: Record<string, unknown>, viewer: string): Nav => ((s.nav ?? {}) as Record<string, Nav>)[viewer] ?? {}
const setNav = (s: Record<string, unknown>, viewer: string, patch: Partial<Nav>) => {
  const all = (s.nav ??= {}) as Record<string, Nav>
  all[viewer] = { ...navOf(s, viewer), ...patch }
}
const nextId = (s: Record<string, unknown>, p: string) => {
  const n = (s.next as number) ?? 1
  s.next = n + 1
  return `${p}_${n}`
}

type LinkState = 'active' | 'expiring' | 'expired' | 'revoked'
const linkState = (l: Link, now: string): LinkState => {
  if (l.revoked) return 'revoked'
  const left = Date.parse(l.expires_at) - Date.parse(now)
  return left <= 0 ? 'expired' : left <= 7 * DAY ? 'expiring' : 'active'
}
const usable = (l: Link, now: string) => linkState(l, now) === 'active' || linkState(l, now) === 'expiring'
const isOpen = (r: Request, now: string) => r.state === 'open' && Date.parse(r.expires_at) > Date.parse(now)
const isOwner = (c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>) => c.store.roleIn(c.ws, c.viewer) === 'owner'
const nameIn = (c: Pick<AddonCtx, 'store' | 'ws'>, person: string) => c.store.workspaces.find((w) => w.id === c.ws)?.members.find((m) => m.person === person)?.name ?? person
const relayLink = (c: Pick<AddonCtx, 'store' | 'ws'>) => relayState(c.store, c.ws).link
const carrierText = (carrier: Carrier, relay?: string) => (carrier === 'local' ? 'Same machine' : relay === undefined ? 'Relay' : relay === 'online' ? 'Relay · online' : `Relay · ${relay}: messages wait`)
const left = (iso: string, now: string) => {
  const ms = Date.parse(iso) - Date.parse(now)
  if (ms <= 0) return 'expired'
  const d = Math.floor(ms / DAY)
  if (d >= 1) return `in ${d} ${d === 1 ? 'day' : 'days'}`
  return ms >= 3_600_000 ? `in ${Math.round(ms / 3_600_000)} h` : `in ${Math.max(1, Math.ceil(ms / 60_000))} min`
}
/**
 * The comparison code both screens show. Each host derives it on its own from both sides' pairing values (in v2: both
 * pinned workspace keys and the pairing transcript); it is never taken from what the other side sends. The mock derives
 * it from the two workspace names, the same way on both sides.
 */
export const comparisonCode = (prefix: string, peer: Peer) => fingerprintOf(['ws:' + prefix, 'peer:' + (peer.ws ?? peer.name)].sort().join('|'))
/** What the peer asked for, kept to the kinds this host knows (the host drops unknown kinds when a request arrives). */
const wantsOf = (r: Request) => KINDS.filter((k) => (r.wants ?? []).includes(k))
const offersOf = (r: Request) => KINDS.filter((k) => (r.offers ?? []).includes(k))
const termsOf = (r: Request): Terms => r.terms ?? { recv: wantsOf(r), send: offersOf(r), days: 90 }
const kindsTag = (k: Kind[]) => (k.length ? k.join('+') : 'none')
/**
 * A decision's id. A pairing request's id carries everything the signature authorises (core shows the id in full in
 * its covers): the comparison code, the carrier, what they may send us, what we may send them and the expiry. Changing
 * the terms changes the id, so a prompt opened on old terms is refused by core (409 decision.closed).
 */
const decisionId = (r: Request, prefix: string) => {
  if (r.kind !== 'pairing') return `req.${r.id}`
  const t = termsOf(r)
  return `pair.${r.id}.${comparisonCode(prefix, r.from)}.${r.carrier === 'local' ? 'spool' : 'relay'}.recv-${kindsTag(t.recv)}.send-${kindsTag(t.send)}.${t.days}d`
}
const DECISION_ACTION: Record<Request['kind'], string> = { pairing: 'decide_pairing', scope: 'decide_scope', handoff: 'decide', question: 'decide' }
/** Text a peer chose (a name, an owner): refused when it hides characters (bidi overrides, zero-width marks, controls). */
const HIDDEN_CHAR = /[\p{Cc}\p{Cf}\u2028\u2029]/u
/** Peer text inside markdown: every markdown punctuation character escaped, so a name never becomes a link or a heading. */
const md = (s: string) => s.replace(/[\\`*_{}\[\]()#+\-.!|<>~]/g, (ch) => `\\${ch}`)
const prefixOf = (c: Pick<AddonCtx, 'store' | 'ws'>) => c.store.workspaces.find((w) => w.id === c.ws)?.prefix ?? ''

/** Write one record: the addon's own log (per link) and, for live actions, the workspace log (Activity). */
function record(c: AddonCtx, linkId: string | null, verb: string, text: string, extra: { by?: string; ticket?: string } = {}) {
  const entry: LogEntry = { id: nextId(c.state, 'lg'), at: c.store.now(), link: linkId, type: `links.${verb}`, epoch: relayEpoch(c.store, c.ws), text, ...extra }
  logOf(c.state).push(entry)
  c.store.appendWs(c.ws, { type: entry.type as 'links.paired', actor: ADDON, link: linkId, ...(extra.by ? { person: extra.by } : {}), ...(extra.ticket ? { ticket: extra.ticket } : {}) })
}

// ------------------------------------------------------------------ decisions (core renders and signs them)

function toDecision(r: Request, l: Link | undefined, now: string, prefix: string): AddonDecision {
  const base = { kind: 'decision' as const, id: decisionId(r, prefix), addon: 'links', action: DECISION_ACTION[r.kind] }
  if (r.kind === 'pairing') {
    const t = termsOf(r)
    return {
      ...base,
      title: `Link request from ${r.from.name}`,
      question: `Link ${r.from.name} with this workspace ${r.carrier === 'local' ? 'on this machine' : 'through the relay'} on these terms?`,
      detail: `Comparison code ${comparisonCode(prefix, r.from)} (worked out on this machine). Ask ${r.from.owner}${r.from.org ? ` (${r.from.org})` : ''} to read the code on their screen and accept only if it is the same. Terms you sign: they may send you ${kindsText(t.recv)}; you may send them ${kindsText(t.send)}; the link expires after ${t.days} days. They asked to send ${kindsText(wantsOf(r))} and to receive ${kindsText(offersOf(r))}: narrow the terms or change the expiry in Workspace links → Requests before you accept. The request expires ${left(r.expires_at, now)}.`,
      options: [{ key: 'accept', label: 'Codes match: link on these terms', primary: true }, { key: 'deny', label: 'Deny' }],
      // Core shows each term as its own line in the signing covers, checks them on the answer and records them.
      terms: {
        peer: r.from.name,
        comparison_code: comparisonCode(prefix, r.from),
        carrier: carrierArg(r.carrier ?? 'relay'),
        they_may_send_us: kindsText(t.recv),
        we_may_send_them: kindsText(t.send),
        expires_after: `${t.days} days`,
      },
    }
  }
  if (r.kind === 'scope')
    return {
      ...base,
      title: r.title,
      question: `Let ${r.from.name} send ${KIND_LABEL[r.scope!]} to this workspace?`,
      detail: `${r.text} Today they may send: ${kindsText(l?.accepts ?? [])}.`,
      options: [{ key: 'allow', label: 'Allow', primary: true }, { key: 'deny', label: 'Deny' }],
    }
  if (r.kind === 'question') return { ...base, title: `Question from ${r.from.name}: ${r.title}`, question: r.text, detail: `The answer goes back ${l?.carrier === 'local' ? 'on this machine' : 'through the relay'}.`, options: (r.options ?? []).map((o, i) => ({ ...o, ...(i === 0 ? { primary: true } : {}) })) }
  return {
    ...base,
    title: `Ticket from ${r.from.name}`,
    question: `Accept "${r.title}" into the Backlog?`,
    detail: `${r.text} It arrives as untrusted data: nothing runs until someone starts it. Reply due ${left(r.expires_at, now)}.`,
    options: [{ key: 'accept', label: 'Accept into Backlog', primary: true }, { key: 'deny', label: 'Deny' }],
  }
}

/** Requests this viewer may decide now: open, on a usable link (a pairing request has none yet), and theirs to decide. */
function openRequestsFor(state: Record<string, unknown>, c: Omit<AddonCtx, 'body' | 'state'>): Request[] {
  const now = c.store.now()
  const role = c.store.roleIn(c.ws, c.viewer)
  if (!role || !can(role, 'addon.decide')) return []
  const owner = role === 'owner'
  const links = new Map(linksOf(state).map((l) => [l.id, l]))
  return requestsOf(state).filter((r) => isOpen(r, now) && (!r.link || (links.has(r.link) && usable(links.get(r.link)!, now))) && (owner || !OWNER_KINDS.includes(r.kind)))
}

// ------------------------------------------------------------------ what a signature covers

/** The carrier as the signature names it. */
const carrierArg = (carrier: Carrier) => (carrier === 'local' ? 'spool (same machine)' : `relay ${RELAY_URL}`)
/**
 * The args of "Codes match: sign": everything the link authorises, so core's covers name it (and the host refuses the
 * signature when any of it no longer matches the pairing, 409 links.stale).
 */
function pairingArgs(p: Pending) {
  return { pairing: p.id, code: p.joined?.fingerprint ?? '', peer: p.peer.name, carrier: carrierArg(p.carrier), they_may_send: kindsText(p.accepts), we_may_send: kindsText(p.joined?.theyAccept ?? []), expires_days: p.days }
}
/** What a handoff sends, in words: the title, the body sections that are filled and the attachments. */
function sendsOf(t: { body: Record<string, string | undefined>; artifacts: { name: string }[] }) {
  const sections = Object.entries(t.body).filter(([, v]) => !!v?.trim()).map(([k]) => k.replace(/_/g, ' '))
  return `title; sections: ${sections.length ? sections.join(', ') : 'none'}; attachments: ${t.artifacts.length ? t.artifacts.map((a) => a.name).join(', ') : 'none'}`
}
const HANDOFF_DEADLINE = '3 days after sending'
/** The args of "Sign and send": the link and peer, the carrier, the ticket and exactly what crosses, and the deadline. */
function handoffArgs(l: Link, t: { key: string; title: string; body: Record<string, string | undefined>; artifacts: { name: string }[] }) {
  return { link: l.id, to: l.peer.name, carrier: carrierArg(l.carrier), ticket_key: t.key, title: t.title, sends: sendsOf(t), deadline: HANDOFF_DEADLINE }
}
/** The keys whose posted value differs from what the host would sign now (empty: the signature still matches). */
const staleKeys = (body: Record<string, unknown>, want: Record<string, string | number>) => Object.keys(want).filter((k) => String(body[k] ?? '') !== String(want[k]))
const stale = (keys: string[]) => conflict('links.stale', `What you signed no longer matches: ${keys.join(', ')}.`, 'Reload and sign again.')

// ------------------------------------------------------------------ the page (built here, drawn by core)

const KIND_WORD: Record<Request['kind'], string> = { pairing: 'Link request', handoff: 'Ticket handoff', question: 'Question', scope: 'Wider scope' }

function page(state: Record<string, unknown>, c: Omit<AddonCtx, 'body' | 'state'>) {
  const now = c.store.now()
  const owner = isOwner(c)
  const role = c.store.roleIn(c.ws, c.viewer)
  const relay = relayLink(c)
  const nav = navOf(state, c.viewer)
  const links = linksOf(state)
  const byId = new Map(links.map((l) => [l.id, l]))
  const seesLog = (e: LogEntry) => !e.ticket || canSeeTicket(c, e.ticket)
  const logs = logOf(state).filter(seesLog).sort((a, b) => b.at.localeCompare(a.at))
  const linkName = (id: string | null) => (id ? (byId.get(id)?.peer.name ?? id) : '–')
  const logRow = (e: LogEntry) => ({ id: e.id, when: fmtWhen(e.at, now), link: linkName(e.link), event: e.type, text: e.text, by: e.by ? nameIn(c, e.by) : 'the addon', ticket: e.ticket ?? '', epoch: e.epoch })

  const linkRows = links
    .slice()
    .sort((a, b) => Number(!!a.revoked) - Number(!!b.revoked) || b.last_at.localeCompare(a.last_at))
    .map((l) => {
      const st = linkState(l, now)
      return {
        id: l.id,
        peer: l.peer.name,
        carrier: carrierText(l.carrier, l.carrier === 'relay' && usable(l, now) ? relay : undefined),
        theySend: kindsText(l.accepts),
        weSend: kindsText(l.theyAccept),
        expires: st === 'revoked' ? '–' : left(l.expires_at, now),
        last: fmtWhen(l.last_at, now),
        state: st,
        canRevoke: owner && st !== 'revoked',
      }
    })
  const active = links.filter((l) => usable(l, now))
  const selected = nav.link ? byId.get(nav.link) : undefined
  const linkTable = {
    type: 'table',
    columns: [
      { key: 'peer', label: 'Peer' },
      { key: 'carrier', label: 'Carrier' },
      { key: 'theySend', label: 'They may send us' },
      { key: 'weSend', label: 'We may send them', hideBelow: 900 },
      { key: 'expires', label: 'Expires', hideBelow: 700 },
      { key: 'last', label: 'Last activity', hideBelow: 1100 },
      { key: 'state', label: 'State', cell: 'state' },
    ],
    rows: linkRows,
    empty: 'No links yet. Set one up in the Set up tab.',
    rowOpen: { action: 'select_link', args: { id: '$row.id' } },
    rowActions: [
      { label: 'Details', action: 'select_link', args: { id: '$row.id' }, variant: 'ghost', primary: true },
      { label: 'Revoke', action: 'revoke', args: { id: '$row.id', peer: '$row.peer' }, variant: 'danger', when: '$row.canRevoke' },
    ],
  }
  const detail = selected
    ? [
        { type: 'markdown', text: `### ${md(selected.peer.name)}` },
        {
          type: 'kv',
          pairs: [
            { label: 'Owner', value: selected.peer.org ? `${selected.peer.owner}, ${selected.peer.org}` : selected.peer.owner },
            { label: 'Carrier', value: selected.carrier === 'local' ? `Same machine (spool, ${MACHINE[c.store.workspaces.find((w) => w.id === c.ws)?.prefix ?? ''] ?? 'this machine'})` : `Relay ${RELAY_URL} · ${relay}` },
            { label: 'Comparison code', value: selected.fingerprint, mono: true },
            { label: 'Paired', value: `${fmtWhen(selected.paired_at, now)} by ${nameIn(c, selected.paired_by)}` },
            { label: selected.revoked ? 'Revoked' : 'Expires', value: selected.revoked ? `${fmtWhen(selected.revoked.at, now)} by ${nameIn(c, selected.revoked.by)}` : left(selected.expires_at, now) },
          ],
        },
        {
          type: 'table',
          columns: [{ key: 'when', label: 'When' }, { key: 'text', label: 'What' }, { key: 'by', label: 'By' }, { key: 'ticket', label: 'Ticket', cell: 'ticket', hideBelow: 700 }],
          rows: logs.filter((e) => e.link === selected.id).slice(0, 8).map(logRow),
          empty: 'Nothing has crossed this link yet.',
        },
      ]
    : [{ type: 'markdown', text: 'Select a peer to see its details and latest log entries.' }]

  // Requests: core draws each open one as a decision (signed in core's prompt); a table lists them all.
  const deciders = openRequestsFor(state, c)
  const canDecide = !!role && can(role, 'addon.decide')
  const prefix = prefixOf(c)
  const requests = requestsOf(state).slice().sort((a, b) => b.at.localeCompare(a.at))
  const onLiveLink = (r: Request) => !r.link || (byId.has(r.link) && usable(byId.get(r.link)!, now))
  // Pairing requests: the owner may narrow the terms and pick the expiry before signing (never more than asked).
  const termsForms = new Map(deciders
    .filter((r) => r.kind === 'pairing')
    .map((r) => {
      const t = termsOf(r)
      const props: Record<string, unknown> = { request: { type: 'string', enum: [r.id], default: r.id }, rev: { type: 'integer' } }
      for (const k of wantsOf(r)) props[`recv_${k}`] = { type: 'boolean', title: `They may send us ${KIND_LABEL[k]}`, default: t.recv.includes(k) }
      for (const k of offersOf(r)) props[`send_${k}`] = { type: 'boolean', title: `We may send them ${KIND_LABEL[k]}`, default: t.send.includes(k) }
      props.days = { type: 'integer', title: 'Link expires after', enum: [30, 90, 365], default: t.days }
      const nodes: unknown[] = [
        { type: 'markdown', text: `### Terms for ${md(r.from.name)}\nYou can narrow what they asked for and pick the expiry. The decision below names these terms line by line when you sign; it cannot be accepted while changes here are unsaved.` },
        {
          type: 'form',
          schema: { type: 'object', properties: props },
          uiSchema: { request: { 'ui:widget': 'hidden' }, rev: { 'ui:widget': 'hidden' }, days: { 'ui:enumNames': ['30 days', '90 days', '365 days'] } },
          formData: { request: r.id, rev: r.termsRev ?? 0, days: t.days, ...Object.fromEntries(wantsOf(r).map((k) => [`recv_${k}`, t.recv.includes(k)])), ...Object.fromEntries(offersOf(r).map((k) => [`send_${k}`, t.send.includes(k)])) },
          action: 'set_pairing_terms',
          submitLabel: 'Use these terms',
          // With Cancel, core tracks unsaved edits: the decision below cannot be accepted until they are saved or cancelled.
          cancel: { label: 'Cancel changes', action: 'reset_pairing_terms' },
          guards: decisionId(r, prefix),
        },
      ]
      return [r.id, nodes] as const
    }))
  const requestRows = requests.map((r) => ({
    id: r.id,
    kind: KIND_WORD[r.kind],
    from: r.from.name,
    title: r.title,
    received: fmtWhen(r.at, now),
    decides: OWNER_KINDS.includes(r.kind) ? 'An owner' : 'Owner or maintainer',
    state: r.state === 'open' && !isOpen(r, now) ? 'expired' : r.state === 'open' && !onLiveLink(r) ? 'closed' : r.state === 'open' ? 'open' : r.decided ? `${r.state} by ${nameIn(c, r.decided.by)}` : r.state,
  }))
  const sentRows = sentOf(state)
    .filter((s) => canSeeTicket(c, s.ticket))
    .sort((a, b) => b.at.localeCompare(a.at))
    .map((s) => {
      const l = byId.get(s.link)
      return { id: s.id, ticket: s.ticket, title: s.title, to: l?.peer.name ?? s.link, by: nameIn(c, s.by), sent: fmtWhen(s.at, now), state: s.state === 'waiting' && l?.carrier === 'relay' && relay !== 'online' ? 'queued' : s.state }
    })
  const handoffLinks = active.filter((l) => l.theyAccept.includes('handoff'))
  // Restricted tickets never leave this workspace, so they are not offered at all.
  const tickets = c.store.listTickets(c.ws).filter((t) => !t.restricted).slice(0, 60)
  const draft = nav.draft && byId.get(nav.draft.link) && canSeeTicket(c, nav.draft.ticket) ? nav.draft : undefined
  const handoff = role === 'viewer'
    ? []
    : [
        { type: 'markdown', text: '## Hand off a ticket\nPick a linked workspace that accepts ticket handoffs and one of your tickets. You sign each handoff. Restricted tickets never leave this workspace.' },
        {
          type: 'form',
          schema: {
            type: 'object',
            required: ['link', 'ticket'],
            properties: {
              link: { type: 'string', title: 'To', enum: handoffLinks.map((l) => l.id) },
              ticket: { type: 'string', title: 'Ticket', enum: tickets.map((t) => t.key) },
            },
          },
          uiSchema: { link: { 'ui:enumNames': handoffLinks.map((l) => `${l.peer.name} (${carrierText(l.carrier)})`) }, ticket: { 'ui:enumNames': tickets.map((t) => `${t.key} · ${t.title}`) } },
          formData: draft ?? {},
          action: 'prepare_handoff',
          submitLabel: 'Review handoff',
        },
        ...(draft
          ? [
              {
                type: 'alert',
                tone: 'info',
                title: `Ready: ${draft.ticket} to ${byId.get(draft.link)!.peer.name}`,
                text: `What crosses is listed in the signing prompt: the title, the sections and the attachments, sealed to their exchange key ${byId.get(draft.link)!.carrier === 'local' ? 'on this machine' : 'through the relay'}. They decide whether to accept it; you get the result back.`,
              },
              {
                type: 'stack',
                direction: 'row',
                fit: true,
                children: [
                  { type: 'button', label: 'Sign and send', action: 'send_handoff', variant: 'primary', args: handoffArgs(byId.get(draft.link)!, c.store.ticket(draft.ticket)!) },
                  { type: 'button', label: 'Cancel', action: 'clear_handoff', variant: 'ghost' },
                ],
              },
            ]
          : []),
      ]

  // Log: filter by link (per viewer).
  const logLink = nav.logLink && byId.get(nav.logLink) ? nav.logLink : 'all'
  const logRows = logs.filter((e) => logLink === 'all' || e.link === logLink).slice(0, 200).map(logRow)

  // Set up: the pairing in progress (one at a time), else the form.
  const pending = state.pending as Pending | undefined
  const linkedWs = new Set(links.filter((l) => usable(l, now) && l.peer.ws).map((l) => l.peer.ws!))
  const ownPeers = c.store.workspaces.filter((w) => w.id !== c.ws && w.members.some((m) => m.person === c.viewer && m.role === 'owner') && !linkedWs.has(w.prefix))
  const peerChoices = [...ownPeers.map((w) => `ws:${w.prefix}`), 'other']
  const peerNames = [...ownPeers.map((w) => `${WS_NAME[w.prefix] ?? w.prefix} · your workspace on ${MACHINE[w.prefix] === MACHINE[prefix] ? 'this machine' : (MACHINE[w.prefix] ?? 'another machine')}`), 'Another person or organisation']
  const relayNote = relay === 'online' ? `The relay is online (${RELAY_URL}).` : `The relay is ${relay}: relay pairing waits until you connect it in Settings → Relay & devices.`
  let setup: unknown[]
  if (!owner) setup = [{ type: 'alert', tone: 'info', title: 'Owners set up links', text: 'Pairing links two workspaces\' trust, so an owner of each side signs it. Ask an owner of this workspace.' }]
  else if (pending) {
    const expired = Date.parse(pending.started_at) + PAIRING_MS <= Date.parse(now) && !pending.joined
    const joinExpired = !!pending.joined && Date.parse(pending.joined.at) + PAIRING_MS <= Date.parse(now)
    const waitRelay = pending.carrier === 'relay' && relay !== 'online'
    setup = [
      { type: 'markdown', text: `## Pairing with ${md(pending.peer.name)}` },
      {
        type: 'kv',
        pairs: [
          { label: 'Carrier', value: pending.carrier === 'local' ? 'Same machine (no relay needed)' : `Relay ${RELAY_URL} · ${relay}` },
          { label: 'They may send us', value: kindsText(pending.accepts) },
          { label: 'Expires after', value: `${pending.days} days` },
          { label: 'Pairing code for the other owner', value: pending.pairing_code, mono: true },
          { label: 'Code valid', value: pending.joined ? 'used' : expired ? 'expired: cancel and start again' : `${left(plus(pending.started_at, PAIRING_MS), now)}, one use` },
        ],
      },
      ...(pending.joined
        ? [
            { type: 'alert', tone: 'success', title: `${pending.peer.name} entered the code and signed on their side`, text: `They accept from you: ${kindsText(pending.joined.theyAccept)}. Compare the code below with the one on their screen; read it aloud or over chat.` },
            { type: 'stat', label: 'Comparison code (both screens)', value: pending.joined.fingerprint, hint: 'Same on both screens: sign. Different: cancel; someone else may have the code.' },
            {
              type: 'stack',
              direction: 'row',
              fit: true,
              children: [
                { type: 'button', label: 'Codes match: sign', action: 'confirm_pairing', variant: 'primary', args: pairingArgs(pending), ...(joinExpired ? { disabled: 'More than 10 minutes since they joined: cancel and pair again.' } : {}) },
                { type: 'button', label: 'Codes differ: cancel', action: 'cancel_pairing', variant: 'ghost' },
              ],
            },
          ]
        : [
            {
              type: 'markdown',
              text:
                pending.carrier === 'local'
                  ? `${md(pending.peer.name)} runs on this machine: its owner sees the request in its own dashboard (Workspace links → Set up) and enters the code there.`
                  : `Give the pairing code to ${pending.peer.owner === '?' ? 'the other owner' : md(pending.peer.owner)}. They enter it in their own dashboard (Workspace links → Set up); their host finds this workspace's card in the relay directory.`,
            },
            {
              type: 'stack',
              direction: 'row',
              fit: true,
              children: [
                { type: 'button', label: 'Simulate: the other owner enters the code', action: 'simulate_peer', variant: 'secondary', ...(waitRelay ? { disabled: relayNote } : expired ? { disabled: 'The code expired.' } : {}) },
                { type: 'button', label: 'Cancel pairing', action: 'cancel_pairing', variant: 'ghost' },
              ],
            },
          ]),
    ]
  } else
    setup = [
      { type: 'markdown', text: `## Link this workspace with another\nPick the carrier, who, what they may send you and for how long. You get a pairing code for the other owner, then both screens show a comparison code and each owner signs.\n\nSame machine needs no relay. Another machine or organisation goes through the relay. ${relayNote} There is no direct network path (D11): for machines in one office network, run an internal relay.` },
      {
        type: 'form',
        schema: {
          type: 'object',
          required: ['carrier', 'peer', 'days'],
          properties: {
            carrier: { type: 'string', title: 'Carrier', enum: ['local', 'relay'], default: 'relay' },
            peer: { type: 'string', title: 'Link with', enum: peerChoices },
            name: { type: 'string', title: 'Their name (another person or organisation)', maxLength: 60 },
            owner: { type: 'string', title: 'Their owner (who reads you the code)', maxLength: 60 },
            handoff: { type: 'boolean', title: 'They may send us ticket handoffs', default: true },
            question: { type: 'boolean', title: 'They may send us questions', default: true },
            status: { type: 'boolean', title: 'They may send us status updates', default: true },
            drop: { type: 'boolean', title: 'They may send us Drop files', default: false },
            days: { type: 'integer', title: 'Link expires after', enum: [30, 90, 365], default: 90 },
          },
        },
        uiSchema: { carrier: { 'ui:enumNames': ['Same machine (no relay)', `Through the relay (${RELAY_URL})`] }, peer: { 'ui:enumNames': peerNames }, days: { 'ui:enumNames': ['30 days', '90 days', '365 days'] } },
        formData: { carrier: 'relay', days: 90, handoff: true, question: true, status: true, drop: false },
        action: 'start_pairing',
        submitLabel: 'Start pairing',
      },
    ]

  const openCount = requests.filter((r) => isOpen(r, now) && onLiveLink(r)).length
  return {
    type: 'stack',
    children: [
      { type: 'alert', tone: 'info', title: 'Simulated', text: 'Pairing, requests and handoffs on this page are simulated: nothing leaves this machine. In orch v2 every envelope is sealed to the peer\'s exchange key and signed by this workspace.' },
      {
        type: 'stack',
        direction: 'row',
        children: [
          { type: 'stat', label: 'Active links', value: active.length, hint: `${links.filter((l) => linkState(l, now) === 'expiring').length} expiring within 7 days` },
          { type: 'stat', label: 'Open requests', value: openCount, hint: !canDecide ? 'owners and maintainers decide them' : deciders.length === openCount ? 'waiting for a decision' : `${deciders.length} for you to decide` },
          { type: 'stat', label: 'Relay', value: relay, hint: 'same-machine links do not need it' },
        ],
      },
      {
        type: 'tabs',
        id: 'links',
        tabs: [
          { id: 'links', label: 'Links', count: active.length, node: { type: 'stack', children: [linkTable, ...detail] } },
          {
            id: 'requests',
            label: 'Requests',
            count: openCount,
            node: {
              type: 'stack',
              children: [
                ...(deciders.length ? [{ type: 'markdown', text: '## Waiting for you\nSigned in orch\'s own prompt. Accepted tickets land in the Backlog as untrusted data.' }, ...deciders.flatMap((r) => [...(termsForms.get(r.id) ?? []), { type: 'decision', id: decisionId(r, prefix) }])] : []),
                { type: 'markdown', text: '## All requests' },
                {
                  type: 'table',
                  columns: [{ key: 'kind', label: 'Kind' }, { key: 'from', label: 'From' }, { key: 'title', label: 'About' }, { key: 'received', label: 'Received', hideBelow: 800 }, { key: 'decides', label: 'Who decides', hideBelow: 1000 }, { key: 'state', label: 'State', cell: 'state' }],
                  rows: requestRows,
                  empty: 'No requests.',
                },
                ...(role === 'viewer' ? [] : [{ type: 'stack', direction: 'row', fit: true, children: [{ type: 'button', label: 'Simulate: INT hands over a ticket', action: 'simulate_request', variant: 'ghost', ...(linkedWs.has('INT') && links.find((l) => l.peer.ws === 'INT' && usable(l, now))?.accepts.includes('handoff') ? {} : { disabled: 'Needs an active link with INT that accepts ticket handoffs.' }) }] }]),
                { type: 'markdown', text: '## Sent by this workspace' },
                {
                  type: 'table',
                  columns: [{ key: 'ticket', label: 'Ticket', cell: 'ticket' }, { key: 'title', label: 'Title' }, { key: 'to', label: 'To' }, { key: 'by', label: 'By', hideBelow: 900 }, { key: 'sent', label: 'Sent', hideBelow: 800 }, { key: 'state', label: 'State', cell: 'state' }],
                  rows: sentRows,
                  empty: 'Nothing sent yet.',
                },
                ...handoff,
              ],
            },
          },
          {
            id: 'log',
            label: 'Log',
            node: {
              type: 'stack',
              children: [
                {
                  type: 'form',
                  schema: { type: 'object', properties: { link: { type: 'string', title: 'Link', enum: ['all', ...links.map((l) => l.id)] } } },
                  uiSchema: { link: { 'ui:enumNames': ['All links', ...links.map((l) => l.peer.name)] } },
                  formData: { link: logLink },
                  action: 'filter_log',
                  live: true,
                },
                {
                  type: 'table',
                  columns: [{ key: 'when', label: 'When' }, { key: 'link', label: 'Link' }, { key: 'text', label: 'What' }, { key: 'by', label: 'By', hideBelow: 900 }, { key: 'ticket', label: 'Ticket', cell: 'ticket', hideBelow: 800 }, { key: 'event', label: 'Record', hideBelow: 1200 }, { key: 'epoch', label: 'Epoch', hideBelow: 1300 }],
                  rows: logRows,
                  empty: 'Nothing logged yet.',
                },
              ],
            },
          },
          { id: 'setup', label: 'Set up', node: { type: 'stack', children: setup } },
        ],
      },
    ],
  }
}

// ------------------------------------------------------------------ actions

const usableLink = (c: AddonCtx, id: unknown) => {
  const l = linksOf(c.state).find((x) => x.id === id)
  if (!l) return notFound('No such link.')
  if (!usable(l, c.store.now())) return conflict(`links.${linkState(l, c.store.now())}`, `The link with ${l.peer.name} is ${linkState(l, c.store.now())}.`, 'Pair again in Set up.')
  return l
}
/** A ticket that may cross: one of this workspace's, visible to the caller, and not restricted. */
function crossable(c: AddonCtx, key: unknown) {
  if (typeof key !== 'string' || !canSeeTicket(c, key)) return notFound(`No ticket ${String(key)}`)
  if (c.store.ticket(key)?.restricted) return conflict('links.restricted', `${key} is restricted: restricted tickets never leave this workspace.`, 'Hand off a ticket the whole workspace can see.')
  return key
}

registerAddon({
  name: 'links',
  seed: seedState,
  seedBusy,

  decisions(state, _pkg, c) {
    const byId = new Map(linksOf(state).map((l) => [l.id, l]))
    return openRequestsFor(state, c).map((r) => toDecision(r, r.link ? byId.get(r.link) : undefined, c.store.now(), prefixOf(c)))
  },

  view(state, c) {
    const now = c.store.now()
    return {
      // Raw lists are replaced by what this viewer may see (the page is built from the same rules).
      links: [],
      requests: [],
      sent: sentOf(state).filter((s) => canSeeTicket(c, s.ticket)).map((s) => s.id),
      log: [],
      nav: {},
      pending: isOwner(c) ? ((state.pending as Pending | undefined)?.id ?? null) : null,
      activeCount: linksOf(state).filter((l) => usable(l, now)).length,
      openCount: requestsOf(state).filter((r) => isOpen(r, now) && (!r.link || linksOf(state).some((l) => l.id === r.link && usable(l, now)))).length,
      page: page(state, c),
    }
  },

  actions: {
    select_link(c) {
      const l = linksOf(c.state).find((x) => x.id === c.body.id)
      if (!l) return notFound('No such link.')
      setNav(c.state, c.viewer, { link: l.id })
      return { ok: true, message: `Showing ${l.peer.name}.` }
    },
    filter_log(c) {
      const id = String(((c.body.formData ?? {}) as { link?: unknown }).link ?? 'all')
      if (id !== 'all' && !linksOf(c.state).some((l) => l.id === id)) return notFound('No such link.')
      setNav(c.state, c.viewer, { logLink: id === 'all' ? undefined : id })
      return { ok: true, message: 'Log filtered.' }
    },

    start_pairing(c) {
      if (c.state.pending) return conflict('links.pairing_open', 'A pairing is already in progress.', 'Finish or cancel it in Set up.')
      const f = (c.body.formData ?? {}) as Record<string, unknown>
      const carrier = f.carrier
      if (carrier !== 'local' && carrier !== 'relay') return invalid('Pick the carrier: same machine or the relay.')
      const days = Number(f.days ?? 90)
      if (![30, 90, 365].includes(days)) return invalid('A link expires after 30, 90 or 365 days.')
      const accepts = KINDS.filter((k) => f[k] === true)
      const prefix = c.store.workspaces.find((w) => w.id === c.ws)?.prefix ?? ''
      const raw = String(f.peer ?? '')
      let peer: Peer
      if (raw.startsWith('ws:')) {
        const other = c.store.workspaces.find((w) => w.prefix === raw.slice(3))
        if (!other || other.id === c.ws || !other.members.some((m) => m.person === c.viewer && m.role === 'owner')) return invalid('Pick one of the workspaces you own.')
        if (linksOf(c.state).some((l) => l.peer.ws === other.prefix && usable(l, c.store.now()))) return conflict('links.exists', `${other.prefix} is linked already.`, 'Revoke the link first to pair again.')
        if (carrier === 'local' && carrierOf(prefix, other.prefix) !== 'local') return invalid(`${other.prefix} runs on ${MACHINE[other.prefix] ?? 'another machine'}, not on this machine: pick the relay.`)
        peer = ownWs(other.prefix)
      } else if (raw === 'other') {
        if (carrier === 'local') return invalid('Same machine works only between your own workspaces on this machine: pick the relay.')
        const name = typeof f.name === 'string' ? f.name.trim() : ''
        const owner = typeof f.owner === 'string' ? f.owner.trim() : ''
        if (HIDDEN_CHAR.test(name) || HIDDEN_CHAR.test(owner)) return invalid('Names may not contain invisible, direction or control characters.')
        if (name.length < 2 || name.length > 60) return invalid('Name the other side (2 to 60 characters).')
        if (owner.length > 60) return invalid('The owner\'s name has at most 60 characters.')
        if (linksOf(c.state).some((l) => !l.peer.ws && l.peer.name === name && usable(l, c.store.now()))) return conflict('links.exists', `${name} is linked already.`, 'Revoke the link first to pair again.')
        peer = { name, owner: owner || '?' }
      } else return invalid('Pick who to link with.')
      const id = nextId(c.state, 'pr')
      c.state.pending = { id, carrier, peer, accepts, days, pairing_code: pairingCode(Number(id.slice(3))), started_at: c.store.now(), started_by: c.viewer } satisfies Pending
      record(c, null, 'pairing_started', `Started pairing with ${peer.name} (${carrier === 'local' ? 'same machine' : 'through the relay'}).`, { by: c.viewer })
      return { ok: true, message: `Pairing code ready for ${peer.name}. It is valid 10 minutes, for one use.`, changed: true }
    },
    // Mock only: what the other owner does on their side (enters the code, chooses what they accept, signs).
    simulate_peer(c) {
      const p = c.state.pending as Pending | undefined
      if (!p) return notFound('No pairing in progress.')
      if (p.joined) return { ok: true, message: `${p.peer.name} already entered the code.` }
      if (Date.parse(p.started_at) + PAIRING_MS <= Date.parse(c.store.now())) return conflict('links.code_expired', 'The pairing code expired.', 'Cancel and start again.')
      if (p.carrier === 'relay' && relayLink(c) !== 'online') return conflict('links.relay_offline', `The relay is ${relayLink(c)}.`, 'Connect it in Settings → Relay & devices, then try again.')
      // Each side works the comparison code out itself from both pairing values; nothing the other side sends sets it.
      p.joined = { at: c.store.now(), fingerprint: comparisonCode(prefixOf(c), p.peer), theyAccept: p.peer.ws ? ['handoff', 'question', 'status'] : ['question', 'drop'] }
      return { ok: true, message: `${p.peer.name} entered the code. Compare the codes.`, changed: true }
    },
    // Signed in core's prompt (manifest confirm: 'sign'): the covers name the peer, carrier, scopes both ways and expiry.
    confirm_pairing(c) {
      const p = c.state.pending as Pending | undefined
      if (!p || p.id !== c.body.pairing) return notFound('That pairing is not in progress.')
      if (!p.joined) return conflict('links.not_joined', `${p.peer.name} has not entered the code yet.`)
      if (c.body.code !== p.joined.fingerprint) return conflict('links.code_mismatch', 'That is not the comparison code of this pairing.', 'Cancel and start again.')
      if (Date.parse(p.joined.at) + PAIRING_MS <= Date.parse(c.store.now())) return conflict('links.join_expired', 'More than 10 minutes passed since the other owner joined.', 'Cancel and pair again.')
      const off = staleKeys(c.body, pairingArgs(p))
      if (off.length) return stale(off)
      const l: Link = { id: nextId(c.state, 'ln'), peer: p.peer, carrier: p.carrier, accepts: p.accepts, theyAccept: p.joined.theyAccept, fingerprint: p.joined.fingerprint, paired_at: c.store.now(), paired_by: c.viewer, expires_at: at(c.store.now(), p.days), last_at: c.store.now() }
      linksOf(c.state).push(l)
      delete c.state.pending
      setNav(c.state, c.viewer, { link: l.id })
      record(c, l.id, 'paired', `Linked with ${p.peer.name} ${p.carrier === 'local' ? 'on this machine' : 'through the relay'}. Comparison codes matched; both owners signed.`, { by: c.viewer })
      return { ok: true, message: `Linked with ${p.peer.name}.`, changed: true }
    },
    cancel_pairing(c) {
      const p = c.state.pending as Pending | undefined
      if (!p) return { ok: true, message: 'No pairing in progress.' }
      delete c.state.pending
      record(c, null, 'pairing_cancelled', `Cancelled pairing with ${p.peer.name}. The code no longer works.`, { by: c.viewer })
      return { ok: true, message: `Cancelled pairing with ${p.peer.name}.`, changed: true }
    },

    // Signed in core's prompt (manifest confirm: 'sign'): the covers name the link and its peer; the host checks both.
    revoke(c) {
      const l = linksOf(c.state).find((x) => x.id === c.body.id)
      if (!l) return notFound('No such link.')
      if (c.body.peer !== l.peer.name) return stale(['peer'])
      if (l.revoked) return { ok: true, message: `The link with ${l.peer.name} is already revoked.` }
      l.revoked = { at: c.store.now(), by: c.viewer }
      let closed = 0
      for (const r of requestsOf(c.state)) {
        if (r.link !== l.id || r.state !== 'open') continue
        r.state = 'closed'
        closed++
      }
      // Handoffs still waiting there come back to their people (as when a deadline passes, §9).
      const back = sentOf(c.state).filter((s) => s.link === l.id && s.state === 'waiting')
      for (const s of back) s.state = 'cancelled'
      // The shared row names no ticket and counts none (a hidden one would leak through it, security review #6): each
      // returned handoff is its own row carrying its ticket, so the per-viewer log filter applies to it.
      record(c, l.id, 'revoked', `Revoked the link. ${l.peer.name} was told with a signed revoke envelope${closed ? `; ${closed} open ${closed === 1 ? 'request' : 'requests'} closed` : ''}.`, { by: c.viewer })
      for (const s of back) record(c, l.id, 'handoff_returned', `The handoff of ${s.ticket} came back: the link was revoked before ${l.peer.name} took it.`, { by: c.viewer, ticket: s.ticket })
      return { ok: true, message: `Revoked the link with ${l.peer.name}. Nothing crosses it from now on.`, changed: true }
    },

    prepare_handoff(c) {
      const f = (c.body.formData ?? {}) as Record<string, unknown>
      const l = usableLink(c, f.link)
      if ('ok' in l) return l
      if (!l.theyAccept.includes('handoff')) return conflict('links.scope', `${l.peer.name} does not accept ticket handoffs from this workspace.`)
      const key = crossable(c, f.ticket)
      if (typeof key !== 'string') return key
      setNav(c.state, c.viewer, { draft: { link: l.id, ticket: key } })
      return { ok: true, message: 'Review and sign the handoff.' }
    },
    clear_handoff(c) {
      setNav(c.state, c.viewer, { draft: undefined })
      return { ok: true, message: 'Handoff discarded.' }
    },
    // Signed in core's prompt: a person co-signs every handoff (orch v2 §9). `ticket` is checked by core too.
    send_handoff(c) {
      const l = usableLink(c, c.body.link)
      if ('ok' in l) return l
      if (!l.theyAccept.includes('handoff')) return conflict('links.scope', `${l.peer.name} does not accept ticket handoffs from this workspace.`)
      // `ticket` is core's reserved arg (the render context), so the handoff names its ticket as `ticket_key`.
      const key = crossable(c, c.body.ticket_key)
      if (typeof key !== 'string') return key
      const t = c.store.ticket(key)!
      const off = staleKeys(c.body, handoffArgs(l, t))
      if (off.length) return stale(off)
      if (sentOf(c.state).some((s) => s.ticket === key && s.link === l.id && s.state === 'waiting')) return conflict('links.already_sent', `${key} is already waiting at ${l.peer.name}.`)
      const now = c.store.now()
      sentOf(c.state).push({ id: nextId(c.state, 'out'), link: l.id, ticket: key, title: t.title, at: now, by: c.viewer, deadline: at(now, 3), state: 'waiting' })
      l.last_at = now
      setNav(c.state, c.viewer, { draft: undefined })
      c.store.append(key, { type: 'links.sent', actor: ADDON, to: l.peer.name, person: c.viewer })
      const queued = l.carrier === 'relay' && relayLink(c) !== 'online'
      record(c, l.id, 'sent', `Handed off ${key} to ${l.peer.name}${queued ? '; queued until the relay is online' : ''}.`, { by: c.viewer, ticket: key })
      return { ok: true, message: queued ? `${key} is queued for ${l.peer.name}: it leaves when the relay is online.` : `Handed off ${key} to ${l.peer.name}. They decide whether to accept it.`, changed: true }
    },

    // Mock only: INT hands this workspace a ticket (arrives as a request).
    simulate_request(c) {
      const l = linksOf(c.state).find((x) => x.peer.ws === 'INT' && usable(x, c.store.now()))
      if (!l || !l.accepts.includes('handoff')) return conflict('links.scope', 'There is no active link with INT that accepts ticket handoffs.')
      const now = c.store.now()
      const n = requestsOf(c.state).length + 1
      const title = HANDOFF_TITLES[n % HANDOFF_TITLES.length]
      requestsOf(c.state).push({ id: nextId(c.state, 'rq'), kind: 'handoff', link: l.id, from: l.peer, at: now, expires_at: at(now, 3), title, text: `INT hands this over: ${title.toLowerCase()}.`, state: 'open' })
      l.last_at = now
      record(c, l.id, 'request_received', `Ticket handoff received: "${title}".`)
      return { ok: true, message: `INT handed over "${title}". It is on Today.`, changed: true }
    },

    // Owner only: narrow what a pairing request asked for and pick the expiry. The decision's id then names these terms.
    set_pairing_terms(c) {
      const f = (c.body.formData ?? {}) as Record<string, unknown>
      const r = requestsOf(c.state).find((x) => x.id === f.request && x.kind === 'pairing' && isOpen(x, c.store.now()))
      if (!r) return notFound('That link request is no longer open.')
      const days = Number(f.days ?? 90)
      if (![30, 90, 365].includes(days)) return invalid('A link expires after 30, 90 or 365 days.')
      const pick = (prefix: string, bound: Kind[]) => {
        const keys = Object.keys(f).filter((k) => k.startsWith(prefix) && f[k] === true).map((k) => k.slice(prefix.length) as Kind)
        return keys.every((k) => bound.includes(k)) ? KINDS.filter((k) => keys.includes(k)) : null
      }
      const recv = pick('recv_', wantsOf(r))
      const send = pick('send_', offersOf(r))
      if (!recv || !send) return invalid('Terms can only narrow what the other side asked for.')
      r.terms = { recv, send, days }
      // One owner can narrow what another signs: the change is on the record.
      record(c, null, 'terms_set', `Set the terms of the link request from ${r.from.name}: they may send ${kindsText(recv)}; we may send ${kindsText(send)}; ${days} days.`, { by: c.viewer })
      return { ok: true, message: `Terms set: they may send ${kindsText(recv)}; you may send ${kindsText(send)}; ${days} days. Accept to sign them.`, changed: true }
    },

    // "Cancel changes" on the terms form: nothing changes but the form, which starts over from the saved terms.
    reset_pairing_terms(c) {
      for (const r of openRequestsFor(c.state, c)) if (r.kind === 'pairing') r.termsRev = (r.termsRev ?? 0) + 1
      return { ok: true, message: 'Changes to the terms discarded.' }
    },

    // Decisions (manifest decision: true): core checked who may decide, that it is open and the option; this applies it.
    // Pairing and scope requests have their own actions so Today never folds them with routine handoffs.
    decide_pairing: (c) => decideRequest(c),
    decide_scope: (c) => decideRequest(c),
    decide: (c) => decideRequest(c),
  },
})

function decideRequest(c: AddonCtx): AddonActionResult | StoreFailure {
  const prefix = prefixOf(c)
  const r = requestsOf(c.state).find((x) => decisionId(x, prefix) === c.decision?.id)
  if (!r) return notFound('That request is gone.')
  const option = String(c.body.option)
  const now = c.store.now()
  const l = r.link ? linksOf(c.state).find((x) => x.id === r.link) : undefined
  if (r.kind !== 'pairing' && (!l || !usable(l, now))) return conflict('links.inactive', 'The link this request came through is no longer active.')
  if (r.kind === 'pairing' && option === 'accept') {
    if (linksOf(c.state).some((x) => usable(x, now) && (r.from.ws ? x.peer.ws === r.from.ws : !x.peer.ws && x.peer.name === r.from.name))) return conflict('links.exists', `${r.from.name} is linked already.`, 'Revoke that link first, or deny this request.')
    if ((r.carrier ?? 'relay') === 'relay' && relayLink(c) !== 'online') return conflict('links.relay_offline', `The relay is ${relayLink(c)}: the link cannot be confirmed to ${r.from.name}.`, 'Connect it in Settings → Relay & devices, then accept again.')
  }
  const yes = r.kind === 'question' || option === 'accept' || option === 'allow'
  r.state = r.kind === 'question' ? 'answered' : yes ? 'accepted' : 'denied'
  r.decided = { at: now, by: c.viewer, option }
  if (l) l.last_at = now
  if (r.kind === 'pairing') {
    if (!yes) {
      record(c, null, 'pairing_denied', `Denied the link request from ${r.from.name}.`, { by: c.viewer })
      return { ok: true, message: `Denied ${r.from.name}.`, changed: true }
    }
    // The terms applied are the ones the signed decision id names (a change of terms changes the id).
    const t = termsOf(r)
    const carrier = r.carrier ?? 'relay'
    const nl: Link = { id: nextId(c.state, 'ln'), peer: r.from, carrier, accepts: t.recv, theyAccept: t.send, fingerprint: comparisonCode(prefix, r.from), paired_at: now, paired_by: c.viewer, expires_at: at(now, t.days), last_at: now }
    linksOf(c.state).push(nl)
    record(c, nl.id, 'paired', `Linked with ${r.from.name} ${carrier === 'local' ? 'on this machine' : 'through the relay'}. Comparison code ${nl.fingerprint} matched; both owners signed. They may send ${kindsText(t.recv)}; we may send ${kindsText(t.send)}; ${t.days} days.`, { by: c.viewer })
    return { ok: true, message: `Linked with ${r.from.name}.`, changed: true }
  }
  if (r.kind === 'scope') {
    if (yes && r.scope && !l!.accepts.includes(r.scope)) l!.accepts = [...l!.accepts, r.scope]
    record(c, l!.id, yes ? 'scope_changed' : 'request_denied', yes ? `${l!.peer.name} may now send ${KIND_LABEL[r.scope!]}.` : `Denied ${l!.peer.name}'s request to send ${KIND_LABEL[r.scope!]}.`, { by: c.viewer })
    return { ok: true, message: yes ? `${l!.peer.name} may now send ${KIND_LABEL[r.scope!]}.` : 'Denied.', changed: true }
  }
  if (r.kind === 'question') {
    const label = r.options?.find((o) => o.key === option)?.label ?? option
    record(c, l!.id, 'answered', `Answered "${r.title}": ${label}. Sent back ${l!.carrier === 'local' ? 'on this machine' : 'through the relay'}.`, { by: c.viewer })
    return { ok: true, message: `Answered ${r.from.name}.`, changed: true }
  }
  if (!yes) {
    record(c, l!.id, 'request_denied', `Denied the ticket "${r.title}" from ${r.from.name}. Their ticket goes back to their people.`, { by: c.viewer })
    return { ok: true, message: `Denied the ticket from ${r.from.name}.`, changed: true }
  }
  const made = c.store.createFromRequest(c.ws, {
    type: 'chore',
    title: r.title,
    priority: 'medium',
    size: null,
    labels: ['from-peer'],
    parent: null,
    due: null,
    visibility: 'workspace',
    people: { owner: null, assignees: [], reviewers: [] },
    sections: { requirements: r.text },
    acceptance: [],
  })
  if (!made.ok) return made
  r.ticket = made.ticket.key
  c.store.append(made.ticket.key, { type: 'links.received', actor: ADDON, from: r.from.name, person: c.viewer })
  record(c, l!.id, 'request_accepted', `Accepted "${r.title}" from ${r.from.name} as ${made.ticket.key} (Backlog).`, { by: c.viewer, ticket: made.ticket.key })
  return { ok: true, message: `Accepted as ${made.ticket.key} in the Backlog.`, changed: true, ticket: made.ticket.key }
}
