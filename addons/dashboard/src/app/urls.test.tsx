import { act, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { validateArtifactsSearch, validateBoardSearch, validateTicketSearch } from './search'
import { isWorkspacePath, shareableLink, splitWorkspacePath, toPublicPath } from './urls'

const T = { timeout: 8000 }
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
    ['/ticket/DEMO-0043', '/ticket/DEMO-0043'],
  ]
  it.each(ROUTES)('%s <-> %s', (inApp, address) => {
    expect(toPublicPath(inApp, 'DEMO')).toBe(address)
    const { prefix, path } = splitWorkspacePath(address)
    expect(path).toBe(inApp)
    expect(prefix).toBe(inApp.startsWith('/ticket/') ? undefined : 'DEMO')
  })

  it('a ticket is not a workspace page (its key names the workspace); /w/ paths are already addresses', () => {
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
    await user.click(await screen.findByRole('option', { name: /Client/ }, T).catch(() => screen.findByText(/Client/, {}, T)))
    await waitFor(() => expect(address()).toBe('/w/CLI/agents'), T)
  })

  it('a ticket URL keeps its key, opens the tab from ?tab= and writes the tab back', async () => {
    const { user, address } = renderApp('/ticket/DEMO-0043?tab=history', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    expect(screen.getByRole('tab', { name: 'History' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('tab', { name: 'Raw' }))
    await waitFor(() => expect(address()).toBe('/ticket/DEMO-0043?tab=raw'), T)
    await user.click(screen.getByRole('tab', { name: 'Overview' }))
    await waitFor(() => expect(address()).toBe('/ticket/DEMO-0043'), T)
  })

  it('an invalid ?tab= opens Overview instead of failing', async () => {
    renderApp('/ticket/DEMO-0043?tab=nope', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    expect(screen.getAllByRole('tab', { selected: true }).map((t) => t.textContent)).toEqual(['Overview'])
  })

  it('a ticket address with a workspace in front loses it (the key names the workspace)', async () => {
    const { address } = renderApp('/w/INT/ticket/DEMO-0043', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await waitFor(() => expect(address()).toBe('/ticket/DEMO-0043'), T)
  })

  it('a pasted URL to a restricted ticket shows the restricted state, and nothing of the ticket', async () => {
    const { address } = renderApp('/ticket/DEMO-0044?tab=history', { viewer: 'p_tom' })
    expect(await screen.findByText('This ticket is not visible to you', undefined, T)).toBeInTheDocument()
    expect(screen.queryByText('Rotate warehouse service credentials')).not.toBeInTheDocument()
    expect(address()).toBe('/ticket/DEMO-0044?tab=history')
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

  it('settings tab and the open addon row are the URL; an unknown addon there is not a crash', async () => {
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
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/ticket/DEMO-0043?tab=history`), T)
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
})
