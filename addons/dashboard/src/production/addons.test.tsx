import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import type { AddOnRow, AddOnsData } from '@/api/addOnSwitches'
import { useFakeHost, workspace, show, MARA } from '@/test/hostHarness'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const ID = 'a'.repeat(32)
const T = { timeout: 5000 }
const row = (over: Partial<AddOnRow> & Pick<AddOnRow, 'id' | 'title'>): AddOnRow => ({ desc: `About ${over.title}.`, state: 'off', locked: null, blocked: null, ...over })
const data = (rows: AddOnRow[]): AddOnsData => ({ available: true, add_ons: rows })
const ROWS = [
  row({ id: 'dashboard', title: 'Dashboard', state: 'on', locked: 'The dashboard is always on: it is what shows this workspace.' }),
  row({ id: 'start-agent', title: 'Start agents', state: 'on' }),
  row({ id: 'terminals', title: 'Terminals', state: 'off', blocked: 'Needs dashboard execution first. In your terminal run: orch runner enable' }),
  row({ id: 'worktrees', title: 'Worktrees', state: 'off' }),
]
const state = useFakeHost(() => [workspace({ id: ID, addOns: data(ROWS) })])
const open = async () => {
  show('/w/DEMO/settings/addons')
  return await screen.findByRole('list', { name: 'Add-ons' }, T)
}

it('lists every add-on with its description and On or Off; the dashboard is always on and has no switch', async () => {
  const list = await open()
  expect(within(list).getAllByRole('listitem')).toHaveLength(4)
  expect(within(screen.getByTestId('addon-dashboard')).getByText('Always on')).toBeInTheDocument()
  expect(within(screen.getByTestId('addon-dashboard')).queryByRole('switch')).toBeNull()
  expect(screen.getByTestId('addon-start-agent-state')).toHaveTextContent('On')
  expect(screen.getByTestId('addon-worktrees-state')).toHaveTextContent('Off')
  expect(screen.getByRole('switch', { name: 'Start agents' })).toBeChecked()
  expect(screen.getByRole('switch', { name: 'Worktrees' })).not.toBeChecked()
  expect(screen.getByText('About Worktrees.')).toBeInTheDocument()
})

it('an add-on that needs something first is greyed with the reason beside it', async () => {
  await open()
  const sw = screen.getByRole('switch', { name: 'Terminals' })
  expect(sw).toBeDisabled()
  expect(sw).toHaveAccessibleDescription(/Needs dashboard execution first/)
  await userEvent.setup().click(sw)
  expect(state.host.posts()).toEqual([])
})

it('turning one on is a signed hand-off: the typed action opens the review, nothing is written by the page', async () => {
  const user = userEvent.setup()
  await open()
  await user.click(screen.getByRole('switch', { name: 'Worktrees' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'addon_grant', name: 'worktrees' }])
  expect(dialog).toHaveTextContent('Turn on an add-on')
  expect(dialog).toHaveTextContent('In your terminal you type worktrees~aaaaaaaa to continue.')
})

it('turning one off is a signed hand-off too', async () => {
  const user = userEvent.setup()
  await open()
  await user.click(screen.getByRole('switch', { name: 'Start agents' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'addon_disable', name: 'start-agent' }])
  expect(dialog).toHaveTextContent('Turn off an add-on')
})

it('an add-on with running work cannot be switched off and says why', async () => {
  state.host.workspaces[0].addOns = data([row({ id: 'terminals', title: 'Terminals', state: 'on', blocked: 'Stop first: 2 terminals are running right now.' })])
  await open()
  const sw = screen.getByRole('switch', { name: 'Terminals' })
  expect(sw).toBeDisabled()
  expect(screen.getByText('Stop first: 2 terminals are running right now.')).toBeInTheDocument()
  await userEvent.setup().click(sw)
  expect(state.host.posts()).toEqual([])
})

it('only an owner can change a switch', async () => {
  const ws = state.host.workspaces[0]
  ws.me = MARA
  ws.role = 'maintainer'
  await open()
  expect(screen.getByRole('switch', { name: 'Worktrees' })).toBeDisabled()
  expect(screen.getAllByText('Only owners can change add-ons.').length).toBeGreaterThan(0)
})
