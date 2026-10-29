import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { uuid8 } from '@/api/workspaces'
import { clickOn, prepareByClick, useFakeHost, workspace, show } from '@/test/hostHarness'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const B = 'b'.repeat(24) + 'c0ffee00'
const C = 'c'.repeat(24) + 'd1ce0000'
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

it('switching workspace with a One click review open cancels that request at the host', async () => {
  clickOn(state.host.workspaces[0])
  const user = userEvent.setup()
  show(`/w/DEMO/settings/members?ws=${uuid8(B)}`)
  await screen.findByRole('list', { name: 'People' }, T)
  // Go to the other workspace through the switcher, so that Back leads to this one.
  await user.click(screen.getByRole('button', { name: 'Switch workspace' }))
  await user.click(await screen.findByRole('button', { name: new RegExp(`Billing \\(${uuid8(A)}\\)`) }))
  await waitFor(() => expect(location.search).toBe(`?ws=${uuid8(A)}`))
  await user.click(await screen.findByRole('button', { name: 'Remove Tom' }, T))
  const dialog = await screen.findByRole('dialog', {}, T)
  await prepareByClick(dialog, /Remove/)
  expect(state.host.clickPrepares().length).toBe(1)
  expect(state.host.clickPrepares()[0].path).toBe(`/api/w/${A}/click/prepare`)
  expect(state.host.clickCancelled).toEqual([])
  act(() => history.back()) // another workspace, by the address (Back, a pasted link)
  await waitFor(() => expect(location.search).toBe(`?ws=${uuid8(B)}`), T)
  await waitFor(() => expect(state.host.clickCancelled.length).toBe(1), T)
  expect(state.host.clickCancelled[0].startsWith(`${A}:hr_`)).toBe(true) // cancelled in the workspace it was prepared in
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), T)
  expect(state.host.clickSigned).toEqual([])
  expect(state.host.posts()).toEqual([]) // no terminal hand-off
})

/** Remove Tom in workspace B with One click: the signer's review, with its workspace line. */
const reviewWorkspace = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(screen.getByRole('button', { name: 'Remove Tom' }))
  const panel = await prepareByClick(await screen.findByRole('dialog', {}, T), /Remove/)
  return within(panel).getByTestId('click-workspace')
}

