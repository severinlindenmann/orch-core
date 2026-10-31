import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { READS, clickOn, show, useFakeHost, workspace } from '@/test/hostHarness'

// #1838 step D: one click to share through the hub. The Publish row (from the dashboard server) carries `suggest_where`
// {server, share}: what the hub signed and core verified. Settings > Hub offers one line and one button; a hub that moved
// pauses sharing with "Use the new address"; a hub that was left says so on Shares. The pre-fill is the row, nothing else.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const SERVER = 'https://pub.example.org'
const SHARE = 'https://share.example.org'
const NEW_SHARE = 'https://links.example.net'
const HUB = { mode: 'selective', joined: true, hub_id: 'b'.repeat(32), member: true, link: 'bound', reachable: true, not_told: false, owed: 0, reseal_needed: false, key_change_pending: false, log_sync_stuck: false, groups: null, snapshot_version: null }
const base = { id: 'publish', title: 'Publish', desc: 'Share files of a ticket with a link.', state: 'off' as const, locked: null, blocked: null, needs: 'publish', set_up: false }
const fromHub = { suggest: SERVER, suggest_where: { server: SERVER, share: SHARE }, suggest_from: 'hub', suggest_hub: 'Anna’s hub' }
const NEW_SERVER = 'https://api.new.example'
const moved = { hub_moved: { server: SERVER, share: NEW_SHARE } }

const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'actions', 'hub', 'publish'] } as never)])
const ws = () => state.host.workspaces[0]
afterEach(() => vi.restoreAllMocks())

const publish = (over: Record<string, unknown>) => {
  ws().addOns = { available: true, add_ons: [{ ...base, ...over }] } as never
}
const serve = (shares?: Record<string, unknown>) => {
  state.host.override = (path, method) => {
    if (method !== 'GET') return undefined
    if (path.endsWith('/hub')) return Response.json(HUB)
    if (path.endsWith('/shares')) return Response.json({ available: true, configured: true, server: SERVER, share: SHARE, workspace: A, owner: 'p_owner', live: true, shares: [], staged: [], ...shares })
    return undefined
  }
}
const hubTab = () => {
  clickOn(ws())
  show('/w/DEMO/settings/hub')
}
const settle = async () => {
  await screen.findByTestId('hub-tab', {}, T)
  await new Promise((r) => setTimeout(r, 300))
}

it('Settings > Hub, joined, a suggestion from the hub: one line and one button; one click opens the review with both values; nothing is signed', async () => {
  publish(fromHub)
  serve()
  hubTab()
  const line = await screen.findByTestId('hub-share-line', {}, T)
  expect(line).toHaveTextContent('Share links through share.example.org')
  expect(line).not.toHaveTextContent('https://') // by name, never by address
  expect(state.host.clickPrepares()).toEqual([]) // nothing is prepared before the press
  await userEvent.setup().click(screen.getByRole('button', { name: 'Turn on sharing' }))
  await screen.findByTestId('click-panel', {}, T)
  // the pair: different server and share, both sent, no typing
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_enable', name: 'publish', publish: { server: SERVER, share: SHARE } }])
  // the hub's name is not signed: the signed server host is named with it
  expect(await screen.findByTestId('where-files-go', {}, T)).toHaveTextContent('Files go to your hub, Anna’s hub (pub.example.org).')
  expect(screen.getByTestId('links-open-at')).toHaveTextContent('Links open at share.example.org.')
  expect(screen.queryByTestId('replaces-old')).toBeNull() // nothing of the person's own is replaced
  expect(await screen.findByRole('button', { name: /^Sign( |$)/ }, T)).toBeInTheDocument()
  expect(state.host.clickSigned).toEqual([]) // never armed or signed by itself
})

it('Settings > Hub: a legacy row with only `suggest` still offers the line and sends that address for both', async () => {
  publish({ suggest: 'https://pub.example.org', suggest_from: 'hub', suggest_hub: 'Anna’s hub' })
  serve()
  hubTab()
  expect(await screen.findByTestId('hub-share-line', {}, T)).toHaveTextContent('Share links through pub.example.org')
  await userEvent.setup().click(screen.getByRole('button', { name: 'Turn on sharing' }))
  await screen.findByTestId('click-panel', {}, T)
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_enable', name: 'publish', publish: { server: SERVER, share: SERVER } }])
})

it('Settings > Hub: with Publish already on there is no line', async () => {
  publish({ ...fromHub, state: 'on', set_up: true })
  serve()
  hubTab()
  await settle()
  expect(screen.queryByTestId('hub-share-line')).toBeNull()
})

