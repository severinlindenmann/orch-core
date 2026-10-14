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
  sessionStorage.setItem('orch-session', 'demo-not-a-real-inherited')
  const fetcher = vi.fn()
  expect(await bootstrap(fetcher)).toBeNull()
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
  expect(screen.getByText(/or a bookmark/)).toBeInTheDocument()
})

it('exchanges the private launcher fragment without opening the token channel', async () => {
  foreign()
  const channel = vi.fn()
  vi.stubGlobal('BroadcastChannel', channel)
  history.replaceState(null, '', '/#launch=demo-not-a-real-code&to=%2Fw%2FDEMO')
  const fetcher = vi.fn(async (_input, init) => {
    expect(location.hash).toBe('')
    expect(init?.body).toBe(JSON.stringify({ code: 'demo-not-a-real-code' }))
    return Response.json({ token: 'demo-not-a-real-launched' })
  }) as unknown as typeof fetch
  expect(await bootstrap(fetcher)).toBe('demo-not-a-real-launched')
  expect(location.pathname).toBe('/w/DEMO')
  expect(channel).not.toHaveBeenCalled()
})

it('keeps the normal login screen for an untagged document', async () => {
  vi.stubGlobal('BroadcastChannel', undefined)
  await bootstrap(vi.fn())
  render(<Shell />)
  expect(screen.getByText('Open your dashboard')).toBeInTheDocument()
})
