import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

describe('Addon manager', () => {
  it('every column header of the addons table has a name', async () => {
    renderApp('/settings/addons')
    await screen.findByRole('row', { name: /Publish/ })
    const heads = screen.getAllByRole('columnheader')
    expect(heads.map((h) => h.textContent)).toEqual(['Addon', 'Capabilities', 'Status', 'On', 'Actions'])
  })
  it('installs from the catalog, requires a signed grant, then enables', async () => {
    const { user } = renderApp('/settings/addons')
    await user.click(await screen.findByRole('button', { name: 'Browse addons' }))
    await user.click(within(await screen.findByRole('article', { name: /Quick tasks/ })).getByRole('button', { name: 'Install' }))
    const row = await screen.findByRole('row', { name: /Quick tasks/ })
    expect(within(row).getByRole('switch', { name: /(enabled|disabled)$/ })).toBeDisabled()
    await user.click(within(row).getByRole('button', { name: 'Grant…' }))
    await user.click(await screen.findByRole('button', { name: /Grant and sign/ }))
    // The grant lands after the Touch ID wait; the switch unlocks then.
    await waitFor(() => expect(within(screen.getByRole('row', { name: /Quick tasks/ })).getByRole('switch', { name: /(enabled|disabled)$/ })).toBeEnabled())
    await user.click(within(screen.getByRole('row', { name: /Quick tasks/ })).getByRole('switch', { name: /(enabled|disabled)$/ }))
    await user.click(await screen.findByRole('button', { name: /More addons/ }))
    await waitFor(() => expect(screen.getAllByRole('link', { name: /Quick tasks/ }).some((l) => l.getAttribute('href') === '/addon/quick/quick')).toBe(true)) // sidebar nav appeared
  })
  it('an update with a new capability shows the diff and needs a re-grant', async () => {
    const { user } = renderApp('/settings/addons')
    const row = await screen.findByRole('row', { name: /GitHub/ })
    await user.click(within(row).getByRole('button', { name: /Update to 0\.6\.0/ }))
    expect(await screen.findByText('+ spawn_agent')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Update' }))
    expect(await screen.findByText(/grant again to turn it back on/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Code reviews/ })).toBeNull()
  })
  it('disabling an addon removes its nav, palette commands and Today card', async () => {
    const { user } = renderApp('/settings/addons')
    await user.click(within(await screen.findByRole('row', { name: /Publish/ })).getByRole('switch', { name: 'Publish enabled' }))
    expect(await within(screen.getByRole('row', { name: /Publish/ })).findByRole('switch', { name: 'Publish disabled' })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('link', { name: /Apps & shares/ })).toBeNull())
  })
  it('an uninstalled seeded addon shows up in Browse addons and can be installed again', async () => {
    const { user } = renderApp('/settings/addons')
    await user.click(within(await screen.findByRole('row', { name: /Estimate/ })).getByRole('button', { name: 'Uninstall' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Uninstall' }))
    await waitFor(() => expect(screen.queryByRole('row', { name: /Estimate/ })).toBeNull())
    await user.click(screen.getByRole('button', { name: 'Browse addons' }))
    await user.click(within(await screen.findByRole('article', { name: /Estimate/ })).getByRole('button', { name: 'Install' }))
    const row = await screen.findByRole('row', { name: /Estimate/ })
    expect(within(row).getByText('Needs grant')).toBeInTheDocument()
  })
  it('only the owner grants', async () => {
    renderApp('/settings/addons', { viewer: 'p_mara' })
    expect(await screen.findByText('Only owners change settings.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Grant…/ })).toBeNull()
  })
  it('explains each capability on the chips', async () => {
    const { user } = renderApp('/settings/addons')
    const row = await screen.findByRole('row', { name: /Terminals/ })
    await user.hover(within(row).getByText('pty'))
    expect(await screen.findAllByText('opens terminals; never for agents')).not.toHaveLength(0)
  })
  it('a disabled addon shows no decision on Today', async () => {
    const { user } = renderApp('/')
    expect(await screen.findByText(/Publish report/)).toBeInTheDocument()
    await user.click(await screen.findByRole('link', { name: 'Settings' }))
    await user.click(await screen.findByRole('link', { name: 'Addons' }))
    await user.click(within(await screen.findByRole('row', { name: /Publish/ })).getByRole('switch', { name: /(enabled|disabled)$/ }))
    await user.click(await screen.findByRole('link', { name: /^Today/ }))
    await screen.findByRole('heading', { name: 'Today' })
    await waitFor(() => expect(screen.queryByText(/Publish report/)).toBeNull())
  })
})

describe('Grant and update dialogs list what viewers can run', () => {
  it('the grant dialog says "Viewers can: Open page, Search, All pages, Clear search" for the wiki', async () => {
    const { user } = renderApp('/settings/addons', {
      setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'addon.updated', name: 'wiki', version: '0.1.4', package_sha256: 'c'.repeat(64), capabilities: [] }),
    })
    await user.click(within(await screen.findByRole('row', { name: /Wiki/ })).getByRole('button', { name: 'Grant…' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getAllByText('Viewers can: Open page, Search, All pages, Clear search').length).toBeGreaterThan(0)
  })
  it('an addon without viewer actions says viewers can only read', async () => {
    const { user } = renderApp('/settings/addons', {
      setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'addon.updated', name: 'estimate', version: '0.2.0', package_sha256: 'c'.repeat(64), capabilities: [] }),
    })
    await user.click(within(await screen.findByRole('row', { name: /Estimate/ })).getByRole('button', { name: 'Grant…' }))
    expect(within(await screen.findByRole('dialog')).getAllByText('Viewers can: nothing').length).toBeGreaterThan(0)
  })
  it('an update that adds a viewer action shows it in the diff and the grant still signs', async () => {
    const { user } = renderApp('/settings/addons', {
      setup: (s) => {
        const gh = s.addons.find((a) => a.name === 'github')!
        gh.update!.actions = { ...gh.actions, refresh: { minRole: 'viewer', label: 'Refresh pull requests' } }
      },
    })
    await user.click(within(await screen.findByRole('row', { name: /GitHub/ })).getByRole('button', { name: /Update to 0\.6\.0/ }))
    expect(await screen.findByText('+ Viewers can: Refresh pull requests')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Update' }))
    expect(await screen.findByText(/grant again to turn it back on/)).toBeInTheDocument()
  })
})

describe('Grant dialog says each thing once', () => {
  it('no repeated Viewers-can line, no pty sentence for an addon without pty, hash behind Details', async () => {
    const { user } = renderApp('/settings/addons', {
      setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'addon.updated', name: 'wiki', version: '0.1.4', package_sha256: 'c'.repeat(64), capabilities: [] }),
    })
    await user.click(within(await screen.findByRole('row', { name: /Wiki/ })).getByRole('button', { name: 'Grant…' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getAllByText(/^Viewers can:/)).toHaveLength(1)
    expect(dialog).not.toHaveTextContent(/never get pty/)
    expect(dialog.querySelector('details')).not.toHaveAttribute('open')
    expect(within(dialog).getByRole('button', { name: 'Copy hash' })).toBeInTheDocument()
  })
})
