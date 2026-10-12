import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { uuid8 } from '@/api/workspaces'
import { useFakeHost, workspace, show } from '@/test/hostHarness'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const B = 'c0ffee00' + 'b'.repeat(24)
const C = 'd1ce0000' + 'c'.repeat(24)
const T = { timeout: 5000 }
const state = useFakeHost(() => [
  workspace({ id: A, name: 'Billing', title: 'Billing export' }),
  workspace({ id: B, name: 'Mobile', title: 'Mobile sign-in' }),
])
const gets = (id: string) => state.host.calls.filter((c) => c.method === 'GET' && c.path.startsWith(`/api/w/${id}/`))

it('two workspaces with one prefix are two rows with their own short ids, keyed by UUID', async () => {
  const user = userEvent.setup()
  show(`/w/DEMO/tickets?ws=${uuid8(A)}`)
  await screen.findByRole('button', { name: 'Switch workspace' }, T)
  await user.click(screen.getByRole('button', { name: 'Switch workspace' }))
  const rows = await screen.findAllByRole('button', { name: /^DEMO · / })
  expect(rows.map((r) => r.getAttribute('aria-label'))).toEqual([expect.stringContaining(`DEMO · Billing (${uuid8(A)})`), expect.stringContaining(`DEMO · Mobile (${uuid8(B)})`)])
  expect(rows[0]).toHaveAttribute('aria-current', 'true')
})

it('an address with the prefix alone asks which workspace, and shows nothing of either', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/tickets')
  expect(await screen.findByRole('heading', { name: 'Which workspace?' }, T)).toBeInTheDocument()
  expect(screen.queryByText('Real P1 ticket')).not.toBeInTheDocument()
  expect(gets(A).filter((c) => c.path.includes('/tickets')).length + gets(B).filter((c) => c.path.includes('/tickets')).length).toBe(0)
  await user.click(screen.getByRole('button', { name: /Mobile/ }))
  await waitFor(() => expect(location.search).toBe(`?ws=${uuid8(B)}`))
  expect(location.pathname).toBe('/w/DEMO/tickets')
  await waitFor(() => expect(gets(B).some((c) => c.path.includes('/tickets'))).toBe(true))
  expect(gets(A).some((c) => c.path.includes('/tickets'))).toBe(false)
})

it('the same ticket key opens each workspace\'s own ticket, chosen by ?ws=', async () => {
  const first = show(`/w/DEMO/ticket/DEMO-0001?ws=${uuid8(A)}`)
  expect(await screen.findByText('Billing export', {}, T)).toBeInTheDocument()
  expect(gets(A).some((c) => c.path === `/api/w/${A}/tickets/DEMO-0001`)).toBe(true)
  expect(gets(B).some((c) => c.path === `/api/w/${B}/tickets/DEMO-0001`)).toBe(false)
  first.unmount()
  show(`/w/DEMO/ticket/DEMO-0001?ws=${uuid8(B)}`)
  expect(await screen.findByText('Mobile sign-in', {}, T)).toBeInTheDocument()
  expect(gets(B).some((c) => c.path === `/api/w/${B}/tickets/DEMO-0001`)).toBe(true)
  expect(screen.queryByText('Billing export')).not.toBeInTheDocument()
})

it('links the app makes carry the hint of the workspace the person is in, and only that', async () => {
  show(`/w/DEMO/tickets?ws=${uuid8(B)}`)
  await screen.findByText('Real P1 ticket', {}, T)
  const board = within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Board' })
  expect(board.getAttribute('href')).toBe(`/w/DEMO/board?ws=${uuid8(B)}`)
  const row = screen.getAllByRole('link').find((a) => a.getAttribute('href')?.includes('/ticket/DEMO-0001'))
  expect(row?.getAttribute('href')).toBe(`/w/DEMO/ticket/DEMO-0001?ws=${uuid8(B)}`)
})

it('a hint that names no workspace of the viewer is not guessed: it asks which', async () => {
  show('/w/DEMO/tickets?ws=deadbeef')
  expect(await screen.findByRole('heading', { name: 'Which workspace?' }, T)).toBeInTheDocument()
})

it('switching workspace keeps the page and moves the address to the other workspace\'s hint', async () => {
  const user = userEvent.setup()
  show(`/w/DEMO/tickets?ws=${uuid8(A)}`)
  await screen.findByText('Real P1 ticket', {}, T)
  await user.click(screen.getByRole('button', { name: 'Switch workspace' }))
  await user.click(await screen.findByRole('button', { name: new RegExp(`Mobile \\(${uuid8(B)}\\)`) }))
  await waitFor(() => expect(location.search).toBe(`?ws=${uuid8(B)}`))
  expect(location.pathname).toBe('/w/DEMO/tickets')
  await waitFor(() => expect(gets(B).some((c) => c.path.includes('/tickets'))).toBe(true))
})

