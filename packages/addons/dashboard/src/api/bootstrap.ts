const STORAGE = 'orch-session'
let currentToken: string | null = null
let channel: BroadcastChannel | null = null
/** A hub switch ticket found in the address (`#t=`): never used until the person clicks Continue (login CSRF). */
let pendingSwitch: string | null = null
export const getPendingSwitch = () => pendingSwitch
export const declineSwitch = () => { pendingSwitch = null }
/** The person clicked Continue: the server (and the hub behind it) uses the ticket once. Returns where to go, or
 * `null` after a sign-out ticket, or `undefined` when the ticket is not good (expired, used, not for this person). */
export async function confirmSwitch(fetcher: typeof fetch = fetch): Promise<string | null | undefined> {
  const t = pendingSwitch
  pendingSwitch = null
  if (!t) return undefined
  const response = await fetcher('/auth/switch', {
    method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(20_000),
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ t }),
  })
  if (!response.ok) return undefined
  const body = await response.json() as { token?: string; return_to?: string; signed_out?: boolean }
  if (body.signed_out) {
    const old = currentToken ?? sessionStorage.getItem(STORAGE)
    if (old) await fetcher('/auth/logout', { method: 'POST', credentials: 'omit', cache: 'no-store', headers: { Authorization: `Bearer ${old}` } }).catch(() => null)
    clearSession()
    return null
  }
  if (typeof body.token !== 'string') return undefined
  remember(body.token)
  return safeDestination(body.return_to ?? '/')
}
const listeners = new Set<() => void>()
export const isForeignNavigation = () => document.querySelector('meta[name="orch-nav"][content="foreign"]') !== null
export const getToken = () => currentToken
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
  // even after a successful launch exchange. Other tabs cannot verify our marker.
  if (!foreign && typeof BroadcastChannel !== 'undefined') {
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
