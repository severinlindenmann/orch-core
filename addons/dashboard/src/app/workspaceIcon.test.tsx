import { screen, waitFor, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { COLOUR_IDS, ICON_IDS, defaultWorkspaceIcon, fnv1a, lookAlike, workspaceIconOf } from '@/api/wsIcons'
import { renderApp } from '@/test/renderApp'

const CORE = `${process.cwd()}/../../plugins/orch-core/src/orch`

describe('workspace icon lists', () => {
  it('equal core: the schemas, the python constant and these ids', () => {
    for (const f of ['schema/schemas/event.settings.changed.json', 'schema/schemas/workspace.json']) {
      const doc = JSON.parse(readFileSync(`${CORE}/${f}`, 'utf8'))
      const props = (doc.properties.set ?? doc.properties.settings).properties.icon.oneOf[0].properties
      expect(props.icon.enum).toEqual([...ICON_IDS])
      expect(props.colour.enum).toEqual([...COLOUR_IDS])
    }
    const py = readFileSync(`${CORE}/wsicons.py`, 'utf8')
    const keys = (name: string) => [...py.split(`${name} = {`)[1].split('}  # fmt')[0].matchAll(/"([a-z]+)":/g)].map((m) => m[1])
    expect(keys('ICON_LABELS')).toEqual([...ICON_IDS])
    expect(keys('COLOUR_LABELS')).toEqual([...COLOUR_IDS])
  })
  it('the default is derived from the id (same vectors as core), not the name', () => {
    expect(fnv1a('')).toBe(0x811c9dc5)
    expect(fnv1a('a')).toBe(0xe40c292c)
    expect(defaultWorkspaceIcon('w1')).toEqual({ icon: 'flame', colour: 'orange' })
    expect(defaultWorkspaceIcon('ws-demo-1')).toEqual({ icon: 'globe', colour: 'orange' })
    expect(workspaceIconOf({ id: 'w1' })).toEqual(defaultWorkspaceIcon('w1'))
  })
  it('a signed icon wins; a value outside the lists is ignored', () => {
    expect(workspaceIconOf({ id: 'w1', icon: { icon: 'star', colour: 'teal' } })).toEqual({ icon: 'star', colour: 'teal' })
    expect(workspaceIconOf({ id: 'w1', icon: { icon: 'http://x/y.png', colour: 'pink' } })).toEqual(defaultWorkspaceIcon('w1'))
    expect(workspaceIconOf({ id: 'w1', icon: { icon: 'star', colour: '#fff' } })).toEqual(defaultWorkspaceIcon('w1'))
  })
  it('lookAlike: same name or same icon+colour is flagged, a chosen icon does not hide it', () => {
    const star = { icon: 'star', colour: 'blue' }
    const all = [
      { id: 'a', name: 'Acme' },
      { id: 'b', name: ' acme ' },
      { id: 'c', name: 'One', icon: star },
      { id: 'd', name: 'Two', icon: star },
      { id: 'e', name: 'Three', icon: { icon: 'heart', colour: 'pink' } },
    ]
    expect(lookAlike(all[0], all)).toBe(true)
    expect(lookAlike(all[2], all)).toBe(true)
    expect(lookAlike(all[4], all)).toBe(false)
  })
})

describe('workspace icon in the app', () => {
  it('the menu shows the icon and the prefix beside it', async () => {
    renderApp('/')
    const trigger = await screen.findByRole('button', { name: 'Switch workspace' })
    await waitFor(() => expect(trigger.querySelector('[data-ws-icon]')).not.toBeNull())
    expect(trigger.textContent).toMatch(/DEMO/)
  })
  it('the owner changes it in Settings (signed) and the menu follows; reset goes back to the automatic one', async () => {
    const { user } = renderApp('/settings/general')
    const trigger = await screen.findByRole('button', { name: 'Switch workspace' })
    const def = defaultWorkspaceIcon(mockStore.workspaces[0].id)
    const icon = def.icon === 'rocket' ? 'Star' : 'Rocket'
    const colour = def.colour === 'pink' ? 'Teal' : 'Pink'
    const group = await screen.findByRole('group', { name: 'Icon' })
    await user.click(within(group).getByRole('button', { name: icon }))
    await user.click(within(screen.getByRole('group', { name: 'Colour' })).getByRole('button', { name: colour }))
    const sel = `[data-ws-icon="${icon.toLowerCase()}"][data-ws-colour="${colour.toLowerCase()}"]`
    expect(trigger.querySelector(sel)).toBeNull() // a draft until it is signed
    const section = screen.getByRole('group', { name: 'Icon' }).closest('section') ?? document.body
    await user.click(within(section).getByRole('button', { name: 'Save' }))
    await user.click(await screen.findByRole('button', { name: 'Sign and save' }))
    await waitFor(() => expect(trigger.querySelector(sel)).not.toBeNull())
    expect(mockStore.workspaces[0].icon).toEqual({ icon: icon.toLowerCase(), colour: colour.toLowerCase() })
    await user.click(screen.getByRole('button', { name: 'Use the automatic icon' }))
    await user.click(await screen.findByRole('button', { name: 'Sign and save' }))
    await waitFor(() => expect(trigger.querySelector(sel)).toBeNull())
  })
  it('a member cannot save it', async () => {
    renderApp('/settings/general', { viewer: 'p_mara' })
    const group = await screen.findByRole('group', { name: 'Icon' })
    expect(within(group).getByRole('button', { name: 'Rocket' })).toBeDisabled()
  })
})
