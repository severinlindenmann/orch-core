import { webcrypto, verify, createPublicKey } from 'node:crypto'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { enrolBrowser, upgradeSession } from './trustedKey'

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

it('enrols a public key, and a switch session signs the server challenge with the private one', async () => {
  const calls: { path: string; body: any }[] = []
  let enrolled: any = null
  const fetcher = vi.fn(async (path: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body))
    calls.push({ path, body })
    if (path === '/api/trusted/enrol') { enrolled = body.jwk; return Response.json({ key_id: 'k' }) }
    if (path === '/api/auth/challenge') return Response.json({ nonce: 'N0NCE' })
    return Response.json({ token: 'full-token' })
  }) as unknown as typeof fetch
  await enrolBrowser('local-token', fetcher)
  expect(enrolled).toMatchObject({ kty: 'EC', crv: 'P-256' })
  expect(enrolled.d).toBeUndefined() // public half only
  expect(await upgradeSession('switch-token', fetcher)).toBe('full-token')
  const up = calls.find((c) => c.path === '/api/auth/upgrade')!.body
  expect(up.key_id).toMatch(/^[0-9a-f]{32}$/)
  const raw = Buffer.from(up.signature.replace(/-/g, '+').replace(/_/g, '/'), 'base64')
  expect(raw.length).toBe(64)
  const ok = verify('sha256', Buffer.from(`orch-switch-upgrade-v1\0N0NCE\0${location.origin}`), { key: createPublicKey({ key: enrolled, format: 'jwk' }), dsaEncoding: 'ieee-p1363' }, raw)
  expect(ok).toBe(true)
})

it('a browser that never signed in here has no key and stays read-mostly', async () => {
  const fetcher = vi.fn() as unknown as typeof fetch
  expect(await upgradeSession('switch-token', fetcher)).toBeNull()
  expect(fetcher).not.toHaveBeenCalled()
})

it('no WebCrypto means no upgrade, never an error', async () => {
  vi.stubGlobal('crypto', {})
  expect(await upgradeSession('t', vi.fn() as unknown as typeof fetch)).toBeNull()
  await expect(enrolBrowser('t', vi.fn() as unknown as typeof fetch)).resolves.toBeUndefined()
})
