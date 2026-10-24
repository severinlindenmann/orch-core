// TEST ONLY: fixed-key helpers for the sealed-field vectors. Page code must never import this file (sealed.test.ts checks it).
import { RevealKey, b64urlDecodeStrictForTests, SealedError, sealInternal, type Bound, type Envelope, type KeyPair, type SealedContext } from './sealed'

const CURVE = { name: 'ECDH', namedCurve: 'P-256' } as const

/** Import a P-256 private JWK as a non-extractable key with its public point. */
export async function importKeyPair(jwk: { x: string; y: string; d: string }): Promise<KeyPair> {
  const privateKey = await globalThis.crypto.subtle.importKey('jwk', { kty: 'EC', crv: 'P-256', x: jwk.x, y: jwk.y, d: jwk.d, ext: false }, CURVE, false, ['deriveBits'])
  const x = b64urlDecodeStrictForTests(jwk.x)
  const y = b64urlDecodeStrictForTests(jwk.y)
  if (!x || !y || x.length !== 32 || y.length !== 32) throw new SealedError('malformed')
  const publicKey = new Uint8Array(65)
  publicKey[0] = 4
  publicKey.set(x, 1)
  publicKey.set(y, 33)
  return { privateKey, publicKey }
}

export function revealKeyFromPair(pair: KeyPair, bound: Bound): RevealKey {
  return new RevealKey(pair, bound)
}

/** seal() with a fixed ephemeral key, to reproduce the vectors byte for byte. */
export function sealWithEphemeral(signerPk: Uint8Array | string, secret: string, ctx: SealedContext, ephemeral: KeyPair): Promise<Envelope> {
  return sealInternal(signerPk, secret, ctx, ephemeral)
}
