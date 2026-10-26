import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { DO, READS, useFakeHost, workspace, show } from '@/test/hostHarness'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const ID = 'a'.repeat(32)
const T = { timeout: 5000 }
const state = useFakeHost(() => [workspace({ id: ID, features: [...READS, ...DO, 'actions', 'move'] })])

it('a move on a real host is a hand-off for backlog or open, and nothing moves until it is signed', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/board')
  const card = await screen.findByTestId('card-DEMO-0001', {}, T)
  card.focus()
  await user.keyboard('m')
  await user.click(await screen.findByRole('menuitem', { name: /Backlog/ }))
  const dialog = await screen.findByRole('dialog', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(dialog).toHaveTextContent('Move DEMO-0001 to Backlog')
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'move', key: 'DEMO-0001', to: 'backlog' }])
})

it('a host that does not serve move offers no move at all', async () => {
  state.host.workspaces[0].features = [...READS, ...DO, 'actions']
  show('/w/DEMO/board')
  expect(await screen.findByText(/Can.t move tickets/, {}, T)).toBeInTheDocument()
})
