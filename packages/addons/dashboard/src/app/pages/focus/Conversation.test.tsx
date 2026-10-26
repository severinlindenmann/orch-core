import { act, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { QueryClient } from '@tanstack/react-query'
import { api, mockStore } from '@/api/client'
import { focusApi } from '@/api/focus'
import type { Run, RunList } from '@/api/runs'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

vi.mock('@/mocks/addons/focus/demo', () => import('@/test/noDemoFocus'))
const terminal = vi.hoisted(() => vi.fn())
const start = vi.hoisted(() => vi.fn())
vi.mock('@/app/terminal/real/RealTerminal', () => ({ RealTerminal: (props: unknown) => { terminal(props); return <div data-testid="real-terminal" /> } }))
vi.mock('@/app/terminal/real/StartAgentDialog', () => ({ StartAgentDialog: (props: { open: boolean; ticket?: string; onStarted: (run: Run) => void; onClose: () => void }) => {
  start(props)
  return props.open ? <div role="dialog" aria-label="Start agent"><button onClick={() => { props.onStarted(run('focus-scratch', { ticket: null })); props.onClose() }}>Start</button></div> : null
} }))
const setup = (store: typeof mockStore) => installAndGrant(store, store.workspaces[0].id, 'focus')
const run = (id: string, over: Partial<Run> = {}): Run => ({
  run: id, rev: 1, kind: 'agent', ticket: 'DEMO-0043', harness: 'claude-code', mode: 'work', model: null,
  as_person: false, state: 'running', started_at: '2026-10-26T10:00:00Z', viewers: 0, writer: false,
  cols: 100, rows: 30, replay_until: null, label: null, label_hidden: false, cwd: {}, exit: null, ...over,
})
// The mock host answers the runs query on its own: let that settle, then put this test's runs over it.
const setRuns = async (client: QueryClient, runs: Run[]) => {
  const key = ['runs', mockStore.workspaces[0].id]
  await waitFor(() => expect(client.getQueryState(key)?.status).toBe('success'))
  act(() => client.setQueryData<RunList>(key, { runner: null, truncated: false, runs }))
}
beforeEach(() => { vi.clearAllMocks(); sessionStorage.clear() })

it('shows the empty state and opens the existing start dialog with the ticket', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  const pane = await screen.findByTestId('focus-conversation')
  await user.click(await within(pane).findByRole('button', { name: 'Start an agent' }))
  expect(await screen.findByRole('dialog', { name: 'Start agent' })).toBeInTheDocument()
  expect(start).toHaveBeenLastCalledWith(expect.objectContaining({ open: true, ticket: 'DEMO-0043', choose: true }))
})

