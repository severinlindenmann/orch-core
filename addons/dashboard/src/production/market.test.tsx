import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'
import { market, stage, installedAddon, updateStage } from '@/test/fixtures/market'
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
  for (const text of ['Off', 'On', 'Waiting for installation approval', 'Withdrawn by its publisher']) expect(screen.getByText(text)).toBeInTheDocument()
  for (const card of screen.getAllByRole('article')) expect(within(card).getByText('official - demo-example/market - 1.2.0')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Add-ons' })).toHaveAttribute('href', '/w/DEMO/addons')
})
// Security review (UI provenance): publisher/marketplace text may only appear inside core's AddonFrame, never as bare core-looking UI.
const inFrame = (el: HTMLElement) => expect(el.closest('[data-addon]')).not.toBeNull()
it('shows publisher text only inside the AddonFrame: Browse, review, update diff, marketplaces', async () => {
  show('/w/DEMO/addons')
  inFrame(await screen.findByText('Connect calendars to tickets.', {}, T))
  await userEvent.click(screen.getByRole('button', { name: 'Marketplaces' }))
  inFrame(await screen.findByText('Official marketplace', { selector: 'span *, span' }, T))
})
it('the review summary sits inside the AddonFrame', async () => {
  const d = await openReview()
  inFrame(within(d).getByText('Calendar integration'))
})
it('update diff values from the package sit inside the AddonFrame', async () => {
  ws().marketStage = updateStage(true)
  const d = await openReview('installed')
  inFrame(within(d).getByText('+ fields:', { exact: false }))
  inFrame(within(d).getByText('- fields:', { exact: false }))
})
it('stages the exact source, hands off in a terminal and refreshes after submission', async () => {
  const d = await openReview()
  expect(state.host.calls.find((c) => c.path.endsWith('/market/stage'))?.body).toEqual({ id: 'calendar', url: market().url })
  expect(within(d).getByText('Next, review and approve the installation with one click. The add-on will be installed turned off.')).toBeInTheDocument()
  await userEvent.click(within(d).getByRole('button', { name: 'Review installation' }))
  await waitFor(() => expect(state.host.posts()).toHaveLength(1), T)
  expect(state.host.posts()[0].body).toEqual({ action: 'market_grant', name: 'calendar', staged: 'demo-stage' })
  expect(screen.getByText('Waiting for installation approval')).toBeInTheDocument()
  ws().market!.markets[0].addons[0].installed = { version: '1.2.0', state: 'disabled' }
  act(() => {
    state.host.submitted.set([...state.host.requests.keys()][0], 'DEMO-0001')
  })
  await screen.findByText('Installed · Turn it on under Installed', {}, T)
})
it('Cancel deletes the stage', async () => {
  const d = await openReview()
  await userEvent.click(within(d).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(state.host.calls.some((c) => c.method === 'DELETE' && c.path.endsWith('/market/stage/demo-stage'))).toBe(true))
})
it('expanded widened review marks new lines in host order and shows additions and removals', async () => {
  ws().marketStage = updateStage(true)
  const d = await openReview('installed')
  expect(within(d).getByText('This update asks for more than before.')).toBeInTheDocument()
  expect(within(d).getByText('+ fields:', { exact: false })).toHaveTextContent('Calendar link')
  expect(within(d).getByText('- fields:', { exact: false })).toHaveTextContent('Old link')
  expect(within(d).getByText('capabilities network:calendar.example.test', { exact: false }).closest('li')).toHaveClass('text-success')
  expect(within(d).queryByText('Show full review')).toBeNull()
  // The warning leads, then the permissions, then the diff, then the source (Codex a10c).
  const before = (a: HTMLElement, b: HTMLElement) => expect(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  const warning = within(d).getByText('This update asks for more than before.')
  const review = within(d).getByRole('list', { name: 'Full review' })
  before(warning, review)
  before(review, within(d).getByRole('region', { name: 'Update changes' }))
  expect(d.textContent!.indexOf('capabilities network:calendar.example.test')).toBeLessThan(d.textContent!.indexOf('from https://'))
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
  await screen.findByText('Publisher key matches the one previously saved on this computer', {}, T)
  for (const name of ['Allowed by', 'Added by', 'Publisher signature', 'Repo', 'Last refreshed']) expect(screen.getByRole('columnheader', { name })).toBeInTheDocument()
  for (const text of ['Unsigned', 'First use - key will be remembered', 'Key changed - review required']) expect(screen.getByText(text)).toBeInTheDocument()
  const add = screen.getByRole('button', { name: '+ Add marketplace' })
  expect(add).toBeDisabled()
  expect(add).toHaveAccessibleDescription(/only allows the official marketplace/i)
  expect(screen.getByRole('link', { name: 'View workspace admins' })).toBeInTheDocument()
})
it('run 9 picker: an offered source is one Add click then one Approve click; the address fold exists only when the policy allows any source', async () => {
  ws().market = { markets: [market()], offered: [{ url: 'https://github.com/acme/orch-dev-test', class: 'dev' }] }
  show('/w/DEMO/addons?tab=marketplaces')
  await userEvent.click(await screen.findByRole('button', { name: '+ Add marketplace' }, T))
  const sheet = screen.getByTestId('add-market-sheet')
  expect(within(sheet).getByText('Dev test marketplace')).toBeInTheDocument()
  expect(within(sheet).getByText('Already added')).toBeInTheDocument()
  expect(within(sheet).queryByTestId('add-market-address')).toBeNull()
  await userEvent.click(within(sheet).getByRole('button', { name: 'Add' }))
  expect(state.host.calls.some((c) => c.path.endsWith('/market/add'))).toBe(false) // review first
  await userEvent.click(within(sheet).getByRole('button', { name: 'Approve' }))
  await waitFor(() => expect(state.host.calls.find((c) => c.path.endsWith('/market/add'))?.body).toEqual({ url: 'https://github.com/acme/orch-dev-test' }), T)
})
it('run 9 picker: Add by address is a closed fold, shown only when the host says any source is allowed', async () => {
  ws().market = { markets: [market()], open_address: true }
  show('/w/DEMO/addons?tab=marketplaces')
  await userEvent.click(await screen.findByRole('button', { name: '+ Add marketplace' }, T))
  const fold = screen.getByTestId('add-market-address')
  expect(fold).not.toHaveAttribute('open')
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
it('run 9: an add-on installed from a marketplace has a row switch; with One click on the press itself signs, no second button', async () => {
  ws().click = { custody: 'convenience', enabled: true, available: true }
  ws().features = [...ws().features, 'click']
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('switch', { name: 'calendar' }, T))
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
  expect(state.host.calls.find((c) => c.path.endsWith('/click/prepare'))?.body).toEqual({ action: 'addon_grant', name: 'calendar' })
  expect(screen.queryByRole('dialog')).toBeNull() // the review lives under the row
})
it('with One click off, the marketplace add-on switch opens the terminal steps under its row', async () => {
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('switch', { name: 'calendar' }, T))
  expect(await screen.findByTestId('addon-calendar-accordion', {}, T)).toBeInTheDocument()
  await waitFor(() => expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'addon_grant', name: 'calendar' }]), T)
})
it('with One click on, install is the signer review and one Sign press: no Terminal, no typed word (V3-D38)', async () => {
  ws().click = { custody: 'convenience', enabled: true, available: true }
  ws().features = [...ws().features, 'click']
  const d = await openReview()
  expect(within(d).queryByText(/Terminal/)).toBeNull()
  await userEvent.click(within(d).getByRole('button', { name: 'Review installation' }))
  expect(await screen.findByTestId('click-panel', {}, T)).toHaveTextContent('FROM-THE-SIGNER addon.grant') // the signer's own review, not the page's
  expect(state.host.posts().filter((c) => c.path.endsWith('/handoff'))).toHaveLength(0)
  expect(state.host.calls.find((c) => c.path.endsWith('/click/prepare'))?.body).toEqual({ action: 'market_grant', name: 'calendar', staged: 'demo-stage' })
  expect(screen.queryByRole('textbox')).toBeNull() // no word to type
  const sign = await screen.findByRole('button', { name: 'Approve and install' }, T)
  await waitFor(() => expect(sign).toBeEnabled(), { timeout: 5000 })
  await userEvent.click(sign)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
})
it('with One click on, stale, dev and widened warnings stay visible above the signer review (V3-D38 L2)', async () => {
  ws().click = { custody: 'convenience', enabled: true, available: true }
  ws().features = [...ws().features, 'click']
  const base = updateStage(true)
  ws().marketStage = { ...base, market: { ...base.market, stale: true, class: 'dev' }, update: { ...base.update!, widened: true } }
  const d = await openReview('installed')
  await userEvent.click(within(d).getByRole('button', { name: 'Review installation' }))
  const lead = await screen.findByTestId('sign-lead', {}, T)
  expect(within(lead).getByRole('alert')).toHaveTextContent('This update asks for more than before.')
  expect(lead).toHaveTextContent('out of date')
  expect(lead).toHaveTextContent('test marketplace (dev sandbox only)')
  expect(lead).toHaveTextContent('not signed')
  expect(lead).toHaveTextContent('not verified')
})
it('the review frames say the text is unverified, never that the publisher wrote it', async () => {
  const d = await openReview()
  expect(within(d).getByText('From the add-on package (not verified)')).toBeInTheDocument()
  expect(within(d).queryByText(/provided by the add-on publisher/i)).toBeNull()
})
it('expired stages must be reviewed again and cannot continue', async () => {
  ws().marketStage = stage({ expires_at: '2000-01-01T00:00:00Z' })
  const d = await openReview()
  expect(within(d).queryByRole('button', { name: 'Review installation' })).toBeNull()
  ws().marketStage = stage({ stage: 'demo-stage-new' })
  await userEvent.click(within(d).getByRole('button', { name: 'Review again' }))
  await screen.findByRole('button', { name: 'Review installation' })
  expect(state.host.calls.filter((c) => c.path.endsWith('/market/stage') && c.method === 'POST')).toHaveLength(2)
})
it('removes a disabled package only after confirmation', async () => {
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }, T))
  const d = screen.getByRole('dialog')
  expect(within(d).getByText(/Its saved settings and data stay on this computer/)).toBeInTheDocument()
  expect(within(d).getByRole('checkbox', { name: 'Also delete its data' })).not.toBeChecked()
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
it('a host without the market ops still has the Add-ons page: built-in add-ons only, no marketplace tabs and no market call', async () => {
  ws().features = [...READS, 'actions', 'settings']
  show('/w/DEMO/addons')
  await screen.findByRole('heading', { name: 'Built in' }, T)
  expect(screen.queryByRole('button', { name: 'Browse' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Marketplaces' })).toBeNull()
  expect(state.host.calls.some((c) => c.path.endsWith('/market'))).toBe(false)
})
it('run-9 feedback: the unfetched official marketplace is fetched on the first visit and shows a GitHub link', async () => {
  const preset = market({ preset: true, cache: 'not_fetched', addons: [], entries: 0, last_fetch: null, index_commit: '' as never })
  ws().market = { markets: [preset] }
  show('/w/DEMO/addons?tab=marketplaces')
  const link = await screen.findByRole('link', { name: /GitHub/ }, T)
  expect(link).toHaveAttribute('href', preset.url)
  expect(link).toHaveAttribute('target', '_blank')
  await waitFor(() => expect(state.host.calls.filter((c) => c.path.endsWith('/market/refresh')).map((c) => c.body)).toEqual([{ url: preset.url }]), T)
  expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull()
})

it('run 9 remove sheet: an On add-on says it is turned off first, then Turn off and remove signs the off step and removes it', async () => {
  ws().click = { custody: 'convenience', enabled: true, available: true }
  ws().features = [...ws().features, 'click']
  ws().marketInstalled = { addons: [installedAddon({ state: 'active' })] }
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }, T))
  const d = screen.getByTestId('remove-sheet')
  expect(d).toHaveTextContent('we will turn it off first')
  await userEvent.click(within(d).getByRole('button', { name: 'Turn off and remove' }))
  await waitFor(() => expect(state.host.calls.find((c) => c.path.endsWith('/click/prepare'))?.body).toEqual({ action: 'addon_disable', name: 'calendar' }), T)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
  await waitFor(() => expect(state.host.calls.find((c) => c.path.endsWith('/market/uninstall'))?.body).toEqual({ name: 'calendar' }), T)
})

