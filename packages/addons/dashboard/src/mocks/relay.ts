// Relay & devices (orch v2, docs/architecture/orch-v2.md §5.4, §6, §7; plugins/orch-core/docs/remote.md), simulated.
// Nothing here talks to a relay. What is real in the product and what this mock does instead:
//  - the link (off / connecting / online / reconnecting / stopped): turned on and off by the owner (workspace events
//    relay.connected / relay.stopped); "connecting" lasts 2 s of the mock clock, a simulated drop 4 s of "reconnecting";
//  - devices: seeded per member, then device.paired / device.removed; removing a device starts a new epoch
//    (epoch.rotated), and the new key is sealed to every remaining device through the queue;
//  - pairing (D6): a code valid 10 minutes, single use; the phone "scans" through POST /api/dev/relay/:ws (mock only),
//    both screens show a 6-character code, the owner confirms (signed), the device is added and the key sealed to it;
//  - the sync queue: what the host hands to the relay (sealed keys, pushes, drops); it drains only while online.
import type { RelayDevice, RelayLink, RelayPairing, RelayQueueItem, RelayRequest, RelaySimRequest, RelayState, WorkspaceEvent } from '@/api/types'
import { can } from '@/api/permissions'
import type { MockStore, StoreFailure } from './store'

export const RELAY_URL = 'https://relay.dev.severin.io'
export const PAIRING_MS = 10 * 60_000
const CONNECT_MS = 2_000
const RECONNECT_MS = 4_000
const SEND_STEP_MS = 1_500
const ROTATION_DAYS = 90
const CREATED = '2026-08-14T07:42:10Z'

/** In-memory simulation per workspace (not persisted: a reload ends a drop or an open pairing code). */
export interface RelaySim {
  dropAt?: number
  pairing?: RelayPairing & { person: string }
  nextPairing: number
}

type SeedDevice = Omit<RelayDevice, 'epoch'>
/** Each person's devices (a device belongs to a person; membership of a workspace is per device). */
const SEED_DEVICES: SeedDevice[] = [
  { id: 'd_mac', person: 'p_sev', label: "Severin's MacBook Pro", platform: 'mac', primary: true, this_device: true, scopes: ['look', 'decide', 'operate', 'type'], paired_at: CREATED, last_seen: null },
  { id: 'd_sev_iphone', person: 'p_sev', label: "Severin's iPhone", platform: 'iphone', primary: false, this_device: false, scopes: ['look', 'decide', 'operate', 'type'], paired_at: '2026-08-20T18:02:00Z', last_seen: '2026-10-09T07:55:00Z' },
  { id: 'd_mara_mba', person: 'p_mara', label: "Mara's MacBook Air", platform: 'mac', primary: true, this_device: false, scopes: ['look', 'decide', 'operate'], paired_at: '2026-08-21T08:30:00Z', last_seen: '2026-10-09T09:12:00Z' },
  { id: 'd_tom_ipad', person: 'p_tom', label: "Tom's iPad", platform: 'ipad', primary: false, this_device: false, scopes: ['look'], paired_at: '2026-09-02T12:10:00Z', last_seen: '2026-10-08T16:40:00Z' },
]

/** Pushes and drops waiting for the relay (seed). A ticket-bound item is shown only to people who can see the ticket. */
const SEED_QUEUE: Omit<RelayQueueItem, 'state'>[] = [
  { id: 'q_drop_notes', kind: 'drop', label: 'Drop: release-notes.md for Severin\'s iPhone', device: 'd_sev_iphone', queued_at: '2026-10-09T10:40:00Z' },
  { id: 'q_push_0043', kind: 'push', label: 'Question on DEMO-0043 for Severin\'s iPhone', device: 'd_sev_iphone', ticket: 'DEMO-0043', queued_at: '2026-10-09T11:05:00Z' },
  { id: 'q_push_0044', kind: 'push', label: 'Question on DEMO-0044 for Mara\'s MacBook Air', device: 'd_mara_mba', ticket: 'DEMO-0044', queued_at: '2026-10-09T11:08:00Z' },
  { id: 'q_push_0045', kind: 'push', label: 'Verdict needed on DEMO-0045 for Mara\'s MacBook Air', device: 'd_mara_mba', ticket: 'DEMO-0045', queued_at: '2026-10-09T11:12:00Z' },
]

