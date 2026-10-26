import { screen, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { DO, READS, show, useFakeHost, workspace } from '@/test/hostHarness'

// The production app on a fake server: no host serves `drop/1`, so Drop is not available and nothing about Drop is called.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, ...DO, 'actions'] })])

it('a real host without drop/1: /drop says Drop is not available, no sidebar entry, no Drop call', async () => {
  show('/w/DEMO/drop')
  expect(await screen.findByTestId('drop-unavailable', {}, T)).toHaveTextContent('Drop is not available on this host yet.')
  expect(within(screen.getByRole('navigation', { name: 'Main' })).queryByRole('link', { name: /^Drop/ })).toBeNull()
  expect(state.host.calls.filter((c) => c.path.includes('drop'))).toEqual([])
})
