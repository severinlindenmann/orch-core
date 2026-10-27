// Trusted browser (run 12). A normal sign-in on this computer makes a key pair with WebCrypto and keeps the private half
// NON-extractable in IndexedDB (origin-scoped: another port is another origin; a cookie would ignore ports). The server
// gets the public half. A hub switch later signs a one-use challenge with it, which turns the read-mostly switch session
// into a full one. Everything here is best effort: no WebCrypto (plain http on a LAN address) just means no upgrade.
const DB = 'orch-trust'
const STORE = 'keys'
const SLOT = 'browser'
const DOMAIN = 'orch-switch-upgrade-v1\0'

interface Held { pair: CryptoKeyPair }

const b64u = (bytes: ArrayBuffer | Uint8Array) => {
  const u = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)
  let s = ''
  u.forEach((b) => { s += String.fromCharCode(b) })
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1)
    req.onupgradeneeded = () => { req.result.createObjectStore(STORE) }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}
async function load(): Promise<Held | null> {
  const db = await open()
  return new Promise((resolve, reject) => {
    const req = db.transaction(STORE).objectStore(STORE).get(SLOT)
    req.onsuccess = () => { db.close(); resolve((req.result as Held | undefined) ?? null) }
    req.onerror = () => { db.close(); reject(req.error) }
  })
}
async function save(held: Held): Promise<void> {
  const db = await open()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite')
    tx.objectStore(STORE).put(held, SLOT)
    tx.oncomplete = () => { db.close(); resolve() }
    tx.onerror = () => { db.close(); reject(tx.error) }
  })
}
const supported = () => typeof indexedDB !== 'undefined' && typeof crypto !== 'undefined' && !!crypto.subtle

const json = (token: string, path: string, method: string, body: unknown, fetcher: typeof fetch) =>
  fetcher(path, {
    method, credentials: 'omit', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(20_000),
    headers: { 'content-type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify(body),
  })

/** After a REAL local sign-in: make (or reuse) this browser's key and tell the server. Never throws. */
export async function enrolBrowser(token: string, fetcher: typeof fetch = fetch): Promise<void> {
  try {
    if (!supported()) return
    let held = await load()
    if (!held) {
      const pair = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify'])
      held = { pair }
      await save(held)
    }
    const jwk = await crypto.subtle.exportKey('jwk', held.pair.publicKey)
    await json(token, '/api/trusted/enrol', 'POST', { jwk: { kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y } }, fetcher)
  } catch { /* no key: a hub switch stays read-mostly */ }
}

/** A switch session proves it holds this browser's enrolled key. The new full session's token, or null. Never throws. */
export async function upgradeSession(token: string, fetcher: typeof fetch = fetch): Promise<string | null> {
  try {
    if (!supported()) return null
    const held = await load()
    if (!held) return null
    const jwk = await crypto.subtle.exportKey('jwk', held.pair.publicKey)
    const canon = JSON.stringify({ crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y })
    const keyId = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canon)))).map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 32)
    const ch = await json(token, '/api/auth/challenge', 'POST', {}, fetcher)
    if (!ch.ok) return null
    const { nonce } = await ch.json() as { nonce?: string }
    if (typeof nonce !== 'string') return null
    const msg = new TextEncoder().encode(`${DOMAIN}${nonce}\0${location.origin}`)
    const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, held.pair.privateKey, msg)
    const up = await json(token, '/api/auth/upgrade', 'POST', { key_id: keyId, signature: b64u(sig) }, fetcher)
    if (!up.ok) return null
    const body = await up.json() as { token?: string }
    return typeof body.token === 'string' ? body.token : null
  } catch { return null }
}
