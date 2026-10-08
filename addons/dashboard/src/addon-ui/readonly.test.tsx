import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

// Tom is a viewer in DEMO (the default workspace) and a member in CLI.
const rail = () => screen.getByRole('complementary', { name: 'Ticket details' })

describe('addon contributions follow the viewer role', () => {
  it('a viewer gets the ticket rail addon controls disabled', async () => {
    renderApp('/ticket/DEMO-0043', { viewer: 'p_tom' })
    await screen.findByRole('heading', { level: 1 })
    const share = await within(rail()).findByRole('button', { name: 'Share report…' })
    const save = await within(rail()).findByRole('button', { name: 'Save estimate' })
    // Give the workspace time to load: the controls must stay disabled for a viewer, not just while loading.
    await screen.findByRole('button', { name: 'Switch workspace' })
    await new Promise((r) => setTimeout(r, 50))
    expect(share).toBeDisabled()
    expect(save).toBeDisabled()
  })

  it('an owner gets them enabled', async () => {
    renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1 })
    const share = await within(rail()).findByRole('button', { name: 'Share report…' })
    await waitFor(() => expect(share).toBeEnabled())
  })

  it('a viewer gets addon nav page buttons disabled', async () => {
    renderApp('/addon/github/reviews', { viewer: 'p_tom' })
    const refresh = await screen.findByRole('button', { name: 'Refresh from GitHub' })
    await new Promise((r) => setTimeout(r, 50))
    expect(refresh).toBeDisabled()
  })

  it('the palette lists no addon commands for a viewer', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_tom' })
    await screen.findByRole('heading', { level: 1 })
    await user.keyboard('{Control>}k{/Control}')
    await screen.findByRole('group', { name: 'Go to' })
    expect(screen.queryByRole('group', { name: 'Addon commands' })).toBeNull()
    expect(screen.queryByRole('option', { name: /Share current ticket/ })).toBeNull()
  })

  it('the palette lists addon commands for an owner', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1 })
    await user.keyboard('{Control>}k{/Control}')
    expect(await screen.findByRole('option', { name: /Share current ticket/ })).toBeInTheDocument()
  })

  it('a viewer cannot import from the board lane', async () => {
    renderApp('/board', { viewer: 'p_tom' })
    const lane = await screen.findByRole('region', { name: /GitHub issues/ })
    const buttons = within(lane).getAllByRole('button', { name: /Import as ticket/ })
    for (const b of buttons) expect(b).toBeDisabled()
  })

  it('a viewer gets Today addon card controls disabled', async () => {
    renderApp('/', {
      viewer: 'p_tom',
      setup: (s) => {
        const card = s.addons.find((a) => a.name === 'publish')!.contributions.find((c) => c.slot === 'today.card')!
        card.node = { type: 'button', label: 'Share all', action: 'share' }
      },
    })
    const b = await screen.findByRole('button', { name: 'Share all' })
    await new Promise((r) => setTimeout(r, 50))
    expect(b).toBeDisabled()
  })
})
