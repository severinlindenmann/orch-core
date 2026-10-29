import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { terminalWaitFor } from '@/app/attention'
import type { TicketDocument } from '@/api/types'
import { phaseOf } from './progress'
import { TicketHeader, TicketProgressLive } from './Header'
import userEvent from '@testing-library/user-event'
import { act } from '@testing-library/react'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), isRealHost: () => true, hasFeature: () => true }))
vi.mock('@tanstack/react-router', async (orig) => ({ ...(await orig<typeof import('@tanstack/react-router')>()), Link: ({ children }: { children?: unknown }) => <a href="#">{children as never}</a> }))
vi.mock('@/app/terminal/real/StartAgentDialog', async (orig) => ({ ...(await orig<typeof import('@/app/terminal/real/StartAgentDialog')>()), StartAgentDialog: () => <div data-testid="start-dialog" /> }))
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspace: { id: 'w1', addons: {}, members: [] }, workspaces: [{ id: 'w1', prefix: 'PW' }] }) }))

// P04: a terminal wait of the ticket's agent turns the pill from "Getting started" into "Needs you".
const items = [
  { kind: 'terminal_wait', ticket: 'PW-0001', ref: 'run_a' },
  { kind: 'terminal_wait', ticket: 'PW-0002', ref: 'run_b' },
  { kind: 'question', ticket: 'PW-0002', ref: 'Q1' },
]

it('terminalWaitFor picks the run of this ticket only', () => {
  expect(terminalWaitFor(items, 'PW-0002')).toBe('run_b')
  expect(terminalWaitFor(items, 'PW-0003')).toBeUndefined()
  expect(terminalWaitFor([], 'PW-0002')).toBeUndefined()
})

const ticket = { key: 'PW-0002', status: 'open', claim: null } as unknown as TicketDocument
const starting = { state: 'running' as const, started_at: new Date(Date.now() - 5000).toISOString() }

it('the pill reads Needs you while a terminal wait exists, Getting started otherwise', () => {
  const now = Date.now()
  expect(phaseOf(ticket, { needsYou: false, run: starting, now }).kind).toBe('starting')
  expect(phaseOf(ticket, { needsYou: !!terminalWaitFor(items, 'PW-0002'), run: starting, now }).kind).toBe('needs')
})

it('the header wiring: a terminal_wait in Today for the ticket makes the pill read Needs you', async () => {
  const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!)
  Object.assign(doc, { key: 'PW-0002', status: 'open', claim: null, questions_state: [] })
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', approvals: [], missing: ['context'] }
  const viewer = { person: 'nobody', role: 'viewer' as const, members: [], name: (id: string | null | undefined) => String(id), ready: true }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['runs', 'w1'], { runs: [{ ticket: 'PW-0002', kind: 'agent', state: 'running', started_at: new Date().toISOString() }], runner: null, truncated: false })
  client.setQueryData(['today', 'w1'], { needs_you: items, read_only_open: [], waiting_on_others: { count: 0, people: [] } })
  render(
    <QueryClientProvider client={client}>
      <TicketHeader ticket={doc} viewer={viewer as never} sign={() => {}} jump={() => {}} />
    </QueryClientProvider>,
  )
  expect(await screen.findByText('Needs you')).toBeInTheDocument()
  expect(screen.queryByText(/Getting started/)).toBeNull()
})

async function stoppedWith(role: 'owner' | 'viewer') {
  const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!)
  Object.assign(doc, { key: 'PW-0002', status: 'open', claim: null, questions_state: [] })
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', approvals: [], missing: ['context'] }
  const viewer = { person: 'nobody', role, members: [], name: (id: string | null | undefined) => String(id), ready: true }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['runs', 'w1'], { runs: [{ ticket: 'PW-0002', kind: 'agent', state: 'exited', started_at: new Date().toISOString() }], runner: null, truncated: false })
  client.setQueryData(['today', 'w1'], { needs_you: [], read_only_open: [], waiting_on_others: { count: 0, people: [] } })
  render(
    <QueryClientProvider client={client}>
      <TicketHeader ticket={doc} viewer={viewer as never} sign={() => {}} jump={() => {}} />
      <TicketProgressLive ticket={doc} viewer={viewer as never} sign={() => {}} jump={() => {}} />
    </QueryClientProvider>,
  )
  await screen.findByText('The agent stopped early')
}

it('a viewer who cannot start an agent sees no "Start agent again"', async () => {
  await stoppedWith('viewer')
  expect(screen.queryByRole('button', { name: 'Start agent again' })).toBeNull()
})

it('Start agent again opens the header start flow for this ticket only', async () => {
  await stoppedWith('owner')
  act(() => {
    window.dispatchEvent(new CustomEvent('orch:start-agent', { detail: 'PW-0009' }))
  })
  expect(screen.queryByTestId('start-dialog')).toBeNull()
  await userEvent.setup().click(screen.getByRole('button', { name: 'Start agent again' }))
  expect(await screen.findByTestId('start-dialog')).toBeInTheDocument()
})
