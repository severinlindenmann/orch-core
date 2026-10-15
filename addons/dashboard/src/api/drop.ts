// Workspace → Drop: the PROPOSED host ops of cap `drop/1` (orch-fa's proposal of 15 Oct 2026, not agreed yet; relay #67
// P3-D39 is design only). Drop is host-mediated: every key stays in the host, which seals, uploads, downloads, verifies
// and decrypts. The browser uploads plaintext with the host's existing chunked `blob.put` (that gives real progress),
// then `drop.send` hands the blob to the host. This module is the one seam: the page calls `dropApi` and nothing else.
// Until the ops are agreed the real implementation answers "not available" for every call and no server route exists;
// the dev layout preview and the tests swap in a fixture (src/dev/dropFixture.ts).
//
// Binding decisions after the proposal (orch-52 host, orch-29 relay, 15 Oct): the host writes FILES only in P4 (document
// versions are display-only); names and MIME types are sanitised by the HOST (the page renders them as untrusted text and
// cleans nothing itself); no auto-claim (unclaimed items are explained, no claim button); Delete for files and documents
// through one host op (`drop.retire`, pending the D39a1 amendment); entry states `live`/`pruned`/`expired`/`retired`.
import { queryOptions } from '@tanstack/react-query'
import { hasFeature } from './features'
import { ApiError } from './types'

/** Relay entry state. `expired` and `retired` entries stay listed as tombstones for 30 days (no download, a reason). */
export type DropState = 'live' | 'pruned' | 'expired' | 'retired'
export type DropKind = 'file' | 'document'
export type DropDirection = 'received' | 'sent'

export interface DropAuthor {
  /** Person id (host-resolved). */
  person: string
  /** ASSUMPTION: the host also gives the person's display name (untrusted text), like `member.list`. */
  person_name?: string | null
  device: string
  /** `agent`: sealed by an agent on the host (marked clearly in the page). */
  origin: 'person' | 'agent' | 'phone'
}

/** One row of `drop.list`. `name` and `mime` are author-controlled; the host cleans them (one rule, shared with device labels). */
export interface DropItem {
  object_id: string
  kind: DropKind
  version: number
  versions: number
  name: string | null
  /** The host could not show the name safely: the page says "a name that can't be shown". */
  name_hidden: boolean
  mime: string | null
  size: number
  created_ms: number
  expires_ms: number | null
  author: DropAuthor
  /** Inbox items before the host claims them (no auto-claim yet): only the host and the author can open them. */
  claimed: boolean
  state: DropState
}

export interface DropListPage {
  items: DropItem[]
  next: string | null
}

export interface DropVersion {
  version: number
  size: number
  created_ms: number
  author: DropAuthor
  state: DropState
}

export interface DropVersionsPage {
  versions: DropVersion[]
  next: string | null
  cap: number
}

/**
 * ASSUMPTION (the "gate answer"): what this host's Drop allows, read once per page. The proposal names the limits but
 * no op that serves them; `drop.info {}` is the shape the page asks for. The page takes the size limit and the version
 * cap from here and nowhere else.
 */
export interface DropInfo {
  /** Plaintext cap per file (relay: 26 MiB ciphertext ≈ 25 MiB plaintext). */
  max_bytes: number
  /** Versions a document keeps (relay: 1024). */
  version_cap: number
  /** What the host may write. P4 (orch-52): `['file']`; document versions are display-only. */
  writes: DropKind[]
}

/** `drop.read`: the host fetched, verified and decrypted into a host blob. The page only ever downloads it. */
export interface DropRead {
  blob: string
  name: string | null
  name_hidden: boolean
  mime: string | null
  size: number
}

/** A `drop` sub item (from the relay's `drop` poll notice): the page refetches, it doesn't poll. */
export interface DropSubItem {
  t: 'drop'
  object_id: string
  version: number
}

export interface BlobPutArgs {
  blob?: string
  upload_key?: string
  offset: number
  b64: string
  last: boolean
  size?: number
  sha256?: string
}

