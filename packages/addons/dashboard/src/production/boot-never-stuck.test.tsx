// The boot invariant (owner run 3: "sometimes when I reload I get a blank page"): the first screen never waits for good.
// Within START_PATIENCE_MS it is the app, the "session ended" screen, or a message with a "Try again" button.
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QueryClientProvider } from '@tanstack/react-query'
import { setFeatures } from '@/api/features'
import { showBootFailure } from '@/api/bootFailure'
import { queryClient, Shell, START_PATIENCE_MS } from './Shell'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
let token: string | null = 'demo-not-a-real-token'
let listener: () => void = () => {}
vi.mock('@/api/bootstrap', () => ({
  getToken: () => token,
  clearSession: vi.fn(),
  isForeignNavigation: () => false,
  onSessionChange: (fn: () => void) => { listener = fn; return () => {} },
}))
vi.mock('@/api/useLiveUpdates', () => ({ useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

beforeEach(() => {
  token = 'demo-not-a-real-token'
  history.replaceState(null, '', '/')
  vi.useFakeTimers({ shouldAdvanceTime: true })
})
afterEach(() => {
  vi.useRealTimers()
  setFeatures(null)
  queryClient.clear()
  vi.unstubAllGlobals()
})

const mount = () => render(<QueryClientProvider client={queryClient}><Shell /></QueryClientProvider>)

it('a server that never answers: after the patience time the skeleton gives way to a message with Try again', async () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
  mount()
  expect(screen.getByRole('status', { name: 'Loading session' })).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(START_PATIENCE_MS + 100) })
  expect(screen.queryByRole('status', { name: 'Loading session' })).not.toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('slow to answer')
  expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
})

it('Try again asks the server again, and a late answer opens the app', async () => {
  let calls = 0
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (String(url) === '/api/session' && ++calls === 1) return new Promise(() => {})
    if (String(url) === '/api/session') return Response.json({ features: [], workspaces: {} })
    return Response.json([])
  }))
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
  mount()
  await act(async () => { await vi.advanceTimersByTimeAsync(START_PATIENCE_MS + 100) })
  await user.click(screen.getByRole('button', { name: 'Try again' }))
  await waitFor(() => expect(calls).toBe(2))
  expect(await screen.findByRole('navigation', { name: 'Main' })).toBeInTheDocument()
})

it('a server error shows the message with Try again', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ok: false, error: { code: 'host.unavailable', message: 'Host not available', retryable: true } }, { status: 503 })))
  mount()
  expect(await screen.findByText('The dashboard server did not answer')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
})

it('an ended session says so in plain words and names the command', async () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
  mount()
  token = null
  act(() => listener())
  expect(await screen.findByText('Your dashboard session ended')).toBeInTheDocument()
  expect(screen.getByText('orch dashboard open')).toBeInTheDocument()
})

it('the boot screen is replaced by a message with a button when the app code cannot load', () => {
  document.body.innerHTML = '<div id="root"><div class="boot" role="status"><b>orch</b><span>Loading…</span></div></div>'
  showBootFailure()
  const root = document.getElementById('root')!
  expect(root.textContent).not.toContain('Loading…')
  expect(root.querySelector('[role="alert"]')?.textContent).toContain('could not start')
  expect(root.querySelector('button')?.textContent).toBe('Try again')
})
