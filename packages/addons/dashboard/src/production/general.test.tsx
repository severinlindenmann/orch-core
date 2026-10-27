import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { OWNER, TOM, clickOn, prepareByClick, show, useFakeHost, workspace } from '@/test/hostHarness'

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

const puts = () => state.host.calls.filter(c => c.method === 'PUT')

it('opens the icon picker in a popover and saves the chosen icon with one click (D6)', async () => {
  state.host.override = path => path === '/api/workspaces' ? Response.json([{ id: ID, prefix: 'DEMO', name: 'Dev workspace', members: state.host.workspaces[0].roster.map(m => ({ person: m.person, name: m.name, role: m.role, devices: m.devices.length })), needs_you: 0, host_state: 'ok', features: state.host.workspaces[0].features, icon_known: true, icon: null }]) : undefined
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  expect(screen.queryByRole('group', { name: 'Symbol' })).toBeNull()
  await user.click(await screen.findByRole('button', { name: 'Change' }, T))
  await user.click(within(screen.getByRole('group', { name: 'Symbol' })).getByRole('button', { name: 'Star' }))
  await user.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(puts()).toHaveLength(1), T)
  expect(puts()).toEqual([{ method: 'PUT', path: `/api/w/${ID}/workspace/cosmetic`, body: { icon: { icon: 'star', colour: 'pink' }, if_current: { name: 'Dev workspace', icon: null } } }])
  expect(state.host.posts()).toEqual([])
  expect(state.host.calls.some(c => c.path.endsWith('/click/prepare'))).toBe(false)
})

it('lets an owner rename the workspace with one click', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  await user.click(await screen.findByRole('button', { name: 'Rename' }, T))
  const input = screen.getByRole('textbox', { name: 'Workspace name' })
  expect(input).toHaveAttribute('maxlength', '80')
  await user.clear(input)
  await user.type(input, 'Team space')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(puts()).toHaveLength(1), T)
  expect(puts()[0].path).toBe(`/api/w/${ID}/workspace/cosmetic`)
  expect(puts()[0].body).toEqual({ name: 'Team space', if_current: { name: 'Dev workspace', icon: null } })
  expect(state.host.posts()).toEqual([])
})

it('offers no Rename to a non-owner', async () => {
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].role = 'member'
  show('/w/DEMO/settings/general')
  expect(await screen.findByRole('heading', { name: 'Dev workspace' }, T)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Rename' })).toBeNull()
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
  clickOn(state.host.workspaces[0])
  const user = userEvent.setup()
  await openRecords()
  const reserved = screen.getByLabelText('Ticket reserved for (minutes)')
  await user.clear(reserved)
  await user.type(reserved, '90')
  await user.selectOptions(screen.getByRole('combobox', { name: 'Longest agent access you can give' }), '24')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  await prepareByClick(await screen.findByRole('dialog', {}, T), 'Change the workspace settings')
  expect(state.host.clickPrepares().map(c => c.body)).toEqual([{ action: 'settings_set', grant_hours: 24 }])
  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
  await user.click(screen.getByRole('button', { name: 'Save settings' }))
  await prepareByClick(await screen.findByRole('dialog', {}, T), 'Change the workspace settings')
  expect(state.host.clickPrepares().map(c => c.body)).toEqual([{ action: 'settings_set', grant_hours: 24 }, { action: 'settings_set', claim_ttl_min: 90 }])
  expect(state.host.posts()).toEqual([])
})
