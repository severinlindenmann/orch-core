import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { mockStore, resetMockStoreForTests } from '@/api/client'
import * as features from '@/api/features'
import { focusApi, type Delegations } from '@/api/focus'
import { runsApi, type Run } from '@/api/runs'
import mockDelegations from '@/mocks/addons/focus/delegations.json'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { PauseButton, TasksChip, TasksList } from './Delegation'
vi.mock('@/app/terminal/real/StartAgentDialog', () => ({ StartAgentDialog: (p: { open: boolean; initial?: { note?: string } }) => p.open ? <div role="dialog" aria-label="Start agent">{p.initial?.note}</div> : null }))
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspace: mockStore.workspaces[0] }) }))
import { installAndGrant } from '@/test/installAddon'

const run = (id: string, extra: Partial<Run> = {}): Run => ({ run: id, rev: 1, kind: 'agent', ticket: 'DEMO-0042', harness: 'codex', mode: 'work', model: 'test-model', as_person: false, state: 'running', started_at: '2026-10-26T10:00:00Z', viewers: 0, writer: false, cols: 80, rows: 24, replay_until: null, label: 'Review changes', label_hidden: false, cwd: {}, exit: null, ...extra })
function setup(store: typeof mockStore) {
  installAndGrant(store, store.workspaces[0].id, 'focus')
}
const none: Delegations = { ticket: 'DEMO-0042', supported: true, sessions: [] }
function show(runs: Run[] = [run('main', { started_at: '2026-10-26T11:00:00Z' }), run('child', { grant_state: 'ended' })], unavailable = false, ticket = 'DEMO-0042', delegations: Delegations = none) {
  vi.spyOn(features, 'isRealHost').mockReturnValue(true)
  vi.spyOn(focusApi, 'delegations').mockResolvedValue(delegations)
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
  const chip = await screen.findByRole('button', { name: '1 task, 1 needs you' })
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
  expect(within(row).queryByText(/^Result:/)).toBeNull() // it repeats the status shown on the row
  expect(within(row).getByRole('button', { name: 'Stop' })).toBeEnabled()
  expect(within(row).getByText('Requested by you')).toBeVisible()
})

it('shows limited visibility without a zero count when runs are unavailable', async () => {
  show([], true)
  expect((await screen.findAllByText('Task details unavailable')).length).toBeGreaterThan(0)
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

it('lists agent-spawned children from the host with files and no stop button action', async () => {
  const { user } = show([run('main', { started_at: '2026-10-26T11:00:00Z' })], false, 'DEMO-0042', mockDelegations as Delegations)
  const list = screen.getByTestId('focus-tasks-list')
  const title = await within(list).findByText('Write boundary tests')
  await user.click(title)
  const row = title.closest('details')!
  expect(within(row).getByText('Spawned by main agent')).toBeVisible()
  expect(within(row).queryByText(/^Result:/)).toBeNull()
  expect(within(row).getByTestId('focus-task-files')).toHaveTextContent('tests/test_invite.py')
  expect(within(row).queryByRole('button', { name: 'Stop' })).toBeNull() // a finished task has nothing to stop
  expect(within(row).getByText(/^Ended /)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Stop all' })).toBeDisabled()
  expect(list.textContent).not.toContain('s_main')
})

it('merges a human-requested child with its run and does not list it twice', async () => {
  const child = { ...mockDelegations.sessions[0].children[0], id: 'run:child', spawned_by: 'human', run: 'child', title: 'Review it', state: 'running', transcript: { run: 'child', child: null }, files: [] }
  const deleg = { ...mockDelegations, sessions: [{ ...mockDelegations.sessions[0], children: [child] }] } as Delegations
  show([run('main', { started_at: '2026-10-26T11:00:00Z' }), run('child', { label: 'Other label' })], false, 'DEMO-0042', deleg)
  const list = screen.getByTestId('focus-tasks-list')
  expect(await within(list).findByText('Review it')).toBeVisible()
  expect(within(list).queryByText('Other label')).toBeNull()
  expect(within(list).getAllByText('Requested by you')).toHaveLength(1)
})

it('is limited when the host does not support the delegation list or hides a harness', async () => {
  show([run('main')], false, 'DEMO-0042', { ...none, supported: false })
  expect((await screen.findAllByText('Task details unavailable')).length).toBeGreaterThan(0)
})

it('offers "Delegate a task" for a live main run and disables it at the policy limit', async () => {
  const base = mockDelegations.sessions[0]
  const open = { ...mockDelegations, sessions: [{ ...base, run: 'main', policy: { delegate: true, max_children: 5 } }] } as unknown as Delegations
  show([run('main', { started_at: '2026-10-26T11:00:00Z' })], false, 'DEMO-0042', open)
  const button = await screen.findByRole('button', { name: 'Delegate a task' })
  await waitFor(() => expect(button).toBeEnabled())
  cleanup()
  const full = { ...mockDelegations, sessions: [{ ...base, run: 'main', policy: { delegate: true, max_children: base.children.length } }] } as unknown as Delegations
  show([run('main', { started_at: '2026-10-26T11:00:00Z' })], false, 'DEMO-0042', full)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Delegate a task' })).toBeDisabled())
})

it('detaches a human-requested child into a session of its own', async () => {
  const child = { ...mockDelegations.sessions[0].children[0], id: 'run:child', spawned_by: 'human', run: 'child', title: 'Review it', state: 'running', transcript: { run: 'child', child: null }, files: [] }
  const deleg = { ...mockDelegations, sessions: [{ ...mockDelegations.sessions[0], children: [child] }] } as Delegations
  const detach = vi.spyOn(focusApi, 'detach').mockResolvedValue({ run: 'child', ticket: 'DEMO-0042' })
  const { user } = show([run('main', { started_at: '2026-10-26T11:00:00Z' }), run('child', { label: 'Other label' })], false, 'DEMO-0042', deleg)
  await user.click(await screen.findByText('Review it'))
  await user.click(screen.getByRole('button', { name: 'Make separate session' }))
  await waitFor(() => expect(detach).toHaveBeenCalledWith(mockStore.workspaces[0].id, 'child'))
})

it('an agent-spawned child can only start anew: the dialog opens with its title and first request', async () => {
  const read = vi.spyOn(focusApi, 'transcript').mockResolvedValue({ run: 'main', harness: 'claude-code', available: true, reason: null, next: 'o:3', eof: true, items: [{ id: 'u', role: 'user', kind: 'text', text: 'Cover the boundary cases', ts: null }] })
  const { user } = show([run('main', { started_at: '2026-10-26T11:00:00Z' })], false, 'DEMO-0042', mockDelegations as Delegations)
  const title = await screen.findByText('Write boundary tests')
  await user.click(title)
  expect(within(title.closest('details')!).queryByRole('button', { name: 'Make separate session' })).toBeNull()
  await user.click(within(title.closest('details')!).getByRole('button', { name: 'Start as separate session' }))
  await waitFor(() => expect(read).toHaveBeenCalledWith(mockStore.workspaces[0].id, 'r_01J0000000000000000000000A', expect.objectContaining({ child: 'agent-a04b67fe' })))
  expect(await screen.findByRole('dialog', { name: 'Start agent' })).toHaveTextContent('Write boundary tests - Cover the boundary cases')
})
