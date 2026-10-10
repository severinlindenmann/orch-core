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
  it('installs from the catalog in one signed act: Install, the capability sheet, "Grant and turn on", Active', async () => {
    const { user } = renderApp('/settings/addons')
    await user.click(await screen.findByRole('button', { name: 'Browse addons' }))
    await user.click(within(await screen.findByRole('article', { name: /Quick tasks/ })).getByRole('button', { name: 'Install' }))
    // Nothing is installed until the signature: the sheet says what it covers, including turning it on.
    const dialog = await screen.findByRole('dialog', { name: /Install Quick tasks/ })
    expect(dialog).toHaveTextContent('and turns it on')
    expect(screen.queryByRole('row', { name: /Quick tasks/ })).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Grant and turn on' }))
    await waitFor(() => expect(within(screen.getByRole('row', { name: /Quick tasks/ })).getByText('Active')).toBeInTheDocument())
    expect(within(screen.getByRole('row', { name: /Quick tasks/ })).getByRole('switch', { name: 'Quick tasks enabled' })).toBeEnabled()
    await user.click(await screen.findByRole('button', { name: /More addons/ }))
    await waitFor(() => expect(screen.getAllByRole('link', { name: /Quick tasks/ }).some((l) => l.getAttribute('href') === '/w/DEMO/addon/quick/quick')).toBe(true)) // sidebar nav appeared
  })
  it('an update with a new capability shows the diff and is one signature (it stays on)', async () => {
    const { user } = renderApp('/settings/addons')
    const row = await screen.findByRole('row', { name: /GitHub/ })
    await user.click(within(row).getByRole('button', { name: /Update to 0\.6\.0/ }))
    expect(await screen.findByText('+ spawn_agent')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toHaveTextContent('The new capabilities take effect now and GitHub (github) stays on')
    // The changelog is the addon's text: shown apart, labelled as the addon's.
    expect(within(screen.getByRole('dialog')).getByRole('region', { name: 'From the addon: changelog' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Update' }))
    await waitFor(() => expect(within(screen.getByRole('row', { name: /GitHub/ })).getByText('Active')).toBeInTheDocument())
    expect(within(screen.getByRole('row', { name: /GitHub/ })).getByText('0.6.0')).toBeInTheDocument()
    expect(screen.queryByText(/grant again/)).toBeNull()
  })
  it('a catalog package whose title core cannot say plainly cannot be signed (alert, Sign off)', async () => {
    const { user } = renderApp('/settings/addons', { setup: (s) => void (s.addons.find((a) => a.name === 'quick')!.title = 'Quick · core') })
    await user.click(await screen.findByRole('button', { name: 'Browse addons' }))
    await user.click(within(await screen.findByRole('article', { name: /Quick/ })).getByRole('button', { name: 'Install' }))
    const dialog = await screen.findByRole('dialog', { name: /^Install / })
    expect(within(dialog).getByRole('alert')).toHaveTextContent(/must not contain parentheses, a colon or a middle dot/)
    expect(within(dialog).getByRole('button', { name: 'Grant and turn on' })).toBeDisabled()
  })
  it('updating a disabled addon says it stays off (the host keeps on/off)', async () => {
    const { user } = renderApp('/settings/addons')
    await user.click(within(await screen.findByRole('row', { name: /GitHub/ })).getByRole('switch', { name: 'GitHub enabled' }))
    await within(screen.getByRole('row', { name: /GitHub/ })).findByRole('switch', { name: 'GitHub disabled' })
    await user.click(within(screen.getByRole('row', { name: /GitHub/ })).getByRole('button', { name: /Update to 0\.6\.0/ }))
    const dialog = await screen.findByRole('dialog', { name: /Update GitHub \(github\) to 0\.6\.0/ })
    expect(dialog).toHaveTextContent('GitHub (github) stays off')
    expect(dialog).not.toHaveTextContent('stays on')
    await user.click(within(dialog).getByRole('button', { name: 'Update' }))
    await waitFor(() => expect(within(screen.getByRole('row', { name: /GitHub/ })).getByText('0.6.0')).toBeInTheDocument())
    expect(within(screen.getByRole('row', { name: /GitHub/ })).getByRole('switch', { name: 'GitHub disabled' })).toBeInTheDocument()
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
    await user.click(await screen.findByRole('button', { name: 'Grant and turn on' }))
    await waitFor(() => expect(within(screen.getByRole('row', { name: /Estimate/ })).getByText('Active')).toBeInTheDocument())
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
  it('the grant dialog says which action ids viewers can run, in core\'s words; the addon\'s labels sit apart', async () => {
    const { user } = renderApp('/settings/addons', {
      setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'addon.updated', name: 'wiki', version: '0.1.4', package_sha256: 'c'.repeat(64), capabilities: [] }),
    })
    await user.click(within(await screen.findByRole('row', { name: /Wiki/ })).getByRole('button', { name: 'Grant…' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getAllByText('Viewers can: Open (open), Search (search), Close (close), Clear search (clear_search)').length).toBeGreaterThan(0)
    // "All pages" is what the addon calls `close`: shown, labelled as the addon's, never in core's covers.
    expect(within(dialog).getByText('Covers').nextElementSibling).not.toHaveTextContent('All pages')
    expect(within(dialog).getByRole('region', { name: 'From the addon: viewer action labels' })).toHaveTextContent('close: All pages')
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
    expect(await screen.findByText('+ Viewers can: Refresh (refresh)')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Update' }))
    await waitFor(() => expect(within(screen.getByRole('row', { name: /GitHub/ })).getByText('0.6.0')).toBeInTheDocument())
    expect(screen.queryByText(/grant again/)).toBeNull()
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
