import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as boot from './bootstrap'
import { bootstrap, clearSession, confirmSwitch, declineSwitch, getPendingSwitch, getToken, landSwitch, safeDestination, switchFailureCode, switchHubOrigin } from './bootstrap'

beforeEach(() => {
  sessionStorage.clear()
  history.replaceState(null, '', '/')
  vi.stubGlobal('BroadcastChannel', undefined)
  clearSession(false)
})
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

const TICKET = 'a'.repeat(32) + '.' + 'b'.repeat(32)

vi.mock('./trustedKey', () => ({ enrolBrowser: vi.fn(async () => {}), switchProof: vi.fn(async () => ({ name: null, proof: null })) }))
import { switchProof } from './trustedKey'

const PROOF = { key_id: 'c'.repeat(32), sig: 'S'.repeat(86) }
const unverified = () => Response.json({ ok: false, error: { code: 'switch.unverified' } }, { status: 401 })

describe('hub switch link, E4: the key proves itself before the ticket is used', () => {
  it('a proven browser lands with one request that carries the proof, and no confirm', async () => {
    history.replaceState(null, '', `/#t=${TICKET}`)
    await bootstrap(vi.fn())
    vi.mocked(switchProof).mockResolvedValueOnce({ name: 'Product', proof: PROOF })
    const bodies: unknown[] = []
    const fetcher = vi.fn(async (_u, init) => {
      bodies.push(JSON.parse(String(init?.body)))
      return Response.json({ token: 'full-token', expires_at: 'x', return_to: '/w/a', workspace_id: 'w' })
    }) as unknown as typeof fetch
    expect(await landSwitch(fetcher)).toEqual({ kind: 'landed', to: '/w/a' })
    expect(switchProof).toHaveBeenCalledWith(TICKET, fetcher)
    expect(bodies).toEqual([{ t: TICKET, ...PROOF }])
    expect(sessionStorage.getItem('orch-session')).toBe('full-token')
    expect(getPendingSwitch()).toBeNull()
  })
  it('no key: the ticket is not sent at all and the one confirm names the workspace', async () => {
    history.replaceState(null, '', `/#t=${TICKET}`)
    await bootstrap(vi.fn())
    vi.mocked(switchProof).mockResolvedValueOnce({ name: 'Product', proof: null })
    const fetcher = vi.fn()
    expect(await landSwitch(fetcher as unknown as typeof fetch)).toEqual({ kind: 'confirm', name: 'Product' })
    expect(fetcher).not.toHaveBeenCalled()
    expect(getPendingSwitch()).toBe(TICKET) // still unused: Open can use it
  })
  it('a refused proof keeps the ticket for the confirm; Open then uses it without a proof', async () => {
    history.replaceState(null, '', `/#t=${TICKET}`)
    await bootstrap(vi.fn())
    vi.mocked(switchProof).mockResolvedValueOnce({ name: null, proof: PROOF })
    expect(await landSwitch(vi.fn(async () => unverified()) as unknown as typeof fetch)).toEqual({ kind: 'confirm', name: null })
    expect(getToken()).toBeNull()
    expect(getPendingSwitch()).toBe(TICKET)
    const fetcher = vi.fn(async (_u, init) => {
      expect(init?.body).toBe(JSON.stringify({ t: TICKET }))
      return Response.json({ token: 'switch-token', expires_at: 'x', return_to: '/', workspace_id: 'w' })
    }) as unknown as typeof fetch
    expect(await confirmSwitch(fetcher)).toBe('/')
    expect(sessionStorage.getItem('orch-session')).toBe('switch-token')
  })
  it('an invalid or expired link with a good proof is the error page, never the confirm', async () => {
    history.replaceState(null, '', `/#t=${TICKET}`)
    await bootstrap(vi.fn())
    vi.mocked(switchProof).mockResolvedValueOnce({ name: 'Product', proof: PROOF })
    const refused = Response.json({ ok: false, error: { code: 'session.required' } }, { status: 401 })
    expect(await landSwitch(vi.fn(async () => refused) as unknown as typeof fetch)).toEqual({ kind: 'failed', code: 'session.required' })
    expect(getPendingSwitch()).toBeNull()
    expect(getToken()).toBeNull()
  })
  it('the upgrade path is gone', () => {
    expect('adoptToken' in boot).toBe(false)
  })
})

