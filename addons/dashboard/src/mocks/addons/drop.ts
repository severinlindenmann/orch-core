import type { Rng } from '../busy/rng'
import { briefs, tokenOf } from '../busy/helpers'
import type { MockStore } from '../store'
import { canSeeTicket, conflict, invalid, notFound, refusal, registerAddon, type AddonCtx } from './registry'
import { fmtWhen } from '@/lib/time'

// drop (Preview; orch v2 P5, docs/architecture/orch-v2.md §6.4): files between your devices, people and workspaces.
//  - Inbox: files addressed to this workspace. An `inbox` object is claim-once: one eligible workspace claims it and
//    handles it (claiming does not take it back from anyone who already downloaded it). A `file` is immutable and expires.
//  - Sent: what this workspace dropped. "Sealed to <person>": only that person's devices can open it, there is no link.
//    "Link": anyone with the link and the orch app (or this dashboard) can open it until it expires or reaches its view
//    limit. The relay has no pages (D54): the link is a universal link the app opens; the key is in the fragment, which
//    never leaves the device. It is shown once (core's "Copy this link now") and never stored. "Workspace <prefix>": into
//    that workspace's inbox, sealed to its exchange key.
//  - Everything is simulated: the "upload" picks a sample file and nothing leaves this machine.
//  - Rows tied to a ticket are shown only to people who can see it; actions on them refuse the same way (404).

type To = { kind: 'person'; person: string } | { kind: 'workspace'; prefix: string } | { kind: 'link'; max_views?: number }
interface Received {
  id: string
  name: string
  bytes: number
  from: string
  received_at: string
  expires_at: string
  /** 'inbox': claim-once among the eligible workspaces; 'workspace': addressed to this one only. */
  addressed: 'inbox' | 'workspace'
  /** Seed only: who had claimed it before the demo starts. Live claims are store-wide (`claimedBy`). */
  claimed_by?: string
  ticket?: string
}
interface Sent {
  id: string
  name: string
  bytes: number
  to: To
  sent_at: string
  sent_by: string
  expires_at: string
  views: number
  revoked?: boolean
  ticket?: string
}

/** The simulated "upload": files on this machine one can pick. */
const SAMPLE_FILES: { name: string; bytes: number }[] = [
  { name: 'release-notes.md', bytes: 2_150 },
  { name: 'tariff-export.csv', bytes: 48_300 },
  { name: 'meter-photos.zip', bytes: 3_400_000 },
  { name: 'contract-scan.pdf', bytes: 812_000 },
  { name: 'reconciliation.xlsx', bytes: 96_400 },
]
const DAY = 86_400_000
/** Owner ruling on D54: the relay has no pages, so a link opens only in the app. */
export const LINK_ONLY_APP = 'Opens only in the orch app (iPhone or Mac). The relay has no download page — seal it to a person or a workspace instead.'

const at = (iso: string, deltaDays: number) => new Date(Date.parse(iso) + deltaDays * DAY).toISOString().replace(/\.\d{3}Z$/, 'Z')
const NOW = '2026-10-09T11:30:00Z'

/**
 * Inbox objects, each delivered to the workspaces it was addressed to. A claim-once object (`addressed: 'inbox'`) sits
 * in every eligible workspace's inbox under the same id; one claim (store-wide) decides who handles it. The sender is
 * never eligible.
 */
const SEED_INBOX: (Received & { to: string[] })[] = [
  { id: 'in_photos', to: ['DEMO'], name: 'meter-room-photos.zip', bytes: 5_200_000, from: "Severin's iPhone", received_at: '2026-10-09T10:58:00Z', expires_at: at(NOW, 6), addressed: 'workspace', ticket: 'DEMO-0043' },
  { id: 'in_notes', to: ['DEMO', 'CLI'], name: 'release-notes.md', bytes: 2_150, from: 'INT · Internal tools', received_at: '2026-10-09T09:40:00Z', expires_at: at(NOW, 2), addressed: 'inbox' },
  { id: 'in_invoice', to: ['DEMO', 'INT'], name: 'supplier-invoice-0923.pdf', bytes: 412_000, from: "Mara's MacBook Air", received_at: '2026-10-08T15:20:00Z', expires_at: at(NOW, 13), addressed: 'inbox', claimed_by: 'DEMO' },
  { id: 'in_private', to: ['DEMO'], name: 'incident-timeline.md', bytes: 6_800, from: "Mara's MacBook Air", received_at: '2026-10-09T08:15:00Z', expires_at: at(NOW, 5), addressed: 'workspace', ticket: 'DEMO-0044' },
]

