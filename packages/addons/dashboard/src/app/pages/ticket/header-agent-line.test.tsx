// Run17full P06: when the header shows the agent line (data-testid=agent-status) and when it does not.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { TicketDocument } from '@/api/types'
import { TicketHeader } from './Header'

vi.mock('@/app/terminal/dock/request', () => ({ openDockOn: vi.fn() }))
vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), isRealHost: () => true, hasFeature: () => true }))
vi.mock('@tanstack/react-router', async (orig) => ({ ...(await orig<typeof import('@tanstack/react-router')>()), Link: ({ children }: { children?: unknown }) => <a href="#">{children as never}</a> }))
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspace: { id: 'w1', addons: {}, members: [] }, workspaces: [{ id: 'w1', prefix: 'PW' }] }) }))

const CLOSED = { resolution: 'wont_do', duplicate_of: null, at: '2026-10-08T09:00:00Z', by: 'p_sev' } as const

async function header(o: { patch: Partial<TicketDocument>; claim?: 'live' | null; wait?: boolean; question?: boolean }) {
  const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!) as TicketDocument
  Object.assign(doc, { key: 'PW-0002', questions_state: [] }, o.patch)
  doc.claim = o.claim === 'live' ? { ...doc.claim!, live: true } : null
  if (o.question) doc.questions_state = [{ id: 'Q1', text: 'Which language?', state: 'open', asked_by: 'agent', options: [] }] as never
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', approvals: [], missing: ['context'] }
  const viewer = { person: 'nobody', role: 'owner' as const, members: [], name: (id: string | null | undefined) => String(id), ready: true }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['runs', 'w1'], { runs: [{ run: 'run_1', ticket: 'PW-0002', kind: 'agent', state: 'running', started_at: new Date(Date.now() - 5000).toISOString() }], runner: null, truncated: false })
  client.setQueryData(['today', 'w1'], { needs_you: o.wait ? [{ kind: 'terminal_wait', ticket: 'PW-0002', ref: 'run_1', source: 'wait' }] : [], read_only_open: [], waiting_on_others: { count: 0, people: [] } })
  render(
    <QueryClientProvider client={client}>
      <TicketHeader ticket={doc} viewer={viewer as never} sign={() => {}} jump={() => {}} />
    </QueryClientProvider>,
  )
  await screen.findByTestId('ticket-header')
}

describe('the agent line in the ticket header (run17 P06)', () => {
  it.each([
    ['status done', { status: 'done' }],
    ['status closed', { status: 'closed' }],
    ['a closed reason set', { status: 'open', closed: CLOSED }],
  ] as const)('a finished ticket (%s) with a live agent run shows no agent line, and never "is starting"', async (_name, patch) => {
    await header({ patch: patch as Partial<TicketDocument>, claim: null })
    expect(screen.queryByTestId('agent-status')).toBeNull()
    expect(screen.queryByText(/is starting/)).toBeNull()
  })

  it('a done ticket whose claim is still live shows no agent line either', async () => {
    await header({ patch: { status: 'done' }, claim: 'live' })
    expect(screen.queryByTestId('agent-status')).toBeNull()
  })

  it('a ticket that waits for the person (open question), the agent not idle at its prompt, shows no agent line', async () => {
    await header({ patch: { status: 'in-progress' }, claim: 'live', question: true })
    expect(await screen.findByTestId('progress-pill')).toHaveTextContent('Needs you')
    expect(screen.queryByTestId('agent-status')).toBeNull()
  })

  it('a ticket needing the person with the agent idle at its prompt keeps the line, without "since"', async () => {
    await header({ patch: { status: 'in-progress' }, claim: 'live', wait: true })
    const line = await screen.findByTestId('agent-status')
    expect(line).toHaveTextContent('is waiting for you')
    expect(line).not.toHaveTextContent('since')
  })

  it('claimed and idle at its prompt: exactly one "Open agent", on the waiting line', async () => {
    await header({ patch: { status: 'in-progress' }, claim: 'live', wait: true })
    const line = await screen.findByTestId('agent-status')
    expect(within(line).getByRole('button', { name: 'Open agent' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Open agent' })).toHaveLength(1)
  })

  it('unclaimed and idle at its prompt: exactly one "Open agent", next to the Needs you label, no agent line', async () => {
    await header({ patch: { status: 'in-progress' }, claim: null, wait: true })
    expect(await screen.findByTestId('progress-pill')).toHaveTextContent('Needs you')
    expect(screen.queryByTestId('agent-status')).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Open agent' })).toHaveLength(1)
  })

  it('a working agent keeps the line, with "since"', async () => {
    await header({ patch: { status: 'in-progress' }, claim: 'live' })
    const line = await screen.findByTestId('agent-status')
    expect(line).toHaveTextContent('is working for')
    expect(line).toHaveTextContent('since')
  })
})
