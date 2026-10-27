import { webcrypto, verify, createPublicKey, createHash } from 'node:crypto'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as trustedKey from './trustedKey'
import { enrolBrowser, switchProof } from './trustedKey'

// A one-slot IndexedDB: enough for open / get / put with structured values kept by reference (CryptoKeys survive).
function fakeIndexedDb() {
  const store = new Map<string, unknown>()
  const db = { createObjectStore: () => ({}), close: () => {},
    transaction: () => {
      const tx: { oncomplete?: () => void; objectStore: () => unknown } = { objectStore: () => ({
        get: (k: string) => { const r: { result?: unknown; onsuccess?: () => void } = {}; queueMicrotask(() => { r.result = store.get(k); r.onsuccess?.() }); return r },
        put: (v: unknown, k: string) => { store.set(k, v); queueMicrotask(() => tx.oncomplete?.()) },
      }) }
      return tx
    } }
  return { open: () => { const r: { result: typeof db; onupgradeneeded?: () => void; onsuccess?: () => void } = { result: db }; queueMicrotask(() => { r.onupgradeneeded?.(); r.onsuccess?.() }); return r } }
}

beforeEach(() => { vi.stubGlobal('indexedDB', fakeIndexedDb()); vi.stubGlobal('crypto', webcrypto) })
afterEach(() => vi.unstubAllGlobals())

const WS = 'a'.repeat(32)
const TICKET = `${WS}.${'b'.repeat(32)}`
const tHash = createHash('sha256').update(TICKET).digest('hex')

it('enrols a public key, and signs the redeem proof for one ticket without sending or using the ticket', async () => {
  const calls: { path: string; body: any }[] = []
  let enrolled: any = null
  const fetcher = vi.fn(async (path: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body))
    calls.push({ path, body })
    if (path === '/api/trusted/enrol') { enrolled = body.jwk; return Response.json({ key_id: 'k' }) }
    if (path === '/auth/switch/challenge') return Response.json({ nonce: 'N0NCE', name: 'Product' })
    throw new Error('unexpected ' + path)
  }) as unknown as typeof fetch
  await enrolBrowser('local-token', fetcher)
  expect(enrolled).toMatchObject({ kty: 'EC', crv: 'P-256' })
  expect(enrolled.d).toBeUndefined() // public half only
  const got = await switchProof(TICKET, fetcher)
  expect(got.name).toBe('Product')
  const ch = calls.find((c) => c.path === '/auth/switch/challenge')!.body
  expect(ch).toEqual({ ticket_hash: tHash, workspace_id: WS })
  expect(JSON.stringify(calls)).not.toContain('b'.repeat(32)) // the ticket never leaves before the proof is made
  const { key_id: kid, sig } = got.proof!
  expect(kid).toMatch(/^[0-9a-f]{32}$/)
  const raw = Buffer.from(sig.replace(/-/g, '+').replace(/_/g, '/'), 'base64')
  expect(raw.length).toBe(64)
  const key = { key: createPublicKey({ key: enrolled, format: 'jwk' }), dsaEncoding: 'ieee-p1363' as const }
  const msg = `orch-switch-redeem-v1\0N0NCE\0${location.origin}\0${WS}\0${tHash}\0${kid}`
  expect(verify('sha256', Buffer.from(msg), key, raw)).toBe(true)
  expect(verify('sha256', Buffer.from(`orch-switch-upgrade-v1\0N0NCE\0${location.origin}`), key, raw)).toBe(false)
})

it('a browser that never signed in here has no proof, but still learns the name for the confirm', async () => {
  const fetcher = vi.fn(async () => Response.json({ nonce: 'N', name: 'Product' })) as unknown as typeof fetch
  expect(await switchProof(TICKET, fetcher)).toEqual({ name: 'Product', proof: null })
})

it('a refused challenge (a remote listener) means no proof and no name', async () => {
  const fetcher = vi.fn(async () => new Response('', { status: 404 })) as unknown as typeof fetch
  expect(await switchProof(TICKET, fetcher)).toEqual({ name: null, proof: null })
})

it('no WebCrypto means no proof, never an error', async () => {
  vi.stubGlobal('crypto', {})
  const fetcher = vi.fn() as unknown as typeof fetch
  expect(await switchProof(TICKET, fetcher)).toEqual({ name: null, proof: null })
  expect(fetcher).not.toHaveBeenCalled()
  await expect(enrolBrowser('t', fetcher)).resolves.toBeUndefined()
})

it('the upgrade path is gone', () => {
  expect('upgradeSession' in trustedKey).toBe(false)
})
