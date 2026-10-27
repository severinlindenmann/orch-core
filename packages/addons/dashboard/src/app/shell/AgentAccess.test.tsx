import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, expect, it, vi } from 'vitest'
import { AgentAccess } from './AgentAccess'
import { agentsApi, type Runner } from '@/api/agents'
import { api } from '@/api/client'
import { setClock } from '@/lib/time'

vi.mock('@/app/useMe', () => ({ useMe: () => ({ data: { person: 'me' } }) }))
vi.mock('@/app/terminal/real/StartAgentDialog', () => ({ RunnerGrantCard: (p: { extend?: { id: string }; onOutcome: () => void }) => <button onClick={p.onOutcome}>Complete {p.extend?.id ?? 'new grant'}</button> }))
vi.mock('@/components/sign/SignPopover', () => ({ SignPopover: (p: { action: { grant: string }; summary: React.ReactNode; onInterrupted: () => void; onClose: () => void; onSettled: (phase: string) => void }) => <div>{p.summary}<button onClick={() => p.onSettled('done')}>Sign stop {p.action.grant}</button><button onClick={() => p.onSettled('cancelled')}>Reject stop</button><button onClick={p.onClose}>Close stop</button><button onClick={p.onInterrupted}>Reject click stop</button></div> }))
const now = Date.parse('2026-10-16T12:00:00Z')
let runner: Runner
beforeEach(() => {
  setClock(() => now)
  runner = { enabled: true, connected: true, grant: { id: 'runner', scope: 'workable', until: '2026-10-16T12:45:00Z' } } as Runner
  vi.spyOn(agentsApi, 'runner').mockImplementation(async () => ({ runner }))
  vi.spyOn(api, 'listHostGrants').mockResolvedValue([
    { id: 'runner', person: 'me', issued_at: '2026-10-16T05:00:00Z', expires_at: '2026-10-16T12:45:00Z', revoked: null },
    { id: 'terminal', person: 'me', expires_at: '2026-10-17T13:00:00Z', revoked: null },
    { id: 'other', person: 'someone-else', expires_at: '2026-10-17T13:00:00Z', revoked: null },
  ] as Awaited<ReturnType<typeof api.listHostGrants>>)
})
async function stopFromPill() { fireEvent.click(await screen.findByRole('button', { name: 'Stop' })) }
function show() { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AgentAccess ws="ws" /></QueryClientProvider>) }
it('hides itself while access has an hour or more left', async () => {
  runner.grant = { id: 'runner', scope: 'workable', until: '2026-10-16T20:00:00Z' } as Runner['grant']
  const view = show()
  await waitFor(() => expect(vi.mocked(agentsApi.runner)).toHaveBeenCalled())
  await new Promise(r => setTimeout(r, 50))
  expect(view.container.querySelector('.agent-access')?.textContent ?? '').toBe('')
  expect(screen.queryByRole('button', { name: 'Allow agents' })).not.toBeInTheDocument()
})
it('with 15 to 60 minutes left it says when access ends and offers Allow agents', async () => {
  show()
  expect(await screen.findByText(/Agent access ends at/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Allow agents' }))
  expect(screen.getByRole('heading', { name: 'Allow agents' })).toBeInTheDocument()
  expect(screen.getByText(/replaces your current access/)).toBeInTheDocument()
  expect(await screen.findByRole('button', { name: 'Complete runner' })).toBeInTheDocument()
})
it('with no agent access (also after a reload) it says agents cannot start yet', async () => {
  runner.grant = null
  show()
  expect(await screen.findByRole('group', { name: 'Agent access' })).toHaveTextContent("Agents can't start yet")
  expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Allow agents' }))
  expect(screen.getByRole('heading', { name: 'Allow agents' })).toBeInTheDocument()
  expect(await screen.findByRole('button', { name: 'Complete new grant' })).toBeInTheDocument()
})
it('the Stop review says whose access and until when in plain words, the id only under Technical details', async () => {
  show()
  await stopFromPill()
  fireEvent.click(screen.getByRole('button', { name: 'Stop agent access' }))
  await screen.findByRole('button', { name: 'Sign stop runner' })
  expect(screen.getByText(/This ends the agent access for you, which runs until/)).toBeInTheDocument()
  const details = screen.getByText('Technical details').closest('details')!
  expect(details).toHaveTextContent('Access id: runner')
  expect(screen.getByText(/This ends the agent access/).closest('details')).toBeNull()
})
it('confirms Stop and signs each own grant separately with progress', async () => {
  show()
  await stopFromPill()
  expect(screen.queryByText('Sign stop runner')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Stop agent access' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Sign stop runner' }))
  expect(screen.getByText('Stopping access 2 of 2. 1 stopped.')).toBeInTheDocument()
  expect(screen.queryByText('Sign stop other')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Sign stop terminal' }))
  expect(screen.queryByText('Stopping access 2 of 2. 1 stopped.')).not.toBeInTheDocument()
})
it('shows the cannot-start line after a completed Extend with no live runner grant, and Allow agents opens a new flow', async () => {
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Allow agents' }))
  runner = { ...runner, grant: null }
  fireEvent.click(screen.getByRole('button', { name: 'Complete runner' }))
  await screen.findByText("Agents can't start yet")
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
  fireEvent.click(await screen.findByRole('button', { name: 'Allow agents' }))
  expect(await screen.findByRole('button', { name: 'Complete new grant' })).toBeInTheDocument()
})

it.each(['Reject stop', 'Close stop', 'Reject click stop'])('retains partial Stop progress after %s', async (name) => {
  show()
  await stopFromPill()
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

it('says agents cannot start yet after Stop completes', async () => {
  show()
  await stopFromPill()
  fireEvent.click(screen.getByRole('button', { name: 'Stop agent access' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Sign stop runner' }))
  runner = { ...runner, grant: null }
  fireEvent.click(screen.getByRole('button', { name: 'Sign stop terminal' }))
  expect(await screen.findByRole('group', { name: 'Agent access' })).toHaveTextContent("Agents can't start yet")
  fireEvent.click(screen.getByRole('button', { name: 'Allow agents' }))
  expect(await screen.findByRole('button', { name: 'Complete new grant' })).toBeInTheDocument()
})

it('K3: cancelling the first Stop signature restores the normal line, no "Stopped 0 of 2"', async () => {
  show()
  await stopFromPill()
  fireEvent.click(screen.getByRole('button', { name: 'Stop agent access' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Close stop' }))
  expect(screen.queryByText(/Stopped 0 of/)).toBeNull()
  expect(screen.queryByRole('button', { name: 'Stop again' })).toBeNull()
  expect(await screen.findByRole('button', { name: 'Allow agents' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()
})

it('F8: after a stop the run list is refreshed, and the Stop dialog says a started run needs its own Stop', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(client, 'invalidateQueries')
  render(<QueryClientProvider client={client}><AgentAccess ws="ws" /></QueryClientProvider>)
  await stopFromPill()
  expect(screen.getByText(/keeps running until you press Stop on it in the agents dock/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Stop agent access' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Sign stop runner' }))
  await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: ['runs', 'ws'] }))
})
