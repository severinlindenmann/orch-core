import { screen, waitFor, within } from '@testing-library/react'
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

// R14 P06: an agent whose ticket is done is "Finished" with Close; one on an open ticket stays Live with Stop.
it('an agent run on a done ticket shows Finished and Close, one on an open ticket stays Live', async () => {
  const run = (id: string, ticket: string) => ({
    run: `r_${id}`, rev: 1, kind: 'agent', ticket, harness: 'claude-code', mode: null, model: null, as_person: true, state: 'running',
    started_at: '2026-10-18T09:00:00Z', viewers: 1, writer: false, cols: 100, rows: 30, replay_until: null, label: null, label_hidden: false, cwd: { kind: 'workspace' }, exit: null,
  })
  const sum = (key: string, status: string) => ({ key, uid: key, title: key, type: 'feature', priority: null, size: null, status, labels: [], assignee: null })
  state.host.override = (path, method) => {
    if (path.endsWith('/runs') && method === 'GET') return Response.json({ ...RUNS, runs: [run('F1', 'DEMO-0001'), run('F2', 'DEMO-0002')] })
    if (/\/tickets(\?|$)/.test(path) && method === 'GET') return Response.json({ rows: [sum('DEMO-0001', 'done'), sum('DEMO-0002', 'in_progress')], next_page: null })
    return undefined
  }
  show('/w/DEMO/terminals')
  const rows = within(await screen.findByRole('list', { name: 'Terminals' }, T)).getAllByRole('listitem')
  await waitFor(() => expect(rows[0]).toHaveTextContent('Finished'), T)
  expect(rows[0]).toHaveTextContent('Ticket is done')
  expect(rows[0]).not.toHaveTextContent('Live')
  expect(within(rows[0]).getByRole('button', { name: /^Close/ })).toBeInTheDocument()
  expect(within(rows[0]).queryByRole('button', { name: /^Stop/ })).toBeNull()
  expect(rows[1]).toHaveTextContent('Live')
  expect(within(rows[1]).getByRole('button', { name: /^Stop/ })).toBeInTheDocument()
})
