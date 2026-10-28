import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { ARM_MS } from '@/api/click'
import * as gesture from '@/components/sign/trustedPress'
import { READS, show, trustedPresses, useFakeHost, workspace } from '@/test/hostHarness'
import { openInfo } from '@/test/info'
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
afterEach(() => vi.restoreAllMocks())
const clickOn = () => {
  ws().click = { custody: 'convenience', enabled: true, available: true }
  ws().features = [...ws().features, 'click']
}
/** The press that opened the review was a real one (jsdom events are untrusted): the review prepares as it opens. */
const trusted = () => vi.spyOn(gesture, 'takeTrustedPress').mockReturnValue(true)
/** The signer's Sign button, pressed for real (trustedPresses: jsdom events are untrusted) once it is armed (1 s on screen). */
const sign = async (name: string | RegExp) => {
  const button = await screen.findByRole('button', { name }, T)
  await waitFor(() => expect(button).toBeEnabled(), { timeout: ARM_MS + 3000 })
  trustedPresses()
  await userEvent.click(button)
}
/** C3: a View without risk lines, prepared by the press: the button shows the signer's headline during the 1 s arm, then it signs once. */
const armSigns = async (op: string) => {
  expect(await screen.findByRole('button', { name: `FROM-THE-SIGNER headline ${op}` }, T)).toBeEnabled()
  expect(state.host.clickSigned).toHaveLength(0)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), { timeout: ARM_MS + 3000 })
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
  for (const card of screen.getAllByRole('article')) expect(within(card).getByText('official - demo-example/market')).toBeInTheDocument()
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
  expect(screen.getAllByRole('button', { name: 'About this marketplace' })).toHaveLength(4)
  for (const text of ['Unsigned', 'First use - key will be remembered', 'Key changed - review required']) expect(screen.getByText(text)).toBeInTheDocument()
  const add = screen.getByRole('button', { name: '+ Add marketplace' })
  expect(add).toBeDisabled()
  expect(add).toHaveAccessibleDescription(/only allows the official marketplace/i)
  expect(screen.getByRole('link', { name: 'View workspace admins' })).toBeInTheDocument()
})
it('run 9 picker: an offered source is a single Add click; the address fold exists only when the policy allows any source', async () => {
  ws().market = { markets: [market()], offered: [{ url: 'https://github.com/acme/orch-dev-test', class: 'dev' }] }
  show('/w/DEMO/addons?tab=marketplaces')
  await userEvent.click(await screen.findByRole('button', { name: '+ Add marketplace' }, T))
  const sheet = screen.getByTestId('add-market-sheet')
  expect(within(sheet).getByText('Dev test marketplace')).toBeInTheDocument()
  expect(within(sheet).getByText('Already added')).toBeInTheDocument()
  expect(within(sheet).queryByTestId('add-market-address')).toBeNull()
  await userEvent.click(within(sheet).getByRole('button', { name: 'Add' }))
  await waitFor(() => expect(state.host.calls.filter((c) => c.path.endsWith('/market/add')).map((c) => c.body)).toEqual([{ url: 'https://github.com/acme/orch-dev-test' }]), T)
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull() // no second step
  await screen.findByText('Marketplace added. Its add-ons are in Browse.', {}, T)
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
  // The version sits behind the row's "i": informational only, the drawer holds no control.
  const versionInfo = await openInfo(await screen.findByTestId('addon-calendar', {}, T))
  expect(versionInfo).toHaveTextContent('Version 1.0.0')
  expect(within(versionInfo).queryAllByRole('button')).toHaveLength(0)
  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Update...' })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: 'Marketplaces' }))
  await userEvent.click(await screen.findByRole('button', { name: 'About this marketplace' }, T))
  const about = await screen.findByTestId('info-drawer-content')
  expect(about).toHaveTextContent('Allowed by:')
  expect(within(about).queryAllByRole('button')).toHaveLength(0)
  expect(screen.queryByRole('button', { name: '+ Add marketplace' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull()
})
it('run 9: an add-on installed from a marketplace has a row switch; with One click on the press prepares and one armed Sign press signs, under the row', async () => {
  clickOn()
  trusted()
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('switch', { name: 'calendar' }, T))
  const accordion = await screen.findByTestId('addon-calendar-accordion', {}, T)
  await within(accordion).findByTestId('click-panel', {}, T)
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_grant', name: 'calendar' }])
  await sign(/^Sign( |$)/)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
  expect(screen.queryByRole('dialog')).toBeNull() // the review lives under the row
})
it('with One click off, the marketplace add-on switch says One click is not ready under its row; nothing is prepared', async () => {
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('switch', { name: 'calendar' }, T))
  const accordion = await screen.findByTestId('addon-calendar-accordion', {}, T)
  expect(accordion).toHaveTextContent('One click is not ready on this computer.')
  expect(within(accordion).getByRole('link', { name: 'Turn on One click' })).toBeInTheDocument()
  expect(state.host.clickPrepares()).toEqual([])
  expect(state.host.posts()).toEqual([])
})




it('removes a disabled package with one press: no confirm dialog, its data is kept', async () => {
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }, T))
  expect(screen.queryByRole('dialog')).toBeNull()
  await waitFor(() => expect(state.host.calls.filter((c) => c.path.endsWith('/market/uninstall')).map((c) => c.body)).toEqual([{ name: 'calendar' }]), T)
  expect(state.host.calls.some((c) => c.path.endsWith('/click/prepare') || c.path.endsWith('/handoff'))).toBe(false) // an Off package needs no signature
  await screen.findByText('calendar removed. Install it again from Browse.', {}, T)
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