const fail = (status: number, code: string, message: string, hint?: string): StoreFailure => ({ ok: false, status, code, message, ...(hint ? { hint } : {}) })
const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')

/** Six characters both screens show (Crockford base32, no I, L, O, U). Deterministic from the pairing. */
export function fingerprintOf(seed: string): string {
  const abc = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
  let h = 2166136261
  for (const c of seed) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0
  let out = ''
  for (let i = 0; i < 6; i++) {
    out += abc[h % 32]
    h = Math.imul(h ^ (h >>> 13), 2246822507) >>> 0
  }
  return out
}

interface Folded {
  devices: SeedDevice[]
  epoch: number
  epochStarted: string
  on: boolean
  onSince: string | null
  stoppedAt: string | null
  /** Sealing a new epoch key to a device (after a rotation or a pairing). */
  seals: Omit<RelayQueueItem, 'state'>[]
}

function fold(memberIds: string[], events: WorkspaceEvent[]): Folded {
  const f: Folded = { devices: SEED_DEVICES.filter((d) => memberIds.includes(d.person)).map((d) => ({ ...d })), epoch: 1, epochStarted: CREATED, on: false, onSince: null, stoppedAt: null, seals: [] }
  for (const e of events) {
    switch (e.type) {
      case 'relay.connected':
        f.on = true
        f.onSince = e.at
        break
      case 'relay.stopped':
        f.on = false
        f.stoppedAt = e.at
        break
      case 'device.paired': {
        const d = e.device as SeedDevice
        if (!f.devices.some((x) => x.id === d.id)) f.devices.push({ ...d, this_device: false, primary: false, paired_at: e.at, last_seen: e.at })
        f.seals.push({ id: `q_seal_${d.id}_${f.epoch}`, kind: 'seal_key', label: `Epoch ${f.epoch} key for ${d.label}`, device: d.id, queued_at: e.at })
        break
      }
      case 'device.removed':
        f.devices = f.devices.filter((x) => x.id !== e.device)
        f.seals = f.seals.filter((s) => s.device !== e.device)
        break
      case 'epoch.rotated':
        f.epoch = Number(e.epoch)
        f.epochStarted = e.at
        for (const d of f.devices) if (!d.this_device) f.seals.push({ id: `q_seal_${d.id}_${f.epoch}`, kind: 'seal_key', label: `Epoch ${f.epoch} key for ${d.label}`, device: d.id, queued_at: e.at })
        break
      default:
        break
    }
  }
  return f
}

/** The workspace's current epoch (General's identity reads it too). */
export function relayEpoch(store: MockStore, wsId: string): number {
  const ws = store.workspaces.find((w) => w.id === wsId)
  return ws ? fold(ws.members.map((m) => m.person), store.wsEventsOf(wsId)).epoch : 1
}

function linkOf(f: Folded, sim: RelaySim | undefined, now: number): { link: RelayLink; since: string | null; onlineFrom: number | null } {
  if (!f.on) return { link: f.stoppedAt ? 'stopped' : 'off', since: f.stoppedAt, onlineFrom: null }
  const t0 = Date.parse(f.onSince!)
  if (now - t0 < CONNECT_MS) return { link: 'connecting', since: f.onSince, onlineFrom: null }
  if (sim?.dropAt !== undefined && sim.dropAt >= t0 && now - sim.dropAt < RECONNECT_MS) return { link: 'reconnecting', since: iso(sim.dropAt), onlineFrom: null }
  const from = Math.max(t0 + CONNECT_MS, sim?.dropAt !== undefined && sim.dropAt >= t0 ? sim.dropAt + RECONNECT_MS : 0)
  return { link: 'online', since: iso(from), onlineFrom: from }
}

