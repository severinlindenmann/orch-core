import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

describe('Addon manager', () => {
  it('installs from the catalog, requires a signed grant, then enables', async () => {
    const { user } = renderApp('/settings/addons')
    await user.click(await screen.findByRole('button', { name: 'Browse addons' }))
    await user.click(within(await screen.findByRole('article', { name: /Quick tasks/ })).getByRole('button', { name: 'Install' }))
    const row = await screen.findByRole('row', { name: /Quick tasks/ })
    expect(within(row).getByRole('switch', { name: /Enable/ })).toBeDisabled()
    await user.click(within(row).getByRole('button', { name: 'Grant…' }))
    await user.click(await screen.findByRole('button', { name: /Grant and sign/ }))
    // The grant lands after the Touch ID wait; the switch unlocks then.
    await waitFor(() => expect(within(screen.getByRole('row', { name: /Quick tasks/ })).getByRole('switch', { name: /Enable/ })).toBeEnabled())
    await user.click(within(screen.getByRole('row', { name: /Quick tasks/ })).getByRole('switch', { name: /Enable/ }))
    expect(await screen.findByRole('link', { name: /Quick tasks/ })).toBeInTheDocument() // sidebar nav appeared
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
    await user.click(within(await screen.findByRole('row', { name: /Publish/ })).getByRole('switch', { name: /Enable/ }))
    await waitFor(() => expect(screen.queryByRole('link', { name: /Apps & shares/ })).toBeNull())
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
})
