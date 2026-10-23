import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { WORKSPACE_COLOURS, WORKSPACE_ICONS, defaultWorkspaceIcon, lookAlike, workspaceIconOf } from './workspaceIcon'

beforeEach(() => {
  try {
    Object.keys(localStorage).filter((k) => k.startsWith('orch.wsicon.')).forEach((k) => localStorage.removeItem(k))
  } catch {
    /* ignore */
  }
})

describe('workspace icon', () => {
  it('the default depends on the id alone (not the name) and is one of the built-in set', () => {
    const a = defaultWorkspaceIcon('6f1c0d2e-8b4a')
    expect(defaultWorkspaceIcon(' 6F1C0D2E-8B4A ')).toEqual(a)
    expect(workspaceIconOf('6f1c0d2e-8b4a')).toEqual(a)
    expect(WORKSPACE_ICONS.map((i) => i.id)).toContain(a.icon)
    expect(WORKSPACE_COLOURS.map((c) => c.id)).toContain(a.colour)
  })
  it('ignores a stored value that is not in the built-in set', () => {
    localStorage.setItem('orch.wsicon.x', JSON.stringify({ icon: 'http://evil/x.png', colour: 'url(x)' }))
    expect(workspaceIconOf('x')).toEqual(defaultWorkspaceIcon('x'))
  })
  it('lookAlike: same name, or same icon and colour, is flagged; a chosen icon does not hide it', () => {
    const iconOf = (id: string) => (id === 'c' ? { icon: 'star', colour: 'blue' } : id === 'd' ? { icon: 'star', colour: 'blue' } : { icon: 'folder', colour: 'green' })
    const all = [
      { id: 'a', name: 'Acme' },
      { id: 'b', name: ' acme ' },
      { id: 'c', name: 'One' },
      { id: 'd', name: 'Two' },
      { id: 'e', name: 'Three' },
    ]
    expect(lookAlike(all[0], all, iconOf)).toBe(true) // same name as b
    expect(lookAlike(all[2], all, iconOf)).toBe(true) // same icon and colour as d
    expect(lookAlike(all[4], all, (id) => ({ icon: id, colour: id }))).toBe(false)
  })
  it('the menu shows the icon with the name, no prefix pill', async () => {
    renderApp('/')
    const trigger = await screen.findByRole('button', { name: 'Switch workspace' })
    await waitFor(() => expect(trigger.querySelector('[data-ws-icon]')).not.toBeNull())
    expect(trigger.textContent).toMatch(/DEMO/) // the prefix stays beside the icon
  })
  it('Settings changes the icon and colour, and the menu follows; reset returns to the automatic one', async () => {
    const { user } = renderApp('/settings/general')
    const def = defaultWorkspaceIcon(mockStore.workspaces[0].id)
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