export function relayState(store: MockStore, wsId: string): RelayState {
  const ws = store.workspaces.find((w) => w.id === wsId)!
  const f = fold(ws.members.map((m) => m.person), store.wsEventsOf(wsId))
  const nowIso = store.now()
  const now = Date.parse(nowIso)
  const sim = store.relaySim.get(wsId)
  const { link, since, onlineFrom } = linkOf(f, sim, now)

  // The queue drains in order, one item every 1.5 s, from the moment the link is online.
  const visible = (i: Omit<RelayQueueItem, 'state'>) => !i.ticket || (store.hasTicket(i.ticket) && store.workspaceOf(i.ticket)?.id === wsId && store.isVisible(i.ticket))
  const deviceIds = new Set(f.devices.map((d) => d.id))
  // Hidden items are left out before the timing, so the gaps in what is sent say nothing about them.
  const all = [...SEED_QUEUE.filter((i) => ws.prefix === 'DEMO' && deviceIds.has(i.device!)), ...f.seals].filter(visible).sort((a, b) => a.queued_at.localeCompare(b.queued_at))
  let cursor = onlineFrom ?? Infinity
  const delivered = new Map<string, number>()
  for (const i of all) {
    if (onlineFrom === null) break
    cursor = Math.max(cursor, Date.parse(i.queued_at)) + SEND_STEP_MS
    if (cursor <= now) delivered.set(i.id, cursor)
  }
  const queue: RelayQueueItem[] = all.map((i) => (delivered.has(i.id) ? { ...i, state: 'sent', sent_at: iso(delivered.get(i.id)!) } : { ...i, state: 'queued' }))

  // A device holds the current epoch once its sealed key is delivered; until then the one before (0: none yet).
  const devices: RelayDevice[] = f.devices.map((d) => {
    const waiting = f.seals.some((s) => s.device === d.id && !delivered.has(s.id))
    const held = !waiting || d.this_device ? f.epoch : d.paired_at >= f.epochStarted ? 0 : f.epoch - 1
    return { ...d, last_seen: d.this_device ? nowIso : d.last_seen, epoch: held }
  })

  let pairing: RelayPairing | null = null
  if (sim?.pairing) {
    const { person: _p, ...p } = sim.pairing
    pairing = now >= Date.parse(p.expires_at) ? { ...p, state: 'expired' } : p
  }
  const next = new Date(Date.parse(f.epochStarted) + ROTATION_DAYS * 86_400_000)
  return { simulated: true, now: nowIso, relay_url: RELAY_URL, link, since, epoch: f.epoch, epoch_started: f.epochStarted, next_rotation: iso(next.getTime()), devices, pairing, queue }
}

const simOf = (store: MockStore, wsId: string): RelaySim => {
  let s = store.relaySim.get(wsId)
  if (!s) store.relaySim.set(wsId, (s = { nextPairing: 1 }))
  return s
}