const seedState = (ws: string, store: MockStore) => {
  const prefix = store.workspaces.find((w) => w.id === ws)?.prefix ?? ''
  const demo = prefix === 'DEMO'
  const inbox: Received[] = SEED_INBOX.filter((x) => x.to.includes(prefix)).map(({ to: _to, ...x }) => ({ ...x }))
  const sent: Sent[] = !demo ? [] : [
    { id: 'out_diff', name: 'tariff-source-diff.csv', bytes: 18_400, to: { kind: 'person', person: 'p_mara' }, sent_at: '2026-10-09T09:02:00Z', sent_by: 'p_sev', expires_at: at(NOW, 7), views: 2, ticket: demo ? 'DEMO-0043' : undefined },
    { id: 'out_report', name: 'reconciliation-report.pdf', bytes: 240_000, to: { kind: 'link', max_views: 20 }, sent_at: '2026-10-07T14:10:00Z', sent_by: 'p_sev', expires_at: at(NOW, 1), views: 11, ticket: demo ? 'DEMO-0041' : undefined },
    { id: 'out_int', name: 'meter-schema.json', bytes: 9_100, to: { kind: 'workspace', prefix: 'INT' }, sent_at: '2026-10-08T10:30:00Z', sent_by: 'p_mara', expires_at: at(NOW, 12), views: 1 },
    { id: 'out_old', name: 'q3-usage.csv', bytes: 120_000, to: { kind: 'link' }, sent_at: '2026-09-20T09:00:00Z', sent_by: 'p_sev', expires_at: at(NOW, -3), views: 34 },
  ]
  return { inbox, sent, next: 1 }
}

function seedBusy(ws: string, store: MockStore, rng: Rng) {
  const state = seedState(ws, store)
  const prefix = store.workspaces.find((w) => w.id === ws)?.prefix ?? ws.slice(0, 4)
  const tickets = briefs(store, ws)
  const people = ['p_sev', 'p_mara', 'p_tom']
  const from = ["Severin's iPhone", "Mara's MacBook Air", 'INT · Internal tools', 'CLI · Command line', "Tom's iPad"]
  const names = ['export', 'photos', 'notes', 'invoice', 'schema', 'log-bundle', 'screenshots', 'contract', 'readings', 'backup']
  const ext = ['csv', 'zip', 'md', 'pdf', 'json', 'tar.gz', 'png']
  for (let i = 0; i < 36; i++) {
    const t = rng.chance(0.4) && tickets.length ? rng.pick(tickets).key : undefined
    state.inbox.push({
      id: `in_${prefix}_b${i + 1}`,
      name: `${rng.pick(names)}-${100 + i}.${rng.pick(ext)}`,
      bytes: rng.int(800, 9_000_000),
      from: rng.pick(from),
      received_at: at(NOW, -rng.int(0, 13) - rng.next()),
      expires_at: at(NOW, rng.int(1, 14)),
      addressed: rng.chance(0.5) ? 'inbox' : 'workspace',
      ...(rng.chance(0.3) ? { claimed_by: prefix } : {}),
      ...(t ? { ticket: t } : {}),
    })
  }
  for (let i = 0; i < 28; i++) {
    const t = rng.chance(0.4) && tickets.length ? rng.pick(tickets).key : undefined
    const k = rng.int(0, 2)
    const to: To = k === 0 ? { kind: 'person', person: rng.pick(people) } : k === 1 ? { kind: 'workspace', prefix: rng.pick(['INT', 'CLI']) } : { kind: 'link', ...(rng.chance(0.5) ? { max_views: rng.pick([5, 10, 50]) } : {}) }
    state.sent.push({
      id: `out_${prefix}_b${i + 1}`,
      name: `${rng.pick(names)}-${200 + i}.${rng.pick(ext)}`,
      bytes: rng.int(800, 9_000_000),
      to,
      sent_at: at(NOW, -rng.int(0, 20) - rng.next()),
      sent_by: rng.pick(['p_sev', 'p_mara']),
      expires_at: at(NOW, rng.int(-4, 30)),
      views: rng.int(0, 60),
      ...(t ? { ticket: t } : {}),
    })
  }
  return state
}

