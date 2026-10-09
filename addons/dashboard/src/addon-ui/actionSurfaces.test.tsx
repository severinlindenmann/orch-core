import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { ActionMeta } from '@/api/types'
import type { MockStore } from '@/mocks/store'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }
afterEach(() => vi.restoreAllMocks())

/** Changes one action's manifest entry of a package (as a new published manifest would). */
const manifest = (addon: string, action: string, meta: ActionMeta) => (s: MockStore) => {
  const pkg = s.addons.find((a) => a.name === addon)!
  pkg.actions = { ...pkg.actions, [action]: meta }
}

async function laneImport(viewer: string, setup?: (s: MockStore) => void) {
  const r = renderApp('/board', { viewer, setup })
  const lane = await screen.findByRole('region', { name: /GitHub issues/ }, T)
  const [button] = await within(lane).findAllByRole('button', { name: 'Import as ticket' }, T)
  return { ...r, button }
}

async function openPalette(path: string, viewer: string, setup?: (s: MockStore) => void) {
  const r = renderApp(path, { viewer, setup })
  await screen.findByRole('heading', { level: 1 }, T)
  await r.user.keyboard('{Control>}k{/Control}')
  await screen.findByPlaceholderText(/Search tickets/, {}, T)
  return r
}

describe('board lane actions use the manifest', () => {
  it('a viewer gets a member action disabled, with the reason', async () => {
    const { button } = await laneImport('p_tom')
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription('Viewers cannot do this.')
  })
  it('a viewer runs an action the manifest opens to viewers', async () => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'imported' })
    const { user, button } = await laneImport('p_tom', manifest('github', 'import', { minRole: 'viewer' }))
    expect(button).toBeEnabled()
    await user.click(button)
    await waitFor(() => expect(post).toHaveBeenCalled(), T)
    expect(post.mock.calls[0][2]).toBe('import')
  })
  it('a signed action opens core\'s signing prompt before anything is posted', async () => {
    const post = vi.spyOn(api, 'runAddonAction')
    const { user, button } = await laneImport('p_sev', manifest('github', 'import', { minRole: 'member', confirm: 'sign', label: 'Import' }))
    await user.click(button)
    expect(await screen.findByRole('dialog', { name: 'Sign: import · GitHub' }, T)).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })
})

describe('palette addon commands use the manifest', () => {
  it('a viewer sees the viewer-level commands only', async () => {
    await openPalette('/', 'p_tom')
    const group = await screen.findByRole('group', { name: 'Addon commands' }, T)
    expect(within(group).getByText('Open terminal')).toBeInTheDocument()
    expect(within(group).queryByText('Refresh pull requests')).toBeNull()
  })
  it('a member does not see a maintainer-only command', async () => {
    await openPalette('/', 'p_sev', (s) => {
      manifest('github', 'refresh', { minRole: 'maintainer' })(s)
      const sev = s.workspaces[0].members.find((m) => m.person === 'p_sev')!
      sev.role = 'member'
    })
    const group = await screen.findByRole('group', { name: 'Addon commands' }, T)
    expect(within(group).queryByText('Refresh pull requests')).toBeNull()
  })
  it('a signed command opens core\'s signing prompt instead of posting', async () => {
    const post = vi.spyOn(api, 'runAddonAction')
    const { user } = await openPalette('/', 'p_sev', manifest('github', 'refresh', { minRole: 'member', confirm: 'sign', label: 'Refresh' }))
    await user.click(await screen.findByRole('option', { name: /Refresh pull requests/ }, T))
    expect(await screen.findByRole('dialog', { name: 'Sign: refresh · GitHub' }, T)).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })
})
