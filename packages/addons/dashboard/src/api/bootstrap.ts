import { enrolBrowser, switchProof } from './trustedKey'

const STORAGE = 'orch-session'
let currentToken: string | null = null
let channel: BroadcastChannel | null = null
/** A hub switch ticket found in the address (`#t=`). Used silently only after this browser's key proved itself
 * (`landSwitch`), otherwise only when the person clicks Open (login CSRF). */
let pendingSwitch: string | null = null
export const getPendingSwitch = () => pendingSwitch
export const declineSwitch = () => { pendingSwitch = null }

// Why the last redeem failed (switch.refused, session.required, origin.bad, rate.limited ...). The failure page says the
// real reason in plain words (D93 run-15 J02: a workspace joined for chosen items only never opens from the hub).
let failureCode: string | null = null
export const switchFailureCode = () => failureCode
// The origin of the hub THIS workspace joined, sent by the dashboard server from the host's own hub state with a
// failed switch. The only origin "Back to the hub" may point at (never the referrer on its own).
let failureHub: string | null = null
export const switchHubOrigin = () => failureHub

type Redeemed = { ok: true; to: string | null } | { ok: false; unverified: boolean; code: string | null }

async function redeem(t: string, proof: { key_id: string; sig: string } | null, fetcher: typeof fetch): Promise<Redeemed> {
  const response = await fetcher('/auth/switch', {
    method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(20_000),
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(proof ? { t, ...proof } : { t }),
  })
  if (!response.ok) {
    const error = await response.json().then((b: { error?: { code?: unknown; hub_origin?: unknown } }) => b?.error ?? null, () => null)
    const code = error?.code
    const hub = error?.hub_origin
    failureCode = typeof code === 'string' ? code : null
    failureHub = typeof hub === 'string' && /^https?:\/\/[A-Za-z0-9.[\]:-]{1,200}$/.test(hub) ? hub : null
    return { ok: false, unverified: response.status === 401 && code === 'switch.unverified', code: failureCode }
  }
  const body = await response.json() as { token?: string; return_to?: string; signed_out?: boolean }
  if (body.signed_out) {
    const old = currentToken ?? sessionStorage.getItem(STORAGE)
    if (old) await fetcher('/auth/logout', { method: 'POST', credentials: 'omit', cache: 'no-store', headers: { Authorization: `Bearer ${old}` } }).catch(() => null)
    clearSession()
    return { ok: true, to: null }
  }
  if (typeof body.token !== 'string') return { ok: false, unverified: false, code: null }
  remember(body.token)
  return { ok: true, to: safeDestination(body.return_to ?? '/') }
}

/** What a hub switch link leads to: a landing (`to`, `null` after a sign-out ticket), the one "Open <name>?" confirm
 * (the ticket is still unused), or an error page (the link is not good: expired, used, not for this person). */
export type SwitchLanding = { kind: 'landed'; to: string | null } | { kind: 'confirm'; name: string | null } | { kind: 'failed'; code: string | null }

/** E4: right after boot. This browser proves its enrolled key for this origin BEFORE the ticket is used; the server
 * then redeems and opens a full session in one step. No key, or a proof the server refuses: the ticket stays unused
 * and the person is asked once. */
export async function landSwitch(fetcher: typeof fetch = fetch): Promise<SwitchLanding> {
  const t = pendingSwitch
  if (!t) return { kind: 'failed', code: null }
  const { name, proof } = await switchProof(t, fetcher)
  if (!proof) return { kind: 'confirm', name }
  const got = await redeem(t, proof, fetcher).catch((): Redeemed => ({ ok: false, unverified: true, code: null }))
  if (got.ok) { pendingSwitch = null; return { kind: 'landed', to: got.to } }
  if (got.unverified) return { kind: 'confirm', name }
  pendingSwitch = null
  return { kind: 'failed', code: got.code }
}

/** The person clicked Open: the server (and the hub behind it) uses the ticket once, with no proof: a read-mostly
 * session. Returns where to go, or `null` after a sign-out ticket, or `undefined` when the ticket is not good. */
