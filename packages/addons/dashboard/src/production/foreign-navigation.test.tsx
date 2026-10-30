import { render, screen, cleanup } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { bootstrap, clearSession, getToken } from '../api/bootstrap'
import { Shell } from './Shell'

afterEach(() => {
  cleanup()
  document.querySelectorAll('meta[name="orch-nav"]').forEach((node) => node.remove())
  clearSession(false)
  sessionStorage.clear()
  history.replaceState(null, '', '/')
  vi.unstubAllGlobals()
})

function foreign() {
  const meta = document.createElement('meta')
  meta.name = 'orch-nav'
  meta.content = 'foreign'
  document.head.append(meta)
}

it('never joins the token channel, ignores offers, clears inherited storage and explains how to open', async () => {
  const channels: EventTarget[] = []
  const postMessage = vi.fn()
  vi.stubGlobal('BroadcastChannel', class extends EventTarget {
    constructor() { super(); channels.push(this) }
    postMessage = postMessage
    close() {}
  })
  foreign()
  history.replaceState(null, '', '/w/DEMO/ticket/DEMO-0001?tab=history#attacker-fragment')
  sessionStorage.setItem('orch-session', 'demo-not-a-real-inherited')
  const fetcher = vi.fn()
  const boot = bootstrap(fetcher)
  expect(location.pathname + location.search + location.hash).toBe('/')
  expect(await boot).toBeNull()
  channels.forEach((channel) => channel.dispatchEvent(new MessageEvent('message', {
    data: { type: 'token', token: 'demo-not-a-real-offer' },
  })))
  expect(channels).toHaveLength(0)
  expect(postMessage).not.toHaveBeenCalled()
  expect(fetcher).not.toHaveBeenCalled()
  expect(getToken()).toBeNull()
  expect(sessionStorage.getItem('orch-session')).toBeNull()
  render(<Shell />)
  expect(screen.getByText('This page was opened from another site.')).toBeInTheDocument()
  expect(screen.getByText('orch-dashboard open')).toBeInTheDocument()
  expect(screen.getByText(/or use a bookmark/)).toBeInTheDocument()
  // P01: plain words first; every command sits inside the Details fold
  expect(screen.getByText(/Sign in again from the hub or from the link on the computer where orch runs/)).toBeInTheDocument()
  document.querySelectorAll('code').forEach((c) => expect(c.closest('details')).not.toBeNull())
})

it('P01: the no-session page says how to sign in again in plain words, the command only under Details', () => {
  render(<Shell />)
  expect(screen.getByText('Open your dashboard')).toBeInTheDocument()
  expect(screen.getByText(/Sign in again from the hub/)).toBeInTheDocument()
  expect(document.querySelectorAll('main > p code').length).toBe(0)
  const codes = document.querySelectorAll('code')
  expect(codes.length).toBeGreaterThan(0)
  codes.forEach((c) => expect(c.closest('details')).not.toBeNull())
})

it('exchanges the private launcher fragment without opening the token channel', async () => {
  foreign()
  const channel = vi.fn()
  vi.stubGlobal('BroadcastChannel', channel)
  history.replaceState(null, '', '/#launch=demo-not-a-real-code&to=%2Fw%2FDEMO')
  const fetcher = vi.fn(async (_input, init) => {
    expect(location.pathname + location.search + location.hash).toBe('/') // the destination only after a redeemed code
    expect(init?.body).toBe(JSON.stringify({ code: 'demo-not-a-real-code' }))
    return Response.json({ token: 'demo-not-a-real-launched' })
  }) as unknown as typeof fetch
  expect(await bootstrap(fetcher)).toBe('demo-not-a-real-launched')
  expect(location.pathname).toBe('/w/DEMO')
  expect(channel).not.toHaveBeenCalled()
})

it.each([
  ['refused', async () => new Response(null, { status: 401 })],
  ['unreachable', async () => { throw new TypeError('network') }],
])('a foreign link with a made-up launch code stays on the root when the exchange is %s', async (_name, answer) => {
  foreign()
  vi.stubGlobal('BroadcastChannel', vi.fn())
  history.replaceState(null, '', '/#launch=demo-not-a-real-bogus&to=%2Fw%2FDEMO%2Fticket%2FDEMO-0001%3Ftab%3Dhistory')
  await bootstrap(vi.fn(answer) as unknown as typeof fetch).catch(() => null)
  expect(location.pathname + location.search + location.hash).toBe('/')
  expect(getToken()).toBeNull()
})

it('keeps the normal login screen for an untagged document', async () => {
  vi.stubGlobal('BroadcastChannel', undefined)
  history.replaceState(null, '', '/w/DEMO/tickets?tab=history#fragment')
  await bootstrap(vi.fn())
  expect(location.pathname + location.search + location.hash).toBe('/w/DEMO/tickets?tab=history')
  render(<Shell />)
  expect(screen.getByText('Open your dashboard')).toBeInTheDocument()
})