it('run 9 remove: an On add-on\'s Remove opens the "Turn off and remove" review; its signature is the only confirmation, then it is removed', async () => {
  clickOn()
  trusted() // the Remove press was a real one: the off step prepares as the review opens
  ws().marketInstalled = { addons: [installedAddon({ state: 'active' })] }
  show('/w/DEMO/addons?tab=installed')
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }, T))
  const d = screen.getByTestId('remove-sheet')
  expect(d).toHaveTextContent('Saved settings and data stay on this computer.')
  await within(d).findByTestId('click-panel', {}, T)
  expect(d).toHaveTextContent('FROM-THE-SIGNER headline addon.disable') // C3: once prepared, the title is the signer's headline
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_disable', name: 'calendar' }])
  expect(state.host.calls.some((c) => c.path.endsWith('/market/uninstall'))).toBe(false) // not before the signature
  await armSigns('addon.disable') // turning off carries no risk lines: the Remove press signs at the end of the 1 s arm
  await waitFor(() => expect(state.host.calls.find((c) => c.path.endsWith('/market/uninstall'))?.body).toEqual({ name: 'calendar' }), T)
})

it('run 9 side panel: Settings opens a panel beside the list with controls from the schema; Save prepares with One click and one armed press signs', async () => {
  clickOn()
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
  trusted()
  await userEvent.click(within(sheet).getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(state.host.calls.find((c) => c.path.endsWith('/click/prepare'))?.body).toEqual({ action: 'addon_settings', name: 'calendar', value: { mode: 'week', limit: 300 } }), T)
  // C3: settings.set is not a daily op: its View carries risk lines, so the Save press never signs; the second press does
  expect(await screen.findByTestId('sign-risk-lines', {}, T)).toHaveTextContent('FROM-THE-SIGNER risk: not a daily action')
  expect(screen.queryByTestId('sign-progress')).toBeNull()
  await new Promise((r) => setTimeout(r, ARM_MS + 200))
  expect(state.host.clickSigned).toHaveLength(0)
  const button = screen.getByTestId('sign-button') // the Sign of the signer's review (the sheet itself holds Save)
  await waitFor(() => expect(button).toBeEnabled(), { timeout: ARM_MS + 3000 })
  trustedPresses()
  await userEvent.click(button)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
})


it('D3: the install press opens the signer inline; risk waits for a second press', async () => {
  clickOn()
  trusted()
  show('/w/DEMO/addons?tab=browse')
  await userEvent.click(await screen.findByRole('button', { name: 'Install...' }, T))
  const panel = await screen.findByTestId('click-panel', {}, T)
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(panel).toHaveTextContent('FROM-THE-SIGNER addon.grant')
  expect(screen.getByTestId('install-summary')).toHaveTextContent('calendar · Version 1.2.0 · Publisher unsigned')
  expect(state.host.clickPrepares().map(c => c.body)).toEqual([{ action: 'market_grant', name: 'calendar', staged: 'demo-stage', grant: stage().grant, settings: {} }])
  const button = await screen.findByRole('button', { name: 'Install and turn on' }, T)
  await waitFor(() => expect(button).toBeEnabled(), { timeout: ARM_MS + 3000 })
  expect(state.host.clickSigned).toHaveLength(0)
  trustedPresses()
  await userEvent.click(button)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
})

