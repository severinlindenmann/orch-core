// DEV/TEST ONLY: an in-memory stand-in for the PROPOSED `drop/1` host ops (src/api/drop.ts). It never talks to a host.
// The layout preview and the tests install it over `dropApi`, and drive live updates with `emit`, the way
// `sub.open {topics:["drop"]}` items would arrive. Ids are `demo-*`; nothing is sealed, the bytes are plain.
import { CHUNK_BYTES, DropError, dropApi, realDropApi, type BlobPutArgs, type DropApi, type DropAuthor, type DropDirection, type DropInfo, type DropItem, type DropSubItem, type DropVersion } from '@/api/drop'

export const FIXTURE_INFO: DropInfo = { max_bytes: 25 * 1024 * 1024, version_cap: 1024, writes: ['file'] }

export interface DropFixtureOptions {
  served?: boolean
  info?: Partial<DropInfo>
  /** Turn on document writes (the proposal's Q4 answered "documents too"). Default: files only (orch-52, P4). */
  documentWrites?: boolean
  received?: DropItem[]
  sent?: DropItem[]
  /** Versions per document object id (newest first). */
  versions?: Record<string, DropVersion[]>
  /** Page size the fixture serves (the page asks for more; the host may cut a page short). */
  pageSize?: number
  /** Each `blob.put` chunk waits this long (so progress and cancel can be seen). */
  chunkDelayMs?: number
  /** Each upload's `blob.put` answers this many chunks, then never answers again (a stalled upload, for the layout preview). */
  stallAfterChunks?: number
  /** `blob.put` fails at this chunk (0-based) with `blob.invalid`. */
  putErrorAtChunk?: number
  /** Patch each `blob.put` answer (a host whose `received` lags or overshoots). */
  putAnswer?: (answer: { blob: string; size: number; received: number }, chunk: number) => { blob: string; size: number; received: number }
  /** `drop.send` waits this long ("Sealing and sending…"). */
  sendDelayMs?: number
  /** `drop.send` fails with this error. */
  sendError?: DropError
  /** `drop.read` (per object id, or '*') fails with this error. */
  readError?: Record<string, DropError>
  /** `drop.retire` fails with this error. */
  retireError?: DropError
  /** Simulate an item changing before deletion. */
  retireConflict?: boolean
  /** `drop.list` fails with this error. */
  listError?: DropError
  /** H6: the count of other people's unclaimed uploads `drop.list` reports (a count only; those rows are never listed). */
  pendingOthers?: number
  /** H3: the first request with a cursor (the second page) fails once with `drop.cursor_reset`; `onReset` runs first (rows may change). */
  cursorResetOnSecondPage?: boolean | (() => void)
  /** A faulty host: EVERY `drop.list` answers `drop.cursor_reset`, with or without a cursor. */
  cursorResetAlways?: boolean
}

export const ME: DropAuthor = { person: 'p_severin', person_name: 'Severin', device: 'd_mac', origin: 'person' }
export const PHONE: DropAuthor = { person: 'p_severin', person_name: 'Severin', device: 'd_phone_1', origin: 'phone' }
/** Phone shares (Drop A2). Device ids and labels are fixtures, not real. */
export const PHONE_SHARE = { device: 'd_fixture_phone_1', label: 'Severin’s iPhone' }
export const AGENT: DropAuthor = { person: 'p_severin', person_name: 'Severin', device: 'd_mac', origin: 'agent' }

const T0 = Date.parse('2026-10-09T11:30:00Z')
let serial = 0
export function dropItem(over: Partial<DropItem> = {}): DropItem {
  serial += 1
  return {
    object_id: `demo-obj-${String(serial).padStart(4, '0')}`,
    kind: 'file',
    version: 1,
    versions: 1,
    head: `demo-head-${serial}-v1`,
    tombstone_until_ms: null,
    name: `report-${serial}.pdf`,
    name_hidden: false,
    mime: 'application/pdf',
    size: 312_000 + serial * 1000,
    created_ms: T0 - serial * 3_600_000,
    expires_ms: T0 + 6 * 86_400_000,
    author: PHONE,
    claimed: true,
    state: 'live',
    ...over,
  }
}
export const dropVersion = (version: number, over: Partial<DropVersion> = {}): DropVersion => ({
  version, size: 4_000 + version * 10, created_ms: T0 - (2000 - version) * 60_000, author: PHONE, state: 'live', ...over,
})

