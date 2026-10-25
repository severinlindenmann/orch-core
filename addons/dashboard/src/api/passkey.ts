// Passkeys in the browser (remote dashboard, #1198 R1-8; host spec 0.8.6 10.3-10.6): sign in, add a device, the touch
// before a big action (step-up) and the passkey list. The WebAuthn browser API does the work; the server checks it.
// Rules: base64url on the wire; a challenge, a credential or an assertion is used at once and dropped: never in a log,
// never in storage, never kept in React state, and nothing here shows them (the page shows plain words only).

import { openSession, getToken } from './bootstrap'
import { isRemoteSession } from './features'
import { createFetchTransport } from './fetchTransport'
import { sha256Hex } from './sha256'
import { ApiError } from './types'

/** What went wrong, in the few kinds the pages have words for. */
export type PasskeyFailure =
  /** The person closed the prompt, or it timed out. Nothing was changed. */
  | 'cancelled'
  /** The device or the server did not accept it. */
  | 'failed'
  /** Too many tries; try again in a minute. */
  | 'locked'
  /** The one-time link is used up or too old. */
  | 'expired'
  /** This browser or device cannot make or use a passkey. */
  | 'unsupported'
  /** The server did not answer. */
  | 'network'
  /** The 10 minutes after a touch ran out, or the session lost it. */
  | 'stale'

/** The plain words for each kind (also the message of the error, so any panel that shows `error.message` is right). */
export const PASSKEY_WORDS: Record<PasskeyFailure, string> = {
  cancelled: 'You cancelled. Nothing was changed.',
  failed: 'Your device did not confirm. Nothing was changed. Try again with the device you signed in with.',
  locked: 'Too many tries. Wait a minute, then try again.',
  expired: 'This link no longer works.',
  unsupported: 'This device cannot use a passkey here.',
  network: 'The server did not answer. Nothing was changed.',
  stale: 'Your passkey check ran out (10 minutes). Nothing was changed. Read the details again, then touch your passkey.',
}

/** An `ApiError`, so every existing panel shows its words; `kind` is what the passkey pages branch on. */
export class PasskeyError extends ApiError {
  readonly kind: PasskeyFailure
  constructor(kind: PasskeyFailure) {
    super(kind === 'locked' ? 429 : 401, { code: kind === 'stale' ? 'passkey.stepup_required' : `passkey.${kind}`, message: PASSKEY_WORDS[kind], retryable: kind !== 'expired' && kind !== 'unsupported' })
    this.kind = kind
  }
}

// ---- base64url (no padding), the only encoding on the wire

export function toBase64Url(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)
  let text = ''
  for (const b of view) text += String.fromCharCode(b)
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function fromBase64Url(value: string): Uint8Array<ArrayBuffer> {
  if (!/^[A-Za-z0-9_-]*$/.test(value)) throw new PasskeyError('failed')
  const padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (value.length % 4)) % 4)
  const text = atob(padded)
  const out = new Uint8Array(text.length)
  for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i)
  return out
}

/** Can this browser make and use passkeys at all (a secure page with the WebAuthn API)? */
export function passkeysSupported(): boolean {
  return typeof window !== 'undefined' && !!window.PublicKeyCredential && !!navigator.credentials?.get && !!navigator.credentials?.create
}

// ---- transport: a few JSON posts; the answer's body is read for its status and a closed set of fields only

async function request<T>(method: 'GET' | 'POST' | 'DELETE', path: string, body?: unknown, signal?: AbortSignal): Promise<{ status: number; json: T | null }> {
  try {
    const result = await createFetchTransport('', getToken).request(method, path, body, signal)
    return { status: result.status, json: result.json as T | null }
  } catch {
    throw new PasskeyError('network')
  }
}

/** The server's refusal as a kind. `where` says what the call was for. */
function refused(status: number, where: 'login' | 'enroll' | 'stepup' | 'other'): PasskeyError {
  if (status === 429) return new PasskeyError('locked')
  if (status === 401 && where === 'enroll') return new PasskeyError('expired')
  if (status === 401 && where === 'stepup') return new PasskeyError('stale')
  if (status >= 500 || status === 0) return new PasskeyError('network')
  return new PasskeyError('failed')
}

