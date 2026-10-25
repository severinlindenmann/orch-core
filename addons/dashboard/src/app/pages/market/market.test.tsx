import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'
import { market, stage, installedAddon } from '@/test/fixtures/market'
import type { AddonPackage } from '@/api/types'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({
  ...(await original<typeof import('@/api/useLiveUpdates')>()),
  useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })),
}))
// The package list (which add-on has a settings form) is not served by the fake host: one package, with a settings form.
vi.mock('@/addon-ui/slots', async (original) => ({
  ...(await original<typeof import('@/addon-ui/slots')>()),
  useAddons: () => ({ data: [{ name: 'calendar', title: 'Calendar', contributions: [{ slot: 'settings', id: 's' }] } as unknown as AddonPackage], isLoading: false, error: null }),
}))
const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const builtIn = (over = {}) => installedAddon({ name: 'wiki', origin: null, state: 'active', available: null, ...over })
const state = useFakeHost(() => [
  workspace({
    id: A,
    features: [...READS, 'actions', 'market'],
    market: { markets: [market()] },
    marketInstalled: { addons: [installedAddon(), builtIn()] },
    marketStage: stage(),
  }),
])
const ws = () => state.host.workspaces[0]

it('the Installed tab shows the one On/Off chip, a Built in tag for built-ins, and no Remove on a built-in', async () => {
  show('/w/DEMO/addons?tab=installed')
  const calendar = within((await screen.findByRole('button', { name: 'Remove' }, T)).closest('tr') as HTMLElement)
  expect(calendar.getByText('Off')).toHaveAttribute('data-state-chip', 'off')
  expect(calendar.getByText('Update available')).not.toHaveAttribute('data-state-chip')
  const wiki = within(screen.getByText('Built in').closest('tr') as HTMLElement)
  expect(wiki.getByText('On')).toHaveAttribute('data-state-chip', 'on')
  expect(wiki.queryByRole('button', { name: 'Remove' })).toBeNull()
  expect(screen.getAllByRole('button', { name: 'Remove' })).toHaveLength(1)
})

it('Remove of an Off add-on is one sheet; nothing is sent until Remove is pressed', async () => {
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }, T))
  const d = await screen.findByRole('dialog')
  expect(d).not.toHaveTextContent('Turn it off')
  expect(within(d).queryByRole('checkbox')).toBeNull()
  expect(state.host.calls.some((c) => c.path.endsWith('/market/uninstall'))).toBe(false)
  await userEvent.click(within(d).getByRole('button', { name: 'Remove' }))
  await waitFor(() => expect(state.host.calls.find((c) => c.path.endsWith('/market/uninstall'))?.body).toEqual({ name: 'calendar' }))
})

const signed = async () => {
  const { act } = await import('@testing-library/react')
  await waitFor(() => expect(state.host.posts().length).toBeGreaterThan(0), T)
  act(() => {
    state.host.submitted.set([...state.host.requests.keys()].at(-1)!, 'DEMO-0001')
  })
}

it('an On add-on: the sheet says we turn it off first, and the button reads Turn off and remove', async () => {
  ws().marketInstalled = { addons: [installedAddon({ state: 'active' })] }
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }, T))
  const d = await screen.findByRole('dialog')
  expect(d).toHaveTextContent('We will turn it off first')
  expect(within(d).queryByRole('button', { name: 'Remove' })).toBeNull()
  expect(within(d).getByRole('button', { name: 'Turn off and remove' })).toBeEnabled()
  expect(state.host.calls.some((c) => c.path.endsWith('/market/uninstall'))).toBe(false)
})

it('Turn off and remove is one signed addon_disable, then the host uninstall', async () => {
  ws().marketInstalled = { addons: [installedAddon({ state: 'active' })] }
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }, T))
  await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Turn off and remove' }))
  await waitFor(() => expect(state.host.posts()).toHaveLength(1), T)
  expect(state.host.posts()[0].body).toMatchObject({ action: 'addon_disable', name: 'calendar' })
  expect(state.host.calls.some((c) => c.path.endsWith('/market/uninstall'))).toBe(false) // not before the signature
  await signed()
  await waitFor(() => expect(state.host.calls.find((c) => c.path.endsWith('/market/uninstall'))?.body).toEqual({ name: 'calendar' }), T)
  expect(state.host.posts()).toHaveLength(1) // one approval only
})

it('if the removal fails after the signature, it says it is turned off and never asks for a second signature', async () => {
  ws().marketInstalled = { addons: [installedAddon({ state: 'active' })] }
  state.host.override = (path) => (path.endsWith('/market/uninstall') ? Response.json({ error: { code: 'market.still_enabled', message: 'It was turned on again.' } }, { status: 409 }) : undefined)
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }, T))
  await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Turn off and remove' }))
  await signed()
  expect(await screen.findByText(/is turned off, but removing it failed/, {}, T)).toBeInTheDocument()
  expect(state.host.posts()).toHaveLength(1)
})

