import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import type { AddOnRow, AddOnsData } from '@/api/addOnSwitches'
import { ARM_MS } from '@/api/click'
import * as gesture from '@/components/sign/trustedPress'
import { useFakeHost, workspace, show, trustedPresses, MARA } from '@/test/hostHarness'

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
  row({ id: 'worktrees', title: 'Work copies', state: 'off' }),
  row({ id: 'focus', title: 'Focus mode', state: 'off' }),
  row({ id: 'wiki', title: 'Wiki', state: 'off' }),
  row({ id: 'quick', title: 'Quick tasks', state: 'on' }),
]
const state = useFakeHost(() => [workspace({ id: ID, addOns: data(ROWS) })])
const open = async () => {
  show('/w/DEMO/addons')
  return await screen.findByRole('list', { name: 'Add-ons' }, T)
}
afterEach(() => vi.restoreAllMocks())
const clickOn = () => {
  const ws = state.host.workspaces[0]
  ws.click = { custody: 'convenience', enabled: true, available: true }
  ws.features = [...ws.features, 'click']
}
/** The switch opens the review under its row; its outcome button prepares on a real press (fireEvent is untrusted: trustedPress stands in). */
const turn = async (user: ReturnType<typeof userEvent.setup>, id: string, title: string, outcome: string) => {
  await user.click(screen.getByRole('switch', { name: title }))
  const accordion = await screen.findByTestId(`addon-${id}-accordion`, {}, T)
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
  fireEvent.click(within(accordion).getByRole('button', { name: outcome }))
  return accordion
}