interface PublicKeyOptions {
  challenge_id: string
  publicKey: {
    challenge: string
    rpId?: string
    timeout?: number
    userVerification?: UserVerificationRequirement
    allowCredentials?: { type: 'public-key'; id: string }[]
    rp?: { id: string; name: string }
    user?: { id: string; name: string; displayName: string }
    pubKeyCredParams?: PublicKeyCredentialParameters[]
    attestation?: AttestationConveyancePreference
    authenticatorSelection?: AuthenticatorSelectionCriteria
    excludeCredentials?: { type: 'public-key'; id: string }[]
  }
}

function ceremonyError(e: unknown): PasskeyError {
  if (e instanceof PasskeyError) return e
  const name = e instanceof DOMException ? e.name : ''
  // NotAllowedError is what every browser says for "closed the prompt", "timed out" and "no passkey chosen".
  if (name === 'NotAllowedError' || name === 'AbortError') return new PasskeyError('cancelled')
  if (name === 'NotSupportedError' || name === 'SecurityError') return new PasskeyError('unsupported')
  return new PasskeyError('failed')
}

/** Ask the device for an assertion over the server's challenge; the result is the body the server expects, built at once. */
async function assertion(options: PublicKeyOptions, signal?: AbortSignal) {
  if (!passkeysSupported()) throw new PasskeyError('unsupported')
  const pk = options.publicKey
  let credential: Credential | null
  try {
    credential = await navigator.credentials.get({
      signal,
      publicKey: {
        challenge: fromBase64Url(pk.challenge),
        rpId: pk.rpId,
        timeout: pk.timeout,
        userVerification: pk.userVerification ?? 'required',
        allowCredentials: (pk.allowCredentials ?? []).map((c) => ({ type: c.type, id: fromBase64Url(c.id) })),
      },
    })
  } catch (e) {
    throw ceremonyError(e)
  }
  const got = credential as PublicKeyCredential | null
  const r = got?.response as AuthenticatorAssertionResponse | undefined
  if (!got || !r) throw new PasskeyError('failed')
  return {
    challenge_id: options.challenge_id,
    id: got.id,
    response: {
      clientDataJSON: toBase64Url(r.clientDataJSON),
      authenticatorData: toBase64Url(r.authenticatorData),
      signature: toBase64Url(r.signature),
      ...(r.userHandle && r.userHandle.byteLength > 0 ? { userHandle: toBase64Url(r.userHandle) } : {}),
    },
  }
}

// ---- sign in

/** A login challenge fetched ahead of the click (a browser may need the click to open the prompt at once). Memory only. */
let waiting: { options: PublicKeyOptions; at: number } | null = null
const FRESH_MS = 90_000 // the server keeps a challenge 120 s

/**
 * Is this the web address (the remote listener)? The login challenge doubles as the answer: on this computer's own
 * listener the route does not exist (404). Returns false then.
 */
export async function prepareLogin(): Promise<boolean> {
  // never a first screen that waits for good: a server that does not answer is 'orch did not answer' with Try again
  const res = await request<PublicKeyOptions>('POST', '/auth/passkey/options', {}, AbortSignal.timeout(8000))
  if (res.status === 404) return false
  if (res.status !== 200 || !res.json) throw refused(res.status, 'login')
  waiting = { options: res.json, at: Date.now() }
  return true
}

/** Touch the passkey and open the session. Resolves when the session is open; throws a `PasskeyError`. */
export async function signIn(): Promise<void> {
  let options = waiting && Date.now() - waiting.at < FRESH_MS ? waiting.options : null
  waiting = null
  if (!options) {
    const res = await request<PublicKeyOptions>('POST', '/auth/passkey/options', {})
    if (res.status !== 200 || !res.json) throw refused(res.status, 'login')
    options = res.json
  }
  const body = await assertion(options)
  const done = await request<{ token?: string }>('POST', '/auth/passkey/login', body)
  if (done.status !== 200 || typeof done.json?.token !== 'string') throw refused(done.status, 'login')
  openSession(done.json.token)
}

// ---- add this device (the one-time link)

/** A plain name for this browser on this device, from what the browser says about itself. Never a typed word. */
export function deviceName(ua: string = typeof navigator === 'undefined' ? '' : navigator.userAgent): string {
  const browser = /Edg\//.test(ua) ? 'Edge' : /OPR\/|Opera/.test(ua) ? 'Opera' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'Browser'
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android' : /Mac OS X|Macintosh/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : /CrOS/.test(ua) ? 'Chromebook' : /Linux/.test(ua) ? 'Linux' : 'this device'
  return `${browser} on ${os}`
}

