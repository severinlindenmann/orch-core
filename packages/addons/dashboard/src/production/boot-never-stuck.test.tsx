// The boot invariant (owner run 3: "sometimes when I reload I get a blank page"): the first screen never waits for good.
// Within START_PATIENCE_MS it is the app, the "session ended" screen, or a message with a "Try again" button.
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QueryClientProvider } from '@tanstack/react-query'
import { setFeatures } from '@/api/features'
import { showBootFailure } from '@/api/bootFailure'
import { queryClient, Shell, START_PATIENCE_MS } from './Shell'
import { SESSION_RETRY_WINDOW_MS } from '@/api/prodApi'

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

const refuse = () => Response.json({ ok: false, error: { code: 'host.unavailable', message: 'Host not available', retryable: true } }, { status: 503 })

it('a server that never answers: after the patience time the skeleton gives way to the calm waiting screen', async () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
  mount()
  expect(screen.getByRole('status')).toHaveTextContent('Loading…') // the boot screen, never an empty page
  await act(async () => { await vi.advanceTimersByTimeAsync(START_PATIENCE_MS + 100) })
  expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Getting your workspace ready…')
  expect(screen.queryByText(/host\.unavailable/)).not.toBeInTheDocument()
})

it('a host that is still starting (503 twice) opens the app by itself, no code shown, no click', async () => {
  let calls = 0
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (String(url) === '/api/session') return ++calls <= 2 ? refuse() : Response.json({ features: [], workspaces: {} })
    return Response.json([])
  }))
  mount()
  await act(async () => { await vi.advanceTimersByTimeAsync(1_100) })
  expect(screen.getByRole('status')).toHaveTextContent('Getting your workspace ready…')
  expect(screen.queryByText(/host\.unavailable/)).not.toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
  expect(await screen.findByRole('navigation', { name: 'Main' })).toBeInTheDocument()
  expect(calls).toBe(3)
})

it('a host that never starts: after the retry window the plain failure screen with Try again; the code only under Details', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => refuse()))
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
  mount()
  await act(async () => { await vi.advanceTimersByTimeAsync(SESSION_RETRY_WINDOW_MS + 10_000) })
  expect(screen.getByRole('alert')).toHaveTextContent('The workspace did not start')
  expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  expect(screen.queryByText(/host\.unavailable/)).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Details' }))
  expect(screen.getByText(/host\.unavailable/)).toBeInTheDocument()
})

it('Try again asks the server again, and a late answer opens the app', async () => {
  let up = false
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (String(url) === '/api/session') return up ? Response.json({ features: [], workspaces: {} }) : refuse()
    return Response.json([])
  }))
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
  mount()
  await act(async () => { await vi.advanceTimersByTimeAsync(SESSION_RETRY_WINDOW_MS + 10_000) })
  up = true
  await user.click(screen.getByRole('button', { name: 'Try again' }))
  expect(await screen.findByRole('navigation', { name: 'Main' })).toBeInTheDocument()
})

it('an error that is not "starting" (not found) shows the failure screen at once', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ok: false, error: { code: 'not_found', message: 'nope', retryable: false } }, { status: 404 })))
  mount()
  expect(await screen.findByText('The workspace did not start')).toBeInTheDocument()
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