export interface DropApi {
  /** Does this build talk to a host for Drop at all? The real one: not until the ops are agreed. */
  served: (ws: string) => boolean
  info: (ws: string) => Promise<DropInfo>
  list: (ws: string, args: { direction: DropDirection; after?: string | null; limit?: number }) => Promise<DropListPage>
  versions: (ws: string, args: { object_id: string; after?: string | null; limit?: number }) => Promise<DropVersionsPage>
  /** The host's existing `blob.put` (§0.10): 768 KiB chunks, allocation by `upload_key`. */
  blobPut: (ws: string, args: BlobPutArgs) => Promise<{ blob: string; size: number; received: number }>
  blobDrop: (ws: string, blob: string) => Promise<void>
  send: (ws: string, args: { blob: string; name: string; mime: string; object_id?: string; base_version?: number }, idem: string) => Promise<{ object_id: string; version: number }>
  read: (ws: string, args: { object_id: string; version: number }) => Promise<DropRead>
  /**
   * The bytes of a `drop.read` blob. FUTURE: a dashboard route that streams the host blob with
   * `Content-Disposition: attachment; filename*=…`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`
   * (DECISIONS-LOG). Nothing serves it yet: the fixture provides the bytes.
   */
  content: (ws: string, blob: string) => Promise<Blob>
  retire: (ws: string, object_id: string) => Promise<void>
  /** `sub.open {topics:["drop"]}`; returns the close function. */
  subscribe: (ws: string, onItem: (item: DropSubItem) => void) => () => void
}

export const DROP_UNAVAILABLE = 'Drop is not available on this host yet.'

const unavailable = (): Promise<never> =>
  Promise.reject(new ApiError(404, { code: 'drop.unavailable', message: DROP_UNAVAILABLE, retryable: false }))

/** The real implementation: "not available" until the proposed ops are agreed and the server serves them. */
export const realDropApi: DropApi = {
  served: () => false,
  info: unavailable,
  list: unavailable,
  versions: unavailable,
  blobPut: unavailable,
  blobDrop: unavailable,
  send: unavailable,
  read: unavailable,
  content: unavailable,
  retire: unavailable,
  subscribe: () => () => {},
}

/** The api the page uses. A mutable object so the dev preview and tests can swap methods (like `pairingApi`). */
export const dropApi: DropApi = { ...realDropApi }

/** The gate: the server lists feature `drop` (the host serves `drop/1` in `welcome.ops`) and this build talks to it. */
export const dropServed = (ws: string): boolean => hasFeature('drop', ws) && dropApi.served(ws)

export const DROP_PAGE = 32

export const dropQueries = {
  info: (ws: string) => queryOptions({ queryKey: ['drop', ws, 'info'], queryFn: () => dropApi.info(ws), staleTime: 60_000 }),
  list: (ws: string, direction: DropDirection, after: string | null) =>
    queryOptions({ queryKey: ['drop', ws, 'list', direction, after], queryFn: () => dropApi.list(ws, { direction, after, limit: DROP_PAGE }) }),
  versions: (ws: string, object_id: string, after: string | null) =>
    queryOptions({ queryKey: ['drop', ws, 'versions', object_id, after], queryFn: () => dropApi.versions(ws, { object_id, after, limit: DROP_PAGE }) }),
}

// ——— Sizes ———

const MiB = 1024 * 1024
/** "25 MiB", "1.5 MiB", "312 KiB", "12 bytes". */
export function fmtBytes(n: number): string {
  if (n < 1024) return n === 1 ? '1 byte' : `${n} bytes`
  if (n < MiB) return `${Math.round(n / 1024)} KiB`
  const m = n / MiB
  if (m < 1024) return `${Number.isInteger(m) ? m : m.toFixed(1)} MiB`
  const g = m / 1024
  return `${Number.isInteger(g) ? g : g.toFixed(1)} GiB`
}

/** Checked BEFORE any upload starts: null when the file fits. */
export function sizeRefusal(size: number, info: Pick<DropInfo, 'max_bytes'>): string | null {
  return size > info.max_bytes ? `This file is ${fmtBytes(size)}; Drop takes at most ${fmtBytes(info.max_bytes)} per file. Nothing was uploaded.` : null
}

