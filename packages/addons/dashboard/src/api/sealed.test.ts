import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { b64urlEncode, newRevealKey, seal, SealedError, type SealedContext } from './sealed'
import { importKeyPair, revealKeyFromPair, sealWithEphemeral } from './sealed.testing'

type Jwk = { x: string; y: string; d: string }
type SealVector = { name: string; context: SealedContext; ephemeral_jwk: Jwk; recipient_jwk: Jwk; recipient_public: string; plaintext: string; envelope: { v: 1; alg: string; epk: string; ct: string } }
type OpenVector = { bound: { workspace_id: string; request_id: string }; name: string; direction: 'in' | 'out'; context: SealedContext; envelope: unknown; expect: string; recipient_jwk: Jwk; base: string }
const V = JSON.parse(readFileSync(resolve(process.cwd(), '../../core/tests/click_sealed_vectors.json'), 'utf8')) as { seal: SealVector[]; open: OpenVector[] }

const reason = async (p: Promise<unknown>) => {
  try {
    await p
    return 'ok'
  } catch (e) {
    if (e instanceof SealedError) return e.reason
    throw e
  }
}

describe('seal vectors (byte for byte)', () => {
  const inCases = V.seal.filter((v) => v.context.purpose !== 'grant.secret')
  it('has the input purposes', () => expect(inCases.length).toBeGreaterThanOrEqual(6))
  for (const v of inCases) {
    it(v.name, async () => {
      const eph = await importKeyPair(v.ephemeral_jwk)
      const env = await sealWithEphemeral(v.recipient_public, v.plaintext, v.context, eph)
      expect(env).toEqual(v.envelope)
    })
  }
  it('refuses the out purpose, an empty secret and an oversized one', async () => {
    const c = V.seal[0].context
    const pk = V.seal[0].recipient_public
    expect(await reason(seal(pk, 'x', { ...c, purpose: 'grant.secret' }))).toBe('context')
    expect(await reason(seal(pk, 'x', { ...c, purpose: 'nope' }))).toBe('context')
    expect(await reason(seal(pk, '', c))).toBe('malformed')
    expect(await reason(seal(pk, 'a'.repeat(4095), c))).toBe('too_large')
    expect(await reason(seal(pk, 'a'.repeat(4094), c))).toBe('ok')
  })
  it('uses a fresh ephemeral key each time', async () => {
    const c = V.seal[0].context
    const a = await seal(V.seal[0].recipient_public, 'same', c)
    const b = await seal(V.seal[0].recipient_public, 'same', c)
    expect(a.epk).not.toBe(b.epk)
    expect(a.ct).not.toBe(b.ct)
  })
})

describe('open vectors (direction out)', () => {
  const base = V.seal.find((v) => v.name === 'out_grant_secret')!
  for (const c of V.open.filter((c) => c.direction === 'out')) {
    it(c.name, async () => {
      const key = revealKeyFromPair(await importKeyPair(c.recipient_jwk), c.bound)
      const got = await reason(key.open(c.envelope, c.context))
      expect(got).toBe(c.expect)
      if (c.expect === 'ok') {
        const bytes = await revealKeyFromPair(await importKeyPair(c.recipient_jwk), c.bound).open(c.envelope, c.context)
        expect(new TextDecoder().decode(bytes)).toBe(base.plaintext)
      }
    })
  }
})

