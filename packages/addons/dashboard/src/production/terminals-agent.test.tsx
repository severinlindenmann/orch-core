import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'

// Run 12a owner feedback: on the Terminals page starting an agent comes first; a plain terminal is the second button.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const RUNS = { runs: [], runner: { connected: true, enabled: true, terminals: true }, truncated: false }
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'terminals_page', 'agent_start'] })])

it('offers Start agent first and opens core\'s start dialog without a ticket', async () => {
  state.host.override = (path, method) => (path.endsWith('/runs') && method === 'GET' ? Response.json(RUNS) : undefined)
  const user = userEvent.setup()
  show('/w/DEMO/terminals')
  const start = await screen.findByTestId('terminals-start-agent', {}, T)
  const buttons = screen.getAllByRole('button').map((b) => b.textContent?.trim())
  expect(buttons.indexOf('Start agent')).toBeLessThan(buttons.indexOf('New terminal'))
  expect(await screen.findByTestId('terminals-empty', {}, T)).toHaveTextContent('Start an agent')
  await user.click(start)
  expect(await screen.findByRole('dialog', {}, T)).toHaveTextContent(/Start /)
})