export async function confirmSwitch(fetcher: typeof fetch = fetch): Promise<string | null | undefined> {
  const t = pendingSwitch
  pendingSwitch = null
  if (!t) return undefined
  const got = await redeem(t, null, fetcher)
  return got.ok ? got.to : undefined
}
const listeners = new Set<() => void>()
export const isForeignNavigation = () => document.querySelector('meta[name="orch-nav"][content="foreign"]') !== null
export const getToken = () => currentToken
/** The remote client's server marks the page it serves: the workspace is reached through the client. */
export const isRemoteClient = () => document.querySelector('meta[name="orch-nav"][content="remote"]') !== null
/** The sub-protocol that carries the page's bearer on a WebSocket (a browser cannot set headers there). Only in a
 * remote session; the client's server checks it and never forwards it. */
export const remoteSocketProtocols = (): string[] | undefined => {
  const token = isRemoteClient() ? currentToken : null
  return token ? ['orch.b', `orch.b.${token}`] : undefined  // the server selects 'orch.b' (a browser needs an offered one)
}
export function onSessionChange(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}
function remember(token: string | null) {
  currentToken = token
  if (token) sessionStorage.setItem(STORAGE, token)
  else sessionStorage.removeItem(STORAGE)
  listeners.forEach((fn) => fn())
}
export function clearSession(broadcast = true, token = currentToken) {
  if (currentToken === token) remember(null)
  if (broadcast && token) channel?.postMessage({ type: 'logout', token })
}
export function safeDestination(value: string | null) {
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.includes('\\') || /[\x00-\x1f]/.test(value)) return '/'
  const url = new URL(value, location.origin)
  return url.origin === location.origin ? url.pathname + url.search : '/'
}

/** Runs before React imports or any data requests. Fragment removed even on failure. */
export async function bootstrap(fetcher: typeof fetch = fetch): Promise<string | null> {
  const fragment = new URLSearchParams(location.hash.slice(1))
  const code = fragment.get('launch')
  const ticket = fragment.get('t')
  pendingSwitch = !code && ticket && /^[0-9a-f]{32}\.[0-9a-f]{32}$/.test(ticket) ? ticket : null
  const destination = safeDestination(fragment.get('to'))
  const foreign = isForeignNavigation()
  // A reload must not authenticate an attacker-chosen deep link: a foreign document starts at the root and moves to
  // its destination only after its own launch code was redeemed (a made-up code keeps it on the root).
  history.replaceState(null, '', foreign ? '/' : code ? destination : location.pathname + location.search) // the ticket leaves the address at once
  currentToken = sessionStorage.getItem(STORAGE)
  if (foreign) clearSession(false) // sessionStorage may have been copied from an opener.
  channel?.close()
  channel = null
  // The server marker governs this document: never request or receive shared tokens,
  // even after a successful launch exchange. Other tabs cannot verify our marker. A remote session (the client's
  // marker) never shares its token over BroadcastChannel either.
  if (!foreign && !isRemoteClient() && typeof BroadcastChannel !== 'undefined') {
    channel = new BroadcastChannel('orch-session')
    channel.onmessage = ({ data }) => {
      if (data?.type === 'request' && currentToken) channel?.postMessage({ type: 'token', token: currentToken })
      if (data?.type === 'logout' && typeof data.token === 'string') clearSession(false, data.token)
    }
  }
  if (code) {
    clearSession(false)
    const response = await fetcher('/auth/session', {
      method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(20_000), // never a boot that waits for good
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ code }),
    })
    if (!response.ok) return null
    const body = await response.json() as { token: string }
    remember(body.token)
    void enrolBrowser(body.token, fetcher) // a real sign-in on this computer: this browser becomes a trusted one
    if (foreign) history.replaceState(null, '', destination)
  } else if (!currentToken && channel) {
    await new Promise<void>((resolve) => {
      const handler = (event: MessageEvent) => {
        if (event.data?.type === 'token' && typeof event.data.token === 'string') {
          remember(event.data.token)
          finish()
        }
      }
      const finish = () => { clearTimeout(timer); channel?.removeEventListener('message', handler); resolve() }
      let timer = setTimeout(() => {
        timer = setTimeout(finish, 2000)
        channel?.postMessage({ type: 'request' })
      }, 2000)
      channel!.addEventListener('message', handler)
      channel!.postMessage({ type: 'request' })
    })
  }
  return currentToken
}