it('D3: required defaults are inline, editable, and posted exactly as displayed', async () => {
  clickOn()
  trusted()
  ws().marketStage = stage({ settings_schema: { type: 'object', required: ['channel'], properties: { channel: { type: 'string', default: 'general' } } } })
  show('/w/DEMO/addons?tab=browse')
  await userEvent.click(await screen.findByRole('button', { name: 'Install...' }, T))
  const input = await screen.findByRole('textbox', { name: 'channel' }, T)
  expect(input).toHaveValue('general')
  expect(state.host.clickPrepares()).toHaveLength(0)
  await userEvent.clear(input)
  await userEvent.type(input, 'team channel')
  await userEvent.click(screen.getByRole('button', { name: 'Install and turn on' }))
  expect(await screen.findByTestId('click-panel', {}, T)).toHaveTextContent('FROM-THE-SIGNER settings.set')
  expect(state.host.clickPrepares()[0].body).toEqual({ action: 'market_grant', name: 'calendar', staged: 'demo-stage', grant: stage().grant, settings: { channel: 'team channel' } })
  const confirm = await screen.findByRole('button', { name: 'Install and turn on' }, T)
  await waitFor(() => expect(confirm).toBeEnabled(), { timeout: ARM_MS + 3000 })
  expect(state.host.clickSigned).toHaveLength(0)
})

it('D3: cancelling inline setup consumes the unsigned stage', async () => {
  ws().marketStage = stage({ settings_schema: { type: 'object', required: ['channel'], properties: { channel: { type: 'string' } } } })
  show('/w/DEMO/addons?tab=browse')
  await userEvent.click(await screen.findByRole('button', { name: 'Install...' }, T))
  const setup = await screen.findByRole('region', { name: 'Install add-on' }, T)
  await userEvent.click(within(setup).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(state.host.calls.some(c => c.method === 'DELETE' && c.path.endsWith('/market/stage/demo-stage'))).toBe(true))
  expect(state.host.clickSigned).toHaveLength(0)
})

it('update diff values from the package sit inside the AddonFrame', async () => {
  ws().marketStage = updateStage(true)
  const d = await openReview('installed')
  inFrame(within(d).getByText('+ fields:', { exact: false }))
  inFrame(within(d).getByText('- fields:', { exact: false }))
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

it('with One click on, stale, dev and widened warnings stay visible above the signer review (V3-D38 L2)', async () => {
  clickOn()
  const base = updateStage(true)
  ws().marketStage = { ...base, market: { ...base.market, stale: true, class: 'dev' }, update: { ...base.update!, widened: true } }
  const d = await openReview('installed')
  trusted()
  await userEvent.click(within(d).getByRole('button', { name: 'Review installation' }))
  const panel = await screen.findByTestId('click-panel', {}, T)
  const lead = screen.getByTestId('install-summary')
  expect(lead.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(within(lead).getByRole('alert')).toHaveTextContent('This update asks for more than before.')
  expect(lead).toHaveTextContent('out of date')
  expect(lead).toHaveTextContent('test marketplace (dev sandbox only)')
  expect(lead).toHaveTextContent('not signed')
  expect(lead).toHaveTextContent('not verified')
})

async function openReview(tab = 'installed', state = 'enabled') {
  ws().marketInstalled = { addons: [installedAddon({ state })] }
  show(`/w/DEMO/addons?tab=${tab}`)
  await userEvent.click(await screen.findByRole('button', { name: 'Update...' }, T))
  return screen.findByRole('dialog', {}, T)
}

it.each(['enabled', 'active'])('%s updates use the separate grant review and require a second press for new capabilities', async status => {
  clickOn()
  ws().marketStage = updateStage(true)
  const d = await openReview('installed', status)
  expect(within(d).getByText('This update asks for more than before.')).toBeInTheDocument()
  trusted()
  await userEvent.click(within(d).getByRole('button', { name: 'Review installation' }))
  await screen.findByTestId('click-panel', {}, T)
  expect(state.host.clickPrepares().map(c => c.body)).toEqual([{
    action: 'market_update', name: 'calendar', staged: 'demo-stage',
    digest: stage().package_sha256, caps: stage().grant.capabilities,
  }])
  const button = await screen.findByRole('button', { name: 'Approve and install' }, T)
  await waitFor(() => expect(button).toBeEnabled(), { timeout: ARM_MS + 3000 })
  expect(state.host.clickSigned).toHaveLength(0)
  trustedPresses()
  await userEvent.click(button)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
})

it('expired update reviews discard the old stage before reviewing again', async () => {
  ws().marketStage = { ...updateStage(true), expires_at: 1 }
  const d = await openReview()
  expect(within(d).queryByRole('button', { name: 'Review installation' })).toBeNull()
  ws().marketStage = { ...updateStage(true), stage: 'demo-stage-new' }
  await userEvent.click(within(d).getByRole('button', { name: 'Review again' }))
  await screen.findByRole('button', { name: 'Review installation' }, T)
  expect(state.host.calls.some(c => c.method === 'DELETE' && c.path.endsWith('/market/stage/demo-stage'))).toBe(true)
  expect(state.host.calls.filter(c => c.method === 'POST' && c.path.endsWith('/market/stage'))).toHaveLength(2)
  expect(state.host.clickPrepares()).toHaveLength(0)
})
