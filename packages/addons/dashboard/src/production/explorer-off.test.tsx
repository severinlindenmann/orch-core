import { screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'

// The add-on is off, or the person is not an owner: the server then does not list the feature. The page asks nothing.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const state = useFakeHost(() => [workspace({ id: 'a'.repeat(32), features: [...READS] })])

it('has no Files entry and asks the server nothing; the address goes back to the board', async () => {
  show('/w/DEMO/explorer')
  await waitFor(() => expect(screen.queryByTestId('explorer')).not.toBeInTheDocument(), { timeout: 5000 })
  expect(screen.queryByRole('link', { name: 'Files' })).toBeNull()
  expect(state.host.calls.filter((c) => c.path.includes('/explorer'))).toEqual([])
})
