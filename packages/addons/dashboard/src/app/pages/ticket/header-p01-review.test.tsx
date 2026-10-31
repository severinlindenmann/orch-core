// P01 review: the integrated header for each live/dead run, claim, wait and failed-mark combination, and what it offers.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { TicketDocument } from '@/api/types'
import { clearMark, failMark } from './progress'
import { TicketHeader } from './Header'

const openDockOn = vi.fn()
vi.mock('@/app/terminal/dock/request', () => ({ openDockOn: (r: string) => openDockOn(r) }))
vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), isRealHost: () => true, hasFeature: () => true }))
vi.mock('@tanstack/react-router', async (orig) => ({ ...(await orig<typeof import('@tanstack/react-router')>()), Link: ({ children }: { children?: unknown }) => <a href="#">{children as never}</a> }))
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspace: { id: 'w1', addons: {}, members: [] }, workspaces: [{ id: 'w1', prefix: 'PW' }] }) }))

const ago = (ms: number) => new Date(Date.now() - ms).toISOString()

async function header(o: { run?: { state: string; ms: number } | null; wait?: boolean; failed?: boolean; claim?: Record<string, unknown> | null }) {
  const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!)
  Object.assign(doc, { key: 'PW-0002', status: 'open', claim: o.claim ?? null, questions_state: [] })
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', approvals: [], missing: ['context'] }
  const viewer = { person: 'nobody', role: 'owner' as const, members: [], name: (id: string | null | undefined) => String(id), ready: true }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const runs = o.run ? [{ run: 'run_1', ticket: 'PW-0002', kind: 'agent', state: o.run.state, started_at: ago(o.run.ms) }] : []
  client.setQueryData(['runs', 'w1'], { runs, runner: null, truncated: false })
  client.setQueryData(['today', 'w1'], { needs_you: o.wait ? [{ kind: 'terminal_wait', ticket: 'PW-0002', ref: 'run_1', source: 'hook' }] : [], read_only_open: [], waiting_on_others: { count: 0, people: [] } })
  if (o.failed) failMark('w1', 'PW-0002', 'The agent was not started.')
  render(
    <QueryClientProvider client={client}>
      <TicketHeader ticket={doc as TicketDocument} viewer={viewer as never} sign={() => {}} jump={() => {}} />
    </QueryClientProvider>,
  )
}
const startItemDisabled = async () => {
  await userEvent.click(screen.getByRole('button', { name: /Actions/ }))
  const item = await screen.findByTestId('real-start-agent')
  return item.getAttribute('aria-disabled') === 'true' || item.hasAttribute('data-disabled')
}

afterEach(() => {
  clearMark('w1', 'PW-0002')
  openDockOn.mockClear()
})

describe('header actions per run state (P01 review)', () => {
  it('a live run beats an old failed mark: no failure, no Start agent, menu item off', async () => {
    await header({ run: { state: 'running', ms: 5000 }, failed: true })
    expect(await screen.findByText('Getting started…')).toBeInTheDocument()
    expect(screen.queryByText('The agent was not started.')).toBeNull()
    expect(screen.queryByRole('button', { name: /Start agent/ })).toBeNull()
    expect(await startItemDisabled()).toBe(true)
  })
  it('a dead run with a stale wait shows failed with Start agent, not Open agent', async () => {
    await header({ run: { state: 'exited', ms: 60_000 }, wait: true })
    expect(await screen.findByText('The agent stopped before it began. Start it again.')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Start agent/ }).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Open agent' })).toBeNull()
  })
  it('a live run with no claim after the start window reads "Agent is open" with Open agent, no Try again, no Start', async () => {
    await header({ run: { state: 'running', ms: 5 * 60_000 } })
    expect(await screen.findByText('Agent is open')).toBeInTheDocument()
    expect(screen.queryByText(/Taking longer/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Start agent/ })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Open agent' }))
    expect(openDockOn).toHaveBeenCalledWith('run_1')
    expect(await startItemDisabled()).toBe(true)
  })
  it('a live run past 90 s but inside the window says it takes longer, without Try again', async () => {
    await header({ run: { state: 'running', ms: 120_000 } })
    expect(await screen.findByText('Taking longer than usual')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })
  it('a live run whose claim ended is idle (history), not "Taking longer"', async () => {
    await header({ run: { state: 'running', ms: 5 * 60_000 }, claim: { agent: 'claude-code', session: 's', for: 'u', since: ago(3_600_000), expires: '', live: false } })
    expect(screen.queryByText(/Taking longer|Agent is open/)).toBeNull()
  })
  it('waiting: Needs you, Agent is waiting for you, Open agent, no Start agent, menu item off', async () => {
    await header({ run: { state: 'running', ms: 60_000 }, wait: true })
    expect(await screen.findByText('Agent is waiting for you')).toBeInTheDocument()
    expect(screen.getByText('Needs you')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Start agent/ })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Open agent' }))
    expect(openDockOn).toHaveBeenCalledWith('run_1')
    expect(await startItemDisabled()).toBe(true)
  })
})
