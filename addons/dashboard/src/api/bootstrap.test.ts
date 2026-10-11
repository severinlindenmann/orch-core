import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { bootstrap, clearSession, getToken, safeDestination } from './bootstrap'

beforeEach(() => {
  sessionStorage.clear()
  history.replaceState(null, '', '/')
  vi.stubGlobal('BroadcastChannel', undefined)
  clearSession(false)
})
afterEach(() => vi.unstubAllGlobals())

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
    Channel.current.onmessage!(new MessageEvent('message', { data: { type: 'logout' } }))
    expect(getToken()).toBeNull()
    expect(sessionStorage.getItem('orch-session')).toBeNull()
  })
})