/** Make a passkey on this device with the one-time code, and store it. Resolves with the new session's bearer (not yet opened). */
export async function enrollDevice(code: string, name: string = deviceName()): Promise<string> {
  if (!passkeysSupported()) throw new PasskeyError('unsupported')
  const res = await request<PublicKeyOptions>('POST', '/auth/enroll/options', { code })
  if (res.status !== 200 || !res.json) throw refused(res.status, 'enroll')
  const options = res.json
  const pk = options.publicKey
  if (!pk.rp || !pk.user) throw new PasskeyError('failed')
  let credential: Credential | null
  try {
    credential = await navigator.credentials.create({
      publicKey: {
        challenge: fromBase64Url(pk.challenge),
        rp: pk.rp,
        user: { id: fromBase64Url(pk.user.id), name: pk.user.name, displayName: pk.user.displayName },
        pubKeyCredParams: pk.pubKeyCredParams ?? [],
        timeout: pk.timeout,
        attestation: pk.attestation,
        authenticatorSelection: pk.authenticatorSelection,
        excludeCredentials: (pk.excludeCredentials ?? []).map((c) => ({ type: c.type, id: fromBase64Url(c.id) })),
      },
    })
  } catch (e) {
    throw ceremonyError(e)
  }
  const made = credential as PublicKeyCredential | null
  const r = made?.response as AuthenticatorAttestationResponse | undefined
  if (!made || !r) throw new PasskeyError('failed')
  const done = await request<{ token?: string }>('POST', '/auth/enroll/register', {
    challenge_id: options.challenge_id,
    code,
    id: made.id,
    name: name.slice(0, 64),
    response: { clientDataJSON: toBase64Url(r.clientDataJSON), attestationObject: toBase64Url(r.attestationObject) },
  })
  if (done.status !== 200 || typeof done.json?.token !== 'string') throw refused(done.status, 'enroll')
  return done.json.token
}

// ---- step-up: a touch for one big action, after its review is on screen

export type StepUpAction = 'custody.on' | 'custody.off' | 'rotate' | 'github.token' | 'runner.on' | 'sign' | 'hub.connect' | 'device.add' | 'passkey.remove'

/** What the touch is for. The fields are exactly the ones the server expects for that action, no more. */
export type StepUpFor =
  | { action: 'custody.on'; ws: string }
  | { action: 'runner.on'; ws: string; view_digest: string }
  | { action: 'custody.off' | 'rotate' | 'sign' | 'github.token'; ws: string; view_digest: string; request_id: string }
  | { action: 'hub.connect'; ws: string; hub_id: string; invite_sha256: string }
  | { action: 'device.add' }
  | { action: 'passkey.remove'; credential_id: string }

export type StepUpPhase = 'idle' | 'waiting' | 'cancelled' | 'failed' | 'expired'
let phase: StepUpPhase = 'idle'
const listeners = new Set<() => void>()
const setPhase = (next: StepUpPhase) => { phase = next; listeners.forEach((fn) => fn()) }
export const stepUpPhase = (): StepUpPhase => phase
export function onStepUpChange(fn: () => void) { listeners.add(fn); return () => { listeners.delete(fn) } }
/** A new review or a closed panel: the last touch's story is over. */
export const resetStepUp = () => setPhase('idle')

const OPTIONS_BODY: Record<StepUpFor['action'], (f: never) => Record<string, unknown>> = {
  'custody.on': () => ({ action: 'custody.on' }),
  'runner.on': (f: { view_digest: string }) => ({ action: 'runner.on', view_digest: f.view_digest }),
  'custody.off': (f: { view_digest: string; request_id: string }) => ({ action: 'custody.off', view_digest: f.view_digest, request_id: f.request_id }),
  'github.token': (f: { view_digest: string; request_id: string }) => ({ action: 'github.token', view_digest: f.view_digest, request_id: f.request_id }),
  rotate: (f: { view_digest: string; request_id: string }) => ({ action: 'rotate', view_digest: f.view_digest, request_id: f.request_id }),
  sign: (f: { view_digest: string; request_id: string }) => ({ action: 'sign', view_digest: f.view_digest, request_id: f.request_id }),
  'hub.connect': (f: { hub_id: string; invite_sha256: string }) => ({ action: 'hub.connect', hub_id: f.hub_id, invite_sha256: f.invite_sha256 }),
  'device.add': () => ({ action: 'device.add' }),
  'passkey.remove': (f: { credential_id: string }) => ({ action: 'passkey.remove', credential_id: f.credential_id }),
}

