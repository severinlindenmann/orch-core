import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import ticket from '@/test/fixtures/server-m2/ticket.json'
import { READS, clickOn, show, useFakeHost, workspace } from '@/test/hostHarness'
import { market, installedAddon, stage } from '@/test/fixtures/market'

// P05 (J07-08): Publish is easy to find while it is off. It is listed in Add-ons > Browse with Turn on, and the ticket's
// Actions menu has Share files... which opens the turn-on review first.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const row = (state: 'on' | 'off') => ({ id: 'publish', title: 'Publish', desc: 'Share files of a ticket with a link.', state, locked: null, blocked: null, needs: 'publish', set_up: true })
const state = useFakeHost(() => [
  workspace({ id: A, features: [...READS, 'actions', 'market'], addOns: { available: true, add_ons: [row('off'), { id: 'dashboard', title: 'Dashboard', desc: 'x', state: 'on', locked: 'Always on', blocked: null }] }, market: { markets: [market()] }, marketInstalled: { addons: [installedAddon()] }, marketStage: stage() } as never),
])
const ws = () => state.host.workspaces[0]
afterEach(() => vi.restoreAllMocks())

it('Browse lists the built-in add-ons that are off, each with Turn on, and not the ones that are on', async () => {
  show('/w/DEMO/addons?tab=browse')
  const list = await screen.findByRole('list', { name: 'Built-in add-ons that are off' }, T)
  expect(within(list).getByText('Publish')).toBeInTheDocument()
  expect(within(list).getByRole('button', { name: 'Turn on Publish' })).toBeEnabled()
  expect(within(list).queryByText('Dashboard')).toBeNull() // always on: nothing to turn on
  expect(within(list).queryByRole('switch')).toBeNull()
})

it('Browse shows no Built in group once everything is on', async () => {
  ws().addOns = { available: true, add_ons: [row('on')] } as never
  show('/w/DEMO/addons?tab=browse')
  await screen.findByLabelText('Search add-ons', {}, T)
  expect(screen.queryByRole('list', { name: 'Built-in add-ons that are off' })).toBeNull()
})

const withTicket = () => {
  state.host.override = (path, method) =>
    path.endsWith('/tickets/DEMO-0001') && method === 'GET'
      ? Response.json({ ...ticket, artifacts: [{ name: 'report.md', kind: 'report', bytes: 2048, sha256: 'ab'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }] })
      : undefined
}

it('the ticket Actions menu offers Share files... while Publish is off and opens the turn-on review first', async () => {
  withTicket()
  clickOn(ws())
  window.innerWidth = 1400
  const user = userEvent.setup()
  show('/w/DEMO/ticket/DEMO-0001')
  await user.click(await screen.findByRole('button', { name: /Actions/ }, T))
  await user.click(await screen.findByRole('menuitem', { name: 'Share files…' }, T))
  const dialog = await screen.findByRole('dialog', { name: 'Turn on Publish' }, T)
  expect(dialog).toHaveTextContent('Nothing leaves this computer until you sign')
  expect(within(dialog).getByRole('button', { name: 'Turn on Publish' })).toBeInTheDocument()
  expect(screen.queryByRole('dialog', { name: 'Share files' })).toBeNull()
})

it('with Publish on, Share files... opens the share dialog straight away', async () => {
  withTicket()
  ws().features = [...ws().features, 'publish']
  ws().addOns = { available: true, add_ons: [row('on')] } as never
  const inner = state.host.override!
  state.host.override = (path, method, body) => (path.includes('/shares') && method === 'GET' ? Response.json({ available: true, configured: true, server: 'http://127.0.0.1:8103', share: 'http://127.0.0.1:8104', workspace: A, owner: 'p_owner', live: true, shares: [], staged: [] }) : inner(path, method, body))
  clickOn(ws())
  window.innerWidth = 1400
  const user = userEvent.setup()
  show('/w/DEMO/ticket/DEMO-0001')
  await user.click(await screen.findByRole('button', { name: /Actions/ }, T))
  await user.click(await screen.findByRole('menuitem', { name: 'Share files…' }, T))
  const dialog = await screen.findByRole('dialog', { name: 'Share files' }, T)
  expect(within(dialog).getByRole('button', { name: 'Add a file' })).toBeInTheDocument()
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Turn on Publish' })).toBeNull(), T)
})