describe('run-15 J02: a switch the workspace refuses by design (D93)', () => {
  it('keeps its error code, so the error page can say to open it on its own computer', async () => {
    history.replaceState(null, '', `/#t=${TICKET}`)
    await bootstrap(vi.fn())
    vi.mocked(switchProof).mockResolvedValueOnce({ name: 'Product', proof: PROOF })
    const fetcher = vi.fn(async () => Response.json({ ok: false, error: { code: 'switch.refused' } }, { status: 403 })) as unknown as typeof fetch
    expect(await landSwitch(fetcher)).toEqual({ kind: 'failed', code: 'switch.refused' })
    expect(switchFailureCode()).toBe('switch.refused')
    expect(switchHubOrigin()).toBeNull() // the server named no hub
  })
})

describe('P01: the joined hub origin from the server', () => {
  const failWith = async (hub_origin: unknown) => {
    history.replaceState(null, '', `/#t=${TICKET}`)
    await bootstrap(vi.fn())
    vi.mocked(switchProof).mockResolvedValueOnce({ name: 'Product', proof: PROOF })
    const body = { ok: false, error: { code: 'session.required', hub_origin } }
    await landSwitch(vi.fn(async () => Response.json(body, { status: 401 })) as unknown as typeof fetch)
  }
  it('keeps a closed http(s) origin, drops anything else', async () => {
    await failWith('https://hub.example.org')
    expect(switchHubOrigin()).toBe('https://hub.example.org')
    await failWith('javascript:alert(1)')
    expect(switchHubOrigin()).toBeNull()
    await failWith('https://hub.example.org/path')
    expect(switchHubOrigin()).toBeNull()
    await failWith(42)
    expect(switchHubOrigin()).toBeNull()
  })
})