// ——— Errors ———

/** The relay's quota names (DESIGN 2668-2677), in words. Count quotas are counted, the rest are bytes. */
const QUOTAS: Record<string, { what: string; count?: string }> = {
  drop_ws: { what: "this workspace's Drop storage" },
  drop_head_ws: { what: "this workspace's Drop metadata storage" },
  drop_account: { what: "the owner account's Drop storage" },
  drop_versions: { what: 'live versions in this workspace', count: 'versions' },
  drop_documents: { what: 'documents in this workspace', count: 'documents' },
  drop_device: { what: "this device's Drop storage in the workspace" },
  drop_device_head: { what: "this device's Drop metadata storage" },
  drop_device_versions: { what: "this device's versions in the workspace", count: 'versions' },
  drop_device_documents: { what: "this device's documents in the workspace", count: 'documents' },
  drop_doc_versions: { what: "this document's versions", count: 'versions' },
}

export interface DropErrorDetail {
  code: string
  /** The core sentence. Host-supplied values in it are numbers or known names only. */
  text: string
  /** An unknown quota name: shown separately as untrusted text. */
  quota?: string
}

/**
 * A Drop refusal with the proposal's extra fields (`limit_bytes`; `quota`, `limit`, `used`). ASSUMPTION: the server passes
 * them through beside `code`; the shared `ApiError` has no slot for them, so they ride here.
 */
export class DropError extends ApiError {
  readonly detail: Readonly<Record<string, unknown>>
  constructor(status: number, body: ConstructorParameters<typeof ApiError>[1], detail: Record<string, unknown> = {}) {
    super(status, body)
    this.detail = detail
  }
}

const field = (e: ApiError, key: string): unknown => (e instanceof DropError ? e.detail[key] : undefined)
const num = (e: ApiError, key: string): number | null => {
  const v = field(e, key)
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}
const str = (e: ApiError, key: string): string | null => {
  const v = field(e, key)
  return typeof v === 'string' ? v : null
}

/** A Drop error as one clear sentence with its code. `cap` / `limit`: the page's own values when the error has none. */
export function dropError(error: unknown, ctx: { max_bytes?: number; version_cap?: number } = {}): DropErrorDetail {
  if (!(error instanceof ApiError)) return { code: 'error', text: 'Something went wrong. Try again in a moment.' }
  const code = error.code
  switch (code) {
    case 'drop.too_large': {
      const limit = num(error, 'limit_bytes') ?? ctx.max_bytes ?? null
      return { code, text: `This file is too large for Drop${limit !== null ? `: the limit is ${fmtBytes(limit)} per file` : ''}.` }
    }
    case 'drop.quota': {
      const name = str(error, 'quota') ?? ''
      const q = QUOTAS[name]
      const used = num(error, 'used')
      const limit = num(error, 'limit')
      const amount = (n: number) => (q?.count ? `${n} ${q.count}` : fmtBytes(n))
      const usage = used !== null && limit !== null ? ` (${amount(used)} of ${amount(limit)} used)` : ''
      return {
        code,
        text: `A Drop limit is full: ${q ? q.what : 'a quota the page does not know'}${usage}. Sending won't work again until something is removed or expires.`,
        ...(q ? {} : { quota: name }),
      }
    }
    case 'drop.version_cap':
      return { code, text: `This document already has ${ctx.version_cap ?? 1024} versions, the most a document keeps. No new version can be saved.` }
    case 'drop.version_conflict':
      return { code, text: 'Someone saved a newer version; reload.' }
    case 'drop.expired':
      return { code, text: 'This item has expired: its content is gone. The entry stays listed for a while.' }
    case 'drop.pruned':
      return { code, text: 'This version was pruned: only the newest 8 versions and those under 30 days old keep their content.' }
    case 'drop.unavailable':
      return { code, text: "Drop can't reach the relay right now (it is unreachable, or this host is not linked to one)." }
    case 'drop.not_visible':
      return { code, text: "You can't see this item (it is not shared with you, or it was removed)." }
  }
  if (error.retryable) {
    const s = error.retry_after_ms !== undefined ? Math.max(1, Math.ceil(error.retry_after_ms / 1000)) : null
    return { code, text: `Drop is busy right now. Try again${s !== null ? ` in ${s} s` : ' in a moment'}.` }
  }
  return { code, text: 'Not done.' }
}