it('Settings > Hub: suggest_from null shows no line and no button', async () => {
  publish({ suggest: null, suggest_from: null, suggest_hub: null })
  serve()
  hubTab()
  await settle()
  expect(screen.queryByTestId('hub-share-line')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Turn on sharing' })).toBeNull()
})

it('Settings > Hub: a suggestion from this computer is not the hub’s line', async () => {
  publish({ suggest: SERVER, suggest_where: { server: SERVER, share: SERVER }, suggest_from: 'computer', suggest_hub: null })
  serve()
  hubTab()
  await settle()
  expect(screen.queryByTestId('hub-share-line')).toBeNull()
})

it('nothing in the address or the page state fills the review: a URL parameter is ignored', async () => {
  publish(fromHub)
  serve()
  clickOn(ws())
  show('/w/DEMO/settings/hub?server=https%3A%2F%2Fevil.example&share=https%3A%2F%2Fevil.example')
  await userEvent.setup().click(await screen.findByTestId('hub-share-turn-on', {}, T))
  await screen.findByTestId('click-panel', {}, T)
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'addon_enable', name: 'publish', publish: { server: SERVER, share: SHARE } }])
})

it('moved: Settings > Hub says sharing is paused; Use the new address opens the review with the new and the old share host', async () => {
  publish({ ...base, state: 'on', set_up: true, ...fromHub, ...moved })
  serve({ live: false })
  hubTab()
  expect(await screen.findByTestId('hub-moved-line', {}, T)).toHaveTextContent('Sharing paused: your hub now shares links through links.example.net.')
  expect(screen.queryByTestId('hub-share-line')).toBeNull()
  expect(state.host.clickPrepares()).toEqual([])
  await userEvent.setup().click(screen.getByRole('button', { name: 'Use the new address' }))
  await screen.findByTestId('click-panel', {}, T)
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'publish_setup', server: SERVER, share: NEW_SHARE }])
  // only the links host moved: the files line names where files go, with no "until now" for it
  expect(await screen.findByTestId('hub-moved-files', {}, T)).toHaveTextContent('Files go to pub.example.org.')
  expect(screen.queryByTestId('hub-moved-files-old')).toBeNull()
  expect(screen.getByTestId('hub-moved-new')).toHaveTextContent('Links open at links.example.net.')
  expect(screen.getByTestId('hub-moved-old')).toHaveTextContent('Until now they opened at share.example.org.')
  expect(state.host.clickSigned).toEqual([])
})

const openMoved = async (hub_moved: { server: string; share: string }) => {
  publish({ ...base, state: 'on', set_up: true, ...fromHub, hub_moved })
  serve({ live: false })
  hubTab()
  const line = await screen.findByTestId('hub-moved-line', {}, T)
  await userEvent.setup().click(screen.getByRole('button', { name: 'Use the new address' }))
  await screen.findByTestId('click-panel', {}, T)
  await screen.findByTestId('hub-moved-files', {}, T)
  return line
}

it('moved, only the server (files): the review says where files go, new and old; the links line has no "until now"; the paused line names files', async () => {
  const line = await openMoved({ server: NEW_SERVER, share: SHARE })
  expect(line).toHaveTextContent('Sharing paused: your hub now takes files at api.new.example.')
  expect(line).not.toHaveTextContent('shares links')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'publish_setup', server: NEW_SERVER, share: SHARE }])
  expect(screen.getByTestId('hub-moved-files')).toHaveTextContent('Files go to api.new.example.')
  expect(screen.getByTestId('hub-moved-files-old')).toHaveTextContent('Until now they went to pub.example.org.')
  expect(screen.getByTestId('hub-moved-new')).toHaveTextContent('Links open at share.example.org.')
  expect(screen.queryByTestId('hub-moved-old')).toBeNull()
  expect(state.host.clickSigned).toEqual([])
})

it('moved, both: the review shows both destinations with both old hosts; the paused line names both', async () => {
  const line = await openMoved({ server: NEW_SERVER, share: NEW_SHARE })
  expect(line).toHaveTextContent('Sharing paused: your hub now takes files at api.new.example and shares links through links.example.net.')
  expect(screen.getByTestId('hub-moved-files')).toHaveTextContent('Files go to api.new.example.')
  expect(screen.getByTestId('hub-moved-files-old')).toHaveTextContent('Until now they went to pub.example.org.')
  expect(screen.getByTestId('hub-moved-new')).toHaveTextContent('Links open at links.example.net.')
  expect(screen.getByTestId('hub-moved-old')).toHaveTextContent('Until now they opened at share.example.org.')
  expect(state.host.clickSigned).toEqual([])
})

