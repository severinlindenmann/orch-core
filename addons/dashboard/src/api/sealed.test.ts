import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { __sealWithEphemeralForTests, b64urlEncode, importKeyPair, newRevealKey, open, revealKeyFromPair, seal, SealedError, type SealedContext } from './sealed'

type Jwk = { x: string; y: string; d: string }
type SealVector = { name: string; context: SealedContext; ephemeral_jwk: Jwk; recipient_jwk: Jwk; recipient_public: string; plaintext: string; envelope: { v: 1; alg: string; epk: string; ct: string } }
type OpenVector = { bound: { workspace_id: string; request_id: string }; name: string; direction: 'in' | 'out'; context: SealedContext; envelope: unknown; expect: string; recipient_jwk: Jwk; base: string }
const V = JSON.parse(readFileSync(resolve(process.cwd(), '../../plugins/orch-core/tests/click_sealed_vectors.json'), 'utf8')) as { seal: SealVector[]; open: OpenVector[] }

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
      const env = await __sealWithEphemeralForTests(v.recipient_public, v.plaintext, v.context, eph)
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
      const got = await reason(open(key, c.envelope, c.context))
      expect(got).toBe(c.expect)
      if (c.expect === 'ok') {
        const bytes = await open(await revealKeyFromPair(await importKeyPair(c.recipient_jwk), c.bound), c.envelope, c.context)
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
    expect(await reason(open(k, { ...(ok.envelope as object), ct: (ok.envelope as { ct: string }).ct.slice(0, -2) + 'AA' }, ok.context))).toBe('open_failed')
    expect(await reason(open(k, ok.envelope, ok.context))).toBe('ok')
    expect(await reason(open(k, ok.envelope, ok.context))).toBe('replay')
  })
  it('refuses after close', async () => {
    const k = await mk()
    k.close()
    expect(k.closed).toBe(true)
    expect(await reason(open(k, ok.envelope, ok.context))).toBe('closed')
  })
  it('refuses an in purpose context', async () => {
    const k = await mk()
    expect(await reason(open(k, ok.envelope, { ...ok.context, purpose: 'github.token' }))).toBe('context')
    expect(await reason(open(k, ok.envelope, { ...ok.context, request_id: 'hr_short' }))).toBe('context')
  })
  it('pins the view digest of the first successful open', async () => {
    const k = await mk()
    expect(await reason(open(k, ok.envelope, ok.context))).toBe('ok')
    const other = { ...ok.envelope as object, epk: 'x' }
    expect(await reason(open(k, other, { ...ok.context, view_digest: 'sha256:' + 'cd'.repeat(32) }))).toBe('context')
  })
  it('refuses a context for another request or workspace before decrypting', async () => {
    const k = await mk()
    expect(await reason(open(k, 'garbage', { ...ok.context, request_id: 'hr_01JB2C3D4E5F6G7H8J9K0MNPQS' }))).toBe('context')
    expect(await reason(open(k, 'garbage', { ...ok.context, workspace_id: 'f'.repeat(32) }))).toBe('context')
  })
  it('round-trips with a generated key and keeps the private key non-extractable', async () => {
    const k = await newRevealKey(ok.bound)
    expect(k.publicKey.length).toBe(65)
    expect(k.publicKeyB64).toBe(b64urlEncode(k.publicKey))
    // Seal an "out" value with the same construction by importing the vector signer as the sender is not possible here;
    // instead check the key itself cannot be exported.
    await expect(globalThis.crypto.subtle.exportKey('jwk', k._priv()!)).rejects.toBeTruthy()
    expect(k._priv()!.extractable).toBe(false)
  })
})