it('run 9 side panel: Settings opens a panel beside the list with controls from the schema; Save is one press with One click on', async () => {
  ws().click = { custody: 'convenience', enabled: true, available: true }
  ws().features = [...ws().features, 'click']
  ws().marketInstalled = { addons: [installedAddon({ state: 'active' })] }
  ws().addonSettings = {
    calendar: {
      name: 'calendar',
      title: 'Calendar',
      schema: { type: 'object', properties: { mode: { type: 'string', title: 'Mode', enum: ['day', 'week'] }, limit: { type: 'integer', title: 'Limit', minimum: 100, maximum: 1000, multipleOf: 100 } } },
      value: { mode: 'day', limit: 200 },
    },
  }
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Settings' }, T))
  const sheet = await screen.findByTestId('settings-sheet', {}, T)
  expect(within(sheet).getByRole('button', { name: 'Save' })).toBeDisabled() // unchanged
  await userEvent.click(await within(sheet).findByRole('radio', { name: 'week' }, T))
  await userEvent.click(within(sheet).getByRole('button', { name: 'More Limit' }))
  await userEvent.click(within(sheet).getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(state.host.calls.find((c) => c.path.endsWith('/click/prepare'))?.body).toEqual({ action: 'addon_settings', name: 'calendar', value: { mode: 'week', limit: 300 } }), T)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
})