it('keeps Chat keyboard reachable but unavailable while no agent runs', async () => {
  const { user } = renderApp('/focus?scratch=1', { setup })
  const pane = await screen.findByTestId('focus-conversation')
  const chat = within(pane).getByRole('button', { name: 'Chat' })
  expect(chat).toHaveAttribute('aria-disabled', 'true')
  expect(chat).toHaveAccessibleDescription('No agent running')
  within(pane).getByRole('button', { name: 'Terminal' }).focus()
  await user.tab()
  expect(chat).toHaveFocus()
  await user.keyboard('{Enter} ')
  expect(within(pane).getByRole('button', { name: 'Terminal' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.queryByRole('region', { name: 'Needs you' })).not.toBeInTheDocument()
})

it('switches to Chat and back while the terminal stays mounted for the composer', async () => {
  const { client, user } = renderApp('/focus/DEMO-0043', { setup })
  const pane = await screen.findByTestId('focus-conversation')
  const chosen = run('chat-run')
  await setRuns(client, [chosen])
  await screen.findByTestId('real-terminal')
  const read = vi.spyOn(focusApi, 'transcript').mockResolvedValue({ run: 'chat-run', harness: 'claude-code', available: true, reason: null, next: 'o:1', eof: true,
    items: [{ id: 'u1', role: 'user', kind: 'text', text: 'hello from the transcript', ts: null }] })
  await user.click(within(pane).getByRole('button', { name: 'Chat' }))
  expect(await screen.findByText('hello from the transcript')).toBeInTheDocument()
  expect(read).toHaveBeenCalledWith(mockStore.workspaces[0].id, 'chat-run', expect.anything())
  expect(screen.getByTestId('real-terminal')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Chat' })).toHaveAttribute('aria-pressed', 'true')
  await user.click(within(pane).getByRole('button', { name: 'Terminal' }))
  expect(screen.queryByRole('region', { name: 'Chat' })).not.toBeInTheDocument()
  expect(screen.getByTestId('real-terminal')).toBeInTheDocument()
})

it('mounts the real terminal for the newest running agent on this ticket', async () => {
  const { client } = renderApp('/focus/DEMO-0043', { setup })
  await screen.findByTestId('focus-conversation')
  const chosen = run('newest', { started_at: '2026-10-26T12:00:00Z' })
  await setRuns(client, [
    run('older'), chosen, run('shell', { kind: 'shell', started_at: '2026-10-26T13:00:00Z' }),
    run('other-ticket', { ticket: 'DEMO-0044' }), run('ended', { state: 'exited' }),
  ])
  expect(await screen.findByTestId('real-terminal')).toBeInTheDocument()
  expect(terminal).toHaveBeenLastCalledWith(expect.objectContaining({ run: chosen, initial: 'read', ws: mockStore.workspaces[0].id }))
})

it('attaches scratch only to an agent started from Focus', async () => {
  const { client, user } = renderApp('/focus?scratch=1', { setup })
  const pane = await screen.findByTestId('focus-conversation')
  await setRuns(client, [run('unrelated', { ticket: null })])
  expect(screen.queryByTestId('real-terminal')).not.toBeInTheDocument()
  await user.click(await within(pane).findByRole('button', { name: 'Start an agent' }))
  await user.click(within(await screen.findByRole('dialog', { name: 'Start agent' })).getByRole('button', { name: 'Start' }))
  expect(await screen.findByTestId('real-terminal')).toBeInTheDocument()
  expect(terminal).toHaveBeenLastCalledWith(expect.objectContaining({ run: expect.objectContaining({ run: 'focus-scratch' }) }))
  expect(JSON.parse(sessionStorage.getItem(`orch.focus.runs.${mockStore.workspaces[0].id}`)!)).toEqual(['focus-scratch'])
})

it('shows an open question and sends the selected answer through the existing sign action', async () => {
  const action = vi.spyOn(api, 'postAction')
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  const tray = await screen.findByRole('region', { name: 'Needs you' })
  expect(tray).toHaveTextContent('Needs you · 1')
  // The tray is a one-row reminder: Review opens Decisions, the only place a question is answered.
  await user.click(within(tray).getByRole('button', { name: 'Review' }))
  await user.click(await within(screen.getByTestId('focus-panel')).findByRole('button', { name: /DATE \(local midnight\)/ }))
  const dialog = await screen.findByRole('dialog')
  expect(action).not.toHaveBeenCalled()
  await user.click(within(dialog).getByRole('button', { name: 'Send answer' }))
  await waitFor(() => expect(action).toHaveBeenCalledWith('DEMO-0043', expect.objectContaining({ action: 'answer', question: 'Q2', option: 'date' })), { timeout: 5000 })
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Needs you' })).not.toBeInTheDocument())
  action.mockRestore()
})

it('waits for an agent that is starting instead of offering another launch', async () => {
  const { client } = renderApp('/focus/DEMO-0043', { setup })
  const pane = await screen.findByTestId('focus-conversation')
  await setRuns(client, [run('starting', { state: 'starting' })])
  expect(await within(pane).findByText('Starting agent...')).toBeInTheDocument()
  expect(within(pane).queryByRole('button', { name: 'Start an agent' })).not.toBeInTheDocument()
})

it('attaches a scratch session opened from "No ticket" by its run id (a person-run harness terminal)', async () => {
  const { client } = renderApp('/focus?scratch=r_scratchrun', { setup })
  await screen.findByTestId('focus-conversation')
  const mine = run('r_scratchrun', { kind: 'shell', ticket: null, as_person: true })
  await setRuns(client, [run('other', { ticket: null }), mine])
  expect(await screen.findByTestId('real-terminal')).toBeInTheDocument()
  expect(terminal).toHaveBeenLastCalledWith(expect.objectContaining({ run: mine, initial: 'write' }))
})