export interface DropFixture {
  api: DropApi
  /** Change or add an item (by object id) in a direction and send the `drop` sub item. */
  emit: (direction: DropDirection, item: DropItem) => void
  items: (direction: DropDirection) => DropItem[]
  calls: { op: string; args: unknown[] }[]
  /** Blobs the fixture holds (upload results and read results). */
  blobs: Map<string, Uint8Array>
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

export function createDropFixture(opts: DropFixtureOptions = {}): DropFixture {
  const info: DropInfo = { ...FIXTURE_INFO, ...(opts.documentWrites ? { writes: ['file', 'document'] } : {}), ...opts.info }
  const lists: Record<DropDirection, DropItem[]> = { received: structuredClone(opts.received ?? []), sent: structuredClone(opts.sent ?? []) }
  const versions: Record<string, DropVersion[]> = structuredClone(opts.versions ?? {})
  const subs = new Set<(item: DropSubItem) => void>()
  let cursorResetDone = false
  const calls: DropFixture['calls'] = []
  const blobs = new Map<string, Uint8Array>()
  const uploads = new Map<string, { size: number; parts: Uint8Array[] }>()
  let n = 0
  let chunks = 0
  const log = (op: string, ...args: unknown[]) => calls.push({ op, args })
  const page = <T,>(all: T[], after: string | null | undefined, limit = 64) => {
    const size = Math.min(limit, opts.pageSize ?? limit)
    const start = after ? Number(after) : 0
    const slice = all.slice(start, start + size)
    return { slice, next: start + size < all.length ? String(start + size) : null }
  }
  const find = (object_id: string) => lists.received.find((i) => i.object_id === object_id) ?? lists.sent.find((i) => i.object_id === object_id)
  const notify = (object_id: string, version: number) => {
    for (const fn of [...subs]) fn({ t: 'drop', object_id, version })
  }
  const emit: DropFixture['emit'] = (direction, item) => {
    const i = lists[direction].findIndex((x) => x.object_id === item.object_id)
    if (i >= 0) lists[direction][i] = item
    else lists[direction].unshift(item)
    notify(item.object_id, item.version)
  }
  const decode = (b64: string) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))

  const api: DropApi = {
    served: () => opts.served ?? true,
    info: async () => (log('info'), { ...info }),
    list: async (_ws, { direction, view, after, limit }) => {
      log('list', direction, after ?? null, ...(view ? [view] : []))
      if (opts.listError) throw opts.listError
      if (opts.cursorResetAlways) throw new DropError(410, { code: 'drop.cursor_reset', message: 'drop.cursor_reset', retryable: false })
      if (after && opts.cursorResetOnSecondPage && !cursorResetDone) {
        cursorResetDone = true
        if (typeof opts.cursorResetOnSecondPage === 'function') opts.cursorResetOnSecondPage()
        throw new DropError(410, { code: 'drop.cursor_reset', message: 'drop.cursor_reset', retryable: false })
      }
      // The host filters; the page renders what comes back.
      const all = view === 'shared_from_phone' ? lists[direction].filter((i) => i.shared_from) : lists[direction]
      const { slice, next } = page(all, after, limit)
      return { items: structuredClone(slice), next, ...(opts.pendingOthers !== undefined ? { pending_others: opts.pendingOthers } : {}) }
    },
    versions: async (_ws, { object_id, after, limit }) => {
      log('versions', object_id, after ?? null)
      const { slice, next } = page(versions[object_id] ?? [], after, limit)
      return { versions: structuredClone(slice), next, cap: info.version_cap }
    },
    blobPut: async (_ws, args: BlobPutArgs) => {
      log('blob.put', { offset: args.offset, last: args.last, size: args.size, has_blob: !!args.blob })
      const chunk = chunks++
      // Per upload, by offset (not a fixture-wide count): a cancelled earlier upload doesn't move where the next one stalls.
      if (opts.stallAfterChunks !== undefined && args.offset >= opts.stallAfterChunks * CHUNK_BYTES) await new Promise(() => {})
      if (opts.putErrorAtChunk === chunk) throw new DropError(400, { code: 'blob.invalid', message: 'blob.invalid', retryable: false })
      if (opts.chunkDelayMs) await sleep(opts.chunkDelayMs)
      let id = args.blob
      if (!id) {
        n += 1
        id = `b_DEMO${String(n).padStart(22, 'A')}`
        uploads.set(id, { size: args.size ?? 0, parts: [] })
      }
      const u = uploads.get(id)
      if (!u) throw new DropError(400, { code: 'blob.invalid', message: 'blob.invalid', retryable: false })
      u.parts.push(decode(args.b64))
      const received = u.parts.reduce((s, p) => s + p.length, 0)
      if (args.last) {
        const all = new Uint8Array(received)
        let o = 0
        for (const p of u.parts) all.set(p, (o += p.length) - p.length)
        blobs.set(id, all)
      }
      const answer = { blob: id, size: u.size, received }
      return opts.putAnswer ? opts.putAnswer(answer, chunk) : answer
    },
    blobDrop: async (_ws, blob) => {
      log('blob.drop', blob)
      uploads.delete(blob)
      blobs.delete(blob)
    },
    send: async (_ws, args, idem) => {
      log('send', args, idem)
      if (opts.sendDelayMs) await sleep(opts.sendDelayMs)
      if (opts.sendError) throw opts.sendError
      const size = blobs.get(args.blob)?.length ?? 0
      if (args.object_id) {
        const item = find(args.object_id)
        if (!item) throw new DropError(403, { code: 'drop.not_visible', message: 'drop.not_visible', retryable: false })
        if (item.versions >= info.version_cap) throw new DropError(429, { code: 'drop.version_cap', message: 'drop.version_cap', retryable: false })
        if (args.base_version !== item.version) throw new DropError(409, { code: 'drop.version_conflict', message: 'drop.version_conflict', retryable: false }, { current: item.version })
        const version = item.version + 1
        versions[item.object_id] = [dropVersion(version, { size, author: ME, created_ms: Date.now() }), ...(versions[item.object_id] ?? [])]
        emit('sent', { ...item, head: `demo-head-${item.object_id}-v${version}`, version, versions: item.versions + 1, size, created_ms: Date.now(), author: ME })
        return { object_id: item.object_id, version }
      }
      const item = dropItem({ name: args.name, mime: args.mime, size, author: ME, created_ms: Date.now(), expires_ms: Date.now() + 7 * 86_400_000 })
      emit('sent', item)
      return { object_id: item.object_id, version: 1 }
    },
    read: async (_ws, { object_id, version }) => {
      log('read', object_id, version)
      const refused = opts.readError?.[object_id] ?? opts.readError?.['*']
      if (refused) throw refused
      const item = find(object_id)
      if (!item) throw new DropError(403, { code: 'drop.not_visible', message: 'drop.not_visible', retryable: false })
      if (item.state === 'expired' || item.state === 'retired') throw new DropError(410, { code: 'drop.expired', message: 'drop.expired', retryable: false })
      if (item.state === 'pruned') throw new DropError(410, { code: 'drop.pruned', message: 'drop.pruned', retryable: false })
      n += 1
      const blob = `b_READ${String(n).padStart(22, 'A')}`
      // The content an author chose: here an HTML payload, which the page must never render.
      blobs.set(blob, new TextEncoder().encode(`<html><body><script>alert(1)</script>${object_id} v${version}</body></html>`))
      return { blob, name: item.name, name_hidden: item.name_hidden, mime: item.mime, size: item.size }
    },
    content: async (_ws, blob) => {
      log('content', blob)
      const bytes = blobs.get(blob)
      if (!bytes) throw new DropError(404, { code: 'blob.invalid', message: 'blob.invalid', retryable: false })
      return new Blob([bytes as BlobPart])
    },
    retire: async (_ws, args) => {
      log('retire', args)
      if (opts.retireError) throw opts.retireError
      const { object_id, expect_version, expect_head } = args
      const current = find(object_id)
      if (opts.retireConflict || (current && (current.version !== expect_version || current.head !== expect_head)))
        throw new DropError(409, { code: 'drop.version_conflict', message: 'drop.version_conflict', retryable: false })
      for (const dir of ['received', 'sent'] as const) {
        const item = lists[dir].find((i) => i.object_id === object_id)
        if (item) emit(dir, { ...item, state: 'retired', tombstone_until_ms: Date.now() + 30 * 86_400_000 })
      }
    },
    subscribe: (_ws, onItem) => {
      subs.add(onItem)
      return () => void subs.delete(onItem)
    },
  }
  return { api, emit, items: (d) => lists[d], calls, blobs }
}

/** Swap the fixture into `dropApi`; returns the undo. */
export function installDropFixture(fixture: DropFixture): () => void {
  Object.assign(dropApi, fixture.api)
  return () => Object.assign(dropApi, realDropApi)
}
