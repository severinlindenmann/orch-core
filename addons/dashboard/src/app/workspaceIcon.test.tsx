import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { WORKSPACE_COLOURS, WORKSPACE_ICONS, defaultWorkspaceIcon, workspaceIconOf } from './workspaceIcon'

beforeEach(() => {
  try {
    Object.keys(localStorage).filter((k) => k.startsWith('orch.wsicon.')).forEach((k) => localStorage.removeItem(k))
  } catch {
    /* ignore */
  }
})

describe('workspace icon', () => {
  it('the default depends on the name alone and is one of the built-in set', () => {
    const a = defaultWorkspaceIcon('Acme')
    expect(defaultWorkspaceIcon(' acme ')).toEqual(a)
    expect(WORKSPACE_ICONS.map((i) => i.id)).toContain(a.icon)
    expect(WORKSPACE_COLOURS.map((c) => c.id)).toContain(a.colour)
  })
  it('ignores a stored value that is not in the built-in set', () => {
    localStorage.setItem('orch.wsicon.x', JSON.stringify({ icon: 'http://evil/x.png', colour: 'url(x)' }))
    expect(workspaceIconOf('x', 'Acme')).toEqual(defaultWorkspaceIcon('Acme'))
  })
  it('the menu shows the icon with the name, no prefix pill', async () => {
    renderApp('/')
    const trigger = await screen.findByRole('button', { name: 'Switch workspace' })
    await waitFor(() => expect(trigger.querySelector('[data-ws-icon]')).not.toBeNull())
    expect(trigger.textContent).not.toMatch(/^DEMO/)
  })
  it('Settings changes the icon and colour, and the menu follows; reset returns to the automatic one', async () => {
    const { user } = renderApp('/settings/general')
    const def = defaultWorkspaceIcon(mockStore.workspaces[0].name)
    const icon = def.icon === 'rocket' ? 'Star' : 'Rocket'
    const colour = def.colour === 'pink' ? 'Teal' : 'Pink'
    const trigger = await screen.findByRole('button', { name: 'Switch workspace' })
    const group = await screen.findByRole('group', { name: 'Icon' })
    await user.click(within(group).getByRole('button', { name: icon }))
    await user.click(within(screen.getByRole('group', { name: 'Colour' })).getByRole('button', { name: colour }))
    await waitFor(() => expect(trigger.querySelector(`[data-ws-icon="${icon.toLowerCase()}"][data-ws-colour="${colour.toLowerCase()}"]`)).not.toBeNull())
    await user.click(screen.getByRole('button', { name: 'Use the automatic icon' }))
    await waitFor(() => expect(trigger.querySelector(`[data-ws-icon="${icon.toLowerCase()}"][data-ws-colour="${colour.toLowerCase()}"]`)).toBeNull())
  })
})
