import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { TicketDocument } from '@/api/types'
import { runsApi } from '@/api/runs'
import { api } from '@/api/client'
import { TicketHeader } from './Header'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), isRealHost: () => true, hasFeature: () => true }))
vi.mock('@tanstack/react-router', async (orig) => ({ ...(await orig<typeof import('@tanstack/react-router')>()), Link: ({ children }: { children?: unknown }) => <a href="#">{children as never}</a> }))
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspace: { id: 'w1', addons: {}, members: [] }, workspaces: [{ id: 'w1', prefix: 'PW' }] }) }))

// W32: on a real host nothing pushes "the agent stopped working" (the wait mark and a run's end have no live event for
// this page), so the live phase must read the host again by itself, bounded to the time the page shows a live phase.
beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }))
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

const viewer = { person: 'nobody', role: 'viewer' as const, members: [], name: (id: string | null | undefined) => String(id), ready: true }
const runOf = (state: 'running' | 'exited') => ({ runs: [{ run: 'run_a', ticket: 'PW-0002', kind: 'agent', state, started_at: new Date(Date.now() - 600_000).toISOString() }], runner: null, truncated: false })
const todayOf = (needs_you: unknown[]) => ({ needs_you, read_only_open: [], waiting_on_others: { count: 0, people: [] } })

async function setup(ticketPatch: Record<string, unknown> = {}) {
  const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!) as TicketDocument
  Object.assign(doc, { key: 'PW-0002', status: 'open', claim: { by: 'agent', live: true }, questions_state: [], ...ticketPatch })
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', approvals: [], missing: ['context'] }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['runs', 'w1'], runOf('running'))
  client.setQueryData(['today', 'w1'], todayOf([]))
  render(
    <QueryClientProvider client={client}>
      <TicketHeader ticket={doc} viewer={viewer as never} sign={() => {}} jump={() => {}} />
    </QueryClientProvider>,
  )
  expect(await screen.findByText(/Working on it/)).toBeInTheDocument()
  return client
}

it('the agent ends its turn and waits: the host marks the wait, the pill leaves "Working on it" without a reload', async () => {
  vi.spyOn(runsApi, 'list').mockResolvedValue(runOf('running') as never)
  vi.spyOn(api, 'getToday').mockResolvedValue(todayOf([{ kind: 'terminal_wait', ticket: 'PW-0002', ref: 'run_a' }]) as never)
  await setup()
  await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
  expect(await screen.findByText('Needs you')).toBeInTheDocument()
  expect(screen.queryByText(/Working on it/)).toBeNull()
})

it('the run ends (Stop in the dock / exit): the run list and the ticket are read again, so the claim and the pill follow', async () => {
  const list = vi.spyOn(runsApi, 'list').mockResolvedValue(runOf('exited') as never)
  vi.spyOn(api, 'getToday').mockResolvedValue(todayOf([]) as never)
  const client = await setup()
  client.setQueryData(['ticket', 'w1', 'PW-0002'], { key: 'PW-0002' })
  await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
  expect(list).toHaveBeenCalled()
  expect(client.getQueryState(['ticket', 'w1', 'PW-0002'])?.isInvalidated).toBe(true)
})

it('no poll while the page shows no live phase', async () => {
  const list = vi.spyOn(runsApi, 'list').mockResolvedValue(runOf('exited') as never)
  vi.spyOn(api, 'getToday').mockResolvedValue(todayOf([]) as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['runs', 'w1'], runOf('exited'))
  client.setQueryData(['today', 'w1'], todayOf([]))
  const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!) as TicketDocument
  Object.assign(doc, { key: 'PW-0002', status: 'open', claim: null, questions_state: [] })
  render(<QueryClientProvider client={client}><TicketHeader ticket={doc} viewer={viewer as never} sign={() => {}} jump={() => {}} /></QueryClientProvider>)
  await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
  expect(list).not.toHaveBeenCalled()
})
