import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, expect, it, vi } from 'vitest'
import { AgentAccess } from './AgentAccess'
import { agentsApi, type Runner } from '@/api/agents'
import { api } from '@/api/client'
import { setClock } from '@/lib/time'

vi.mock('@/app/useMe', () => ({ useMe: () => ({ data: { person: 'me' } }) }))
vi.mock('@/app/terminal/real/StartAgentDialog', () => ({ RunnerGrantCard: (p: { extend?: { id: string }; onOutcome: () => void }) => <button onClick={p.onOutcome}>Complete {p.extend?.id ?? 'new grant'}</button> }))
vi.mock('@/components/sign/HandoffDialog', () => ({ HandoffDialog: (p: { action: { grant: string }; summary: React.ReactNode; onInterrupted: () => void; onClose: () => void; onSettled: (phase: string) => void }) => <div>{p.summary}<button onClick={() => p.onSettled('done')}>Sign stop {p.action.grant}</button><button onClick={() => p.onSettled('cancelled')}>Reject stop</button><button onClick={p.onClose}>Close stop</button><button onClick={p.onInterrupted}>Reject click stop</button></div> }))
const now = Date.parse('2026-10-16T12:00:00Z')
let runner: Runner
beforeEach(() => {
  setClock(() => now)
  runner = { enabled: true, connected: true, grant: { id: 'runner', scope: 'workable', until: '2026-10-16T13:00:00Z' } } as Runner
  vi.spyOn(agentsApi, 'runner').mockImplementation(async () => ({ runner }))
  vi.spyOn(api, 'listHostGrants').mockResolvedValue([
    { id: 'runner', person: 'me', issued_at: '2026-10-16T05:00:00Z', expires_at: '2026-10-16T13:00:00Z', revoked: null },
    { id: 'terminal', person: 'me', expires_at: '2026-10-17T13:00:00Z', revoked: null },
    { id: 'other', person: 'someone-else', expires_at: '2026-10-17T13:00:00Z', revoked: null },
  ] as Awaited<ReturnType<typeof api.listHostGrants>>)
})
function show() { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AgentAccess ws="ws" /></QueryClientProvider>) }
it('shows the runner expiry and short-grant reminder and opens Extend', async () => {
  show()
  expect(await screen.findByRole('group', { name: 'Agent access' })).toHaveTextContent('Agents can work for you until Fri 13:00')
  expect(await screen.findByRole('status')).toHaveTextContent('Agent access ends at 13:00')
  expect(screen.getByRole('button', { name: 'Extend agent access from reminder' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Extend agent access' }))
  expect(screen.getByText(/Agents that are running now stop; new agents use the extended access/)).toBeInTheDocument()
  expect(await screen.findByRole('button', { name: 'Complete runner' })).toBeInTheDocument()
})
it('hides terminal-only access', async () => {
  runner.grant = null
  const { container } = show()
  await waitFor(() => expect(agentsApi.runner).toHaveBeenCalled())
  expect(container).toBeEmptyDOMElement()
})
it('confirms Stop and signs each own grant separately with progress', async () => {
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
  expect(screen.queryByText('Sign stop runner')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Stop agent access' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Sign stop runner' }))
  expect(screen.getByText('Stopping access 2 of 2. 1 stopped.')).toBeInTheDocument()
  expect(screen.queryByText('Sign stop other')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Sign stop terminal' }))
  expect(screen.queryByText('Stopping access 2 of 2. 1 stopped.')).not.toBeInTheDocument()
})
it('shows stopped after a completed Extend with no live runner grant, and Start again opens a new flow', async () => {
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Extend agent access' }))
  runner = { ...runner, grant: null }
  fireEvent.click(screen.getByRole('button', { name: 'Complete runner' }))
  await screen.findByText('Agents stopped')
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
  fireEvent.click(await screen.findByRole('button', { name: 'Start again' }))
  expect(await screen.findByRole('button', { name: 'Complete new grant' })).toBeInTheDocument()
})

it.each(['Reject stop', 'Close stop', 'Reject click stop'])('retains partial Stop progress after %s', async (name) => {
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
  fireEvent.click(screen.getByRole('button', { name: 'Stop agent access' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Sign stop runner' }))
  runner = { ...runner, grant: null }
  vi.mocked(api.listHostGrants).mockResolvedValue([{ id: 'terminal', person: 'me', expires_at: '2026-10-17T13:00:00Z', revoked: null }] as Awaited<ReturnType<typeof api.listHostGrants>>)
  fireEvent.click(screen.getByRole('button', { name }))
  expect(screen.getByText(/Stopped 1 of 2/)).toHaveTextContent('Stopped 1 of 2. 1 still active — Stop again')
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: 'Stop again' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop agent access' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Stop agent access' }))
  expect(await screen.findByRole('button', { name: 'Sign stop terminal' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Sign stop runner' })).not.toBeInTheDocument()
})

it('keeps "Agents stopped · Start again" after Stop completes', async () => {
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
  fireEvent.click(screen.getByRole('button', { name: 'Stop agent access' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Sign stop runner' }))
  runner = { ...runner, grant: null }
  fireEvent.click(screen.getByRole('button', { name: 'Sign stop terminal' }))
  expect(await screen.findByRole('group', { name: 'Agent access' })).toHaveTextContent('Agents stopped')
  fireEvent.click(screen.getByRole('button', { name: 'Start again' }))
  expect(await screen.findByRole('button', { name: 'Complete new grant' })).toBeInTheDocument()
})

it('K3: cancelling the first Stop signature restores the normal pill, no "Stopped 0 of 2"', async () => {
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
  fireEvent.click(screen.getByRole('button', { name: 'Stop agent access' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Close stop' }))
  expect(screen.queryByText(/Stopped 0 of/)).toBeNull()
  expect(screen.queryByRole('button', { name: 'Stop again' })).toBeNull()
  expect(await screen.findByRole('group', { name: 'Agent access' })).toHaveTextContent('Agents can work for you until')
  expect(screen.getByRole('button', { name: 'Extend agent access' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()
})

it('K8: the pill shows the soonest end of all own access, with "+1 more"', async () => {
  runner = { ...runner, grant: { id: 'runner', scope: 'workable', until: '2026-10-19T13:00:00Z' } } as Runner
  vi.mocked(api.listHostGrants).mockResolvedValue([
    { id: 'runner', person: 'me', issued_at: '2026-10-16T05:00:00Z', expires_at: '2026-10-19T13:00:00Z', revoked: null },
    { id: 'terminal', person: 'me', expires_at: '2026-10-16T20:00:00Z', revoked: null },
  ] as Awaited<ReturnType<typeof api.listHostGrants>>)
  show()
  const pill = await screen.findByRole('group', { name: 'Agent access' })
  await waitFor(() => expect(pill).toHaveTextContent('(+1 more)'))
  expect(pill.querySelector('time')?.getAttribute('datetime')).toBe('2026-10-16T20:00:00.000Z')
})
