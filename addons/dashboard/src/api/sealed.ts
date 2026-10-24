// The sealed field, browser side (#1198 C6). Contract: docs/architecture/orch-v2-sealed-field.md (read it before changing
// anything here); vectors: plugins/orch-core/tests/click_sealed_vectors.json. WebCrypto only, no dependencies.
//
// seal(): the four typed-secret purposes (direction "in"), browser -> click signer.
// open(): grant.secret only (direction "out"), click signer -> browser.
// Not wired into any page yet (C7, C10, C12, C13, C16 do that).

export const ALG = 'P256-HKDF-SHA256-A256GCM'
export const IN_PURPOSES = ['hub.backup_key', 'device.recovery_code', 'custody.passphrase', 'github.token'] as const
export const OUT_PURPOSE = 'grant.secret'
export type InPurpose = (typeof IN_PURPOSES)[number]

export type SealedContext = { purpose: string; workspace_id: string; request_id: string; view_digest: string }
export type Envelope = { v: 1; alg: typeof ALG; epk: string; ct: string }

export type SealedReason = 'closed' | 'context' | 'malformed' | 'replay' | 'open_failed' | 'too_large'
/** A refusal with a static reason: never the secret, never crypto detail. */
export class SealedError extends Error {
  readonly reason: SealedReason
  constructor(reason: SealedReason) {
    super(`sealed: ${reason}`)
    this.name = 'SealedError'
    this.reason = reason
  }
}

const LABEL = new TextEncoder().encode('orch-sealed-v1')
const MAX_SECRET = 4094
const BLOCK = 256
const TAG = 16
const CURVE = { name: 'ECDH', namedCurve: 'P-256' } as const

const subtle = (): SubtleCrypto => globalThis.crypto.subtle

// ---- base64url (no padding, canonical) ----

