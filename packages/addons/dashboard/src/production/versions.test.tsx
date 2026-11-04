import userEvent from '@testing-library/user-event'
// R3-A1: Settings shows the versions it already knows: core (package + release), the dashboard, and each add-on's manifest version.
import { screen, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import type { AddOnRow } from '@/api/addOnSwitches'
import { versionsOf, vLabel } from '@/api/versions'
import { useFakeHost, workspace, show } from '@/test/hostHarness'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const ID = 'a'.repeat(32)
const T = { timeout: 5000 }
const row = (over: Partial<AddOnRow> & Pick<AddOnRow, 'id' | 'title'>): AddOnRow => ({ desc: `About ${over.title}.`, state: 'on', locked: null, blocked: null, ...over })
const ROWS = [
  row({ id: 'dashboard', title: 'Dashboard', locked: 'always on', version: '0.1.0' }),
  row({ id: 'worktrees', title: 'Work copies', version: '1.2.3' }),
  row({ id: 'publish', title: 'Publish', state: 'off', version: null }),
  row({ id: 'usage', title: 'Usage', state: 'off' }),
]
const state = useFakeHost(() => [
  workspace({ id: ID, addOns: { available: true, add_ons: ROWS }, versions: { core: { version: '2.0.1', release: '0123456789ab' }, dashboard: { version: '0.1.0' } } }),
])

it('General shows the core version with its release, and the dashboard version', async () => {
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced'))
  const box = await screen.findByTestId('versions', {}, T)
  expect(within(box).getByText('v2.0.1')).toBeInTheDocument()
  expect(within(box).getByText('release 0123456789ab')).toBeInTheDocument()
  expect(within(box).getByText('v0.1.0')).toBeInTheDocument()
  expect(state.host.posts()).toEqual([]) // nothing is written
})

it('the answer is checked: odd values read as not known', () => {
  expect(versionsOf({ core: { version: '<b>1</b>', release: 'XYZ' }, dashboard: { version: 5 } })).toEqual({ core: { version: null, release: null }, dashboard: { version: null } })
  expect(versionsOf(null).core.version).toBeNull()
  expect(versionsOf({ core: { version: '1.0.0+abc', release: 'ffffffffffff' } }).core).toEqual({ version: '1.0.0+abc', release: 'ffffffffffff' })
  expect(vLabel('1.2')).toBe('v1.2')
  expect(vLabel('v1.2')).toBe('v1.2')
})
