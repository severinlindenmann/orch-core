import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore } from '@/api/client'
import type { MockStore } from '@/mocks/store'
import { renderApp } from '@/test/renderApp'

// Ticket-rail tests render at 1440 px (the rail is a column from 1280 px; below, the Panels sheet).
afterEach(() => vi.unstubAllGlobals())

const disable = (name: string) => (s: MockStore) => s.appendWs(s.workspaces[0].id, { type: 'addon.disabled', name })

afterEach(() => vi.restoreAllMocks())

describe('inactive addons', () => {
  it('hides every contribution of a disabled addon', async () => {
    renderApp('/board', { setup: disable('github') })
    await screen.findByTestId('card-DEMO-0043')
    expect(screen.queryByRole('link', { name: /Code reviews/ })).toBeNull()
    expect(document.querySelector('[aria-label="From addon: github"]')).toBeNull()
  })

  it('does not fetch state for inactive addons', async () => {
    const spy = vi.spyOn(api, 'getAddonState')
    renderApp('/board', { setup: disable('github') })
    await screen.findByTestId('card-DEMO-0043')
    await vi.waitFor(() => expect(spy.mock.calls.length).toBeGreaterThan(0))
    expect(spy.mock.calls.some(([, name]) => name === 'github')).toBe(false)
    expect(spy.mock.calls.length).toBeGreaterThan(0)
  })

  it('shows inactive addon data on a ticket', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0043', { setup: disable('estimate') })
    const item = await screen.findByRole('button', { name: /Estimate · inactive/ })
    await user.click(item)
    expect(await screen.findByText(/"points"/)).toBeInTheDocument()
  })

  it('a direct URL to a disabled addon page explains and links to the manager', async () => {
    renderApp('/addon/wiki/pages', { setup: disable('wiki') })
    expect(await screen.findByText(/Wiki is not enabled in/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open the addon manager' })).toBeInTheDocument()
  })

  it('non-owners get no manager link', async () => {
    renderApp('/addon/wiki/pages', { viewer: 'p_tom', setup: disable('wiki') })
    expect(await screen.findByText(/Wiki is not enabled in/)).toBeInTheDocument()
    expect(screen.getByText(/Ask an owner to enable it/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open the addon manager' })).toBeNull()
  })

  it('the board lane import passes the workspace id', async () => {
    const spy = vi.spyOn(api, 'runAddonAction')
    const { user } = renderApp('/board')
    const lane = await screen.findByRole('region', { name: /GitHub issues/ })
    await user.click(within(lane).getAllByRole('button', { name: /Import as ticket/ })[0])
    await vi.waitFor(() => expect(spy).toHaveBeenCalled())
    expect(spy.mock.calls[0][0]).toBe(mockStore.workspaces[0].id)
  })
})
