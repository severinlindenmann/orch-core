import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'

// The production app on a fake server that speaks the runs routes (Terminals add-on): the page lists the person's
// terminals and agent sessions; it appears in the menu only while the add-on is ON (the server then lists `terminals_page`).
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const run = (id: string, over: Record<string, unknown> = {}) => ({
  run: `r_${id}`, rev: 1, kind: 'shell', ticket: null, harness: 'shell', mode: null, model: null, as_person: true, state: 'running',
  started_at: '2026-10-18T09:00:00Z', viewers: 1, writer: false, cols: 100, rows: 30, replay_until: null, label: null, label_hidden: false,
  cwd: { kind: 'workspace' }, exit: null, ...over,
})
const RUNS = {
  runs: [
    run('A1', { kind: 'agent', harness: 'claude-code', ticket: 'DEMO-0001' }),
    run('B2', { label: 'Build the docs', writer: true }),
    run('C3', { state: 'exited', exit: { code: 0, signal: null, at: '2026-10-18T09:30:00Z', reason: 'exited', cleanup: 'complete' } }),
    run('D4', { kind: 'agent', harness: 'codex', state: 'exited', exit: { code: 127, signal: null, at: '2026-10-18T09:31:00Z', reason: 'exited', cleanup: 'complete' } }),
  ],
  runner: { connected: true, enabled: true, terminals: true },
  truncated: false,
}
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'terminals_page'] })])
const route = () => {
  state.host.override = (path, method) => {
    if (path.endsWith('/runs') && method === 'GET') return Response.json(RUNS)
    if (path.includes('/runs/') && path.endsWith('/stop') && method === 'POST') return Response.json({ state: 'exited', cleanup: 'complete' })
    return undefined
  }
}
const calls = (p: string, method?: string) => state.host.calls.filter((c) => c.path.includes(p) && (!method || c.method === method))

it('lists terminals and agents with their state and who controls them, and the menu has the entry', async () => {
  route()
  show('/w/DEMO/terminals')
  const list = await screen.findByRole('list', { name: 'Terminals' }, T)
  const rows = within(list).getAllByRole('listitem')
  expect(rows).toHaveLength(4)
  expect(rows[0]).toHaveTextContent('DEMO-0001 Claude')
  expect(rows[0]).toHaveTextContent('Agent')
  expect(rows[0]).toHaveTextContent('Live')
  expect(rows[0]).toHaveTextContent('Read only')
  expect(rows[1]).toHaveTextContent('Build the docs')
  expect(rows[1]).toHaveTextContent('Controlled')
  expect(rows[2]).toHaveTextContent('Ended')
  expect(rows[2]).not.toHaveTextContent(/Read only|Controlled/)
  expect(within(rows[2]).queryByRole('button', { name: /^Stop/ })).toBeNull()
  // M-5: an ended row says why it ended (an agent whose program is missing is not a silent "Ended").
  expect(rows[3]).toHaveTextContent('Ended')
  expect(within(rows[3]).getByTestId('run-exit')).toHaveTextContent('its program was not found on this computer (code 127)')
  expect(screen.getAllByRole('link', { name: 'Terminals' }).length).toBeGreaterThan(0)
})

it('Stop uses the dock\'s stop route; New terminal opens the host\'s previewed spec in one press, with no confirm dialog', async () => {
  route()
  const user = userEvent.setup()
  show('/w/DEMO/terminals')
  const list = await screen.findByRole('list', { name: 'Terminals' }, T)
  await user.click(within(list).getByRole('button', { name: 'Stop Build the docs' }))
  await waitFor(() => expect(calls('/runs/r_B2/stop', 'POST')).toHaveLength(1))
  state.host.override = (path, method) => {
    if (path.endsWith('/terminals/preview') && method === 'POST')
      return Response.json({ spec: 'f'.repeat(64), display: 'zsh', display_hidden: false, cwd_path: '/Users/demo/code', cwd_path_hidden: false, notice: null, as_person: true, login_shell: false, env: [], blocked: null, warnings: [] })
    if (path.endsWith('/terminals/open') && method === 'POST') return Response.json({ run: run('E5') })
    return Response.json(RUNS)
  }
  expect(screen.queryByTestId('terminals-start-agent')).toBeNull() // the host does not serve agent_start here
  await user.click(screen.getByRole('button', { name: /New terminal/ }))
  await waitFor(() => expect(calls('/terminals/open', 'POST')).toHaveLength(1), T)
  expect(calls('/terminals/preview', 'POST')).toHaveLength(1) // the preview comes first
  expect(state.host.calls.findIndex((c) => c.path.endsWith('/terminals/preview'))).toBeLessThan(state.host.calls.findIndex((c) => c.path.endsWith('/terminals/open')))
  expect(calls('/terminals/open')[0].body).toMatchObject({ spec: 'f'.repeat(64), idem: expect.stringMatching(/^a_/) })
  expect(screen.queryByRole('dialog')).toBeNull() // no "Open a terminal?" confirm
  expect(screen.queryByRole('alert')).toBeNull()
})
