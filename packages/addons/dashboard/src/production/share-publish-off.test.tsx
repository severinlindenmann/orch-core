import { api } from '@/api/client'
import { toast } from 'sonner'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { queryClient } from '@/production/Shell'
import ticket from '@/test/fixtures/server-m2/ticket.json'
import { READS, clickOn, show, trustedPresses, useFakeHost, workspace } from '@/test/hostHarness'
import { market, installedAddon, stage } from '@/test/fixtures/market'

// P05 (J07-08): Publish is easy to find while it is off. It is listed in Add-ons > Browse with Turn on, and the ticket's
// Actions menu has Share files... which opens the turn-on review first.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const row = (state: 'on' | 'off') => ({ id: 'publish', title: 'Publish', desc: 'Share files of a ticket with a link.', state, locked: null, blocked: null, needs: 'publish', set_up: true })
const state = useFakeHost(() => [
  workspace({ id: A, features: [...READS, 'actions', 'market'], addOns: { available: true, add_ons: [row('off'), { id: 'dashboard', title: 'Dashboard', desc: 'x', state: 'on', locked: 'Always on', blocked: null }] }, market: { markets: [market()] }, marketInstalled: { addons: [installedAddon()] }, marketStage: stage() } as never),
])
const ws = () => state.host.workspaces[0]
afterEach(() => vi.restoreAllMocks())

it('Browse lists the built-in add-ons that are off, each with Turn on, and not the ones that are on', async () => {
  show('/w/DEMO/addons?tab=browse')
  const list = await screen.findByRole('list', { name: 'Built-in add-ons that are off' }, T)
  expect(within(list).getByText('Publish')).toBeInTheDocument()
  expect(within(list).getByRole('button', { name: 'Turn on Publish' })).toBeEnabled()
  expect(within(list).queryByText('Dashboard')).toBeNull() // always on: nothing to turn on
  expect(within(list).queryByRole('switch')).toBeNull()
})

it('Browse shows no Built in group once everything is on', async () => {
  ws().addOns = { available: true, add_ons: [row('on')] } as never
  show('/w/DEMO/addons?tab=browse')
  await screen.findByLabelText('Search add-ons', {}, T)
  expect(screen.queryByRole('list', { name: 'Built-in add-ons that are off' })).toBeNull()
})

const withTicket = () => {
  state.host.override = (path, method) =>
    path.endsWith('/tickets/DEMO-0001') && method === 'GET'
      ? Response.json({ ...ticket, artifacts: [{ name: 'report.md', kind: 'report', bytes: 2048, sha256: 'ab'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }] })
      : undefined
}

it('the ticket Actions menu offers Share files... while Publish is off and opens the turn-on review first', async () => {
  withTicket()
  clickOn(ws())
  window.innerWidth = 1400
  const user = userEvent.setup()
  show('/w/DEMO/ticket/DEMO-0001')
  await user.click(await screen.findByRole('button', { name: /Actions/ }, T))
  await user.click(await screen.findByRole('menuitem', { name: 'Share…' }, T))
  await user.click(await screen.findByRole('button', { name: 'Make a link to a file' }, T))
  const dialog = await screen.findByRole('dialog', { name: 'Turn on Publish' }, T)
  expect(dialog).toHaveTextContent('Nothing leaves this computer until you sign')
  expect(within(dialog).getByRole('button', { name: 'Turn on Publish' })).toBeInTheDocument()
  expect(screen.queryByRole('dialog', { name: 'Share files' })).toBeNull()
})

it('with Publish on, Share files... opens the share dialog straight away', async () => {
  withTicket()
  ws().features = [...ws().features, 'publish']
  ws().addOns = { available: true, add_ons: [row('on')] } as never
  const inner = state.host.override!
  state.host.override = (path, method, body) => (path.includes('/shares') && method === 'GET' ? Response.json({ available: true, configured: true, server: 'http://127.0.0.1:8103', share: 'http://127.0.0.1:8104', workspace: A, owner: 'p_owner', live: true, shares: [], staged: [] }) : inner(path, method, body))
  clickOn(ws())
  window.innerWidth = 1400
  const user = userEvent.setup()
  show('/w/DEMO/ticket/DEMO-0001')
  await user.click(await screen.findByRole('button', { name: /Actions/ }, T))
  await user.click(await screen.findByRole('menuitem', { name: 'Share…' }, T))
  await user.click(await screen.findByRole('button', { name: 'Make a link to a file' }, T))
  const dialog = await screen.findByRole('dialog', { name: 'Share files' }, T)
  expect(within(dialog).getByRole('button', { name: 'Add a file' })).toBeInTheDocument()
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Turn on Publish' })).toBeNull(), T)
})

