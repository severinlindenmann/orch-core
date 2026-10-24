import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { OWNER, show, useFakeHost, workspace } from '@/test/hostHarness'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const ID = 'a'.repeat(32)
const T = { timeout: 5000 }
const status = {
  available: true, configured: true, repo: { state: 'ok' },
  auto: { on: true, every_min: 15, push: false },
  last: { commit: 'c'.repeat(40), at: '2026-10-12T08:00:00Z', by: OWNER, ws_seq: 40, pushed_at: null },
}
const state = useFakeHost(() => [workspace({ id: ID, records: status })])
async function openRecords() {
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  return screen.findByTestId('records-summary', {}, T)
}

it('shows the workspace name and numbering with identity inside the closed Advanced fold', async () => {
  show('/w/DEMO/settings/general')
  expect(await screen.findByRole('heading', { name: 'Dev workspace' }, T)).toBeInTheDocument()
  expect(screen.getByText('Tickets are numbered DEMO-0001, DEMO-0002 ...')).toBeInTheDocument()
  const advanced = screen.getByText('Advanced').closest('details')!
  expect(advanced).not.toHaveAttribute('open')
  expect(within(advanced).getByText(ID)).toBeInTheDocument()
  expect(screen.getAllByText(ID).every(node => advanced.contains(node))).toBe(true)
})

it('opens the icon picker in a popover and saves the chosen icon through the hand-off', async () => {
  state.host.override = path => path === '/api/workspaces' ? Response.json([{ id: ID, prefix: 'DEMO', name: 'Dev workspace', members: state.host.workspaces[0].roster.map(m => ({ person: m.person, name: m.name, role: m.role, devices: m.devices.length })), needs_you: 0, host_state: 'ok', features: state.host.workspaces[0].features, icon_known: true, icon: { icon: 'cloud', colour: 'teal' } }]) : undefined
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  expect(screen.queryByRole('group', { name: 'Symbol' })).toBeNull()
  await user.click(await screen.findByRole('button', { name: 'Change' }, T))
  await user.click(within(screen.getByRole('group', { name: 'Symbol' })).getByRole('button', { name: 'Star' }))
  await user.click(screen.getByRole('button', { name: 'Save' }))
  await screen.findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map(c => c.body)).toEqual([{ action: 'icon_set', icon: { icon: 'star', colour: 'teal' } }])
})

it.each([
  [false, null, 'Your tickets and history are not saved yet.'],
  [true, { on: true, every_min: 15, push: false }, 'Your tickets and history are saved automatically on this computer.'],
  [true, { on: false, every_min: 15, push: false }, 'Your tickets and history are saved only when you press Save now.'],
  [true, null, 'Your tickets and history are saved only when you press Save now.'],
])('describes the real records status: configured %s, auto %j', async (configured, auto, sentence) => {
  state.host.workspaces[0].records = { ...status, configured, auto }
  expect(await openRecords()).toHaveTextContent(sentence as string)
  expect(screen.getByRole('button', { name: 'Save now' })).toBeEnabled()
  if (!configured) expect(screen.getByRole('button', { name: 'Set up' })).toBeEnabled()
})

it('counts waiting changes and does not invent a last save', async () => {
  state.host.workspaces[0].records = { ...status, last: null, pending: { events: 3, tickets: [], more: false, workspace_events: 0 } }
  const summary = await openRecords()
  expect(summary).toHaveTextContent('Nothing saved yet. 3 changes are waiting to be saved.')
})

it('explains a missing records folder and keeps its error code under closed Details', async () => {
  state.host.override = (path, method) => path.endsWith('/records/commit') && method === 'POST' ? Response.json({ ok: false, error: { code: 'records.not_dedicated', message: 'records.not_dedicated' } }, { status: 409 }) : undefined
  const user = userEvent.setup()
  await openRecords()
  await user.click(screen.getByRole('button', { name: 'Save now' }))
  const failure = await screen.findByTestId('records-failure', {}, T)
  expect(failure).toHaveTextContent("The records folder isn't set up yet.")
  const details = within(failure).getByText('Details').closest('details')!
  expect(details).not.toHaveAttribute('open')
  expect(within(details).getByText('records.not_dedicated')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Set up records' }))
  expect(state.host.calls.filter(c => c.method === 'POST' && c.path.endsWith('/records/commit')).map(c => c.body)).toEqual([{ push: false }])
  // #1198 C15: Set up records is the init step, not a second commit
  expect(state.host.calls.filter(c => c.method === 'POST' && c.path.endsWith('/records/init')).map(c => c.body)).toEqual([{}])
})

it('saves agent access separately from changed reservation times', async () => {
  const user = userEvent.setup()
  await openRecords()
  const reserved = screen.getByLabelText('Ticket reserved for (minutes)')
  await user.clear(reserved)
  await user.type(reserved, '90')
  await user.selectOptions(screen.getByRole('combobox', { name: 'Longest agent access you can give' }), '24')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  await screen.findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map(c => c.body)).toEqual([{ action: 'settings_set', grant_hours: 24 }])
  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
  await user.click(screen.getByRole('button', { name: 'Save settings' }))
  await screen.findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map(c => c.body)).toEqual([{ action: 'settings_set', grant_hours: 24 }, { action: 'settings_set', claim_ttl_min: 90 }])
})