/**
 * Ask for the touch the server wants before this action, and return once it is verified (it then holds for ten minutes,
 * once). A session opened on this computer needs none: this resolves at once. Call it only when the review is on
 * screen and the person pressed the confirm button. `device.add` resolves with the `link_id` of the link it is for.
 */
export async function stepUp(what: StepUpFor): Promise<{ link_id?: string }> {
  if (!isRemoteSession()) return {}
  setPhase('waiting')
  try {
    const path = 'ws' in what ? `/api/w/${encodeURIComponent(what.ws)}/stepup/options` : '/api/stepup/options'
    const body = (OPTIONS_BODY[what.action] as (f: StepUpFor) => Record<string, unknown>)(what)
    const res = await request<PublicKeyOptions & { link_id?: string }>('POST', path, JSON.parse(JSON.stringify(body)))
    if (res.status !== 200 || !res.json) throw refused(res.status, 'stepup')
    const linkId = res.json.link_id
    const proof = await assertion(res.json)
    const verified = await request<{ ok?: boolean }>('POST', '/api/stepup/verify', proof)
    if (verified.status !== 200 || verified.json?.ok !== true) throw refused(verified.status, 'stepup')
    setPhase('idle')
    return linkId ? { link_id: linkId } : {}
  } catch (e) {
    const err = ceremonyError(e)
    setPhase(err.kind === 'cancelled' ? 'cancelled' : err.kind === 'stale' ? 'expired' : 'failed')
    throw err
  }
}

/** The action itself was refused for want of a fresh touch (ten minutes passed, or it was spent): say so, once. */
export function noteStepUpRefused(e: unknown): boolean {
  const code = (e as { code?: unknown } | null)?.code
  if (code !== 'passkey.stepup_required') return false
  setPhase('expired')
  return true
}

/** The same digest the server computes for a hub invite. */
export const inviteDigest = (invite: string): string => sha256Hex(invite)

// ---- the person's passkeys, the "Add a device" link, log out everywhere

export interface PasskeyRow {
  id: string
  name: string
  created: string
  last_used: string | null
}

export async function listPasskeys(): Promise<PasskeyRow[]> {
  const res = await request<{ passkeys?: PasskeyRow[] }>('GET', '/api/passkeys')
  if (res.status !== 200 || !Array.isArray(res.json?.passkeys)) throw refused(res.status, 'other')
  return res.json.passkeys
}

export interface EnrollLink {
  /** The link to open on the new device. Treated like a password until it is used or expires. */
  link: string
  expiresInS: number
}

/** "Add a device": a touch for this one link, then the link itself (single use, 15 minutes). */
export async function createEnrollLink(): Promise<EnrollLink> {
  const { link_id } = await stepUp({ action: 'device.add' })
  const res = await request<{ link?: string; expires_in?: number }>('POST', '/api/passkeys/enroll-link', { link_id: link_id ?? 'local-session-no-touch-needed-0000' })
  if (res.status !== 200 || typeof res.json?.link !== 'string') {
    if (res.status === 401 || res.status === 403) noteStepUpRefused({ code: 'passkey.stepup_required' })
    throw refused(res.status, 'other')
  }
  return { link: res.json.link, expiresInS: typeof res.json.expires_in === 'number' ? res.json.expires_in : 900 }
}

/** Remove one passkey (never the last: the server refuses and the page does not offer it). */
export async function removePasskey(id: string): Promise<void> {
  await stepUp({ action: 'passkey.remove', credential_id: id })
  const res = await request('DELETE', `/api/passkeys/${encodeURIComponent(id)}`)
  if (res.status !== 200) {
    if (res.status === 401 || res.status === 403) noteStepUpRefused({ code: 'passkey.stepup_required' })
    throw refused(res.status, 'other')
  }
}

/** Sign out every browser on the web address, this one included. Passkeys stay. */
export async function logoutEverywhere(): Promise<void> {
  const res = await request('POST', '/api/session/logout-all', {})
  if (res.status !== 200) throw refused(res.status, 'other')
}
