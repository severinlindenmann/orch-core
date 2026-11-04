// Before `orch host enable` / `orch setup` the host registry is missing and /api/session is refused (503). The tester's
// checklist still opens then; every other address waits calmly for the host (it may still be starting).
import { render, screen, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { bootstrap, clearSession } from '../api/bootstrap'
import { QueryClientProvider } from '@tanstack/react-query'
import { Shell, queryClient } from './Shell'

vi.mock('@/api/testrun', async (orig) => {
  const real = await orig<typeof import('@/api/testrun')>()
  return { ...real, loadRun: vi.fn().mockResolvedValue({ path: '/state/testrun/run2', images_path: '/state/testrun/run2/images', notes: {} }), saveStep: vi.fn() }
})

beforeEach(async () => {
  sessionStorage.setItem('orch-session', 'demo-not-a-real-token')
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ok: false, error: { code: 'host.unavailable', message: 'Host registry unavailable', retryable: true } }), { status: 503 })))
  await bootstrap(vi.fn())
})
afterEach(() => {
  cleanup()
  clearSession(false)
  sessionStorage.clear()
  history.replaceState(null, '', '/')
  vi.unstubAllGlobals()
})

it('opens the checklist although the host does not answer', async () => {
  history.replaceState(null, '', '/checklist')
  render(<QueryClientProvider client={queryClient}><Shell /></QueryClientProvider>)
  expect(await screen.findByRole('heading', { name: /Test checklist/ }, { timeout: 8000 })).toBeInTheDocument()
  expect(await screen.findByText('/state/testrun/run2')).toBeInTheDocument()
})

it('any other address shows the calm waiting screen, never the raw code', async () => {
  history.replaceState(null, '', '/board')
  render(<QueryClientProvider client={queryClient}><Shell /></QueryClientProvider>)
  expect(await screen.findByText('Getting your workspace ready…', {}, { timeout: 4000 })).toBeInTheDocument()
  expect(screen.queryByText(/host\.unavailable/)).not.toBeInTheDocument()
})