it('lists every add-on with its description and On or Off; the dashboard is always on and has no switch', async () => {
  const list = await open()
  expect(within(list).getAllByRole('listitem')).toHaveLength(7)
  expect(within(screen.getByTestId('addon-dashboard')).getByText('Always on')).toBeInTheDocument()
  expect(within(screen.getByTestId('addon-dashboard')).queryByRole('switch')).toBeNull()
  expect(screen.queryByTestId('addon-start-agent-state')).toBeNull() // no state chip: the switch says it
  expect(screen.getByRole('switch', { name: 'Start agents' })).toBeChecked()
  expect(screen.getByRole('switch', { name: 'Work copies' })).not.toBeChecked()
  expect(screen.getByText('About Work copies.')).toBeInTheDocument()
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

it('turning one on is a One click signature: the switch opens the review under the row, the outcome press prepares the typed action, nothing is written by the page', async () => {
  clickOn()
  const user = userEvent.setup()
  await open()
  const accordion = await turn(user, 'worktrees', 'Work copies', 'Turn on Work copies')
  await within(accordion).findByTestId('click-panel', {}, T)
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_grant', name: 'worktrees' }])
  expect(state.host.posts()).toEqual([]) // no hand-off is ever prepared
  // C3: addon.grant is not a daily op: the signer's headline and risk lines, no arm, a separate Sign (the second press)
  expect(accordion).toHaveTextContent('FROM-THE-SIGNER headline addon.grant')
  expect(within(accordion).getByTestId('sign-risk-lines')).toHaveTextContent('FROM-THE-SIGNER risk: the add-on worktrees gets its capabilities')
  expect(within(accordion).queryByTestId('sign-progress')).toBeNull()
  expect(within(accordion).getByRole('button', { name: /^Sign( |$)/ })).toBeInTheDocument()
  expect(state.host.clickSigned).toEqual([])
})

it('the synthetic switch press grants no intent: the review waits for the outcome press, nothing is prepared', async () => {
  clickOn()
  const user = userEvent.setup()
  await open()
  await user.click(screen.getByRole('switch', { name: 'Work copies' }))
  const accordion = await screen.findByTestId('addon-worktrees-accordion', {}, T)
  expect(within(accordion).getByRole('button', { name: 'Turn on Work copies' })).toBeEnabled()
  expect(state.host.clickPrepares()).toEqual([])
})

it('with One click off, the switch says One click is not ready and links to turning it on; nothing is prepared', async () => {
  const user = userEvent.setup()
  await open()
  await user.click(screen.getByRole('switch', { name: 'Work copies' }))
  const accordion = await screen.findByTestId('addon-worktrees-accordion', {}, T)
  expect(accordion).toHaveTextContent('One click is not ready on this computer.')
  expect(within(accordion).getByRole('link', { name: 'Turn on One click' })).toBeInTheDocument()
  expect(accordion).not.toHaveTextContent(/orch sign|terminal/i)
  expect(state.host.clickPrepares()).toEqual([])
  expect(state.host.posts()).toEqual([])
})

it('feedback 3.3: the Wiki switch opens the sign step (it was missing from the page list)', async () => {
  clickOn()
  const user = userEvent.setup()
  await open()
  const accordion = await turn(user, 'wiki', 'Wiki', 'Turn on Wiki')
  await within(accordion).findByTestId('click-panel', {}, T)
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_grant', name: 'wiki' }])
})

it('turning one off is a One click signature too', async () => {
  clickOn()
  const user = userEvent.setup()
  await open()
  const accordion = await turn(user, 'start-agent', 'Start agents', 'Turn off Start agents')
  await within(accordion).findByTestId('click-panel', {}, T)
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_disable', name: 'start-agent' }])
  // C3: addon.disable is a daily op (no risk lines): the press that prepared arms, the button shows the signer's headline
  expect(within(accordion).queryByTestId('sign-risk-lines')).toBeNull()
  expect(within(accordion).getByTestId('sign-progress')).toBeInTheDocument()
  expect(within(accordion).getByTestId('sign-button')).toHaveTextContent('FROM-THE-SIGNER headline addon.disable')
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
  expect(screen.getByRole('switch', { name: 'Work copies' })).toBeDisabled()
  expect(screen.getAllByText('Only owners can change add-ons.').length).toBeGreaterThan(0)
})

it('an add-on with settings says what it needs while Off; the toggle still opens the normal review (no two-step flow yet)', async () => {
  state.host.workspaces[0].addOns = data([row({ id: 'publish', title: 'Publish' }), row({ id: 'model-routing', title: 'Model routing' }), row({ id: 'worktrees', title: 'Work copies' })])
  clickOn()
  await open()
  expect(screen.queryByTestId('addon-publish-needs')).toBeNull() // no "Needs" line: only an amber "Needs setup" once it is on
  expect(screen.queryByTestId('addon-publish-expand')).toBeNull() // nothing to open while Off
  await turn(userEvent.setup(), 'publish', 'Publish', 'Turn on Publish')
  await waitFor(() => expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_grant', name: 'publish' }]), T)
})

it.each([['quick', 'Quick tasks', 'addon_disable', 'Turn off Quick tasks'], ['focus', 'Focus mode', 'addon_grant', 'Turn on Focus mode']])('run 9: %s uses the Built in tag and inline switch review (no popup)', async (name, title, action, outcome) => {
  clickOn()
  const user = userEvent.setup()
  await open()
  expect(within(screen.getByTestId(`addon-${name}`)).queryByText('Built in')).toBeNull() // the group title says it
  const accordion = await turn(user, name, title, outcome)
  expect(screen.getByTestId(`addon-${name}`)).toContainElement(accordion)
  expect(screen.queryByRole('dialog')).toBeNull()
  await waitFor(() => expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action, name }]), T)
})

it('run 9: with One click on, the trusted switch press prepares directly; after the 1 s arm one Sign press signs, under the row (no popup)', async () => {
  clickOn()
  vi.spyOn(gesture, 'takeTrustedPress').mockReturnValue(true) // the switch press was a real one (jsdom events are untrusted)
  await open()
  const user = userEvent.setup()
  await user.click(screen.getByRole('switch', { name: 'Work copies' }))
  const accordion = await screen.findByTestId('addon-worktrees-accordion', {}, T)
  const sign = await within(accordion).findByRole('button', { name: /^Sign( |$)/ }, T)
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_grant', name: 'worktrees' }])
  await waitFor(() => expect(sign).toBeEnabled(), { timeout: ARM_MS + 3000 })
  trustedPresses() // the armed Sign signs only on a trusted press
  await user.click(sign)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
  expect(screen.queryByRole('dialog')).toBeNull()
})