// ——— Upload (browser → host blob) ———

/** `blob.put` chunks decode to at most 768 KiB (§0.10). */
export const CHUNK_BYTES = 768 * 1024

export class UploadCancelled extends Error {
  constructor() {
    super('cancelled')
    this.name = 'UploadCancelled'
  }
}

const hex = (buf: ArrayBuffer) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('')
function b64(bytes: Uint8Array): string {
  let s = ''
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(s)
}
const randomKey = () => (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`).replace(/-/g, '')

/** Read a Blob's bytes (File.arrayBuffer where there is one; FileReader otherwise). */
function bytesOf(file: Blob): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === 'function') return file.arrayBuffer()
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(r.result as ArrayBuffer)
    r.onerror = () => reject(r.error)
    r.readAsArrayBuffer(file)
  })
}

/**
 * Upload `file` to a host blob in `blob.put` chunks; `onProgress(sent, total)` after each acknowledged chunk. Abort with
 * `signal` between chunks: the allocated blob is dropped (best effort) and `UploadCancelled` is thrown.
 */
export async function uploadBlob(ws: string, file: Blob, opts: { onProgress?: (sent: number, total: number) => void; signal?: AbortSignal } = {}): Promise<string> {
  const { onProgress, signal } = opts
  const check = () => {
    if (signal?.aborted) throw new UploadCancelled()
  }
  // Cancel also ends the wait for a chunk in flight (the host may answer it later; the blob is dropped either way).
  const aborted = new Promise<never>((_, reject) => signal?.addEventListener('abort', () => reject(new UploadCancelled()), { once: true }))
  aborted.catch(() => {})
  check()
  const data = new Uint8Array(await bytesOf(file))
  const sha256 = `sha256:${hex(await crypto.subtle.digest('SHA-256', data))}`
  const total = data.length
  const upload_key = randomKey()
  let blob: string | undefined
  let inflight: Promise<{ blob: string }> | null = null
  let offset = 0
  onProgress?.(0, total)
  try {
    do {
      check()
      const end = Math.min(total, offset + CHUNK_BYTES)
      const last = end === total
      const put = dropApi.blobPut(ws, {
        ...(blob ? { blob } : { upload_key, size: total }),
        offset,
        b64: b64(data.subarray(offset, end)),
        last,
        ...(last ? { sha256 } : {}),
      })
      inflight = put
      const r = await Promise.race([put, aborted])
      blob = r.blob
      offset = Math.max(end, r.received)
      onProgress?.(offset, total)
      check()
    } while (offset < total)
    return blob!
  } catch (e) {
    if (e instanceof UploadCancelled) {
      // Drop the allocation: known already, or the one the first chunk in flight will answer with.
      const drop = (id: string) => void dropApi.blobDrop(ws, id).catch(() => {})
      if (blob) drop(blob)
      else inflight?.then((r) => drop(r.blob), () => {})
    }
    throw e
  }
}

/** A fresh idempotency key for one `drop.send`. */
export const newIdem = randomKey

// ——— Download ———

/**
 * Start a download of the bytes: a blob URL typed `application/octet-stream` behind an anchor with `download`. Never
 * shown inline, never opened in a tab. The name is the host's cleaned name (decision 2: the page cleans nothing itself;
 * the browser strips path separators from `download`); a hidden name downloads as `drop-<object id>`.
 */
export function saveBytes(bytes: Blob, name: string | null, fallback: string): void {
  const url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }))
  const a = document.createElement('a')
  a.href = url
  a.download = name && name.length > 0 ? name : fallback
  a.rel = 'noopener'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}