/** The owner's relay requests. The dashboard signs connect, stop, confirm and remove first (core's prompt). */
export function relayRequest(store: MockStore, wsId: string, req: RelayRequest | null): { ok: true; relay: RelayState } | StoreFailure {
  const ws = store.workspaces.find((w) => w.id === wsId)
  if (!ws) return fail(404, 'not_found', 'No such workspace')
  const role = store.roleIn(wsId, store.viewer)
  if (!role) return fail(403, 'forbidden', 'You are not a member of this workspace.', 'Ask an owner.')
  if (!can(role, 'settings')) return fail(403, 'forbidden', 'Only owners change the relay and devices.')
  if (!req || typeof req !== 'object' || !('op' in req)) return fail(400, 'validation', 'Body must be {op, ...}')
  const cur = relayState(store, wsId)
  const sim = simOf(store, wsId)
  switch (req.op) {
    case 'connect':
      if (cur.link !== 'off' && cur.link !== 'stopped') return { ok: true, relay: cur } // already on: nothing to do
      sim.dropAt = undefined
      store.appendWs(wsId, { type: 'relay.connected', relay_url: RELAY_URL })
      break
    case 'stop':
      if (cur.link === 'off' || cur.link === 'stopped') return { ok: true, relay: cur }
      sim.dropAt = undefined
      sim.pairing = undefined // an open code cannot be used without the link
      store.appendWs(wsId, { type: 'relay.stopped' })
      break
    case 'pair.start': {
      if (cur.link !== 'online') return fail(409, 'relay.offline', 'The relay is not online, so a phone cannot reach this workspace.', 'Connect the relay first.')
      const now = Date.parse(store.now())
      const id = `pr_${sim.nextPairing++}`
      sim.pairing = { id, person: store.viewer, state: 'waiting', started_at: iso(now), expires_at: iso(now + PAIRING_MS) }
      break
    }
    case 'pair.cancel':
      sim.pairing = undefined
      break
    case 'pair.confirm': {
      const p = cur.pairing
      if (!p || p.id !== req.pairing) return fail(404, 'not_found', 'That pairing code is no longer open.', 'Make a new code.')
      if (cur.link !== 'online') return fail(409, 'relay.offline', 'The relay is not online, so the new device cannot get its key.', 'Wait until it is online again.')
      if (sim.pairing!.person !== store.viewer) return fail(403, 'forbidden', 'Only the person who made this code can confirm it.')
      if (p.state === 'expired') return fail(409, 'pairing.expired', 'The pairing code expired.', 'Make a new code; each one is valid for 10 minutes.')
      if (p.state !== 'confirm') return fail(409, 'pairing.waiting', 'No device has joined with this code yet.')
      if (req.fingerprint !== p.fingerprint) return fail(409, 'pairing.mismatch', 'The code does not match the one this workspace shows.', 'Cancel and start again if the codes differ.')
      const d: SeedDevice = { id: `d_${p.id}`, person: sim.pairing!.person, label: p.label!, platform: p.platform!, primary: false, this_device: false, scopes: ['look', 'decide', 'operate', 'type'], paired_at: store.now(), last_seen: store.now() }
      sim.pairing = undefined // single use
      store.appendWs(wsId, { type: 'device.paired', device: d, person: d.person })
      break
    }
    case 'device.remove': {
      const d = cur.devices.find((x) => x.id === req.device)
      if (!d) return fail(404, 'not_found', 'No such device in this workspace.')
      if (d.this_device) return fail(409, 'relay.this_device', 'This is the device the workspace runs on; it cannot remove itself.')
      store.appendWs(wsId, { type: 'device.removed', device: d.id, label: d.label, person: d.person })
      store.appendWs(wsId, { type: 'epoch.rotated', epoch: cur.epoch + 1, reason: 'device.removed', device: d.id })
      break
    }
    default:
      return fail(400, 'validation', `Unknown op ${(req as { op: string }).op}`)
  }
  return { ok: true, relay: relayState(store, wsId) }
}

/** Mock only: what the relay or a phone would do (a dropped connection; a phone scanning the code). */
export function relaySim(store: MockStore, wsId: string, req: RelaySimRequest | null): { ok: true; relay: RelayState } | StoreFailure {
  const ws = store.workspaces.find((w) => w.id === wsId)
  if (!ws) return fail(404, 'not_found', 'No such workspace')
  const role = store.roleIn(wsId, store.viewer)
  if (!role) return fail(403, 'forbidden', 'You are not a member of this workspace.')
  if (!can(role, 'settings')) return fail(403, 'forbidden', 'Only owners run the relay simulation.')
  const cur = relayState(store, wsId)
  const sim = simOf(store, wsId)
  if (req?.op === 'drop') {
    if (cur.link !== 'online') return fail(409, 'relay.offline', 'The relay is not online.')
    sim.dropAt = Date.parse(store.now())
  } else if (req?.op === 'scan') {
    const p = sim.pairing
    if (!p || cur.pairing?.state === 'expired') return fail(409, 'pairing.none', 'There is no open pairing code to scan.', 'Make a new code.')
    if (p.state !== 'waiting') return fail(409, 'pairing.used', 'A device has already joined with this code.')
    const name = ws.members.find((m) => m.person === p.person)?.name ?? p.person
    const taken = cur.devices.filter((d) => d.label.startsWith(`${name}'s iPad`)).length
    sim.pairing = { ...p, state: 'confirm', fingerprint: fingerprintOf(`${wsId}|${p.id}`), label: `${name}'s iPad${taken ? ` (${taken + 1})` : ''}`, platform: 'ipad' }
  } else return fail(400, 'validation', 'op must be "drop" or "scan"')
  store.bumpCursor(wsId)
  return { ok: true, relay: relayState(store, wsId) }
}
