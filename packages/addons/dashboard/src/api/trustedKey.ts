// Trusted browser (run 12, E4). A normal sign-in on this computer makes a key pair with WebCrypto and keeps the private
// half NON-extractable in IndexedDB (origin-scoped: another port is another origin; a cookie would ignore ports). The
// server gets the public half. A hub switch link later signs a one-use challenge bound to the ticket's hash BEFORE the
// ticket is used: a good proof lands with a full session and no question. Everything here is best effort: no WebCrypto
// (plain http on a LAN address) or no key just means the "Open <name>?" confirm and a read-mostly session.
const DB = 'orch-trust'
const STORE = 'keys'
const SLOT = 'browser'
const DOMAIN = 'orch-switch-redeem-v1\0'

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

const hex = (bytes: ArrayBuffer) => Array.from(new Uint8Array(bytes)).map((b) => b.toString(16).padStart(2, '0')).join('')
const sha256 = async (text: string) => hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)))

export interface SwitchProof {
  /** The workspace's name for "Open <name>?", or null when the server did not say. */
  name: string | null
  /** This browser's proof for this ticket, or null (no WebCrypto, no key, no challenge). */
  proof: { key_id: string; sig: string } | null
}

/**
 * Before a hub switch ticket is used: ask the server for a one-use nonce bound to the ticket's hash and this origin, and
 * sign `orch-switch-redeem-v1\0nonce\0origin\0ws_id\0sha256(t)\0key_id` with this browser's enrolled key. The ticket
 * itself is not sent and not used here. Never throws.
 */
export async function switchProof(ticket: string, fetcher: typeof fetch = fetch): Promise<SwitchProof> {
  const none: SwitchProof = { name: null, proof: null }
  try {
    if (!supported()) return none
    const ws = ticket.split('.')[0]
    const tHash = await sha256(ticket)
    const ch = await fetcher('/auth/switch/challenge', {
      method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(20_000),
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ticket_hash: tHash, workspace_id: ws }),
    })
    if (!ch.ok) return none
    const got = await ch.json() as { nonce?: unknown; name?: unknown }
    const name = typeof got.name === 'string' && got.name ? got.name : null
    if (typeof got.nonce !== 'string') return { name, proof: null }
    const held = await load()
    if (!held) return { name, proof: null }
    const jwk = await crypto.subtle.exportKey('jwk', held.pair.publicKey)
    const keyId = (await sha256(JSON.stringify({ crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y }))).slice(0, 32)
    const msg = new TextEncoder().encode(`${DOMAIN}${[got.nonce, location.origin, ws, tHash, keyId].join('\0')}`)
    const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, held.pair.privateKey, msg)
    return { name, proof: { key_id: keyId, sig: b64u(sig) } }
  } catch { return none }
}