export function b64urlEncode(bytes: Uint8Array): string {
  let s = ''
  for (const b of bytes) s += String.fromCharCode(b)
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** Strict decode: only the url alphabet, no padding, and the last character must be the canonical one. */
function b64urlDecodeStrict(text: unknown): Uint8Array | null {
  if (typeof text !== 'string' || !/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) return null
  let bin: string
  try {
    bin = atob(text.replace(/-/g, '+').replace(/_/g, '/'))
  } catch {
    return null
  }
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return b64urlEncode(out) === text ? out : null
}

/** INTERNAL, for sealed.testing.ts only. */
export const b64urlDecodeStrictForTests = b64urlDecodeStrict

// ---- context ----

const WORKSPACE_RE = /^[0-9a-f]{32}$/
const REQUEST_RE = /^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$/ // as the signer's: a 128-bit ULID, so the first character is 0-7
const DIGEST_RE = /^sha256:[0-9a-f]{64}$/

function directionOf(purpose: unknown): 'in' | 'out' | null {
  if (purpose === OUT_PURPOSE) return 'out'
  return (IN_PURPOSES as readonly unknown[]).includes(purpose) ? 'in' : null
}

function validContext(ctx: SealedContext): 'in' | 'out' | null {
  if (!ctx || typeof ctx !== 'object') return null
  const dir = directionOf(ctx.purpose)
  if (!dir) return null
  if (typeof ctx.workspace_id !== 'string' || !WORKSPACE_RE.test(ctx.workspace_id)) return null
  if (typeof ctx.request_id !== 'string' || !REQUEST_RE.test(ctx.request_id)) return null
  if (typeof ctx.view_digest !== 'string' || !DIGEST_RE.test(ctx.view_digest)) return null
  return dir
}

// ---- construction ----

function lengthPrefixed(parts: Uint8Array[]): Uint8Array {
  let n = 0
  for (const p of parts) n += 2 + p.length
  const out = new Uint8Array(n)
  let o = 0
  for (const p of parts) {
    out[o++] = p.length >> 8
    out[o++] = p.length & 255
    out.set(p, o)
    o += p.length
  }
  return out
}

function buildAad(dir: 'in' | 'out', ctx: SealedContext, epk: Uint8Array, rpk: Uint8Array): Uint8Array {
  const enc = new TextEncoder()
  return lengthPrefixed([LABEL, enc.encode(dir), enc.encode(ctx.purpose), enc.encode(ctx.workspace_id), enc.encode(ctx.request_id), enc.encode(ctx.view_digest), epk, rpk])
}

/** Import a peer public point; WebCrypto refuses a point that is not on the curve. */
async function importPeer(raw: Uint8Array): Promise<CryptoKey> {
  try {
    return await subtle().importKey('raw', raw as BufferSource, CURVE, false, [])
  } catch {
    throw new SealedError('malformed')
  }
}

async function deriveKeyNonce(priv: CryptoKey, peer: CryptoKey, aad: Uint8Array): Promise<{ key: CryptoKey; nonce: Uint8Array }> {
  const z = new Uint8Array(await subtle().deriveBits({ name: 'ECDH', public: peer }, priv, 256))
  const ikm = await subtle().importKey('raw', z, 'HKDF', false, ['deriveBits'])
  z.fill(0)
  const okm = new Uint8Array(await subtle().deriveBits({ name: 'HKDF', hash: 'SHA-256', salt: LABEL as BufferSource, info: aad as BufferSource }, ikm, 44 * 8))
  const key = await subtle().importKey('raw', okm.slice(0, 32), 'AES-GCM', false, ['encrypt', 'decrypt'])
  const nonce = okm.slice(32)
  okm.fill(0)
  return { key, nonce }
}

/** A P-256 key: a non-extractable private key and its 65-byte public point. */
export type KeyPair = { privateKey: CryptoKey; publicKey: Uint8Array }

async function newKeyPair(): Promise<KeyPair> {
  // The private key is never extractable: it is born that way, nothing ever holds its scalar. A public key always exports.
  const pair = await subtle().generateKey(CURVE, false, ['deriveBits'])
  const publicKey = new Uint8Array(await subtle().exportKey('raw', pair.publicKey))
  return { privateKey: pair.privateKey, publicKey }
}

function padSecret(secret: Uint8Array): Uint8Array {
  const n = secret.length
  if (n < 1) throw new SealedError('malformed')
  if (n > MAX_SECRET) throw new SealedError('too_large')
  const padded = new Uint8Array(Math.ceil((n + 2) / BLOCK) * BLOCK)
  padded[0] = n >> 8
  padded[1] = n & 255
  padded.set(secret, 2)
  return padded
}

/**
 * Seal `secret` (UTF-8 text) to the signer's public key `signerPk` (65 bytes, or base64url from the View's `seal.pk`).
 * The ephemeral key is always fresh: the nonce is derived from the key agreement, so a reused key would reuse a nonce.
 */
export function seal(signerPk: Uint8Array | string, secret: string, ctx: SealedContext): Promise<Envelope> {
  return sealInternal(signerPk, secret, ctx, undefined)
}

/** INTERNAL, for sealed.testing.ts only (the vectors fix the ephemeral key). Page code calls seal(). */
export async function sealInternal(signerPk: Uint8Array | string, secret: string, ctx: SealedContext, ephemeral: KeyPair | undefined): Promise<Envelope> {
  const dir = validContext(ctx)
  if (dir !== 'in') throw new SealedError('context')
  const rpk = typeof signerPk === 'string' ? b64urlDecodeStrict(signerPk) : signerPk
  if (!rpk || rpk.length !== 65 || rpk[0] !== 4) throw new SealedError('malformed')
  const bytes = new TextEncoder().encode(secret)
  let padded: Uint8Array | null = null
  try {
    padded = padSecret(bytes)
    const eph = ephemeral ?? (await newKeyPair())
    const aad = buildAad('in', ctx, eph.publicKey, rpk)
    const derived = await deriveKeyNonce(eph.privateKey, await importPeer(rpk), aad) // a recipient key off the curve: malformed
    const ct = new Uint8Array(await subtle().encrypt({ name: 'AES-GCM', iv: derived.nonce as BufferSource, additionalData: aad as BufferSource }, derived.key, padded as BufferSource))
    return { v: 1, alg: ALG, epk: b64urlEncode(eph.publicKey), ct: b64urlEncode(ct) }
  } finally {
    bytes.fill(0)
    padded?.fill(0)
  }
}

/** What a reveal key is made for: the approval's workspace and request. open() refuses any other context. */
export type Bound = { workspace_id: string; request_id: string }

function equalBytes(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false
  let d = 0
  for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i]
  return d === 0
}

/**
 * The browser's key for one sign call that expects a grant-secret reveal. Create it with `newRevealKey()`, send the public key at prepare, `bind()` it when the request id is known, send
 * `publicKeyB64` as `reveal_key`, call `open()` once on the reveal, then `close()` it.
 * The context of every open is built from the caller's own state (the approval it holds), never from the envelope or the server.
 */