it('moved: the Shares page shows the same paused line and button', async () => {
  publish({ ...base, state: 'on', set_up: true, ...fromHub, ...moved })
  serve({ live: false, why: 'hub_moved' })
  clickOn(ws())
  show('/w/DEMO/shares')
  const box = await screen.findByTestId('shares-hub-moved', {}, T)
  expect(box).toHaveTextContent('Sharing paused: your hub now shares links through links.example.net.')
  expect(within(box).getByRole('button', { name: 'Use the new address' })).toBeInTheDocument()
  expect(screen.queryByTestId('shares-unreachable')).toBeNull()
})

it('no hub_moved on the row: no paused line on Shares', async () => {
  publish({ ...base, state: 'on', set_up: true, ...fromHub })
  serve()
  show('/w/DEMO/shares')
  await screen.findByRole('heading', { name: 'Shares' }, T)
  await new Promise((r) => setTimeout(r, 300))
  expect(screen.queryByTestId('shares-hub-moved')).toBeNull()
})

it('one source: the add-ons row alone decides "moved"; a list that says so on its own adds no banner and no review', async () => {
  publish({ ...base, state: 'on', set_up: true, ...fromHub })
  serve({ live: false, why: 'hub_moved', hub_moved: { server: SERVER, share: NEW_SHARE } })
  show('/w/DEMO/shares')
  await screen.findByRole('heading', { name: 'Shares' }, T)
  await new Promise((r) => setTimeout(r, 300))
  expect(screen.queryByTestId('shares-hub-moved')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Use the new address' })).toBeNull()
})

it('a refusal with reason hub_moved on Shares does not point to Settings: the button is right there', async () => {
  const { REASON_TEXT } = await import('@/api/shares')
  expect(REASON_TEXT.hub_moved).toContain('Use the new address')
  expect(REASON_TEXT.hub_moved).not.toMatch(/Settings|Hub\./)
})

it('Settings > Hub, set up with the person’s own settings that differ from the hub’s: the review says "This replaces <old host>."', async () => {
  publish({ ...fromHub, set_up: true, state: 'off' })
  serve({ server: 'https://own.example.org', share: 'https://own-links.example.org' })
  hubTab()
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Turn on sharing' }, T))
  await screen.findByTestId('click-panel', {}, T)
  expect(await screen.findByTestId('replaces-old', {}, T)).toHaveTextContent('This replaces own.example.org and own-links.example.org.')
  expect(state.host.clickSigned).toEqual([])
})

it('Settings > Hub, set up with settings equal to the hub’s: the review says nothing about replacing', async () => {
  publish({ ...fromHub, set_up: true, state: 'off' })
  serve({ server: `${SERVER}/`, share: SHARE })
  hubTab()
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Turn on sharing' }, T))
  await screen.findByTestId('click-panel', {}, T)
  await screen.findByTestId('where-files-go', {}, T)
  expect(screen.queryByTestId('replaces-old')).toBeNull()
})

// The review is prepared on open, so Sign is enabled early: focus is never on it, and an Enter pressed at once signs nothing.
const notOnSign = async () => {
  await screen.findByRole('button', { name: /^Sign( |$)/ }, T)
  const sign = screen.getByRole('button', { name: /^Sign( |$)/ })
  expect(document.activeElement).not.toBe(sign)
  await userEvent.setup().keyboard('{Enter}')
  await new Promise((r) => setTimeout(r, 100))
  expect(state.host.clickSigned).toEqual([])
}

it('focus: after "Turn on sharing" opens the review the focus is not on Sign, and Enter signs nothing', async () => {
  publish(fromHub)
  serve()
  hubTab()
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Turn on sharing' }, T))
  await screen.findByTestId('click-panel', {}, T)
  await notOnSign()
})

it('focus: after "Use the new address" opens the review the focus is not on Sign, and Enter signs nothing', async () => {
  publish({ ...base, state: 'on', set_up: true, ...fromHub, ...moved })
  serve({ live: false })
  hubTab()
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Use the new address' }, T))
  await screen.findByTestId('click-panel', {}, T)
  await notOnSign()
})

it('left: the Shares page shows the host’s plain text with a way to Settings, Hub, and no Try again', async () => {
  publish({ ...base, state: 'on', set_up: true })
  serve({ live: false, why: 'hub_left' })
  show('/w/DEMO/shares')
  const note = await screen.findByTestId('shares-hub-left', {}, T)
  expect(note).toHaveTextContent('Sharing went through your hub. Connect again, or set a sharing server.')
  expect(within(note).getByRole('link', { name: 'Open Settings, Hub' })).toHaveAttribute('href', expect.stringContaining('/settings/hub'))
  expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  await waitFor(() => expect(screen.queryByTestId('shares-unreachable')).toBeNull())
})
