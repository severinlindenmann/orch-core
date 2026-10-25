import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearSession, getToken } from './bootstrap'
import { setFeatures } from './features'
import { createEnrollLink, deviceName, enrollDevice, fromBase64Url, listPasskeys, logoutEverywhere, PasskeyError, prepareLogin, removePasskey, signIn, stepUp, stepUpPhase, toBase64Url } from './passkey'

const bytes = (...n: number[]) => new Uint8Array(n).buffer
const options = (extra: object = {}) => ({ challenge_id: 'c'.repeat(22), publicKey: { challenge: toBase64Url(new Uint8Array(32).fill(7)), rpId: 'orch.example.com', timeout: 120000, userVerification: 'required', allowCredentials: [], ...extra } })
const assertion = () => ({ id: 'cred_id_' + 'a'.repeat(20), response: { clientDataJSON: bytes(1, 2), authenticatorData: bytes(3, 4), signature: bytes(5, 6), userHandle: bytes(9) } })

let calls: { method: string; path: string; body: unknown }[]
let answers: Record<string, { status: number; json?: unknown }>
beforeEach(() => {
  calls = []
  answers = {}
  sessionStorage.clear()
  clearSession(false)
  setFeatures({ features: [], login: 'remote' })
  vi.stubGlobal('PublicKeyCredential', class {})
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${input}`
    calls.push({ method: init?.method ?? 'GET', path: input, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    const a = answers[key] ?? { status: 404 }
    return new Response(a.json === undefined ? '' : JSON.stringify(a.json), { status: a.status })
  }))
  Object.defineProperty(navigator, 'credentials', { configurable: true, value: { get: vi.fn(async () => assertion()), create: vi.fn() } })
})
afterEach(() => { vi.unstubAllGlobals(); setFeatures(null) })

describe('base64url', () => {
  it('round-trips without padding and refuses other characters', () => {
    const raw = new Uint8Array([251, 255, 0, 62, 63])
    const text = toBase64Url(raw)
    expect(text).toBe('-_8APj8')
    expect([...fromBase64Url(text)]).toEqual([...raw])
    expect(() => fromBase64Url('a+b/')).toThrow(PasskeyError)
  })
})

describe('sign in', () => {
  it('is not the web address when the challenge route does not exist', async () => {
    answers['POST /auth/passkey/options'] = { status: 404 }
    expect(await prepareLogin()).toBe(false)
  })
  it('touches, posts the assertion in base64url and keeps only the bearer', async () => {
    answers['POST /auth/passkey/options'] = { status: 200, json: options() }
    answers['POST /auth/passkey/login'] = { status: 200, json: { token: 'bearer-token-1', expires_at: 'x' } }
    expect(await prepareLogin()).toBe(true)
    await signIn()
    expect(calls.filter((c) => c.path === '/auth/passkey/options')).toHaveLength(1) // the prepared challenge was used
    const login = calls.find((c) => c.path === '/auth/passkey/login')!.body as { challenge_id: string; id: string; response: Record<string, string> }
    expect(Object.keys(login.response).sort()).toEqual(['authenticatorData', 'clientDataJSON', 'signature', 'userHandle'])
    expect(login.response.signature).toBe('BQY')
    expect(getToken()).toBe('bearer-token-1')
    // nothing of the ceremony is kept in storage
    expect(JSON.stringify({ ...sessionStorage })).not.toMatch(/BQY|cred_id/)
    expect(JSON.stringify({ ...localStorage })).not.toMatch(/BQY|cred_id/)
  })
  it('asks the device with the server challenge and no allow list', async () => {
    answers['POST /auth/passkey/options'] = { status: 200, json: options() }
    answers['POST /auth/passkey/login'] = { status: 200, json: { token: 't' } }
    await signIn()
    const get = (navigator.credentials.get as ReturnType<typeof vi.fn>).mock.calls[0][0].publicKey
    expect(get.userVerification).toBe('required')
    expect(get.allowCredentials).toEqual([])
    expect(new Uint8Array(get.challenge)).toEqual(new Uint8Array(32).fill(7))
  })
  it.each([
    ['a closed prompt', new DOMException('x', 'NotAllowedError'), 'cancelled'],
    ['an unsupported device', new DOMException('x', 'NotSupportedError'), 'unsupported'],
    ['anything else', new Error('boom'), 'failed'],
  ])('names %s', async (_n, thrown, kind) => {
    answers['POST /auth/passkey/options'] = { status: 200, json: options() }
    ;(navigator.credentials.get as ReturnType<typeof vi.fn>).mockRejectedValueOnce(thrown)
    await expect(signIn()).rejects.toMatchObject({ kind })
    expect(getToken()).toBeNull()
  })
  it('names a lockout and a refusal', async () => {
    answers['POST /auth/passkey/options'] = { status: 429 }
    await expect(signIn()).rejects.toMatchObject({ kind: 'locked' })
    answers['POST /auth/passkey/options'] = { status: 200, json: options() }
    answers['POST /auth/passkey/login'] = { status: 401, json: { ok: false } }
    await expect(signIn()).rejects.toMatchObject({ kind: 'failed' })
    expect(getToken()).toBeNull()
  })
})

describe('add this device', () => {
  const create = { id: 'new_cred_' + 'b'.repeat(20), response: { clientDataJSON: bytes(1), attestationObject: bytes(2) } }
  const enrollOptions = { challenge_id: 'd'.repeat(22), publicKey: { challenge: toBase64Url(new Uint8Array(32)), rp: { id: 'orch.example.com', name: 'orch' }, user: { id: toBase64Url(new Uint8Array(32)), name: 'orch', displayName: 'orch' }, pubKeyCredParams: [{ type: 'public-key', alg: -7 }], timeout: 120000, attestation: 'none', authenticatorSelection: { residentKey: 'required', userVerification: 'required' }, excludeCredentials: [] } }
  it('registers with the code, a plain device name and returns the bearer without opening it', async () => {
    answers['POST /auth/enroll/options'] = { status: 200, json: enrollOptions }
    answers['POST /auth/enroll/register'] = { status: 200, json: { token: 'new-bearer' } }
    ;(navigator.credentials.create as ReturnType<typeof vi.fn>).mockResolvedValueOnce(create)
    expect(await enrollDevice('a'.repeat(30), 'Safari on Mac')).toBe('new-bearer')
    expect(getToken()).toBeNull()
    expect(calls.find((c) => c.path === '/auth/enroll/register')!.body).toMatchObject({ code: 'a'.repeat(30), name: 'Safari on Mac', id: create.id })
  })
  it('says the link is spent when the server does', async () => {
    answers['POST /auth/enroll/options'] = { status: 401, json: { ok: false } }
    await expect(enrollDevice('a'.repeat(30))).rejects.toMatchObject({ kind: 'expired' })
  })
  it('says unsupported without WebAuthn', async () => {
    vi.stubGlobal('PublicKeyCredential', undefined)
    await expect(enrollDevice('a'.repeat(30))).rejects.toMatchObject({ kind: 'unsupported' })
  })
  it('names the device from the browser, plainly', () => {
    expect(deviceName('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17 Safari/605.1.15')).toBe('Safari on Mac')
    expect(deviceName('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit Safari/604.1')).toBe('Safari on iPhone')
    expect(deviceName('Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537 Edg/120')).toBe('Edge on Windows')
  })
})

describe('step-up', () => {
  it('needs no touch on a session opened on this computer', async () => {
    setFeatures({ features: [], login: 'local' })
    expect(await stepUp({ action: 'device.add' })).toEqual({})
    expect(calls).toHaveLength(0)
  })
  it('asks for the digest of the review, touches, verifies and keeps nothing', async () => {
    answers['POST /api/w/WS/stepup/options'] = { status: 200, json: options({ allowCredentials: [{ type: 'public-key', id: 'abc_def' }] }) }
    answers['POST /api/stepup/verify'] = { status: 200, json: { ok: true, valid_for: 600 } }
    const digest = 'sha256:' + 'a'.repeat(64)
    await stepUp({ action: 'sign', ws: 'WS', view_digest: digest, request_id: 'hr_X' })
    expect(calls[0].body).toEqual({ action: 'sign', view_digest: digest, request_id: 'hr_X' })
    expect(calls[1].path).toBe('/api/stepup/verify')
    expect(stepUpPhase()).toBe('idle')
  })
  it('a cancelled touch is calm: nothing is sent to verify', async () => {
    answers['POST /api/stepup/options'] = { status: 200, json: { ...options(), link_id: 'l'.repeat(22) } }
    ;(navigator.credentials.get as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new DOMException('x', 'NotAllowedError'))
    await expect(stepUp({ action: 'device.add' })).rejects.toMatchObject({ kind: 'cancelled' })
    expect(stepUpPhase()).toBe('cancelled')
    expect(calls.some((c) => c.path === '/api/stepup/verify')).toBe(false)
  })
  it('"Add a device" ties the link to the touch', async () => {
    answers['POST /api/stepup/options'] = { status: 200, json: { ...options(), link_id: 'l'.repeat(22) } }
    answers['POST /api/stepup/verify'] = { status: 200, json: { ok: true } }
    answers['POST /api/passkeys/enroll-link'] = { status: 200, json: { code: 'x', link: 'https://orch.example.com/#enroll=abc', expires_in: 900 } }
    const got = await createEnrollLink()
    expect(got).toEqual({ link: 'https://orch.example.com/#enroll=abc', expiresInS: 900 })
    expect(calls.find((c) => c.path === '/api/passkeys/enroll-link')!.body).toEqual({ link_id: 'l'.repeat(22) })
  })
  it('removing names the passkey, touches, then deletes', async () => {
    answers['POST /api/stepup/options'] = { status: 200, json: options() }
    answers['POST /api/stepup/verify'] = { status: 200, json: { ok: true } }
    answers['DELETE /api/passkeys/id_one'] = { status: 200, json: { removed: true } }
    await removePasskey('id_one')
    expect(calls[0].body).toEqual({ action: 'passkey.remove', credential_id: 'id_one' })
    expect(calls.at(-1)).toMatchObject({ method: 'DELETE', path: '/api/passkeys/id_one' })
  })
})

describe('passkeys and log out', () => {
  it('lists and logs out everywhere', async () => {
    answers['GET /api/passkeys'] = { status: 200, json: { passkeys: [{ id: 'a', name: 'Mac', created: 'x', last_used: null }] } }
    answers['POST /api/session/logout-all'] = { status: 200, json: { ok: true } }
    expect(await listPasskeys()).toHaveLength(1)
    await logoutEverywhere()
    expect(calls.at(-1)!.path).toBe('/api/session/logout-all')
  })
})