export class RevealKey {
  readonly publicKey: Uint8Array
  #priv: CryptoKey | null
  readonly #seen = new Set<string>()
  #bound: Bound | null
  #pinnedDigest: string | null = null

  /** Use newRevealKey(). Taking a ready key pair is for sealed.testing.ts (fixed vector keys). Unbound until bind(). */
  constructor(pair: KeyPair) {
    this.publicKey = pair.publicKey
    this.#priv = pair.privateKey
    this.#bound = null
  }
  /**
   * Bind the key to its approval once. The grant reveal sends the public key when it PREPARES the request (the key is
   * part of the review), before the request id exists; the id is bound as soon as prepare answers. open() refuses until bound.
   */
  bind(bound: Bound): void {
    if (this.#bound || !bound || !WORKSPACE_RE.test(bound.workspace_id) || !REQUEST_RE.test(bound.request_id)) throw new SealedError('context')
    this.#bound = { workspace_id: bound.workspace_id, request_id: bound.request_id }
  }
  get publicKeyB64(): string {
    return b64urlEncode(this.publicKey)
  }
  get closed(): boolean {
    return this.#priv === null
  }
  close(): void {
    this.#priv = null
    this.#seen.clear()
  }

  /**
   * Open the `reveal.envelope` of a sign result. Returns the secret bytes: the caller shows them, then overwrites them
   * (`fill(0)`). Checks run in the order of contract section 5, all before any decryption.
   */
  async open(envelope: unknown, ctx: SealedContext): Promise<Uint8Array> {
    // closed
    const priv = this.#priv
    if (!priv) throw new SealedError('closed')
    // context (and the direction: the browser opens "out" values only), then this key's own approval and pinned View
    if (validContext(ctx) !== 'out') throw new SealedError('context')
    const bound = this.#bound
    if (!bound || ctx.workspace_id !== bound.workspace_id || ctx.request_id !== bound.request_id) throw new SealedError('context')
    if (this.#pinnedDigest !== null && ctx.view_digest !== this.#pinnedDigest) throw new SealedError('context')
    // exactly v, alg, epk, ct; v the integer 1; the one algorithm
    if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) throw new SealedError('malformed')
    const env = envelope as Record<string, unknown>
    if (Object.keys(env).sort().join(',') !== 'alg,ct,epk,v') throw new SealedError('malformed')
    if (typeof env.v !== 'number' || !Number.isInteger(env.v) || env.v !== 1) throw new SealedError('malformed')
    if (env.alg !== ALG) throw new SealedError('malformed')
    // canonical base64url
    const epk = b64urlDecodeStrict(env.epk)
    const ct = b64urlDecodeStrict(env.ct)
    if (!epk || !ct) throw new SealedError('malformed')
    // epk: 65 bytes, 0x04, on the curve (the import checks), not our own key
    if (epk.length !== 65 || epk[0] !== 4) throw new SealedError('malformed')
    const peer = await importPeer(epk)
    if (equalBytes(epk, this.publicKey)) throw new SealedError('malformed')
    // ct length
    const body = ct.length - TAG
    if (body < BLOCK || body > 4096 || body % BLOCK !== 0) throw new SealedError('malformed')
    // replay (a failed open below does not burn the epk)
    const epkText = env.epk as string
    if (this.#seen.has(epkText)) throw new SealedError('replay')
    // authenticate + decrypt
    const aad = buildAad('out', ctx, epk, this.publicKey)
    const derived = await deriveKeyNonce(priv, peer, aad)
    let padded: Uint8Array
    try {
      padded = new Uint8Array(await subtle().decrypt({ name: 'AES-GCM', iv: derived.nonce as BufferSource, additionalData: aad as BufferSource }, derived.key, ct as BufferSource))
    } catch {
      throw new SealedError('open_failed')
    }
    // padding
    try {
      const n = (padded[0] << 8) | padded[1]
      if (n < 1 || n > padded.length - 2 || Math.ceil((n + 2) / BLOCK) * BLOCK !== padded.length) throw new SealedError('malformed')
      for (let i = 2 + n; i < padded.length; i++) if (padded[i] !== 0) throw new SealedError('malformed')
      this.#seen.add(epkText)
      this.#pinnedDigest ??= ctx.view_digest
      return padded.slice(2, 2 + n)
    } finally {
      padded.fill(0)
    }
  }
}

export async function newRevealKey(): Promise<RevealKey> {
  return new RevealKey(await newKeyPair())
}
