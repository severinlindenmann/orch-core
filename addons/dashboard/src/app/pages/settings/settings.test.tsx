import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore } from '@/api/client'
import { ApiError } from '@/api/types'
import { renderApp } from '@/test/renderApp'

afterEach(() => vi.restoreAllMocks())

describe('Settings', () => {
  it('changes a member role after signing', async () => {
    const { user } = renderApp('/settings/members')
    const row = await screen.findByRole('row', { name: /Tom/ })
    await user.selectOptions(within(row).getByRole('combobox', { name: 'Role' }), 'member')
    await user.click(await screen.findByRole('button', { name: 'Sign and save' }))
    await within(screen.getByRole('row', { name: /Tom/ })).findByDisplayValue('member')
    expect(within(screen.getByRole('row', { name: /Tom/ })).getByRole('combobox', { name: 'Role' })).toHaveValue('member')
  })
  it('refuses to demote the last owner', async () => {
    const { user } = renderApp('/settings/members')
    const row = await screen.findByRole('row', { name: /Severin/ })
    expect(within(row).getByRole('combobox', { name: 'Role' })).toBeDisabled()
    await user.hover(within(row).getByRole('combobox', { name: 'Role' }))
    expect(await screen.findByText(/last owner/)).toBeInTheDocument()
  })
  it('describes a gate policy in a sentence and saves it', async () => {
    const { user } = renderApp('/settings/gates')
    expect(await screen.findByText(/Plan needs 1 approval from/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Plan: 2 approvals' }))
    await user.click(await screen.findByRole('button', { name: 'Sign and save' }))
    expect(await screen.findByText(/Plan needs 2 approvals/)).toBeInTheDocument()
    expect(screen.getByText('Open approvals stay valid; new approvals use the new policy.')).toBeInTheDocument()
  })
  it('Mara sees settings read-only', async () => {
    renderApp('/settings/members', { viewer: 'p_mara' })
    expect(await screen.findByText('Only owners change settings.')).toBeInTheDocument()
    expect(within(await screen.findByRole('row', { name: /Tom/ })).getByRole('combobox', { name: 'Role' })).toBeDisabled()
  })
  it('shows the relay as not connected', async () => {
    renderApp('/settings/general')
    expect(await screen.findByText(/Not connected/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Connect' })).toBeDisabled()
  })
  it('the nav shows General, Members, Gates and Addons only: no entry per addon', async () => {
    renderApp('/settings/general')
    const nav = await screen.findByRole('navigation', { name: 'Settings' })
    expect(within(nav).getAllByRole('link').map((l) => l.textContent)).toEqual(['General', 'Members', 'Gates', 'Addons'])
    expect(within(nav).queryByRole('img', { name: /From addon/ })).toBeNull()
  })
  it("an addon's settings open from its row in Addons, with Addons marked as the current section", async () => {
    const { user } = renderApp('/settings/addons')
    const row = await screen.findByRole('row', { name: /Publish/ })
    await user.click(within(row).getByRole('link', { name: 'Settings' }))
    expect(await screen.findByRole('link', { name: /Back to Addons/ })).toHaveAttribute('href', '/settings/addons')
    expect(within(screen.getByRole('navigation', { name: 'Settings' })).getByRole('link', { name: 'Addons' })).toHaveAttribute('aria-current', 'page')
  })
  it('shows the workspace identity', async () => {
    renderApp('/settings/general')
    expect(await screen.findByText('6f1c0d2e-8b4a-4e1f-9c3d-2a7b5e9f0c11')).toBeInTheDocument()
    expect(await screen.findByText(/epoch 1/i)).toBeInTheDocument() // the identity loads after the workspace id
  })
  it('archiving needs the prefix and then the mock refuses', async () => {
    const { user } = renderApp('/settings/general')
    await user.click(await screen.findByRole('button', { name: 'Archive workspace' }))
    const confirm = screen.getByRole('button', { name: 'Archive' })
    expect(confirm).toBeDisabled()
    await user.type(screen.getByLabelText(/Type DEMO to confirm/), 'DEMO')
    await user.click(confirm)
    expect(await screen.findByText(/Archiving is CLI-only: `orch workspace archive`/)).toBeInTheDocument()
  })
  it('adds a member after signing', async () => {
    const { user } = renderApp('/settings/members')
    await user.click(await screen.findByRole('button', { name: 'Add member' }))
    await user.type(screen.getByLabelText('Name'), 'Ida')
    await user.type(screen.getByLabelText('Person id'), 'p_ida')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await user.click(await screen.findByRole('button', { name: 'Sign and save' }))
    expect(await screen.findByRole('row', { name: /Ida/ })).toBeInTheDocument()
  })
  it('saves an addon settings form and the value is in the addon state', async () => {
    const { user } = renderApp('/settings/addon/estimate')
    await user.selectOptions(await screen.findByLabelText(/Scale/), 'fibonacci')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/Settings saved/)).toBeInTheDocument()
    const state = await api.getAddonState(mockStore.workspaces[0].id, 'estimate')
    expect((state.settings as { scale: string }).scale).toBe('fibonacci')
  })
  it('a disabled addon asks to be enabled first', async () => {
    renderApp('/settings/addon/estimate', { setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'addon.disabled', name: 'estimate' }) })
    expect(await screen.findByText(/Enable Estimate to change its settings/)).toBeInTheDocument()
  })
  it('non-owners see the addon form disabled', async () => {
    renderApp('/settings/addon/estimate', { viewer: 'p_mara' })
    expect(await screen.findByLabelText(/Scale/)).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })
  it('the mock refuses save_settings from a non-owner', async () => {
    mockStore.setViewer('p_mara')
    await expect(api.runAddonAction(mockStore.workspaces[0].id, 'estimate', 'save_settings', { formData: { scale: 't-shirt' } })).rejects.toThrow()
  })
  it.each(['/settings/bogus', '/settings/addon'])('redirects the unknown settings page %s to General', async (path) => {
    renderApp(path)
    expect(await screen.findByText(/Not connected/)).toBeInTheDocument() // the General tab
    expect(screen.getByRole('link', { name: 'General' })).toHaveAttribute('aria-current', 'page')
  })
  it('shows a skeleton while addon settings load', async () => {
    vi.spyOn(api, 'getAddonState').mockReturnValue(new Promise(() => {}))
    renderApp('/settings/addon/estimate')
    expect(await screen.findByLabelText('Loading addon settings')).toBeInTheDocument()
  })
  it('says so when addon settings fail to load', async () => {
    vi.spyOn(api, 'getAddonState').mockRejectedValue(new ApiError(500, { code: 'internal', message: 'Host is down', retryable: true }))
    renderApp('/settings/addon/estimate')
    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not load the Estimate settings.*Host is down/)
  })
})
