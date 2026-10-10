import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { installAndGrant } from '@/test/installAddon'
import type { ActionMeta } from '@/api/types'
import type { MockStore } from '@/mocks/store'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

const T = { timeout: 8000 }
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

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
    expect(await screen.findByRole('dialog', { name: 'Sign: Import (import) · GitHub (github)' }, T)).toBeInTheDocument()
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
    expect(await screen.findByRole('dialog', { name: 'Sign: Refresh (refresh) · GitHub (github)' }, T)).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })
})

describe('navigation actions are quiet', () => {
  it('a filter on the activity page shows no toast and refetches only that addon\'s state', async () => {
    const { user } = renderApp('/addon/activity/activity', { viewer: 'p_sev', setup: (s) => installAndGrant(s, s.workspaces[0].id, 'activity') })
    const type = await screen.findByRole('combobox', { name: 'Type' }, T)
    const success = vi.spyOn(toast, 'success')
    const states = vi.spyOn(api, 'getAddonState')
    const today = vi.spyOn(api, 'getToday')
    await user.selectOptions(type, within(type).getAllByRole('option').find((o) => /^Gates \(/.test(o.textContent ?? ''))!)
    await screen.findByRole('button', { name: 'Clear filters' }, T)
    expect(success).not.toHaveBeenCalled()
    expect(new Set(states.mock.calls.map((c) => c[1]))).toEqual(new Set(['activity']))
    expect(today).not.toHaveBeenCalled()
  })
  it('a navigation command in the palette opens the addon page, without a toast', async () => {
    const success = vi.spyOn(toast, 'success')
    const { user } = await openPalette('/', 'p_sev')
    await user.click(await screen.findByRole('option', { name: /Open terminal/ }, T))
    expect(await screen.findByRole('heading', { level: 1, name: /Terminals/ }, T)).toBeInTheDocument()
    expect(success).not.toHaveBeenCalled()
  })
})

describe('decision actions from addon surfaces go through core\'s prompt', () => {
  /** The publish page's attention list carries an addon-authored item action that answers a decision. */
  const decideFromNode = () => {
    const real = api.getAddonState
    vi.spyOn(api, 'getAddonState').mockImplementation(async (ws, name, ...rest) => {
      const st = await real(ws, name, ...rest)
      if (name !== 'publish') return st
      const node = st.attentionNode as { children: { type: string; items?: { actions: unknown[] }[] }[] }
      const list = node.children.find((c) => c.type === 'list')!
      const items = list.items!.map((a) => ({ ...a, actions: [{ label: 'Roll back now', action: 'decide', args: { id: 'dec_publish_failed_build', option: 'retry', confirmed: true }, variant: 'secondary' }] }))
      return { ...st, attentionNode: { ...node, children: node.children.map((c) => (c === list ? { ...c, items } : c)) } }
    })
  }
  it('an item action that posts decide opens "Decide for …" with core\'s facts; nothing is posted until signed, then addon.decided is recorded', async () => {
    decideFromNode()
    const post = vi.spyOn(api, 'runAddonAction')
    const { user } = renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    await user.click(await screen.findByRole('button', { name: 'Roll back now' }, T))
    const prompt = await screen.findByRole('dialog', { name: 'Decide for Publish (publish)' }, T)
    expect(within(prompt).getByText('Covers').nextElementSibling).toHaveTextContent('Answer: option retry')
    // The addon's own words: in the labelled region, never in the title or covers.
    const from = within(prompt).getByRole('region', { name: 'From addon publish' })
    expect(from).toHaveTextContent('Retry last good version')
    expect(from).toHaveTextContent('Ops notebook failed to build: retry with the last good version?')
    expect(post).not.toHaveBeenCalled()
    await user.click(within(prompt).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith(expect.any(String), 'publish', 'decide', { id: 'dec_publish_failed_build', option: 'retry', confirmed: true }), T)
    const { mockStore } = await import('@/api/client')
    await waitFor(() => expect(mockStore.wsEventsOf(mockStore.workspaces[0].id).filter((e) => e.type === 'addon.decided')).toEqual([expect.objectContaining({ id: 'dec_publish_failed_build', option: 'retry', presence: 'touchid' })]), T)
  })
  it('the host refuses a decision an addon node posts by itself (no core confirmation): 409, no event', async () => {
    const { mockStore } = await import('@/api/client')
    renderApp('/', { viewer: 'p_sev' })
    const ws = mockStore.workspaces[0].id
    await expect(api.runAddonAction(ws, 'publish', 'decide', { id: 'dec_publish_failed_build', option: 'retry' })).rejects.toMatchObject({ status: 409, code: 'confirm.required' })
    expect(mockStore.wsEventsOf(ws).some((e) => e.type === 'addon.decided')).toBe(false)
  })
})

describe('confirm: options fails closed', () => {
  // The show-once button lives in a ticket's Shares panel (a link is always to one ticket).
  const open = (setup: (s: MockStore) => void) => {
    vi.stubGlobal('innerWidth', 1440)
    return renderApp('/ticket/DEMO-0041', { viewer: 'p_sev', setup })
  }
  const press = async (user: ReturnType<typeof renderApp>['user']) => {
    const panel = await openTicketPanel(user, 'Shares')
    await user.click(await within(panel).findByRole('button', { name: 'New show-once link' }, T))
    return panel
  }
  it('options core cannot read: an inline error, nothing is posted, no dialog', async () => {
    const post = vi.spyOn(api, 'runAddonAction')
    const { user } = open(manifest('publish', 'share_once', { minRole: 'member', confirm: 'options', options: { fields: [] } } as ActionMeta))
    const panel = await press(user)
    expect(await within(panel).findByRole('alert', {}, T)).toHaveTextContent(/did not describe correctly/)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(post).not.toHaveBeenCalled()
  })
  it('confirm options without any options object posts nothing either', async () => {
    const post = vi.spyOn(api, 'runAddonAction')
    const { user } = open(manifest('publish', 'share_once', { minRole: 'member', confirm: 'options' }))
    const panel = await press(user)
    expect(await within(panel).findByRole('alert', {}, T)).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })
  it('a default outside the choices shows the first choice and posts the same value', async () => {
    const post = vi.spyOn(api, 'runAddonAction')
    const meta = { minRole: 'member', confirm: 'options', label: 'Create show-once link', options: { fields: [{ key: 'expires_days', label: 'Works for', default: 99, choices: [{ value: 3, label: '3 days' }, { value: 7, label: '7 days' }] }] } } as ActionMeta
    const { user } = open(manifest('publish', 'share_once', meta))
    await press(user)
    const ask = await screen.findByRole('dialog', { name: 'Choose: Share once (share_once) · Publish (publish)' }, T)
    expect(within(ask).getByLabelText('Works for')).toHaveValue('3')
    await userEvent.click(within(ask).getByRole('button', { name: 'Continue: Share once (share_once)' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith(expect.anything(), 'publish', 'share_once', expect.objectContaining({ expires_days: 3 })), T)
  })
})

describe('Undo on a success toast follows the manifest', () => {
  const stopApp = async (setup?: (s: MockStore) => void, viewer = 'p_sev') => {
    const success = vi.spyOn(toast, 'success')
    const { user } = renderApp('/addon/publish/shares', { viewer, setup })
    const row = (await screen.findByText('Billing explorer', {}, T)).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Stop' }))
    // Stopping a shared app asks first (core's confirm, named for the consequence); a manifest without it posts at once.
    const confirm = screen.queryByRole('button', { name: 'Confirm: Stop (stop)' })
    if (confirm) await user.click(confirm)
    await waitFor(() => expect(success.mock.calls.some((c) => c[0] === 'Billing explorer stopped.')).toBe(true), T)
    return success.mock.calls.find((c) => c[0] === 'Billing explorer stopped.')![1] as { action?: unknown } | undefined
  }
  it('an undo the manifest does not declare gets no Undo button', async () => {
    const opts = await stopApp(manifest('publish', 'stop', { minRole: 'member' }))
    expect(opts?.action).toBeUndefined()
  })
  it('a declared undo that points at a confirm action gets no Undo button', async () => {
    const opts = await stopApp((s) => {
      manifest('publish', 'stop', { minRole: 'member', undo: 'start' })(s)
      manifest('publish', 'start', { minRole: 'member', confirm: 'sign', label: 'Start' })(s)
    })
    expect(opts?.action).toBeUndefined()
  })
  it('a response naming another action than the declared pair gets no Undo button', async () => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'Billing explorer stopped.', undo: { action: 'revoke', args: { id: 'sh_report' } } })
    const opts = await stopApp()
    expect(opts?.action).toBeUndefined()
    expect(post).toHaveBeenCalledTimes(1)
  })
  it('undo args that differ from the request get no Undo button', async () => {
    vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'Billing explorer stopped.', undo: { action: 'start', args: { id: 'app_ops' } } })
    expect((await stopApp())?.action).toBeUndefined()
  })
  it.each([
    ['a target the viewer\'s role may not run', { minRole: 'owner' as const }, 'p_mara'],
    ['a navigation target', { minRole: 'member' as const, kind: 'navigation' as const }, 'p_sev'],
    ['a decision target', { minRole: 'member' as const, decision: true }, 'p_sev'],
  ])('a declared undo pointing at %s gets no Undo button', async (_n, startMeta, viewer) => {
    const opts = await stopApp((s) => {
      manifest('publish', 'stop', { minRole: 'member', undo: 'start' })(s)
      manifest('publish', 'start', startMeta)(s)
    }, viewer)
    expect(opts?.action).toBeUndefined()
  })
  it('a valid declared undo shows Undo, and pressing it runs the undo action through the normal path', async () => {
    const opts = (await stopApp()) as { action: { label: string; onClick: () => void } }
    expect(opts.action.label).toBe('Undo')
    const post = vi.spyOn(api, 'runAddonAction')
    opts.action.onClick()
    await waitFor(() => expect(post).toHaveBeenCalledWith(expect.anything(), 'publish', 'start', expect.objectContaining({ id: 'app_billing' })), T)
  })
})