it('a workspace whose host is not running is listed as such and the others keep working', async () => {
  const user = userEvent.setup()
  state.host.workspaces.push(workspace({ id: C, host_state: 'host.unavailable' }))
  show(`/w/DEMO/tickets?ws=${uuid8(A)}`)
  expect(await screen.findByText('Real P1 ticket', {}, T)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Switch workspace' }))
  const stopped = await screen.findByText('Host not running')
  expect(stopped.closest('[data-ws-stopped]')).toHaveAttribute('data-ws-stopped', C)
  expect(screen.queryByRole('button', { name: new RegExp(uuid8(C)) })).not.toBeInTheDocument() // not selectable
  // Nothing is read from it, and the two running workspaces are still the only rows to pick.
  expect(gets(C)).toEqual([])
  expect(screen.getAllByRole('button', { name: /^DEMO · / }).length).toBe(2)
})

it('an address that names a stopped host says so; it does not show another workspace instead', async () => {
  state.host.workspaces.push(workspace({ id: C, host_state: 'host.unavailable' }))
  show(`/w/DEMO/tickets?ws=${uuid8(C)}`)
  expect(await screen.findByRole('heading', { name: 'Host not running' }, T)).toBeInTheDocument()
  expect(screen.queryByText('Real P1 ticket')).not.toBeInTheDocument()
  expect(gets(C)).toEqual([])
})

it('the workspace list keeps working when one host stops while the page is open', async () => {
  show(`/w/DEMO/tickets?ws=${uuid8(A)}`)
  await screen.findByText('Real P1 ticket', {}, T)
  state.host.workspaces[1].host_state = 'host.unavailable'
  const { queryClient } = await import('@/production/Shell')
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: ['workspaces'] })
  })
  await userEvent.setup().click(screen.getByRole('button', { name: 'Switch workspace' }))
  expect(await screen.findByText('Host not running')).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: /^DEMO · / }).length).toBe(1)
  expect(screen.getByText('Real P1 ticket')).toBeInTheDocument()
})

it('switching workspace with a hand-off panel open cancels that request at the host', async () => {
  const user = userEvent.setup()
  show(`/w/DEMO/settings/members?ws=${uuid8(B)}`)
  await screen.findByRole('table', {}, T)
  // Go to the other workspace through the switcher, so that Back leads to this one.
  await user.click(screen.getByRole('button', { name: 'Switch workspace' }))
  await user.click(await screen.findByRole('button', { name: new RegExp(`Billing \\(${uuid8(A)}\\)`) }))
  await waitFor(() => expect(location.search).toBe(`?ws=${uuid8(A)}`))
  await user.click(await screen.findByRole('button', { name: 'Remove Tom' }, T))
  const dialog = await screen.findByRole('dialog', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().length).toBe(1)
  expect(state.host.deletes()).toEqual([])
  act(() => history.back()) // another workspace, by the address (Back, a pasted link)
  await waitFor(() => expect(location.search).toBe(`?ws=${uuid8(B)}`), T)
  await waitFor(() => expect(state.host.cancelled.length).toBe(1), T)
  expect(state.host.cancelled[0].startsWith(`${A}:hr_`)).toBe(true) // cancelled in the workspace it was prepared in
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), T)
  expect(state.host.deletes().length).toBe(1)
})

it('the trigger and the hand-off dialog say which workspace when the prefix is shared', async () => {
  const user = userEvent.setup()
  show(`/w/DEMO/settings/members?ws=${uuid8(B)}`)
  await screen.findByRole('table', {}, T)
  expect(document.querySelector('[data-ws-uuid-current]')).toHaveTextContent(uuid8(B))
  await user.click(screen.getByRole('button', { name: 'Remove Tom' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  expect(within(dialog).getByTestId('handoff-workspace')).toHaveTextContent(`DEMO · Mobile (${uuid8(B)})`)
})

it('a ticket address with a shared prefix and no matching hint reads no ticket', async () => {
  show('/w/DEMO/ticket/DEMO-0001')
  expect(await screen.findByRole('heading', { name: 'Which workspace?' }, T)).toBeInTheDocument()
  await new Promise((r) => setTimeout(r, 300))
  expect(state.host.calls.some((c) => c.path.includes('/tickets/DEMO-0001'))).toBe(false)
})

it('a ticket address whose hint names a stopped host reads no ticket', async () => {
  state.host.workspaces.push(workspace({ id: C, host_state: 'host.unavailable' }))
  show(`/w/DEMO/ticket/DEMO-0001?ws=${uuid8(C)}`)
  expect(await screen.findByRole('heading', { name: 'Host not running' }, T)).toBeInTheDocument()
  await new Promise((r) => setTimeout(r, 300))
  expect(state.host.calls.some((c) => c.path.includes('/tickets/DEMO-0001'))).toBe(false)
})
