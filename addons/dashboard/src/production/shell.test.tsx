import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { Shell } from './Shell'
import { realApi } from '../api/realApi'

vi.mock('../api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', onSessionChange: () => () => {} }))
vi.mock('../api/realApi', () => ({ realApi: { workspaces: vi.fn(), session: vi.fn(), identity: vi.fn(), today: vi.fn(), tickets: vi.fn(), logout: vi.fn() } }))
vi.mock('../api/useLiveUpdates', () => ({ useLiveUpdates: () => ({ ready: true, revoked: false }) }))
const ws = 'a'.repeat(32)
beforeEach(() => {
  vi.clearAllMocks()
  history.replaceState(null, '', '/')
  vi.mocked(realApi.workspaces).mockResolvedValue([{ id: ws, prefix: 'DEMO', name: 'Dev workspace', members: [], needs_you: 0, features: ['tickets'], host_state: 'ok' }])
  vi.mocked(realApi.session).mockResolvedValue({ features: ['tickets'], workspaces: { [ws]: ['tickets'] } })
  vi.mocked(realApi.tickets).mockResolvedValue({ rows: [], total: 0, next_page: null })
})
function show() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Shell /></QueryClientProvider>) }
it('only renders advertised navigation; calls reads by UUID', async () => {
  show()
  await userEvent.click(await screen.findByRole('button', { name: 'Tickets' }))
  expect(await screen.findByText('0 tickets')).toBeInTheDocument()
  expect(realApi.tickets).toHaveBeenCalledWith(ws, undefined)
  expect(realApi.identity).not.toHaveBeenCalled()
  for (const name of ['Today', 'Identity', 'Agents', 'Settings', 'New ticket', 'Demo data', 'Terminals']) expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
})
it('asks for a workspace when readable prefixes collide', async () => {
  const other = 'b'.repeat(32)
  const rows = [ws, other].map((id) => ({ id, prefix: 'DEMO', name: id === ws ? 'First' : 'Second', members: [], needs_you: 0, features: ['tickets'], host_state: 'ok' }))
  vi.mocked(realApi.workspaces).mockResolvedValue(rows)
  vi.mocked(realApi.session).mockResolvedValue({ features: ['tickets'], workspaces: { [ws]: ['tickets'], [other]: ['tickets'] } })
  history.replaceState(null, '', '/w/DEMO/tickets')
  show()
  expect(await screen.findByText('Choose a workspace: this prefix is shared.')).toBeInTheDocument()
  expect(realApi.tickets).not.toHaveBeenCalled()
  await userEvent.selectOptions(screen.getByLabelText('Workspace'), other)
  expect(location.search).toBe('?ws=bbbbbbbb')
  await userEvent.click(await screen.findByRole('button', { name: 'Tickets' }))
  await waitFor(() => expect(realApi.tickets).toHaveBeenCalledWith(other, undefined))
})

it('shows a skeleton until session capabilities arrive', async () => {
  vi.mocked(realApi.session).mockReturnValue(new Promise(() => {}))
  show()
  await screen.findByRole('option', { name: 'Dev workspace · DEMO' })
  expect(screen.queryByText(/Host too old/)).not.toBeInTheDocument()
  expect(screen.getByLabelText('Loading session')).toBeInTheDocument()
})