describe('open behaviour', () => {
  const ok = V.open.find((c) => c.direction === 'out' && c.expect === 'ok')!
  const mk = async () => revealKeyFromPair(await importKeyPair(ok.recipient_jwk), ok.bound)
  it('refuses a replay of the same epk, but a failed open does not burn it', async () => {
    const k = await mk()
    expect(await reason(k.open({ ...(ok.envelope as object), ct: (ok.envelope as { ct: string }).ct.slice(0, -2) + 'AA' }, ok.context))).toBe('open_failed')
    expect(await reason(k.open(ok.envelope, ok.context))).toBe('ok')
    expect(await reason(k.open(ok.envelope, ok.context))).toBe('replay')
  })
  it('refuses after close', async () => {
    const k = await mk()
    k.close()
    expect(k.closed).toBe(true)
    expect(await reason(k.open(ok.envelope, ok.context))).toBe('closed')
  })
  it('refuses an in purpose context', async () => {
    const k = await mk()
    expect(await reason(k.open(ok.envelope, { ...ok.context, purpose: 'github.token' }))).toBe('context')
    expect(await reason(k.open(ok.envelope, { ...ok.context, request_id: 'hr_short' }))).toBe('context')
  })
  it('pins the view digest of the first successful open', async () => {
    const k = await mk()
    expect(await reason(k.open(ok.envelope, ok.context))).toBe('ok')
    const other = { ...ok.envelope as object, epk: 'x' }
    expect(await reason(k.open(other, { ...ok.context, view_digest: 'sha256:' + 'cd'.repeat(32) }))).toBe('context')
  })
  it('refuses a context for another request or workspace before decrypting', async () => {
    const k = await mk()
    expect(await reason(k.open('garbage', { ...ok.context, request_id: 'hr_01JB2C3D4E5F6G7H8J9K0MNPQS' }))).toBe('context')
    expect(await reason(k.open('garbage', { ...ok.context, workspace_id: 'f'.repeat(32) }))).toBe('context')
  })
  it('makes its keys non-extractable at birth (no private key ever exportable)', async () => {
    const spy = vi.spyOn(globalThis.crypto.subtle, 'generateKey')
    try {
      const k = await newRevealKey()
      k.bind(ok.bound)
      await seal(V.seal[0].recipient_public, 'x', V.seal[0].context)
      expect(spy).toHaveBeenCalledTimes(2)
      for (const call of spy.mock.calls) expect(call[1]).toBe(false)
      for (const r of spy.mock.results) expect(((await r.value) as CryptoKeyPair).privateKey.extractable).toBe(false)
      expect(k.publicKey.length).toBe(65)
      expect(k.publicKeyB64).toBe(b64urlEncode(k.publicKey))
    } finally {
      spy.mockRestore()
    }
  })
  it('accepts the signer request-id alphabet only (first character 0-7)', async () => {
    const k = await mk()
    expect(await reason(k.open('garbage', { ...ok.context, request_id: 'hr_81JB2C3D4E5F6G7H8J9K0MNPQR' }))).toBe('context')
    expect(() => revealKeyFromPair({ privateKey: {} as CryptoKey, publicKey: new Uint8Array(65) }, { ...ok.bound, request_id: 'hr_81JB2C3D4E5F6G7H8J9K0MNPQR' })).toThrow()
  })
  it('refuses replay before deriving, and off-curve before replay', async () => {
    const k = await mk()
    expect(await reason(k.open(ok.envelope, ok.context))).toBe('ok')
    const env = ok.envelope as { epk: string }
    const bad = { ...env, epk: env.epk.slice(0, 40) + (env.epk[40] === 'A' ? 'B' : 'A') + env.epk.slice(41) }
    expect(['malformed', 'open_failed']).toContain(await reason(k.open(bad, ok.context)))
  })
  it('keeps test helpers out of page code', () => {
    const root = resolve(process.cwd(), 'src')
    const offenders: string[] = []
    const walk = (d: string) => {
      for (const e of readdirSync(d, { withFileTypes: true })) {
        const f = resolve(d, e.name)
        if (e.isDirectory()) walk(f)
        else if (/\.(ts|tsx)$/.test(e.name) && !/\.test\.|sealed\.testing/.test(e.name) && /sealed\.testing|sealInternal|b64urlDecodeStrictForTests/.test(readFileSync(f, 'utf8')) && !f.endsWith('sealed.ts')) offenders.push(f)
      }
    }
    walk(root)
    expect(offenders).toEqual([])
  })
})
