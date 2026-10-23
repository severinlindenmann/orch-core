import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { ARM_MS, CLICK_RISK } from '@/api/click'
import { uuid8 } from '@/api/workspaces'
import { DO, MARA, OWNER, READS, roster, show, useFakeHost, workspace } from '@/test/hostHarness'

// The production app on a fake server that speaks the click routes, as the build wires it (vite.config.ts alias).
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const B = 'b'.repeat(32)
const T = { timeout: 5000 }
const ON = { custody: 'convenience', enabled: true, available: true } as const
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, ...DO, 'actions', 'click'], click: ON })])

const save = async (user: ReturnType<typeof userEvent.setup>) => {
  const input = await screen.findByRole('button', { name: '1 day' }, T)
  await user.click(input)
  await user.click(screen.getByRole('button', { name: 'Save settings' }))
  return screen.findByRole('dialog', {}, T)
}
const press = (b: HTMLElement) => {
  fireEvent.pointerDown(b)
  fireEvent.click(b)
}

it('the workspace card shows the risk line when the key is in convenience custody, and not otherwise', async () => {
  show('/w/DEMO/settings/general')
  expect(await screen.findByTestId('workspace-card-signing', {}, T)).toHaveTextContent(CLICK_RISK)
})

it('no Sign button when the signer has nothing for the workspace; the card offers to switch it on', async () => {
  state.host.workspaces[0].click = { custody: 'none', enabled: false, available: false, problem: 'disabled' }
  state.host.workspaces[0].features = [...READS, ...DO, 'actions']
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  const dialog = await save(user)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(screen.getByTestId('signing-state')).toHaveTextContent(/^Off/)
  expect(within(dialog).queryByRole('button', { name: /^Sign( |$)/ })).toBeNull()
  // the card reads the signer's status; nothing is prepared, viewed or signed
  expect(state.host.calls.filter((c) => c.path.includes('/click/') && !c.path.endsWith('/click/status'))).toEqual([])
})

it('click-to-sign end to end: Sign, the signer’s View only, 500 ms, Sign, Signed', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  const dialog = await save(user)
  expect(state.host.calls.filter((c) => c.path.endsWith('/handoff') && c.method === 'POST')).toEqual([]) // nothing yet
  await user.click(await within(dialog).findByRole('button', { name: /^Sign( |$)/ }, T))
  const panel = await within(dialog).findByTestId('click-panel', {}, T)
  expect(within(panel).queryByRole('button', { name: 'One-click' })).toBeNull()
  expect(panel).toHaveTextContent('FROM-THE-SIGNER settings.set')
  expect(panel).not.toHaveTextContent(/HOST-BEFORE-VALUE|Preview|orch sign/)
  expect(state.host.calls.find((c) => c.path.endsWith('/click/prepare'))?.body).toEqual({ action: 'settings_set', grant_hours: 24 })
  const sign = within(dialog).getByRole('button', { name: /^Sign( |$)/ })
  expect(sign).toBeDisabled()
  await waitFor(() => expect(sign).toBeEnabled(), { timeout: ARM_MS + 3000 })
  press(sign)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T) // closes by itself; one toast says it
  expect(state.host.clickSigned).toEqual([{ request_id: 'hr_' + '1'.padStart(26, '0'), view_digest: 'sha256:' + '9'.repeat(64) }])
  expect(state.host.clickCancelled).toEqual([])
})

it('a click workspace offers only Sign: no terminal path, and no hand-off is ever prepared', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  const dialog = await save(user)
  expect(within(dialog).queryByRole('button', { name: 'Sign in the terminal instead' })).toBeNull()
  await user.click(await within(dialog).findByRole('button', { name: /^Sign( |$)/ }, T))
  await within(dialog).findByTestId('click-panel', {}, T)
  expect(within(dialog).queryByRole('button', { name: 'Sign in the terminal instead' })).toBeNull()
  expect(state.host.posts().length).toBe(0)
})

it('each workspace has its own person and role: the second workspace draws its action buttons', async () => {
  const second = workspace({ id: B, name: 'Second', me: MARA, role: 'owner', roster: roster().map((m) => (m.person === MARA ? { ...m, role: 'owner' as const } : m.person === OWNER ? { ...m, role: 'viewer' as const } : m)), features: [...READS, ...DO, 'actions'] })
  state.host.workspaces.push(second)
  const user = userEvent.setup()
  show(`/w/DEMO/settings/general?ws=${uuid8(B)}`)
  const input = await screen.findByRole('button', { name: '1 day' }, T)
  expect(input).toBeEnabled()
  await user.click(input)
  expect(screen.getByRole('button', { name: 'Save settings' })).toBeEnabled() // an owner there, though not under the first workspace's person id
  expect(OWNER).not.toBe(MARA)
})