it('the trigger and the review say which workspace when the prefix is shared', async () => {
  clickOn(state.host.workspaces[1])
  const user = userEvent.setup()
  show(`/w/DEMO/settings/members?ws=${uuid8(B)}`)
  await screen.findByRole('list', { name: 'People' }, T)
  expect(document.querySelector('[data-ws-uuid-current]')).toHaveTextContent(uuid8(B))
  const line = await reviewWorkspace(user)
  expect([...line.querySelectorAll(':scope > bdi')].map((b) => b.textContent)).toEqual(['DEMO', 'Mobile'])
  expect(line.querySelector('[data-ws-id]')?.textContent).toBe(`id ${B}`)
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

it('an older link with the FIRST 8 characters of the id names nothing: it asks which workspace', async () => {
  show('/w/DEMO/tickets?ws=bbbbbbbb'.replace('bbbbbbbb', B.slice(0, 8)))
  expect(await screen.findByRole('heading', { name: 'Which workspace?' }, T)).toBeInTheDocument()
})

it('a name with a bidi override is shown escaped and cannot fake or reorder the short id', async () => {
  const user = userEvent.setup()
  clickOn(state.host.workspaces[1])
  state.host.workspaces[1].name = 'Main\u202e (a1b2c3d4)'
  show(`/w/DEMO/settings/members?ws=${uuid8(B)}`)
  await screen.findByRole('list', { name: 'People' }, T)
  const line = await reviewWorkspace(user)
  expect(line.textContent).toContain('\\u{202e}')
  expect(line.textContent).not.toContain('\u202e')
  expect(line.querySelector('bdi')).not.toBeNull() // the name is isolated
  expect(line.querySelector('[data-ws-id]')?.textContent).toBe(`id ${B}`) // the real id, in its own element
  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
  await user.click(screen.getByRole('button', { name: 'Switch workspace' }))
  const row = await screen.findByRole('button', { name: new RegExp(`${uuid8(A)}`) })
  expect(row).toBeInTheDocument()
  const labels = screen.getAllByRole('button', { name: /^DEMO · / }).map((b) => b.getAttribute('aria-label'))
  expect(labels.some((l) => l?.includes('\\u{202e}'))).toBe(true)
  expect(labels.some((l) => l?.includes('\u202e'))).toBe(false)
})

it('a name that looks like an id comes after the real id, which sits in its own badge', async () => {
  const user = userEvent.setup()
  clickOn(state.host.workspaces[1])
  state.host.workspaces[1].name = `Main id ${uuid8(A)}`
  show(`/w/DEMO/settings/members?ws=${uuid8(B)}`)
  await screen.findByRole('list', { name: 'People' }, T)
  const line = await reviewWorkspace(user)
  expect(line.textContent?.indexOf(`id ${B}`)).toBeLessThan(line.textContent!.indexOf('Main'))
  expect(line.querySelector('[data-ws-id]')?.textContent).toBe(`id ${B}`)
})

it('default-ignorable and private-use characters in a name are shown by number in the trigger', async () => {
  state.host.workspaces[1].name = 'Mob\u{2800}ile\ue000'
  show(`/w/DEMO/tickets?ws=${uuid8(B)}`)
  const trigger = await screen.findByRole('button', { name: 'Switch workspace' }, T)
  await waitFor(() => expect(trigger.textContent).toContain('\\u{2800}'))
  expect(trigger.textContent).toContain('\\u{e000}')
})

it('a workspace with an unsafe name says so, not "Host not running"', async () => {
  const user = userEvent.setup()
  state.host.workspaces.push(workspace({ id: C, host_state: 'workspace.name_invalid' }))
  show(`/w/DEMO/tickets?ws=${uuid8(A)}`)
  await screen.findByRole('button', { name: 'Switch workspace' }, T)
  await user.click(screen.getByRole('button', { name: 'Switch workspace' }))
  const row = (await screen.findByText("This workspace's name can't be shown safely")).closest('[data-ws-stopped]')
  expect(row).toHaveAttribute('data-ws-stopped', C)
  expect(screen.queryByText('Host not running')).not.toBeInTheDocument()
})

it('an address for a workspace the host cannot serve shows the host\'s reason, not "No workspace"', async () => {
  const note = 'this folder has an orch store from an older version in the project root; orch now keeps its files in .orch/. Delete config.json and run orch setup again'
  state.host.workspaces.push(workspace({ id: C, prefix: 'OLD', host_state: 'workspace.unavailable', host_message: note }))
  show('/w/OLD/tickets')
  expect(await screen.findByRole('heading', { name: "This workspace can't be opened" }, T)).toBeInTheDocument()
  expect(screen.getByText(note)).toBeInTheDocument()
  expect(screen.queryByText(/No workspace/)).not.toBeInTheDocument()
  expect(gets(C)).toEqual([])
})

it('#951: when no host serves the dashboard because it is not granted, the home page says how to switch it on', async () => {
  state.host.workspaces.splice(0, state.host.workspaces.length, workspace({ id: A, host_state: 'addon.disabled' }))
  show('/')
  expect(await screen.findByRole('heading', { name: 'Getting your workspace ready…' }, T)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument() // r9-P09: it rechecks by itself; Try again only after it gives up
  expect(screen.queryByText(/Could not load/)).not.toBeInTheDocument()
  expect(gets(A)).toEqual([]) // nothing is read from it
})

it('#951: a running workspace still wins over one that is not granted', async () => {
  state.host.workspaces.push(workspace({ id: C, host_state: 'addon.disabled' }))
  show(`/w/DEMO/tickets?ws=${uuid8(A)}`)
  expect(await screen.findByText('Real P1 ticket', {}, T)).toBeInTheDocument()
  expect(screen.queryByText('Getting your workspace ready…')).not.toBeInTheDocument()
})
