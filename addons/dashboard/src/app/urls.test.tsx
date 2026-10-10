import { act, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import workspacesFixture from '@/mocks/fixtures/workspaces.json'
import { renderApp } from '@/test/renderApp'
import { validateArtifactsSearch, validateBoardSearch, validateTicketSearch } from './search'
import { isWorkspacePath, shareableLink, splitWorkspacePath, toPublicPath } from './urls'

const T = { timeout: 8000 }
const DEMO_ID = (workspacesFixture as { id: string; prefix: string }[]).find((w) => w.prefix === 'DEMO')!.id
const switcher = () => screen.getByRole('button', { name: 'Switch workspace' })

describe('permanent URLs: the address <-> in-app path mapping', () => {
  const ROUTES: [inApp: string, address: string][] = [
    ['/', '/w/DEMO'],
    ['/board', '/w/DEMO/board'],
    ['/tickets', '/w/DEMO/tickets'],
    ['/tickets/new', '/w/DEMO/tickets/new'],
    ['/artifacts', '/w/DEMO/artifacts'],
    ['/agents', '/w/DEMO/agents'],
    ['/settings/members', '/w/DEMO/settings/members'],
    ['/settings/addon/publish', '/w/DEMO/settings/addon/publish'],
    ['/addon/usage/overview', '/w/DEMO/addon/usage/overview'],
    ['/ticket/DEMO-0043', '/w/DEMO/ticket/DEMO-0043'],
  ]
  it.each(ROUTES)('%s <-> %s', (inApp, address) => {
    expect(toPublicPath(inApp, 'DEMO')).toBe(address)
    const { prefix, path } = splitWorkspacePath(address)
    expect(path).toBe(inApp)
    expect(prefix).toBe('DEMO')
  })

  it('a ticket takes the workspace of its key, not the current one; /w/ paths are already addresses', () => {
    expect(toPublicPath('/ticket/INT-0007', 'DEMO')).toBe('/w/INT/ticket/INT-0007')
    expect(toPublicPath('/ticket/INT-0007', null)).toBe('/w/INT/ticket/INT-0007')
    expect(isWorkspacePath('/ticket/INT-0007')).toBe(false)
    expect(isWorkspacePath('/w/DEMO/board')).toBe(false)
    expect(isWorkspacePath('/board')).toBe(true)
    expect(toPublicPath('/board', null)).toBe('/board')
  })

  it('a shareable link is the origin plus the address', () => {
    expect(shareableLink('/w/DEMO/board?view=list', 'http://localhost:5222')).toBe('http://localhost:5222/w/DEMO/board?view=list')
  })
})

describe('permanent URLs: search params are validated, invalid ones fall back to defaults', () => {
  it('ticket tab', () => {
    expect(validateTicketSearch({ tab: 'history' })).toEqual({ tab: 'history' })
    expect(validateTicketSearch({ tab: 'secrets' })).toEqual({})
    expect(validateTicketSearch({ tab: 42 })).toEqual({})
  })
  it('board view and filters', () => {
    expect(validateBoardSearch({ view: 'list', mine: true, q: 'tariff', person: 'p_sev' })).toEqual({ view: 'list', mine: true, q: 'tariff', person: 'p_sev' })
    expect(validateBoardSearch({ view: 'kanban', mine: 'maybe', q: '', type: { x: 1 } })).toEqual({})
    // The router parses `q=42` as a number: still a search text.
    expect(validateBoardSearch({ q: 42 })).toEqual({ q: '42' })
    expect(validateBoardSearch({ q: 'x'.repeat(201) })).toEqual({})
  })
  it('artifacts view and shown artifact', () => {
    expect(validateArtifactsSearch({ view: 'grid', a: 'DEMO-0043.3f2a' })).toEqual({ view: 'grid', a: 'DEMO-0043.3f2a' })
    expect(validateArtifactsSearch({ view: 'table', a: ['x'] })).toEqual({})
  })
})

describe('permanent URLs in the app', () => {
  afterEach(() => vi.restoreAllMocks())

  it('an old path redirects to the current workspace (/board -> /w/DEMO/board)', async () => {
    const { address } = renderApp('/board', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: 'Board' }, T)
    await waitFor(() => expect(address()).toBe('/w/DEMO/board'), T)
  })

  it('/ is Today of the current workspace (/w/DEMO)', async () => {
    const { address } = renderApp('/', { viewer: 'p_sev' })
    await waitFor(() => expect(address()).toBe('/w/DEMO'), T)
  })

  it('a URL for another workspace switches to it, and links follow it', async () => {
    const { address } = renderApp('/w/INT/tickets', { viewer: 'p_sev' })
    await waitFor(() => expect(within(switcher()).getByText('INT')).toBeInTheDocument(), T)
    expect(address()).toBe('/w/INT/tickets')
    expect(await screen.findByRole('link', { name: 'Board' }, T)).toHaveAttribute('href', '/w/INT/board')
  })

  it('an unknown workspace shows "not found" and keeps the URL (no silent redirect)', async () => {
    const { address } = renderApp('/w/NOPE/board', { viewer: 'p_sev' })
    expect(await screen.findByRole('heading', { name: 'No workspace NOPE' }, T)).toBeInTheDocument()
    expect(address()).toBe('/w/NOPE/board')
    expect(screen.getByRole('link', { name: /Go to/ })).toHaveAttribute('href', '/w/DEMO')
  })

  it('switching workspace moves the address to the same page there', async () => {
    const { user, address } = renderApp('/w/DEMO/agents', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: 'Agents' }, T)
    await user.click(switcher())
    await user.click(await screen.findByRole('button', { name: /^CLI · / }, T))
    await waitFor(() => expect(address()).toBe('/w/CLI/agents'), T)
    // In-app links follow the switch (the router's cached link addresses are dropped).
    await waitFor(() => expect(screen.getByRole('link', { name: 'Board' })).toHaveAttribute('href', '/w/CLI/board'), T)
  })

  it('a ticket URL carries its workspace, keeps its key, opens the tab from ?tab= and writes the tab back', async () => {
    const { user, address } = renderApp('/w/DEMO/ticket/DEMO-0043?tab=history', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    expect(screen.getByRole('tab', { name: 'History' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('tab', { name: 'Raw' }))
    await waitFor(() => expect(address()).toBe('/w/DEMO/ticket/DEMO-0043?tab=raw'), T)
    await user.click(screen.getByRole('tab', { name: 'Overview' }))
    await waitFor(() => expect(address()).toBe('/w/DEMO/ticket/DEMO-0043'), T)
  })

  it('an invalid ?tab= opens Overview instead of failing', async () => {
    renderApp('/ticket/DEMO-0043?tab=nope', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    expect(screen.getAllByRole('tab', { selected: true }).map((t) => t.textContent)).toEqual(['Overview'])
  })

  it('/ticket/KEY redirects (replace) to the canonical address, keeping ?tab and the hash', async () => {
    const { router, address } = renderApp('/ticket/DEMO-0043?tab=history#question-q1', { viewer: 'p_sev', storage: { 'orch.workspace': workspacesFixture.find((w) => w.prefix === 'INT')!.id } })
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    // The prefix is the key's workspace, not the remembered one (INT).
    await waitFor(() => expect(address()).toBe('/w/DEMO/ticket/DEMO-0043?tab=history#question-q1'), T)
    // Replaced, not pushed: Back leaves the ticket instead of returning to the short address.
    expect(router.history.length).toBe(1)
  })

  it.each([
    ['/ticket/DEMO-0043/?tab=history', '/w/DEMO/ticket/DEMO-0043?tab=history'],
    ['/w/INT/ticket/DEMO-0043/?tab=history', '/w/DEMO/ticket/DEMO-0043?tab=history'],
    ['/w/DEMO/ticket/DEMO-0043/?tab=history', '/w/DEMO/ticket/DEMO-0043?tab=history'],
  ])('a ticket address with a trailing slash (%s) is redirected to the canonical one', async (from, to) => {
    const { address } = renderApp(from, { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await waitFor(() => expect(address()).toBe(to), T)
  })

  it('the prefix of a key may contain a dash', () => {
    expect(toPublicPath('/ticket/MY-APP-0007', 'DEMO')).toBe('/w/MY-APP/ticket/MY-APP-0007')
    expect(toPublicPath('/ticket/DEMO-0043/', 'INT')).toBe('/w/DEMO/ticket/DEMO-0043')
  })

  it('a ticket under another workspace prefix redirects to the key\'s own workspace', async () => {
    const { address } = renderApp('/w/INT/ticket/DEMO-0043?tab=history', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await waitFor(() => expect(address()).toBe('/w/DEMO/ticket/DEMO-0043?tab=history'), T)
  })

  it('ticket links everywhere carry the key\'s workspace', async () => {
    renderApp('/w/INT/tickets', { viewer: 'p_sev' })
    await waitFor(() => expect(within(switcher()).getByText('INT')).toBeInTheDocument(), T)
    const links = await screen.findAllByRole('link', { name: /DEMO-\d{4}|INT-\d{4}/ }, T)
    const tickets = links.map((a) => a.getAttribute('href')!).filter((h) => h.includes('/ticket/'))
    expect(tickets.length).toBeGreaterThan(0)
    for (const h of tickets) expect(h).toMatch(/^\/w\/([A-Z]+)\/ticket\/\1-\d+/)
  })

  it.each([
    ['/w/DEMO/ticket/DEMO-0044?tab=history'],
    ['/ticket/DEMO-0044?tab=history'],
  ])('a pasted URL to a restricted ticket (%s) shows the restricted state, and nothing of the ticket', async (from) => {
    const { address } = renderApp(from, { viewer: 'p_tom' })
    expect(await screen.findByText('This ticket is not visible to you', undefined, T)).toBeInTheDocument()
    expect(screen.queryByText('Rotate warehouse service credentials')).not.toBeInTheDocument()
    await waitFor(() => expect(address()).toBe('/w/DEMO/ticket/DEMO-0044?tab=history'), T)
    expect(screen.queryByText('Rotate warehouse service credentials')).not.toBeInTheDocument()
  })

  it('board view and filters come from the URL and go back into it', async () => {
    const { user, address } = renderApp('/w/DEMO/board?view=list&q=tariff', { viewer: 'p_sev' })
    const box = await screen.findByPlaceholderText('Filter tickets', {}, T)
    expect(box).toHaveValue('tariff')
    expect(screen.getByRole('radio', { name: /List/ })).toHaveAttribute('data-state', 'on')
    await user.click(screen.getByRole('radio', { name: /Board/ }))
    await waitFor(() => expect(address()).toBe('/w/DEMO/board?q=tariff'), T)
    await user.clear(box)
    await waitFor(() => expect(address()).toBe('/w/DEMO/board'), T)
  })

  it('settings tab and the open addon row are the URL', async () => {
    const { address } = renderApp('/w/DEMO/settings/addon/publish', { viewer: 'p_sev' })
    await screen.findByRole('navigation', { name: 'Settings' }, T)
    expect(address()).toBe('/w/DEMO/settings/addon/publish')
    expect(screen.getByRole('link', { name: 'Addons' })).toHaveAttribute('aria-current', 'page')
  })

  it('an addon page that is not there says so', async () => {
    renderApp('/w/DEMO/addon/publish/nope', { viewer: 'p_sev' })
    expect(await screen.findByRole('heading', { name: 'Page not found' }, T)).toBeInTheDocument()
  })

  it('Copy link copies the permanent link of the page as shown', async () => {
    const { user } = renderApp('/ticket/DEMO-0043?tab=history', { viewer: 'p_sev' })
    // user-event puts its own clipboard in place on setup: watch that one.
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await user.click(screen.getByRole('button', { name: 'Copy link to DEMO-0043' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/w/DEMO/ticket/DEMO-0043?tab=history`), T)
    // Only the key and the tab: never the title.
    expect(String((writeText.mock.calls[0] as unknown[])[0])).not.toMatch(/tariff/i)
  })

  it('Copy link on a settings page carries the workspace', async () => {
    const { user } = renderApp('/settings/members', { viewer: 'p_sev' })
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    await screen.findByRole('navigation', { name: 'Settings' }, T)
    await user.click(screen.getByRole('button', { name: 'Copy link to this settings page' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/w/DEMO/settings/members`), T)
  })

  it('Back after a workspace switch returns to the old workspace', async () => {
    const { router, address } = renderApp('/w/DEMO/board', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: 'Board' }, T)
    await act(async () => void router.history.push('/w/CLI/board'))
    await waitFor(() => expect(within(switcher()).getByText('CLI')).toBeInTheDocument(), T)
    await act(async () => router.history.back())
    await waitFor(() => expect(within(switcher()).getByText('DEMO')).toBeInTheDocument(), T)
    expect(address()).toBe('/w/DEMO/board')
  })

  it('an addon page tab (core\'s tabs node) is in the URL: opened from it and written back', async () => {
    const { user, address } = renderApp('/w/DEMO/addon/publish/shares?tab.publish=shares', { viewer: 'p_sev' })
    const shares = await screen.findByRole('tab', { name: /^Shares/ }, T)
    expect(shares).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('tab', { name: /^Apps/ }))
    await waitFor(() => expect(address()).toBe('/w/DEMO/addon/publish/shares?tab.publish=apps'), T)
  })

  it('an unknown addon tab in the URL falls back to the first tab', async () => {
    renderApp('/w/DEMO/addon/publish/shares?tab.publish=nope', { viewer: 'p_sev' })
    expect(await screen.findByRole('tab', { name: /^Apps/ }, T)).toHaveAttribute('aria-selected', 'true')
  })

  it('an unknown addon in a settings address is not a crash', async () => {
    const { address } = renderApp('/w/DEMO/settings/addon/nope', { viewer: 'p_sev' })
    await screen.findByRole('navigation', { name: 'Settings' }, T)
    expect(address()).toBe('/w/DEMO/settings/addon/nope')
  })

  describe('redirects keep the workspace of the address (DEMO is the current one)', () => {
    const cases: [string, string][] = [
      ['/w/INT/settings', '/w/INT/settings/general'],
      ['/w/INT/settings/addons/publish', '/w/INT/settings/addon/publish'],
      ['/w/INT/settings/bogus', '/w/INT/settings/general'],
    ]
    it.each(cases)('%s -> %s', async (from, to) => {
      const { address } = renderApp(from, { viewer: 'p_sev', storage: { 'orch.workspace': DEMO_ID } })
      await screen.findByRole('navigation', { name: 'Settings' }, T)
      await waitFor(() => expect(address()).toBe(to), T)
      await waitFor(() => expect(within(switcher()).getByText('INT')).toBeInTheDocument(), T)
    })
  })

  it('a workspace prefix in another case opens that workspace and the address is corrected', async () => {
    const { address } = renderApp('/w/int/board', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: 'Board' }, T)
    await waitFor(() => expect(address()).toBe('/w/INT/board'), T)
  })

  it('/w/ alone is Today of the current workspace', async () => {
    const { address } = renderApp('/w/', { viewer: 'p_sev' })
    await waitFor(() => expect(address()).toBe('/w/DEMO'), T)
  })

  it('Copy link copies the address as shown, also on "No workspace NOPE" (button-free: from ⌘K)', async () => {
    const { user } = renderApp('/w/NOPE/board?view=list', { viewer: 'p_sev' })
    await screen.findByRole('heading', { name: 'No workspace NOPE' }, T)
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    await user.keyboard('{Control>}k{/Control}')
    await user.click(await screen.findByRole('option', { name: /Copy link to this page/ }, T))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/w/NOPE/board?view=list`), T)
  })

  it('⌘K "Copy link to this page" on a ticket copies its address with the tab', async () => {
    const { user } = renderApp('/ticket/DEMO-0043?tab=history', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    await user.keyboard('{Control>}k{/Control}')
    await user.click(await screen.findByRole('option', { name: /Copy link to this page/ }, T))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/w/DEMO/ticket/DEMO-0043?tab=history`), T)
  })

  it('an addon tab stays in the address when the address loses it (same page again)', async () => {
    const { router, address } = renderApp('/w/DEMO/addon/publish/shares?tab.publish=shares', { viewer: 'p_sev' })
    expect(await screen.findByRole('tab', { name: /^Shares/ }, T)).toHaveAttribute('aria-selected', 'true')
    await act(async () => void router.history.push('/w/DEMO/addon/publish/shares'))
    await waitFor(() => expect(address()).toBe('/w/DEMO/addon/publish/shares?tab.publish=shares'), T)
    expect(screen.getByRole('tab', { name: /^Shares/ })).toHaveAttribute('aria-selected', 'true')
    // And a tab named by the address wins over the one on screen (Back, a pasted link).
    await act(async () => void router.history.push('/w/DEMO/addon/publish/shares?tab.publish=apps'))
    await waitFor(() => expect(screen.getByRole('tab', { name: /^Apps/ })).toHaveAttribute('aria-selected', 'true'), T)
  })
})