describe('hub switch link (H1)', () => {
  it('strips the ticket at once and signs nobody in until the person confirms', async () => {
    history.replaceState(null, '', `/#t=${TICKET}`)
    const fetcher = vi.fn()
    expect(await bootstrap(fetcher)).toBeNull()
    expect(location.hash).toBe('')
    expect(getPendingSwitch()).toBe(TICKET)
    expect(fetcher).not.toHaveBeenCalled()
    declineSwitch()
    expect(getPendingSwitch()).toBeNull()
    expect(await confirmSwitch(fetcher)).toBeUndefined() // nothing to use after Cancel
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('a ticket that is not the expected shape is never pending', async () => {
    history.replaceState(null, '', '/#t=../../etc')
    await bootstrap(vi.fn())
    expect(getPendingSwitch()).toBeNull()
  })
  it('Open uses the ticket once and lands on a path of this site', async () => {
    history.replaceState(null, '', `/#t=${TICKET}`)
    await bootstrap(vi.fn())
    const fetcher = vi.fn(async (_u, init) => {
      expect(init?.body).toBe(JSON.stringify({ t: TICKET }))
      return Response.json({ token: 'demo-not-a-real-token', expires_at: 'x', return_to: '//evil.test', workspace_id: 'w' })
    }) as unknown as typeof fetch
    expect(await confirmSwitch(fetcher)).toBe('/')
    expect(sessionStorage.getItem('orch-session')).toBe('demo-not-a-real-token')
    expect(await confirmSwitch(fetcher)).toBeUndefined()
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
  it('a refused ticket signs nobody in; a sign-out ticket ends this tab\'s session', async () => {
    history.replaceState(null, '', `/#t=${TICKET}`)
    await bootstrap(vi.fn())
    expect(await confirmSwitch(vi.fn(async () => new Response('', { status: 401 })) as unknown as typeof fetch)).toBeUndefined()
    expect(getToken()).toBeNull()
    sessionStorage.setItem('orch-session', 'demo-not-a-real-token')
    history.replaceState(null, '', `/#t=${TICKET}`)
    await bootstrap(vi.fn())
    const calls: string[] = []
    const fetcher = vi.fn(async (u) => { calls.push(String(u)); return Response.json({ signed_out: true }) }) as unknown as typeof fetch
    expect(await confirmSwitch(fetcher)).toBeNull()
    expect(calls).toEqual(['/auth/switch', '/auth/logout'])
    expect(sessionStorage.getItem('orch-session')).toBeNull()
  })
})

describe('production bootstrap', () => {
  it('scrubs the fragment before any request, exchanges once and stores in this tab', async () => {
    history.replaceState(null, '', '/#launch=one-time-code&to=%2Fw%2FDEMO%2Ftickets')
    const fetcher = vi.fn(async (_input, init) => {
      expect(location.hash).toBe('')
      expect(location.pathname).toBe('/w/DEMO/tickets')
      expect(init?.credentials).toBe('omit')
      expect(init?.body).toBe(JSON.stringify({ code: 'one-time-code' }))
      return Response.json({ token: 'demo-not-a-real-token', expires_at: 'later' })
    }) as unknown as typeof fetch
    expect(await bootstrap(fetcher)).toBe('demo-not-a-real-token')
    expect(sessionStorage.getItem('orch-session')).toBe('demo-not-a-real-token')
    await bootstrap(fetcher)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
  it('clears stale token when the launch is refused and strips even bad fragments', async () => {
    sessionStorage.setItem('orch-session', 'demo-not-a-real-old-token')
    history.replaceState(null, '', '/#launch=expired&to=//evil.test')
    expect(await bootstrap(vi.fn(async () => new Response('', { status: 401 })))).toBeNull()
    expect(location.hash).toBe('')
    expect(location.pathname).toBe('/')
    expect(getToken()).toBeNull()
  })
  it('has no credential without a launch or an existing tab', async () => {
    const fetcher = vi.fn()
    expect(await bootstrap(fetcher)).toBeNull()
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('cannot navigate to foreign destinations', () => {
    for (const value of ['//evil.test', '/\\evil.test', 'https://evil.test', '/\n']) expect(safeDestination(value)).toBe('/')
    expect(safeDestination('/w/DEMO/today?ws=abc#secret')).toBe('/w/DEMO/today?ws=abc')
  })
  it('joins another same-origin tab and processes logout', async () => {
    class Channel {
      onmessage: ((event: MessageEvent) => void) | null = null
      handlers = new Set<(event: MessageEvent) => void>()
      static current: Channel
      constructor(name: string) { expect(name).toBe('orch-session'); Channel.current = this }
      close() {}
      addEventListener(_name: string, handler: (event: MessageEvent) => void) { this.handlers.add(handler) }
      removeEventListener(_name: string, handler: (event: MessageEvent) => void) { this.handlers.delete(handler) }
      postMessage(data: { type: string }) {
        if (data.type === 'request') queueMicrotask(() => this.handlers.forEach((fn) => fn(new MessageEvent('message', { data: { type: 'token', token: 'demo-not-a-real-shared-token' } }))))
      }
    }
    vi.stubGlobal('BroadcastChannel', Channel)
    expect(await bootstrap(vi.fn())).toBe('demo-not-a-real-shared-token')
    Channel.current.onmessage!(new MessageEvent('message', { data: { type: 'logout', token: 'demo-not-a-real-shared-token' } }))
    expect(getToken()).toBeNull()
    expect(sessionStorage.getItem('orch-session')).toBeNull()
  })
})

it('ignores logout for an older token and broadcasts the failed token', async () => {
  const postMessage = vi.fn()
  let receive: (event: MessageEvent) => void = () => {}
  vi.stubGlobal('BroadcastChannel', class {
    set onmessage(fn: (event: MessageEvent) => void) { receive = fn }
    postMessage = postMessage
    close() {}
  })
  sessionStorage.setItem('orch-session', 'demo-not-a-real-new')
  await bootstrap()
  receive(new MessageEvent('message', { data: { type: 'logout', token: 'demo-not-a-real-old' } }))
  expect(getToken()).toBe('demo-not-a-real-new')
  clearSession(true, 'demo-not-a-real-old')
  expect(getToken()).toBe('demo-not-a-real-new')
  expect(postMessage).toHaveBeenLastCalledWith({ type: 'logout', token: 'demo-not-a-real-old' })
  receive(new MessageEvent('message', { data: { type: 'logout', token: 'demo-not-a-real-new' } }))
  expect(getToken()).toBeNull()
})

it('waits two seconds, retries once, and accepts a delayed tab reply', async () => {
  vi.useFakeTimers()
  const handlers = new Set<(event: MessageEvent) => void>()
  const postMessage = vi.fn()
  vi.stubGlobal('BroadcastChannel', class {
    postMessage = postMessage
    close() {}
    addEventListener(_name: string, fn: (event: MessageEvent) => void) { handlers.add(fn) }
    removeEventListener(_name: string, fn: (event: MessageEvent) => void) { handlers.delete(fn) }
  })
  const joined = bootstrap()
  await vi.advanceTimersByTimeAsync(1999)
  expect(postMessage).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(postMessage).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(1500)
  handlers.forEach((fn) => fn(new MessageEvent('message', { data: { type: 'token', token: 'demo-not-a-real-delayed' } })))
  expect(await joined).toBe('demo-not-a-real-delayed')
  expect(handlers.size).toBe(0)
  expect(vi.getTimerCount()).toBe(0)
})

it('a late API 401 broadcasts its request token and preserves a newer session', async () => {
  const { api } = await import('./prodApi')
  const postMessage = vi.fn()
  vi.stubGlobal('BroadcastChannel', class { postMessage = postMessage; close() {} })
  sessionStorage.setItem('orch-session', 'demo-not-a-real-old')
  await bootstrap()
  let respond!: (value: Response) => void
  vi.stubGlobal('fetch', vi.fn((_path, init) => {
    expect(init.headers.Authorization).toBe('Bearer demo-not-a-real-old')
    return new Promise<Response>((resolve) => { respond = resolve })
  }))
  const request = api.getWorkspaces()
  const rejected = expect(request).rejects.toThrow('Run orch dashboard')
  sessionStorage.setItem('orch-session', 'demo-not-a-real-new')
  await bootstrap()
  respond(new Response('', { status: 401 }))
  await rejected
  expect(getToken()).toBe('demo-not-a-real-new')
  expect(postMessage).toHaveBeenLastCalledWith({ type: 'logout', token: 'demo-not-a-real-old' })
})

describe('where the bearer lives (host contract 0.8.6 item 7)', () => {
  it('only in tab memory and sessionStorage: never localStorage, a cookie or the address', async () => {
    localStorage.clear()
    history.replaceState(null, '', '/#launch=one-time-code&to=%2F')
    const fetcher = vi.fn(async () => Response.json({ token: 'demo-not-a-real-token', expires_at: 'later' })) as unknown as typeof fetch
    await bootstrap(fetcher)
    expect(getToken()).toBe('demo-not-a-real-token')
    expect(JSON.stringify({ ...localStorage })).not.toContain('demo-not-a-real-token')
    expect(document.cookie).not.toContain('demo-not-a-real-token')
    expect(location.href).not.toContain('demo-not-a-real-token')
    expect(location.hash).toBe('')
  })
})