const inboxOf = (s: Record<string, unknown>) => s.inbox as Received[]
/** Store-wide claims: object id -> the prefix of the workspace that claimed it. */
const claimsOf = (store: MockStore) => ((store.sharedAddonState('drop').claims ??= {}) as Record<string, string>)
const claimedBy = (store: MockStore, x: Received): string | undefined => claimsOf(store)[x.id] ?? x.claimed_by
const sentOf = (s: Record<string, unknown>) => s.sent as Sent[]
const size = (n: number) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`)
const when = (iso: string) => fmtWhen(iso)
const left = (iso: string, now: string) => {
  const ms = Date.parse(iso) - Date.parse(now)
  if (ms <= 0) return 'expired'
  const d = Math.floor(ms / DAY)
  return d >= 1 ? `in ${d} ${d === 1 ? 'day' : 'days'}` : `in ${Math.max(1, Math.round(ms / 3_600_000))} h`
}

function nameIn(c: Pick<AddonCtx, 'store' | 'ws'>, person: string) {
  return c.store.workspaces.find((w) => w.id === c.ws)?.members.find((m) => m.person === person)?.name ?? person
}
function toText(c: Pick<AddonCtx, 'store' | 'ws'>, to: To): string {
  if (to.kind === 'person') return `Sealed to ${nameIn(c, to.person)}`
  if (to.kind === 'workspace') return `Workspace ${to.prefix} (inbox)`
  return to.max_views ? `Link · orch app only · up to ${to.max_views} views` : 'Link · orch app only'
}
const sentState = (x: Sent, now: string) => (x.revoked ? 'revoked' : Date.parse(x.expires_at) <= Date.parse(now) ? 'expired' : x.to.kind === 'link' && x.to.max_views !== undefined && x.views >= x.to.max_views ? 'used up' : 'live')

const visibleReceived = (c: AddonCtx, id: unknown) => inboxOf(c.state).find((x) => x.id === id && (!x.ticket || canSeeTicket(c, x.ticket)))
const visibleSent = (c: AddonCtx, id: unknown) => sentOf(c.state).find((x) => x.id === id && (!x.ticket || canSeeTicket(c, x.ticket)))
const event = (c: AddonCtx, ticket: string | undefined, type: string, extra: Record<string, unknown> = {}) => {
  if (ticket && canSeeTicket(c, ticket)) c.store.append(ticket, { type, actor: { kind: 'addon', id: 'drop' }, ...extra })
}

registerAddon({
  name: 'drop',
  seed: seedState,
  seedBusy,

  view(state, c) {
    const now = c.store.now()
    const prefix = c.store.workspaces.find((w) => w.id === c.ws)?.prefix
    const inbox = inboxOf(state).filter((x) => !x.ticket || canSeeTicket(c, x.ticket))
    const sent = sentOf(state).filter((x) => !x.ticket || canSeeTicket(c, x.ticket))
    const fresh = inbox.filter((x) => !claimedBy(c.store, x) && x.addressed === 'inbox').length
    const live = sent.filter((x) => sentState(x, now) === 'live')
    const ws = c.store.workspaces.find((w) => w.id === c.ws)
    const others = (ws?.members ?? []).filter((m) => m.person !== c.viewer)
    const peers = c.store.workspaces.filter((w) => w.id !== c.ws && w.members.some((m) => m.person === c.viewer)).map((w) => w.prefix)
    const tickets = c.store.listTickets(c.ws).slice(0, 60) // the viewer's visible tickets, most recent first
    const sendTo = [...others.map((m) => `person:${m.person}`), ...peers.map((p) => `workspace:${p}`), 'link']
    const sendToNames = [...others.map((m) => `${m.name}: sealed to their devices`), ...peers.map((p) => `Workspace ${p}: its inbox`), 'Link: opens only in the orch app']
    return {
      // Raw lists are replaced by what this viewer may see.
      inbox: inbox.map(({ ticket: _t, ...x }) => x),
      sent: sent.map(({ ticket: _t, ...x }) => x),
      summary: `${fresh} to claim · ${live.length} live ${live.length === 1 ? 'link' : 'links'}`,
      toClaim: fresh,
      liveCount: live.length,
      views: sent.reduce((n, x) => n + x.views, 0),
      inboxRows: inbox
        .slice()
        .sort((a, b) => b.received_at.localeCompare(a.received_at))
        .map((x) => ({ x, by: claimedBy(c.store, x) }))
        .map(({ x, by }) => ({
          id: x.id,
          file: x.name,
          from: x.from,
          size: size(x.bytes),
          received: when(x.received_at),
          ticket: x.ticket ?? '',
          expires: left(x.expires_at, now),
          state: by ? (by === prefix ? 'claimed here' : `claimed by ${by}`) : x.addressed === 'inbox' ? 'to claim' : 'for this workspace',
          canClaim: !by && x.addressed === 'inbox' && left(x.expires_at, now) !== 'expired',
        })),
      sentRows: sent
        .slice()
        .sort((a, b) => b.sent_at.localeCompare(a.sent_at))
        .map((x) => {
          const st = sentState(x, now)
          return {
            id: x.id,
            file: x.name,
            to: toText(c, x.to),
            by: nameIn(c, x.sent_by),
            ticket: x.ticket ?? '',
            expires: st === 'live' ? left(x.expires_at, now) : '–',
            views: x.to.kind === 'link' && x.to.max_views !== undefined ? `${x.views} of ${x.to.max_views}` : String(x.views),
            state: st,
            canChange: st === 'live' || st === 'used up' || st === 'expired',
            // Extend is the sender's or an owner's (the action refuses everyone else), so it is offered only to them.
            canExtend: (st === 'live' || st === 'used up' || st === 'expired') && (x.sent_by === c.viewer || c.store.roleIn(c.ws, c.viewer) === 'owner'),
          }
        }),
      shareSchema: {
        type: 'object',
        required: ['file', 'to', 'days'],
        properties: {
          file: { type: 'string', title: 'File (simulated upload)', enum: SAMPLE_FILES.map((f) => f.name) },
          to: { type: 'string', title: 'Send to', enum: sendTo },
          days: { type: 'integer', title: 'Expires after', enum: [1, 7, 30], default: 7 },
          max_views: { type: 'integer', title: 'View limit (links only, optional)', minimum: 1, maximum: 1000 },
          ticket: { type: 'string', title: 'Ticket (optional)', enum: ['', ...tickets.map((t) => t.key)], default: '' },
        },
      },
      fileNames: SAMPLE_FILES.map((f) => `${f.name} (${size(f.bytes)})`),
      sendToNames,
      ticketNames: ['None', ...tickets.map((t) => `${t.key} · ${t.title}`)],
    }
  },

  actions: {
    // The form posts `{formData}`; its `ticket` is checked here (core checks only a top-level body.ticket).
    share(c) {
      const { state, store } = c
      const body = (c.body.formData ?? {}) as Record<string, unknown>
      const file = SAMPLE_FILES.find((f) => f.name === body.file)
      if (!file) return invalid('Pick a file.')
      const days = Number(body.days ?? 7)
      if (![1, 7, 30].includes(days)) return invalid('Expiry must be 1, 7 or 30 days.')
      const raw = String(body.to ?? '')
      const ws = store.workspaces.find((w) => w.id === c.ws)!
      let to: To
      if (raw === 'link') {
        const mv = body.max_views === undefined || body.max_views === '' || body.max_views === null ? undefined : Number(body.max_views)
        if (mv !== undefined && !(Number.isInteger(mv) && mv >= 1 && mv <= 1000)) return invalid('The view limit is a whole number from 1 to 1000.')
        to = { kind: 'link', ...(mv ? { max_views: mv } : {}) }
      } else if (raw.startsWith('person:')) {
        const person = raw.slice(7)
        if (person === c.viewer || !ws.members.some((m) => m.person === person)) return invalid('Pick someone in this workspace.')
        to = { kind: 'person', person }
      } else if (raw.startsWith('workspace:')) {
        const prefix = raw.slice(10)
        const peer = store.workspaces.find((w) => w.prefix === prefix)
        if (!peer || peer.id === c.ws || !peer.members.some((m) => m.person === c.viewer)) return invalid('Pick one of your other workspaces.')
        to = { kind: 'workspace', prefix }
      } else return invalid('Pick who receives it.')
      const ticket = typeof body.ticket === 'string' && body.ticket ? body.ticket : undefined
      if (ticket && !canSeeTicket(c, ticket)) return notFound(`No ticket ${ticket}`)
      const id = `out_${(state.next as number) ?? 1}`
      state.next = ((state.next as number) ?? 1) + 1
      const now = store.now()
      sentOf(state).push({ id, name: file.name, bytes: file.bytes, to, sent_at: now, sent_by: c.viewer, expires_at: at(now, days), views: 0, ...(ticket ? { ticket } : {}) })
      event(c, ticket, 'drop.shared', { name: file.name })
      const span = `${days} ${days === 1 ? 'day' : 'days'}`
      if (to.kind === 'link') {
        // The key lives in the fragment: shown once, never stored (the row cannot copy it again).
        const key = tokenOf(Number(id.slice(4)), 13) + tokenOf(Number(id.slice(4)) + 97, 29)
        return {
          ok: true,
          message: `Dropped ${file.name} as a link for ${span}.`,
          changed: true,
          secret: { label: `Link to ${file.name}`, value: `https://relay.dev.severin.io/d/${id.slice(4)}#k=${key}`, note: `Simulated link, valid for ${span}${to.max_views ? ` or ${to.max_views} views` : ''}. ${LINK_ONLY_APP} The key is in the part after #, which never leaves the device; it is shown once.` },
        }
      }
      return { ok: true, message: to.kind === 'person' ? `Sealed ${file.name} to ${nameIn(c, to.person)}. Only their devices can open it, for ${span}.` : `Dropped ${file.name} into ${to.prefix}'s inbox for ${span}.`, changed: true }
    },
    claim(c) {
      const x = visibleReceived(c, c.body.id)
      if (!x) return notFound('That file is no longer in the inbox.')
      const prefix = c.store.workspaces.find((w) => w.id === c.ws)?.prefix ?? ''
      const by = claimedBy(c.store, x)
      if (by === prefix) return { ok: true, message: `${x.name} is already claimed here.` }
      if (by) return conflict('drop.claimed', `${by} claimed ${x.name} first.`, 'A claim decides who handles it; it is not yours to handle now.')
      if (x.addressed !== 'inbox') return conflict('drop.not_claimable', `${x.name} was sent to this workspace only; there is nothing to claim.`)
      if (Date.parse(x.expires_at) <= Date.parse(c.store.now())) return conflict('drop.expired', `${x.name} expired.`)
      claimsOf(c.store)[x.id] = prefix // one record for every workspace that got it
      for (const w of c.store.workspaces) if (w.id !== c.ws) c.store.bumpCursor(w.id) // the others' inboxes change too
      event(c, x.ticket, 'drop.claimed', { name: x.name })
      return { ok: true, message: `Claimed ${x.name}. ${prefix} handles it now.`, changed: true }
    },
    download(c) {
      const x = visibleReceived(c, c.body.id)
      if (!x) return notFound('That file is no longer in the inbox.')
      if (Date.parse(x.expires_at) <= Date.parse(c.store.now())) return conflict('drop.expired', `${x.name} expired.`)
      return { ok: true, message: `Downloaded ${x.name} (${size(x.bytes)}) to Downloads. Simulated: no file was written.` }
    },
    remove(c) {
      const list = inboxOf(c.state)
      const x = visibleReceived(c, c.body.id)
      if (!x) return notFound('That file is no longer in the inbox.')
      list.splice(list.indexOf(x), 1)
      event(c, x.ticket, 'drop.removed', { name: x.name })
      return { ok: true, message: `Removed ${x.name} from this inbox.`, changed: true }
    },
    extend(c) {
      const x = visibleSent(c, c.body.id)
      if (!x) return notFound('That drop no longer exists.')
      if (x.revoked) return conflict('drop.revoked', `${x.name} was revoked.`, 'Drop the file again.')
      if (x.sent_by !== c.viewer && c.store.roleIn(c.ws, c.viewer) !== 'owner') return refusal(403, 'forbidden', `Only ${nameIn(c, x.sent_by)} or an owner can extend ${x.name}.`)
      const base = Math.max(Date.parse(x.expires_at), Date.parse(c.store.now()))
      x.expires_at = new Date(base + 7 * DAY).toISOString().replace(/\.\d{3}Z$/, 'Z')
      event(c, x.ticket, 'drop.extended', { name: x.name })
      return { ok: true, message: `${x.name} now expires ${left(x.expires_at, c.store.now())}.`, changed: true }
    },
    revoke(c) {
      const x = visibleSent(c, c.body.id)
      if (!x) return notFound('That drop no longer exists.')
      if (x.revoked) return { ok: true, message: `${x.name} is already revoked.` }
      x.revoked = true
      event(c, x.ticket, 'drop.revoked', { name: x.name, by: c.viewer })
      return { ok: true, message: `Revoked ${x.name}. It stops opening now; copies already downloaded stay where they are.`, changed: true }
    },
  },
})
