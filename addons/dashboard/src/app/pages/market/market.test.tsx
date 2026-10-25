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

it('an On add-on can show its Remove sheet but it says to turn it off first and cannot remove', async () => {
  ws().marketInstalled = { addons: [installedAddon({ state: 'active' })] }
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }, T))
  const d = await screen.findByRole('dialog')
  expect(d).toHaveTextContent('Turn it off in Settings > Add-ons first')
  expect(within(d).getByRole('button', { name: 'Remove' })).toBeDisabled()
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