// P04 (J07-04) + run17 P01b: Share files... with Publish off and not set up. When the address is already known (suggested)
// there is NO address dialog: the host's review opens at once, prepared only, and Sign stays the person's own press.
// Only when nothing is suggested does the one address dialog remain (its button goes straight to the review).
const notSetUp = (suggest: string | null) => {
  ws().addOns = { available: true, add_ons: [{ ...row('off'), set_up: false, suggest }] } as never
}
const openShare = async (user: ReturnType<typeof userEvent.setup>) => {
  withTicket()
  clickOn(ws())
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  await user.click(await screen.findByRole('button', { name: /Actions/ }, T))
  await user.click(await screen.findByRole('menuitem', { name: 'Share…' }, T))
  await user.click(await screen.findByRole('button', { name: 'Make a link to a file' }, T))
}

it('run17 P01b: a suggested address skips the address dialog; the review opens at once, nothing is signed', async () => {
  notSetUp('http://127.0.0.1:8102')
  const user = userEvent.setup()
  await openShare(user)
  await screen.findByTestId('click-panel', {}, T)
  expect(screen.queryByRole('button', { name: 'Turn on and sign' })).toBeNull() // no address dialog
  expect(screen.queryByLabelText('Publish server address')).toBeNull()
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_enable', name: 'publish', publish: { server: 'http://127.0.0.1:8102', share: 'http://127.0.0.1:8102' } }])
  expect(await screen.findByRole('button', { name: /^Sign( |$)/ }, T)).toBeInTheDocument()
  expect(state.host.clickSigned).toEqual([]) // never armed or signed by itself
})

it('run17 P01b: a hub-suggested address shows "Files go to your hub, <name>." in the review lead before Sign', async () => {
  ws().addOns = { available: true, add_ons: [{ ...row('off'), set_up: false, suggest: 'http://127.0.0.1:8102', suggest_from: 'hub', suggest_hub: 'Anna’s hub' }] } as never
  const user = userEvent.setup()
  await openShare(user)
  await screen.findByTestId('click-panel', {}, T)
  expect(await screen.findByTestId('where-files-go', {}, T)).toHaveTextContent('Files go to your hub, Anna’s hub.')
  expect(screen.getByRole('button', { name: /^Sign( |$)/ })).toBeInTheDocument()
  expect(state.host.clickSigned).toEqual([])
})

it('run17 P01b: a suggestion that appears after the form was shown does not replace the form', async () => {
  notSetUp(null)
  const user = userEvent.setup()
  await openShare(user)
  const dialog = await screen.findByRole('dialog', { name: 'Turn on Publish' }, T)
  await user.type(within(dialog).getByLabelText('Publish server address'), 'http://127.0.0.1:9')
  notSetUp('http://127.0.0.1:8102')
  await queryClient.invalidateQueries({ queryKey: ['add-ons', A] })
  await new Promise((r) => setTimeout(r, 300))
  expect(screen.queryByTestId('click-panel')).toBeNull()
  expect(within(screen.getByRole('dialog')).getByLabelText('Publish server address')).toHaveValue('http://127.0.0.1:9')
  expect(state.host.clickPrepares()).toEqual([])
})

it('P04: with nothing suggested the one address dialog stays and its button goes straight to Sign', async () => {
  notSetUp(null)
  const user = userEvent.setup()
  await openShare(user)
  const dialog = await screen.findByRole('dialog', { name: 'Turn on Publish' }, T)
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
  expect(within(dialog).queryByRole('list')).toBeNull() // no add-on list inside
  expect(within(dialog).getByLabelText('Publish server address')).toBeInTheDocument()
  expect(state.host.clickPrepares()).toEqual([]) // nothing prepared before the press
  await user.type(within(dialog).getByLabelText('Publish server address'), 'http://127.0.0.1:8102')
  await user.click(within(dialog).getByRole('button', { name: 'Turn on and sign' }))
  await screen.findByTestId('click-panel', {}, T)
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_enable', name: 'publish', publish: { server: 'http://127.0.0.1:8102', share: 'http://127.0.0.1:8102' } }])
  expect(screen.queryByRole('button', { name: 'Turn on and sign' })).toBeNull()
  expect(state.host.clickSigned).toEqual([])
})