it('Turn on now after an install is one signed addon_grant of that name; Done, leave it Off signs nothing', async () => {
  show('/w/DEMO/addons')
  await userEvent.click(await screen.findByRole('button', { name: 'Install...' }, T))
  await userEvent.click(await screen.findByRole('button', { name: 'Review installation' }, T))
  await waitFor(() => expect(state.host.posts()).toHaveLength(1), T)
  await signed()
  const d = await screen.findByRole('dialog', { name: /calendar is installed/ }, T)
  await userEvent.click(within(d).getByRole('button', { name: 'Turn on now' }))
  await waitFor(() => expect(state.host.posts()).toHaveLength(2), T)
  expect(state.host.posts()[1].body).toMatchObject({ action: 'addon_grant', name: 'calendar' })
})

it('after the install is approved the result dialog says Off and offers Done, leave it Off', async () => {
  show('/w/DEMO/addons')
  await userEvent.click(await screen.findByRole('button', { name: 'Install...' }, T))
  await userEvent.click(await screen.findByRole('button', { name: 'Review installation' }, T))
  await waitFor(() => expect(state.host.posts()).toHaveLength(1), T)
  ws().market!.markets[0].addons[0].installed = { version: '1.2.0', state: 'disabled' }
  const { act } = await import('@testing-library/react')
  act(() => {
    state.host.submitted.set([...state.host.requests.keys()][0], 'DEMO-0001')
  })
  const d = await screen.findByRole('dialog', { name: /calendar is installed/ }, T)
  expect(within(d).getByText('Off')).toHaveAttribute('data-state-chip', 'off')
  await userEvent.click(within(d).getByRole('button', { name: 'Done, leave it Off' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(within(screen.getByRole('article', { name: 'calendar' })).getByText('Off')).toHaveAttribute('data-state-chip', 'off') // the card uses the same chip
})

it('Settings opens a side panel over the list and Cancel puts focus back on the row’s Settings button', async () => {
  show('/w/DEMO/addons?tab=installed')
  const button = await screen.findByRole('button', { name: 'Settings' }, T)
  await userEvent.click(button)
  const panel = await screen.findByRole('dialog', { name: /Calendar settings/ }, T)
  expect(button).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByRole('table')).toBeInTheDocument() // the list stays in view
  await userEvent.click(await within(panel).findByRole('button', { name: 'Cancel' }, T))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Settings' })).toHaveFocus())
})

const SRC = { url: 'https://github.com/acme/hub-market', class: 'company' as const, added: false }
const DEV = { url: 'https://github.com/acme/test-market', class: 'dev' as const, added: false }
const OFFICIAL = { url: market().url, class: 'official' as const, added: true }

it('+ Add marketplace is greyed with the one sentence when no source is addable', async () => {
  ws().market = { markets: [market()], sources: [OFFICIAL], any_source: false }
  show('/w/DEMO/addons?tab=marketplaces')
  const add = await screen.findByRole('button', { name: '+ Add marketplace' }, T)
  expect(add).toBeDisabled()
  expect(add).toHaveAccessibleDescription("Only the official and your hub's marketplaces can be added.")
  expect(screen.queryByText(/by address/i)).toBeNull()
})

it('the picker lists only the sources not yet added, tags the dev one, and adds with one Add and one Approve', async () => {
  ws().market = { markets: [market()], sources: [OFFICIAL, SRC, DEV], any_source: false }
  show('/w/DEMO/addons?tab=marketplaces')
  await userEvent.click(await screen.findByRole('button', { name: '+ Add marketplace' }, T))
  const d = await screen.findByRole('dialog')
  const rows = within(d).getAllByRole('listitem')
  expect(rows).toHaveLength(2)
  expect(within(rows[0]).getByText('Your hub')).toBeInTheDocument()
  expect(within(rows[1]).getByText('dev sandbox only')).toBeInTheDocument()
  expect(d).not.toHaveTextContent(market().url)
  expect(state.host.calls.some((c) => c.path.endsWith('/market/add'))).toBe(false)
  await userEvent.click(within(rows[0]).getByRole('button', { name: 'Add' }))
  expect(state.host.calls.some((c) => c.path.endsWith('/market/add'))).toBe(false) // Add only picks
  await userEvent.click(within(d).getByRole('button', { name: 'Approve' }))
  await waitFor(() => expect(state.host.calls.find((c) => c.path.endsWith('/market/add'))?.body).toEqual({ url: SRC.url }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
})

it('Back from the confirm returns to the list and sends nothing', async () => {
  ws().market = { markets: [market()], sources: [SRC], any_source: false }
  show('/w/DEMO/addons?tab=marketplaces')
  await userEvent.click(await screen.findByRole('button', { name: '+ Add marketplace' }, T))
  await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Add' }))
  await userEvent.click(screen.getByRole('button', { name: 'Back' }))
  expect(screen.getByRole('list', { name: 'Marketplaces you can add' })).toBeInTheDocument()
  expect(state.host.calls.some((c) => c.path.endsWith('/market/add'))).toBe(false)
})
