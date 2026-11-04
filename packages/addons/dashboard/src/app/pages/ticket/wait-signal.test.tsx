import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { agentInWait } from '@/app/attention'
import type { TicketDocument } from '@/api/types'
import { TicketProgressLive } from './Header'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), isRealHost: () => true, hasFeature: () => true }))
vi.mock('@tanstack/react-router', async (orig) => ({ ...(await orig<typeof import('@tanstack/react-router')>()), Link: ({ children }: { children?: unknown }) => <a href="#">{children as never}</a> }))
vi.mock('@/app/terminal/real/StartAgentDialog', async (orig) => ({ ...(await orig<typeof import('@/app/terminal/real/StartAgentDialog')>()), StartAgentDialog: () => <div data-testid="start-dialog" /> }))
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspace: { id: 'w1', addons: {}, members: [] }, workspaces: [{ id: 'w1', prefix: 'PW' }] }) }))

// Run-10 J04: the agent blocks in `orch wait`; Claude Code ticks "Running orch wait · 3m", so its terminal is never
// quiet. The host's own "agent waits" record (terminal_wait, source "wait") is THE idle signal for the ticket page.
const inWait = { kind: 'terminal_wait', ticket: 'PW-0002', ref: 'run_b', source: 'wait' }

async function show(opts: { missing: string[]; needs: unknown[]; role?: 'owner' | 'viewer' }) {
  const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!) as TicketDocument
  Object.assign(doc, { key: 'PW-0002', status: 'in-progress', questions_state: [] })
  doc.claim = { ...doc.claim!, live: true }
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', approvals: [], missing: opts.missing }
  const viewer = { person: 'nobody', role: opts.role ?? 'owner', members: [], name: (id: string | null | undefined) => String(id), ready: true }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['runs', 'w1'], { runs: [{ ticket: 'PW-0002', kind: 'agent', state: 'running', started_at: new Date().toISOString() }], runner: null, truncated: false })
  client.setQueryData(['today', 'w1'], { needs_you: opts.needs, read_only_open: [], waiting_on_others: { count: 0, people: [] } })
  render(
    <QueryClientProvider client={client}>
      <TicketProgressLive ticket={doc} viewer={viewer as never} sign={() => {}} jump={() => {}} />
    </QueryClientProvider>,
  )
}

it('agentInWait reads the host source of this ticket only', () => {
  expect(agentInWait([inWait], 'PW-0002')).toBe(true)
  expect(agentInWait([{ ...inWait, source: 'quiet' }], 'PW-0002')).toBe(false)
  expect(agentInWait([inWait], 'PW-0003')).toBe(false)
})

it('an agent inside orch wait with items missing shows the not-finished card, not "Working on it"', async () => {
  await show({ missing: ['out_of_scope'], needs: [inWait] })
  expect(await screen.findByText('The requirements are not finished yet')).toBeInTheDocument()
  expect(screen.queryByText('Agent is working on it')).toBeNull()
  expect(screen.queryByTestId('activity-line')).toBeNull()
})

it('without the wait signal the same busy agent still reads as working (the fallback is terminal silence)', async () => {
  await show({ missing: ['out_of_scope'], needs: [] })
  expect(await screen.findByTestId('activity-line')).toBeInTheDocument()
  expect(screen.queryByText('The requirements are not finished yet')).toBeNull()
})

it('a complete gate shows the approve card while the agent waits', async () => {
  await show({ missing: [], needs: [inWait] })
  expect(await screen.findByRole('button', { name: 'Approve requirements' })).toBeInTheDocument()
  expect(screen.queryByTestId('activity-line')).toBeNull()
})

it('someone who cannot approve sees that a decision is due, not "Working on it" and not "Open terminal"', async () => {
  await show({ missing: [], needs: [inWait], role: 'viewer' })
  expect(await screen.findByText('Waiting for a decision')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Open terminal' })).toBeNull()
  expect(screen.queryByTestId('activity-line')).toBeNull()
})
