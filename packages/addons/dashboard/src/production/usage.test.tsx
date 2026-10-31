import { fireEvent, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import ticket from '@/test/fixtures/server-m2/ticket.json'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'

// The production app on a fake server that speaks the Usage route (add-on): time and runs, tokens and cost never invented.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const totals = (runs: number, ms: number, over: Record<string, unknown> = {}) => ({ runs, ms, running: 0, unknown: 0, ...over })
const win = (t: ReturnType<typeof totals>, tickets: unknown[], since: string | null = null) => ({ ...t, tickets, counted_since: since })
const trow = (t: ReturnType<typeof totals>, over: Record<string, unknown>) => ({ ...t, ticket: null, title: null, other: false, none: false, ...over })
let body: unknown
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'usage'] })])
const route = (data: unknown = null) => {
  body = data ?? {
    available: true,
    scope: 'your',
    windows: {
      week: win(totals(3, 5_400_000, { unknown: 1 }), [
        trow(totals(2, 3_600_000), { ticket: 'DEMO-0001', title: 'First ticket' }),
        trow(totals(1, 0, { unknown: 1 }), { ticket: 'DEMO-0002', title: null }),
      ]),
      month: win(totals(9, 36_000_000), [trow(totals(9, 36_000_000), { ticket: 'DEMO-0001', title: 'First ticket' }), trow(totals(0, 0), { other: true })], '2026-10-11T08:00:00Z'),
    },
    recent: [{ started_at: '2026-10-18T10:00:00Z', ended_at: '2026-10-18T10:30:00Z', ms: 1_800_000, running: false, harness: 'claude' }],
    tokens: null,
    cost: null,
  }
  state.host.override = (path, method) => {
    if (path.endsWith('/tickets/DEMO-0001') && method === 'GET') return Response.json(ticket)
    if (path.includes('/usage') && method === 'GET') return Response.json(body)
    return undefined
  }
}
const calls = (p: string) => state.host.calls.filter((c) => c.path.includes(p))

it('shows time and runs per ticket in words, and says tokens and cost are not reported', async () => {
  route()
  show('/w/DEMO/usage')
  const list = await screen.findByRole('list', { name: 'Time per ticket' }, T)
  const [first, second] = within(list).getAllByRole('listitem')
  expect(first).toHaveTextContent('DEMO-0001')
  expect(first).toHaveTextContent('1 h')
  expect(first).toHaveTextContent('2 runs')
  expect(second).toHaveTextContent('Time not recorded') // unknown is never "0"
  expect(second).not.toHaveTextContent(/\b0 (s|min)\b/)
  // US4: one page note instead of "Not reported" twice per tile, and "duration unavailable" instead of the long form
  expect(screen.getAllByText(/Token counts and cost aren't available\./)).toHaveLength(1)
  expect(screen.queryByTestId('usage-week-tokens')).toBeNull()
  expect(second).toHaveTextContent(/duration unavailable for \d/)
  // US2 / US3
  expect(screen.getByRole('heading', { name: 'Agent running time' })).toBeInTheDocument()
  expect(screen.getByText(/ran at the same time are each counted/)).toBeInTheDocument()
  expect(screen.getByTestId('usage-tickets-heading')).toHaveTextContent('Time by ticket — Last 7 days')
  expect(screen.getByTestId('usage-month')).toHaveTextContent('Counted since')
  expect(screen.getByTestId('usage-week')).not.toHaveTextContent('Counted since')
  expect(document.body.textContent).not.toMatch(/\$|CHF|EUR|\d tokens/)
})

it('the month tab lists other tickets without naming them', async () => {
  route()
  const user = userEvent.setup()
  show('/w/DEMO/usage')
  await screen.findByRole('list', { name: 'Time per ticket' }, T)
  await user.click(screen.getByRole('button', { name: 'Last 30 days' }))
  expect(screen.getByTestId('usage-tickets-heading')).toHaveTextContent('Time by ticket — Last 30 days')
  expect(within(screen.getByRole('list', { name: 'Time per ticket' })).getAllByRole('listitem')[1]).toHaveTextContent('Other tickets')
})

it('an empty period says so', async () => {
  route({ available: true, scope: 'your', windows: { week: win(totals(0, 0), []), month: win(totals(0, 0), []) }, recent: [], tokens: null, cost: null })
  show('/w/DEMO/usage')
  expect(await screen.findByTestId('usage-empty', {}, T)).toHaveTextContent('No agent ran in the last 7 days yet. Start an agent from a ticket on the Board')
  expect(within(screen.getByTestId('usage-empty')).getByRole('link', { name: 'Board' })).toBeInTheDocument()
  expect(screen.getByTestId('usage-week')).toHaveTextContent('None yet')
})

it('with the add-on off there is no Usage entry, no ticket box, and no call to the usage route', async () => {
  route()
  state.host.workspaces[0].features = [...READS]
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  await screen.findByRole('navigation', { name: 'Main' }, T)
  await screen.findByRole('complementary', { name: 'Ticket details' }, T)
  expect(within(screen.getByRole('navigation', { name: 'Main' })).queryByRole('link', { name: 'Usage' })).toBeNull()
  expect(screen.queryByTestId('ticket-usage')).toBeNull()
  expect(calls('/usage')).toEqual([])
})

it('the ticket page has a Usage box with the ticket’s time and recent runs behind a fold', async () => {
  route()
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  const box = await screen.findByTestId('ticket-usage', {}, T)
  expect(box).toHaveTextContent('Last 7 days')
  expect(box).toHaveTextContent('Tokens')
  expect(box).toHaveTextContent('Not reported')
  expect(within(box).getByText('Recent runs').closest('details')).not.toHaveAttribute('open')
  expect(calls('/usage')[0].path).toContain('ticket=DEMO-0001')
  // P67: this entry sits in the side rail's "More" group
  fireEvent.click(await screen.findByRole('button', { name: 'More' }))
  expect(await screen.findByRole('link', { name: 'Usage' })).toBeInTheDocument()
})