it('P04: Cancel at the review withdraws the open request and leaves Publish off', async () => {
  notSetUp('http://127.0.0.1:8102')
  const user = userEvent.setup()
  await openShare(user)
  await screen.findByTestId('click-panel', {}, T)
  await user.click(await screen.findByRole('button', { name: 'Cancel' }, T))
  await waitFor(() => expect(new Set(state.host.clickCancelled).size).toBe(1), T) // the one prepared request, withdrawn
  await waitFor(() => expect(screen.queryByTestId('click-panel')).toBeNull(), T)
  expect(screen.queryByRole('dialog', { name: 'Share files' })).toBeNull()
})

it('P04: when the settings step lands (set_up flips to true) the review stays mounted; Sign runs through and the share dialog follows', async () => {
  notSetUp('http://127.0.0.1:8102')
  const success = vi.spyOn(toast, 'success')
  const user = userEvent.setup()
  await openShare(user)
  const inner = state.host.override!
  state.host.override = (path, method, body) => (path.includes('/shares') && method === 'GET' ? Response.json({ available: true, configured: true, server: 'http://127.0.0.1:8102', share: 'http://127.0.0.1:8102', workspace: A, owner: 'p_owner', live: true, shares: [], staged: [] }) : inner(path, method, body))
  await screen.findByTestId('click-panel', {}, T)
  // the first step is in: the host now says the settings are set, Publish still off; the page refetches
  ws().addOns = { available: true, add_ons: [{ ...row('off'), set_up: true }] } as never
  const reads = () => state.host.calls.filter((c) => c.path.endsWith('/add-ons') && c.method === 'GET').length
  const before = reads()
  await queryClient.invalidateQueries({ queryKey: ['add-ons', A] })
  await waitFor(() => expect(reads()).toBeGreaterThan(before), T)
  expect(screen.getByTestId('click-panel')).toBeInTheDocument() // the review did not unmount between the steps
  expect(screen.queryByRole('list', { name: 'Publish' })).toBeNull() // and no list dialog came back
  const sign = await screen.findByRole('button', { name: /^Sign( |$)/ }, T)
  await waitFor(() => expect(sign).toBeEnabled(), { timeout: 8000 })
  trustedPresses() // the armed Sign signs only on a trusted press
  await user.click(sign)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
  expect(success).not.toHaveBeenCalled()
  // the host now reports Publish on (the onOn path): the share dialog follows, the turn-on review is gone
  ws().features = [...ws().features, 'publish']
  ws().addOns = { available: true, add_ons: [row('on')] } as never
  await queryClient.invalidateQueries({ queryKey: ['add-ons', A] })
  expect(await screen.findByRole('dialog', { name: 'Share files' }, T)).toBeInTheDocument()
  expect(screen.queryByTestId('click-panel')).toBeNull()
})

it.each([false, true])('destination and risk remain with quiet built-ins: %s', async quiet => {
  if (!quiet) notSetUp('https://publish.example.test')
  const original = api.viewClick
  vi.spyOn(api, 'viewClick').mockImplementation(async (...args) => {
    const answer = await original(...args)
    return { ...answer, view: { ...answer.view, content: { headline: 'Turn on Publish', summary: { title: 'Unrelated', lines: quiet ? ['Files go to publish.example.test.'] : ['Runs on this computer.'], risk: 'Anyone with the link can read the files.' } } } }
  })
  const success = vi.spyOn(toast, 'success')
  const user = userEvent.setup()
  await openShare(user)
  if (quiet) {
    await user.click(await screen.findByRole('button', { name: 'Turn on Publish' }, T))
    const accordion = await screen.findByTestId('addon-publish-accordion', {}, T)
    await user.click(within(accordion).getByRole('button', { name: 'Turn on Publish' }))
  }
  await screen.findByTestId('sign-summary', {}, T)
  if (quiet) expect(screen.getByTestId('sign-summary')).toHaveTextContent('Files go to publish.example.test.')
  else expect(screen.getByTestId('where-files-go')).toHaveTextContent('Files go to publish.example.test.')
  expect(screen.getByTestId('sign-risk')).toHaveTextContent('Anyone with the link can read the files.')
  const sign = await screen.findByRole('button', { name: /^Sign( |$)/ }, T)
  await waitFor(() => expect(sign).toBeEnabled(), T)
  trustedPresses()
  await user.click(sign)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
  expect(success).not.toHaveBeenCalled()
})
