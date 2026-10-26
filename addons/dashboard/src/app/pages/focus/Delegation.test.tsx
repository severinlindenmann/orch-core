import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { mockStore, resetMockStoreForTests } from '@/api/client'
import * as features from '@/api/features'
import { runsApi, type Run } from '@/api/runs'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { PauseButton, TasksChip, TasksList } from './Delegation'
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspace: mockStore.workspaces[0] }) }))
import { installAndGrant } from '@/test/installAddon'

const run = (id: string, extra: Partial<Run> = {}): Run => ({ run: id, rev: 1, kind: 'agent', ticket: 'DEMO-0042', harness: 'codex', mode: 'work', model: 'test-model', as_person: false, state: 'running', started_at: '2026-10-26T10:00:00Z', viewers: 0, writer: false, cols: 80, rows: 24, replay_until: null, label: 'Review changes', label_hidden: false, cwd: {}, exit: null, ...extra })
function setup(store: typeof mockStore) {
  installAndGrant(store, store.workspaces[0].id, 'focus')
}
function show(runs: Run[] = [run('main', { started_at: '2026-10-26T11:00:00Z' }), run('child', { grant_state: 'ended' })], unavailable = false, ticket = 'DEMO-0042') {
  vi.spyOn(features, 'isRealHost').mockReturnValue(true)
  const list = vi.spyOn(runsApi, 'list')
  if (unavailable) list.mockRejectedValue(new Error('Unavailable'))
  else list.mockResolvedValue({ runs, runner: null, truncated: false })
  resetMockStoreForTests()
  setup(mockStore)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const user = userEvent.setup()
  render(<QueryClientProvider client={client}><TasksChip ticket={ticket} /><TasksList ticket={ticket} /><PauseButton ticket={ticket} /></QueryClientProvider>)
  return { user, client }
}
afterEach(() => vi.restoreAllMocks())

it('counts tasks and needs-you tasks and opens the task list', async () => {
  const { user } = show()
  const chip = await screen.findByRole('button', { name: '1 tasks · 1 need you' })
  await user.click(chip)
  expect(within(screen.getByRole('dialog')).getByRole('heading', { name: 'Tasks' })).toBeInTheDocument()
})

it('expands a collapsed row with model and state', async () => {
  const { user } = show()
  const title = await screen.findByText('Review changes')
  const row = title.closest('details')!
  expect(row.open).toBe(false)
  await user.click(title)
  expect(row.open).toBe(true)
  expect(within(row).getByText('test-model')).toBeVisible()
  expect(within(row).getByText('Result: needs you')).toBeVisible()
  expect(within(row).getByText('Requested by you')).toBeVisible()
})

it('shows limited visibility without a zero count when runs are unavailable', async () => {
  show([], true)
  expect((await screen.findAllByText('Delegation visibility is limited in this version')).length).toBeGreaterThan(0)
  expect(screen.queryByText(/0 tasks/)).toBeNull()
  expect(screen.getByRole('button', { name: 'Stop all' })).toBeDisabled()
})

it('hides the chip when the known task list is empty', async () => {
  show([run('main')])
  await screen.findByText('No tasks yet.')
  expect(screen.queryByTestId('focus-tasks-chip')).toBeNull()
})

it('confirms and stops the main run and every active task, excluding other tickets', async () => {
  const { user } = show([run('main', { started_at: '2026-10-26T11:00:00Z' }), run('child'), run('other', { ticket: 'DEMO-0043' })])
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  const stop = vi.spyOn(runsApi, 'stop').mockResolvedValue({ state: 'exited', cleanup: 'complete' })
  const button = await screen.findByRole('button', { name: 'Stop all' })
  await waitFor(() => expect(button).toBeEnabled())
  await user.click(button)
  expect(stop).not.toHaveBeenCalled()
  confirm.mockReturnValue(true)
  await user.click(button)
  expect(confirm).toHaveBeenLastCalledWith('Stop the main agent and 1 tasks?')
  await waitFor(() => expect(stop).toHaveBeenCalledTimes(2))
  expect(stop.mock.calls.map(args => args[1])).toEqual(['main', 'child'])
})

it('includes child sessions from the mock host and exposes their stop limitation', async () => {
  show([], false, 'DEMO-0043')
  const list = screen.getByTestId('focus-tasks-list')
  await waitFor(() => expect(within(list).getAllByText('Spawned by main agent').length).toBeGreaterThan(0))
  expect(screen.getByRole('button', { name: 'Stop all' })).toBeDisabled()
  expect(within(list).getAllByText('Stopping this task is not supported by this host.').length).toBeGreaterThan(0)
  expect(list.textContent).not.toContain('s_77c2')
})
