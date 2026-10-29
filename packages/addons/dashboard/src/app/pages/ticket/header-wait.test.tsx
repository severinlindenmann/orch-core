import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { terminalWaitFor } from '@/app/attention'
import type { TicketDocument } from '@/api/types'
import { runsApi } from '@/api/runs'
import { phaseOf } from './progress'
import { TicketHeader, TicketProgressLive } from './Header'
import userEvent from '@testing-library/user-event'
import { act } from '@testing-library/react'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), isRealHost: () => true, hasFeature: () => true }))
vi.mock('@tanstack/react-router', async (orig) => ({ ...(await orig<typeof import('@tanstack/react-router')>()), Link: ({ children }: { children?: unknown }) => <a href="#">{children as never}</a> }))
vi.mock('@/app/terminal/real/StartAgentDialog', async (orig) => ({ ...(await orig<typeof import('@/app/terminal/real/StartAgentDialog')>()), StartAgentDialog: ({ initial }: { initial?: { mode?: string; stopFirst?: string } }) => <div data-testid="start-dialog" data-mode={initial?.mode} data-stop-first={initial?.stopFirst} /> }))
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
  await screen.findByText('The requirements are not finished yet')
}

it('a viewer who cannot start an agent sees no "Ask the agent to finish"', async () => {
  await stoppedWith('viewer')
  expect(screen.queryByRole('button', { name: 'Ask the agent to finish' })).toBeNull()
})

it('Ask the agent to finish opens the start flow for this ticket only, in continue mode; no "Agent is working" next to the card', async () => {
  await stoppedWith('owner')
  act(() => {
    window.dispatchEvent(new CustomEvent('orch:start-agent', { detail: { ticket: 'PW-0009', mode: 'continue' } }))
  })
  expect(screen.queryByTestId('start-dialog')).toBeNull()
  expect(screen.queryByTestId('agent-working-note')).toBeNull()
  await userEvent.setup().click(screen.getByRole('button', { name: 'Ask the agent to finish' }))
  expect(await screen.findByTestId('start-dialog')).toBeInTheDocument()
})

it('no "Agent is working…" next to the not-finished card, even with a live claim', async () => {
  const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!)
  Object.assign(doc, { key: 'PW-0002', status: 'in-progress', questions_state: [] })
  doc.claim = { ...doc.claim!, live: true }
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', approvals: [], missing: ['context'] }
  const viewer = { person: 'nobody', role: 'owner' as const, members: [], name: (id: string | null | undefined) => String(id), ready: true }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['runs', 'w1'], { runs: [{ ticket: 'PW-0002', kind: 'agent', state: 'running', started_at: new Date().toISOString() }], runner: null, truncated: false })
  client.setQueryData(['today', 'w1'], { needs_you: [{ kind: 'terminal_wait', ticket: 'PW-0002', ref: 'run_b' }], read_only_open: [], waiting_on_others: { count: 0, people: [] } })
  render(
    <QueryClientProvider client={client}>
      <TicketHeader ticket={doc} viewer={viewer as never} sign={() => {}} jump={() => {}} />
      <TicketProgressLive ticket={doc} viewer={viewer as never} sign={() => {}} jump={() => {}} />
    </QueryClientProvider>,
  )
  await screen.findByText('The requirements are not finished yet')
  expect(screen.queryByTestId('agent-working-note')).toBeNull()
  expect(screen.queryByText('Agent is working…')).toBeNull()
  // Ask the agent to finish: the idle run is stopped first (the existing run stop), then the start flow opens.
  const stop = vi.spyOn(runsApi, 'stop').mockResolvedValue({ state: 'exited', cleanup: 'complete' } as never)
  await userEvent.setup().click(screen.getByRole('button', { name: 'Ask the agent to finish' }))
  // The dialog is open with the stop still pending: cancelling leaves the run untouched.
  const dialog = await screen.findByTestId('start-dialog')
  expect(dialog).toHaveAttribute('data-stop-first', 'run_b')
  expect(dialog).toHaveAttribute('data-mode', 'continue')
  expect(stop).not.toHaveBeenCalled()
})
