import { screen, waitFor, within } from '@testing-library/react'
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
  row({ id: 'terminals', title: 'Terminals', state: 'off', blocked: 'Needs dashboard execution first. Turn it on here, then switch this on.', runner_switch: true }),
  row({ id: 'worktrees', title: 'Worktrees', state: 'off' }),
  row({ id: 'wiki', title: 'Wiki', state: 'off' }),
  row({ id: 'quick', title: 'Quick tasks', state: 'on' }),
]
const state = useFakeHost(() => [workspace({ id: ID, addOns: data(ROWS) })])
const open = async () => {
  show('/w/DEMO/addons')
  return await screen.findByRole('list', { name: 'Add-ons' }, T)
}

it('lists every add-on with its description and On or Off; the dashboard is always on and has no switch', async () => {
  const list = await open()
  expect(within(list).getAllByRole('listitem')).toHaveLength(6)
  expect(within(screen.getByTestId('addon-dashboard')).getByText('Always on')).toBeInTheDocument()
  expect(within(screen.getByTestId('addon-dashboard')).queryByRole('switch')).toBeNull()
  expect(screen.queryByTestId('addon-start-agent-state')).toBeNull() // no state chip: the switch says it
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

it('C14: what is missing is dashboard execution: a button turns it on here, no terminal command', async () => {
  await open()
  const row = screen.getByTestId('addon-terminals')
  expect(within(row).getByTestId('addon-terminals-runner')).toHaveTextContent('Turn on dashboard execution')
  expect(row).not.toHaveTextContent('orch runner')
  expect(within(screen.getByTestId('addon-worktrees')).queryByTestId('addon-worktrees-runner')).toBeNull()
})

it('turning one on is a signed hand-off: the typed action opens the review, nothing is written by the page', async () => {
  const user = userEvent.setup()
  await open()
  await user.click(screen.getByRole('switch', { name: 'Worktrees' }))
  const dialog = await screen.findByTestId('addon-worktrees-accordion', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'addon_grant', name: 'worktrees' }])
  expect(dialog).toHaveTextContent('Turn on')
})

it('feedback 3.3: the Wiki switch opens the sign step (it was missing from the page list)', async () => {
  const user = userEvent.setup()
  await open()
  await user.click(screen.getByRole('switch', { name: 'Wiki' }))
  const dialog = await screen.findByTestId('addon-wiki-accordion', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'addon_grant', name: 'wiki' }])
})

it('turning one off is a signed hand-off too', async () => {
  const user = userEvent.setup()
  await open()
  await user.click(screen.getByRole('switch', { name: 'Start agents' }))
  const dialog = await screen.findByTestId('addon-start-agent-accordion', {}, T)
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

it('an add-on with settings says what it needs while Off; the toggle still opens the normal review (no two-step flow yet)', async () => {
  state.host.workspaces[0].addOns = data([row({ id: 'publish', title: 'Publish' }), row({ id: 'model-routing', title: 'Model routing' }), row({ id: 'worktrees', title: 'Worktrees' })])
  await open()
  expect(screen.queryByTestId('addon-publish-needs')).toBeNull() // no "Needs" line: only an amber "Needs setup" once it is on
  expect(screen.queryByTestId('addon-publish-expand')).toBeNull() // nothing to open while Off
  await userEvent.setup().click(screen.getByRole('switch', { name: 'Publish' }))
  await waitFor(() => expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'addon_grant', name: 'publish' }]), T)
})

it('run 9: the Built in group tags every row, Quick tasks is switchable like Wiki, the review opens under the row (no popup)', async () => {
  const user = userEvent.setup()
  await open()
  expect(within(screen.getByTestId('addon-quick')).queryByText('Built in')).toBeNull() // the group title says it
  await user.click(screen.getByRole('switch', { name: 'Quick tasks' }))
  const accordion = await screen.findByTestId('addon-quick-accordion', {}, T)
  expect(screen.getByTestId('addon-quick')).toContainElement(accordion)
  expect(screen.queryByRole('dialog')).toBeNull()
  await waitFor(() => expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'addon_disable', name: 'quick' }]), T)
})

it('run 9: with One click on, pressing the switch signs directly, the review shows under the row (no popup)', async () => {
  const ws = state.host.workspaces[0]
  ws.click = { custody: 'convenience', enabled: true, available: true }
  ws.features = [...ws.features, 'click']
  await open()
  const user = userEvent.setup()
  await user.click(screen.getByRole('switch', { name: 'Worktrees' }))
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
  expect(screen.queryByRole('dialog')).toBeNull()
})
