import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, expect, it, vi } from 'vitest'
import { AgentAccess } from './AgentAccess'
import { agentsApi, type Runner } from '@/api/agents'
import { api } from '@/api/client'
import { setClock } from '@/lib/time'

vi.mock('@/app/useMe', () => ({ useMe: () => ({ data: { person: 'me' } }) }))
vi.mock('@/app/terminal/real/StartAgentDialog', () => ({ RunnerGrantCard: (p: { extend?: { id: string }; onOutcome: () => void }) => <button onClick={p.onOutcome}>Complete {p.extend?.id ?? 'new grant'}</button> }))
vi.mock('@/components/sign/HandoffDialog', () => ({ HandoffDialog: (p: { action: { grant: string }; summary: React.ReactNode; onSettled: (phase: string) => void }) => <div>{p.summary}<button onClick={() => p.onSettled('done')}>Sign stop {p.action.grant}</button></div> }))
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
  expect(await screen.findByText(/Agents can work for you until/)).toHaveTextContent('Fri 13:00')
  expect(await screen.findByRole('status')).toHaveTextContent('Agent access ends at 13:00')
  fireEvent.click(screen.getAllByRole('button', { name: 'Extend' })[0])
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
it('shows stopped after a completed Extend with no live runner grant, and Retry opens a new flow', async () => {
  show()
  fireEvent.click((await screen.findAllByRole('button', { name: 'Extend' }))[0])
  runner = { ...runner, grant: null }
  fireEvent.click(screen.getByRole('button', { name: 'Complete runner' }))
  await screen.findByText('Agents stopped')
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
  fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
  expect(await screen.findByRole('button', { name: 'Complete new grant' })).toBeInTheDocument()
})
