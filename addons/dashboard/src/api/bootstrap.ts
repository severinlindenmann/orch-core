const STORAGE = 'orch-session'
let currentToken: string | null = null
let channel: BroadcastChannel | null = null
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
/** A passkey login or enrollment opened a session on the server: keep its bearer exactly like a launch exchange does. */
export function openSession(token: string) { remember(token) }

/** The one-time enrollment code of the opened link (`/#enroll=<code>`). Memory and the address bar only, never storage. */
let enrollCode: string | null = null
export const getEnrollCode = () => enrollCode
export function finishEnroll() {
  enrollCode = null
  history.replaceState(null, '', location.pathname + location.search)
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
  const destination = safeDestination(fragment.get('to'))
  const foreign = isForeignNavigation()
  // An enrollment link keeps its fragment while the page is open, so a reload (after turning on a screen lock) works.
  const enroll = !foreign && !code ? fragment.get('enroll') : null
  enrollCode = enroll && /^[A-Za-z0-9_-]{16,128}$/.test(enroll) ? enroll : null
  // A reload must not authenticate an attacker-chosen deep link: a foreign document starts at the root and moves to
  // its destination only after its own launch code was redeemed (a made-up code keeps it on the root).
  if (!enrollCode) history.replaceState(null, '', foreign ? '/' : code ? destination : location.pathname + location.search)
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
