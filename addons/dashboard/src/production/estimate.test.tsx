import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import board from '@/test/fixtures/server-m2/board-page.json'
import ticket from '@/test/fixtures/server-m2/ticket.json'
import { DO, READS, show, useFakeHost, workspace } from '@/test/hostHarness'

// The Estimate add-on on a real host: points are the ticket's size, shown on cards, columns, epic lanes, the list and
// the ticket; a change is one ordinary signed size change; with the add-on off none of it is drawn.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const ID = 'a'.repeat(32)
const T = { timeout: 5000 }
const row = (key: string, over: Record<string, unknown> = {}) => ({ ...board.rows[0], key, uid: `U-${key}`, title: `Title ${key}`, parent: null, size: null, status: 'open', ...over })
const rows = [
  row('DEMO-0010', { type: 'epic', title: 'The epic' }),
  row('DEMO-0011', { parent: 'DEMO-0010', size: 'm' }), // 3
  row('DEMO-0012', { parent: 'DEMO-0010', size: 'xl' }), // 8
  row('DEMO-0013', { parent: 'DEMO-0010' }), // not estimated
  row('DEMO-0014', { parent: 'DEMO-0010', size: 'xs', status: 'closed' }), // won't do: not counted
  row('DEMO-0020', { size: 's' }), // 2, no epic
]
const state = useFakeHost(() => [workspace({ id: ID, features: [...READS, ...DO, 'actions', 'estimate'] })])
const serve = (extra: Record<string, unknown> = {}) => {
  state.host.override = (path, method) => {
    if (method !== 'GET') return undefined
    // the fake host hands over the path without its query: the only `/tickets` read on these pages is an epic's children
    if (path.endsWith('/board')) return Response.json({ ...board, rows, next_page: null })
    if (path.endsWith('/tickets')) return Response.json({ ...board, rows: rows.filter((r) => r.parent === 'DEMO-0010'), next_page: null })
    if (path.endsWith('/tickets/DEMO-0001')) return Response.json({ ...ticket, ...extra })
    return undefined
  }
}
afterEach(() => {
  state.host.override = undefined
})

it('the board shows points on cards, a sum per column and an epic lane that says how many are not estimated', async () => {
  serve()
  show('/w/DEMO/board')
  const card = await screen.findByTestId('card-DEMO-0011', {}, T)
  expect(within(card).getByTestId('estimate-chip')).toHaveTextContent('3')
  expect(within(await screen.findByTestId('card-DEMO-0012')).getByTestId('estimate-chip')).toHaveTextContent('8')
  expect(within(screen.getByTestId('card-DEMO-0013')).queryByTestId('estimate-chip')).toBeNull()
  // the epic lane: 3 + 8, the third child has none, the closed one is a plain card in the lane but still a child here
  const lane = (await screen.findByRole('button', { name: /Collapse DEMO-0010/ })).closest('div')!.parentElement!
  await waitFor(() => expect(within(lane).getAllByTestId('estimate-sum')[0]).toHaveTextContent(/\d+ pts/))
  expect(lane).toHaveTextContent('not estimated')
})

it('with the add-on off there are no points on the board', async () => {
  state.host.workspaces[0].features = [...READS, ...DO, 'actions']
  serve({ size: 'm' })
  show('/w/DEMO/board')
  await screen.findByTestId('card-DEMO-0011', {}, T)
  expect(screen.queryByTestId('estimate-chip')).toBeNull()
  expect(screen.queryByTestId('estimate-sum')).toBeNull()
})

it('with the add-on off the ticket has no Estimate box', async () => {
  state.host.workspaces[0].features = [...READS, ...DO, 'actions']
  serve({ size: 'm' })
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  await screen.findByRole('complementary', { name: 'Ticket details' }, T)
  expect(screen.queryByTestId('ticket-estimate')).toBeNull()
})

it('the ticket shows its points and picking another one asks for one signed size change', async () => {
  serve({ size: 'm' })
  window.innerWidth = 1400
  const user = userEvent.setup()
  show('/w/DEMO/ticket/DEMO-0001')
  const box = await screen.findByTestId('ticket-estimate', {}, T)
  expect(within(box).getByTestId('estimate-current')).toHaveTextContent('3 points')
  expect(within(box).getByRole('button', { name: '3 points' })).toBeDisabled() // already this one
  await user.click(within(box).getByRole('button', { name: '5 points' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(dialog).toHaveTextContent('Estimate DEMO-0001')
  expect(dialog).toHaveTextContent('5 points')
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'details', key: 'DEMO-0001', size: 'l', via: 'estimate' }])
})

it('an unestimated ticket offers the scale and no "not estimated" button; clearing is offered once it has points', async () => {
  serve({ size: null })
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  const box = await screen.findByTestId('ticket-estimate', {}, T)
  expect(box).toHaveTextContent('Not estimated yet')
  expect(within(box).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual(['1 point', '2 points', '3 points', '5 points', '8 points'])
})

it('an epic shows the sum of its children and how many have no estimate, never a number to set', async () => {
  serve({ type: 'epic' })
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  const box = await screen.findByTestId('ticket-estimate', {}, T)
  const sum = await within(box).findByTestId('estimate-rollup', {}, T)
  expect(sum).toHaveTextContent('11 points in this epic')
  expect(sum).toHaveTextContent('1 not estimated')
  expect(within(box).queryByRole('group', { name: 'Set the estimate' })).toBeNull()
})
