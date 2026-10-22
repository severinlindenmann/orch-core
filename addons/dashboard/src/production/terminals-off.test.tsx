import { screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'

// The add-on is off: the server does not list `terminals_page`. No menu entry, and the address goes back to the board.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const state = useFakeHost(() => [workspace({ id: 'a'.repeat(32), features: [...READS] })])

it('has no Terminals entry and asks the server nothing for the page', async () => {
  show('/w/DEMO/terminals')
  await waitFor(() => expect(screen.queryByRole('heading', { name: 'Terminals' })).not.toBeInTheDocument(), { timeout: 5000 })
  expect(screen.queryByRole('link', { name: 'Terminals' })).toBeNull()
  expect(state.host.calls.filter((c) => c.path.includes('/runs'))).toEqual([])
})
