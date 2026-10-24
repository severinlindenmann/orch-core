import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'
import { market, stage, installedAddon, updateStage } from '@/test/fixtures/market'
import { EXTERNAL_HELP } from '@/api/market'
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({
  ...(await original<typeof import('@/api/useLiveUpdates')>()),
  useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })),
}))
const A = 'a'.repeat(32)
const state = useFakeHost(() => [
  workspace({ id: A, features: [...READS, 'actions', 'market'], market: { markets: [market()] }, marketInstalled: { addons: [installedAddon()] }, marketStage: stage() }),
])
const ws = () => state.host.workspaces[0]
const T = { timeout: 5000 }
async function openReview(tab = 'browse') {
  show(`/w/DEMO/addons?tab=${tab}`)
  await userEvent.click(await screen.findByRole('button', { name: tab === 'browse' ? 'Install...' : 'Update...' }, T))
  return screen.findByRole('dialog', {}, T)
}
it('shows a source on every card, states and a permanent nav link', async () => {
  const base = market().addons[0]
  ws().market = {
    markets: [
      market({
        addons: [base, ...['disabled', 'active', 'pending'].map((s) => ({ ...base, id: s, installed: { version: '1', state: s } })), { ...base, id: 'revoked', revoked: true }],
      }),
    ],
  }
  show('/w/DEMO/addons')
  await screen.findByText('Available', {}, T)
  for (const text of ['Off', 'On', 'Awaiting Terminal approval', 'Withdrawn by its publisher']) expect(screen.getByText(text)).toBeInTheDocument()
  for (const card of screen.getAllByRole('article')) expect(within(card).getByText('official - demo-example/market - 1.2.0')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Add-ons' })).toHaveAttribute('href', '/w/DEMO/addons')
})
it('stages the exact source, hands off in Terminal and refreshes after submission', async () => {
  const d = await openReview()
  expect(state.host.calls.find((c) => c.path.endsWith('/market/stage'))?.body).toEqual({ id: 'calendar', url: market().url })
  expect(within(d).getByText('You will approve this exact version and these permissions in the Terminal. Nothing is enabled until you sign.')).toBeInTheDocument()
  await userEvent.click(within(d).getByRole('button', { name: 'Continue in Terminal' }))
  await waitFor(() => expect(state.host.posts()).toHaveLength(1), T)
  expect(state.host.posts()[0].body).toEqual({ action: 'market_grant', name: 'calendar', staged: 'demo-stage' })
  expect(screen.getByText('Awaiting Terminal approval')).toBeInTheDocument()
  ws().market!.markets[0].addons[0].installed = { version: '1.2.0', state: 'disabled' }
  act(() => {
    state.host.submitted.set([...state.host.requests.keys()][0], 'DEMO-0001')
  })
  await screen.findByText('Installed · Turn on in Settings > Add-ons', {}, T)
})
it('Cancel deletes the stage', async () => {
  const d = await openReview()
  await userEvent.click(within(d).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(state.host.calls.some((c) => c.method === 'DELETE' && c.path.endsWith('/market/stage/demo-stage'))).toBe(true))
})
it('expanded widened review puts new lines first and shows additions and removals', async () => {
  ws().marketStage = updateStage(true)
  const d = await openReview('installed')
  expect(within(d).getByText('This update asks for more than before.')).toBeInTheDocument()
  expect(within(d).getByText('+ fields:', { exact: false })).toHaveTextContent('Calendar link')
  expect(within(d).getByText('- fields:', { exact: false })).toHaveTextContent('Old link')
  expect(within(d).getByRole('list', { name: 'Full review' }).firstChild).toHaveTextContent('Connects to calendar.example.test')
  expect(within(d).queryByText('Show full review')).toBeNull()
  // The warning leads, then the permissions, then the diff, then the source (Codex a10c).
  const before = (a: HTMLElement, b: HTMLElement) => expect(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  const warning = within(d).getByText('This update asks for more than before.')
  const review = within(d).getByRole('list', { name: 'Full review' })
  before(warning, review)
  before(review, within(d).getByRole('region', { name: 'Update changes' }))
  expect(d.textContent!.indexOf('Connects to calendar.example.test')).toBeLessThan(d.textContent!.indexOf('from https://'))
  expect(state.host.calls.find((c) => c.path.endsWith('/market/stage'))?.body).toEqual({ id: 'calendar', update: true })
})
it('non-widened updates offer the compact disclosure', async () => {
  ws().marketStage = updateStage(false)
  const d = await openReview('installed')
  expect(within(d).getByText('Show full review').parentElement).not.toHaveAttribute('open')
  expect(within(d).getByText('Dependency lock updated')).toBeInTheDocument()
})
it('up to date makes no approval dialog', async () => {
  ws().marketStage = { up_to_date: true, name: 'calendar', version: '1.2.0' }
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Update...' }, T))
  await screen.findByText('Already up to date')
  expect(screen.queryByRole('dialog')).toBeNull()
})
it('marketplaces explain each publisher state and hub refusal', async () => {
  ws().market = { markets: (['unsigned', 'new', 'known', 'changed'] as const).map((key, i) => market({ url: `${market().url}${i}`, publisher_key: key })) }
  show('/w/DEMO/addons?tab=marketplaces')
  await screen.findByText('Matches remembered key', {}, T)
  for (const name of ['Allowed by', 'Added by', 'Publisher signature', 'Repo', 'Last refreshed']) expect(screen.getByRole('columnheader', { name })).toBeInTheDocument()
  for (const text of ['Unsigned', 'First use - key will be remembered', 'Key changed - review required']) expect(screen.getByText(text)).toBeInTheDocument()
  const add = screen.getByRole('button', { name: '+ Add marketplace' })
  expect(add).toBeDisabled()
  expect(add).toHaveAccessibleDescription(/external marketplaces aren't available in this workspace yet/i)
  expect(screen.getByText(EXTERNAL_HELP)).toBeInTheDocument()
})
it.each(['member', 'no-actions'] as const)('hides every write control for %s', async (mode) => {
  if (mode === 'member') {
    ws().role = 'member'
    ws().roster.find((m) => m.person === ws().me)!.role = 'member'
  } else ws().features = [...READS, 'market']
  show('/w/DEMO/addons')
  await screen.findByText('Available', {}, T)
  expect(screen.queryByRole('button', { name: 'Install...' })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: 'Installed' }))
  await screen.findByText('1.0.0')
  expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Update...' })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: 'Marketplaces' }))
  expect(screen.queryByRole('button', { name: '+ Add marketplace' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull()
})
it('requires turning an active add-on off before Remove', async () => {
  ws().marketInstalled = { addons: [installedAddon({ state: 'active' })] }
  show('/w/DEMO/addons?tab=installed')
  expect(await screen.findByRole('button', { name: 'Remove' }, T)).toBeDisabled()
  expect(screen.getByText('Turn it off in Settings > Add-ons first')).toBeInTheDocument()
})
it('expired stages must be reviewed again and cannot continue', async () => {
  ws().marketStage = stage({ expires_at: '2000-01-01T00:00:00Z' })
  const d = await openReview()
  expect(within(d).queryByRole('button', { name: 'Continue in Terminal' })).toBeNull()
  ws().marketStage = stage({ stage: 'demo-stage-new' })
  await userEvent.click(within(d).getByRole('button', { name: 'Review again' }))
  await screen.findByRole('button', { name: 'Continue in Terminal' })
  expect(state.host.calls.filter((c) => c.path.endsWith('/market/stage') && c.method === 'POST')).toHaveLength(2)
})
it('removes a disabled package only after confirmation', async () => {
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }, T))
  const d = screen.getByRole('dialog')
  expect(within(d).getByText('Its data stays until you purge it.')).toBeInTheDocument()
  expect(state.host.calls.some((c) => c.path.endsWith('/market/uninstall'))).toBe(false)
  await userEvent.click(within(d).getByRole('button', { name: 'Remove' }))
  await waitFor(() => expect(state.host.calls.find((c) => c.path.endsWith('/market/uninstall'))?.body).toEqual({ name: 'calendar' }))
})
it('host refuses marketplace writes for a non-owner even if a caller bypasses controls', async () => {
  ws().role = 'member'
  const result = await state.host.fetch(`/api/w/${A}/market/stage`, { method: 'POST', body: JSON.stringify({ id: 'calendar', url: market().url }) })
  expect(result.status).toBe(403)
  expect((await result.json()).error.code).toBe('permission.denied')
})
it('hides the permanent route when the host does not serve market', async () => {
  ws().features = [...READS, 'actions']
  show('/w/DEMO/addons')
  await screen.findByRole('link', { name: 'Board' }, T)
  expect(screen.queryByRole('link', { name: 'Add-ons' })).toBeNull()
  expect(state.host.calls.some((c) => c.path.endsWith('/market'))).toBe(false)
})
