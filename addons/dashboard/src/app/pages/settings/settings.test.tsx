import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore, resetMockStoreForTests } from '@/api/client'
import { ApiError } from '@/api/types'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'

afterEach(() => vi.restoreAllMocks())

describe('Settings', () => {
  it('changes a member role after signing', async () => {
    const { user } = renderApp('/settings/members')
    const row = await screen.findByRole('row', { name: /Tom/ })
    await user.selectOptions(within(row).getByRole('combobox', { name: /^Role of / }), 'member')
    await user.click(await screen.findByRole('button', { name: 'Sign and save' }))
    await within(screen.getByRole('row', { name: /Tom/ })).findByDisplayValue('member')
    expect(within(screen.getByRole('row', { name: /Tom/ })).getByRole('combobox', { name: /^Role of / })).toHaveValue('member')
  })
  it('refuses to demote the last owner', async () => {
    const { user } = renderApp('/settings/members')
    const row = await screen.findByRole('row', { name: /Severin/ })
    expect(within(row).getByRole('combobox', { name: /^Role of / })).toBeDisabled()
    await user.hover(within(row).getByRole('combobox', { name: /^Role of / }))
    expect(await screen.findByText(/last owner/)).toBeInTheDocument()
  })
  it('describes a gate policy in a sentence and saves it', async () => {
    const { user } = renderApp('/settings/gates', { setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'gate.policy_set', gate: 'plan', approvers: 'maintainer', count: 1 }) })
    expect(await screen.findByText(/Plan needs 1 approval from/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Plan: 2 approvals' }))
    await user.click(await screen.findByRole('button', { name: 'Sign and save' }))
    expect(await screen.findByText(/Plan needs 2 approvals/)).toBeInTheDocument()
    expect(screen.getByText(/Approvals already given stay valid; new approvals use the new policy\. To re-review an approved ticket, request changes on it\./)).toBeInTheDocument()
  })
  it('the owner sets the agent grant length in General (signed, 1 to 24 h)', async () => {
    const { user } = renderApp('/settings/general')
    const field = await screen.findByLabelText('Agent grant length (hours)')
    expect(field).toHaveValue(8)
    const save = screen.getAllByRole('button', { name: 'Save' })[1]
    expect(save).toBeDisabled() // unchanged
    await user.clear(field)
    await user.type(field, '25')
    expect(save).toBeDisabled() // out of range
    await user.clear(field)
    await user.type(field, '24')
    await user.click(save)
    expect(await screen.findByText(/Default length of a grant: 24 hours \(was 8 hours\)/)).toBeInTheDocument()
    await user.click(await screen.findByRole('button', { name: 'Sign and save' }))
    await waitFor(() => expect(mockStore.workspaces[0].grant_hours).toBe(24))
  })
  it('Mara sees settings read-only', async () => {
    renderApp('/settings/members', { viewer: 'p_mara' })
    expect(await screen.findByText('Only owners change settings.')).toBeInTheDocument()
    expect(within(await screen.findByRole('row', { name: /Tom/ })).getByRole('combobox', { name: /^Role of / })).toBeDisabled()
  })
  it('shows the relay as not connected and links to Relay & devices', async () => {
    renderApp('/settings/general')
    expect(await screen.findByText(/Not connected yet/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Relay & devices' })).toHaveAttribute('href', '/settings/relay')
  })
  it('the nav shows General, Members, Gates, Relay & devices (Preview), Addons, Skills and Connections only: no entry per addon', async () => {
    renderApp('/settings/general')
    const nav = await screen.findByRole('navigation', { name: 'Settings' })
    expect(within(nav).getAllByRole('link').map((l) => l.textContent)).toEqual(['General', 'Members', 'Gates', 'Relay & devicesPreview', 'Addons', 'Skills', 'Connections'])
    expect(within(nav).queryByRole('img', { name: /From addon/ })).toBeNull()
  })
  it("an addon's settings expand as an accordion beneath its row, one at a time, with Addons marked as the current section", async () => {
    const { user } = renderApp('/settings/addons')
    const row = await screen.findByRole('row', { name: /Publish/ })
    expect(within(row).getByRole('button', { name: 'Settings' })).toHaveAttribute('aria-expanded', 'false')
    await user.click(within(row).getByRole('button', { name: 'Settings' }))
    const panel = await screen.findByRole('group', { name: /Publish settings/ })
    expect(screen.queryByRole('dialog')).toBeNull() // no drawer
    await waitFor(() => expect(within(screen.getByRole('row', { name: /Publish [\d.]+/ })).getByRole('button', { name: 'Settings' })).toHaveAttribute('aria-expanded', 'true'))
    expect(screen.getByRole('row', { name: /^Publish \d/ }).nextElementSibling).toContainElement(panel) // right beneath the row
    expect(await within(panel).findByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(within(screen.getByRole('navigation', { name: 'Settings', hidden: true })).getByRole('link', { name: 'Addons', hidden: true })).toHaveAttribute('aria-current', 'page')
    // Opening another row's settings closes this one.
    await user.click(within(screen.getByRole('row', { name: /^Estimate \d/ })).getByRole('button', { name: 'Settings' }))
    expect(await screen.findByRole('group', { name: /Estimate settings/ })).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: /Publish settings/ })).toBeNull()
    await user.click(within(await screen.findByRole('group', { name: /Estimate settings/ })).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('group', { name: /Estimate settings/ })).toBeNull())
    await waitFor(() => expect(within(screen.getByRole('row', { name: /^Estimate \d/ })).getByRole('button', { name: 'Settings' })).toHaveFocus())
  })
  it('clicking Settings of the open row closes it again', async () => {
    const { user } = renderApp('/settings/addon/estimate')
    await screen.findByRole('group', { name: /Estimate settings/ })
    await user.click(within(screen.getByRole('row', { name: /^Estimate \d/ })).getByRole('button', { name: 'Settings' }))
    await waitFor(() => expect(screen.queryByRole('group', { name: /Estimate settings/ })).toBeNull())
  })
  it('the deep link opens the Addons tab with that row expanded; closing returns to the Addons list', async () => {
    const { user } = renderApp('/settings/addon/estimate')
    const panel = await screen.findByRole('group', { name: /Estimate settings/ })
    expect(await screen.findByRole('row', { name: /Publish/ })).toBeInTheDocument() // the list is there around it
    expect(panel.closest('tr')?.previousElementSibling).toBe(screen.getByRole('row', { name: /^Estimate \d/ }))
    await user.click(within(panel).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('group', { name: /Estimate settings/ })).toBeNull())
    expect(screen.getByRole('heading', { name: 'Addons' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Back to Addons/ })).toBeNull()
    await waitFor(() => expect(within(screen.getByRole('row', { name: /^Estimate \d/ })).getByRole('button', { name: 'Settings' })).toHaveFocus())
  })
  it('the footer Save is the form submit (form=id), and the form draws no button of its own', async () => {
    const { user } = renderApp('/settings/addon/models', { setup: (st) => installAndGrant(st, st.workspaces[0].id, 'models') })
    const field = await screen.findByRole('textbox', { name: 'Standard model' })
    expect(screen.getAllByRole('button', { name: 'Save' })).toHaveLength(1) // the form draws no button of its own
    expect(screen.getByRole('button', { name: 'Save' })).toHaveAttribute('form', 'addon-settings-models')
    await user.clear(field)
    await user.type(field, 'haiku') // Enter in a field is native browser behaviour (jsdom/user-event does not follow `form=`): checked in the browser
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(async () => expect(((await api.getAddonState(mockStore.workspaces[0].id, 'models')).settings as { standard: string }).standard).toBe('haiku'))
    // A successful save closes the panel and says so (the toast outlives the route change); no "discard?" question.
    await waitFor(() => expect(screen.queryByRole('region', { name: /Standard model|models settings/i })).toBeNull())
    expect(await screen.findByText(/settings saved$/)).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).toBeNull()
    expect(screen.getByRole('heading', { name: 'Addons' })).toBeInTheDocument()
  })
  it('changing a value and putting it back asks nothing', async () => {
    const { user } = renderApp('/settings/addon/estimate')
    const scale = await screen.findByLabelText(/Scale/)
    await user.selectOptions(scale, 't-shirt')
    expect(await screen.findByText('Unsaved changes')).toBeInTheDocument()
    await user.selectOptions(scale, 'fibonacci')
    await waitFor(() => expect(screen.queryByText('Unsaved changes')).toBeNull())
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('group', { name: /Estimate settings/ })).toBeNull())
  })
  it('a palette navigation with unsaved changes asks first, and keeps the edits on request', async () => {
    const { user } = renderApp('/settings/addon/estimate')
    await user.selectOptions(await screen.findByLabelText(/Scale/), 't-shirt')
    await user.keyboard('{Meta>}k{/Meta}')
    await user.keyboard('Board{Enter}')
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(await screen.findByLabelText(/Scale/)).toHaveValue('t-shirt')
    await user.keyboard('{Meta>}k{/Meta}')
    await user.keyboard('Board{Enter}')
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByRole('heading', { name: 'Board' })).toBeInTheDocument()
  })
  it('a workspace switch with unsaved changes asks first', async () => {
    const { user } = renderApp('/settings/addon/estimate')
    await user.selectOptions(await screen.findByLabelText(/Scale/), 't-shirt')
    await user.keyboard('{Meta>}k{/Meta}')
    await user.keyboard('Switch to INT{Enter}')
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(screen.getByLabelText(/Scale/)).toHaveValue('t-shirt')
  })
  it('asks before closing with unsaved changes, and keeps editing on request', async () => {
    const { user } = renderApp('/settings/addon/estimate')
    await user.selectOptions(await screen.findByLabelText(/Scale/), 't-shirt')
    expect(await screen.findByText('Unsaved changes')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(screen.getByLabelText(/Scale/)).toHaveValue('t-shirt')
    expect(screen.getByRole('group', { name: /Estimate settings/ })).toBeInTheDocument()
    screen.getByLabelText(/Scale/).focus()
    await user.keyboard('{Escape}') // Esc closes the panel like Cancel: asks first
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(within(await screen.findByRole('row', { name: /^Estimate \d/ })).getByRole('button', { name: 'Settings' })).toHaveFocus()
    const state = await api.getAddonState(mockStore.workspaces[0].id, 'estimate')
    expect((state.settings as { scale: string }).scale).toBe('fibonacci')
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
    await user.click(screen.getByRole('combobox', { name: 'Person' }))
    await user.type(screen.getByRole('combobox', { name: 'Person' }), 'Ida')
    await user.click(await screen.findByRole('option', { name: /Ida/ }))
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await user.click(await screen.findByRole('button', { name: 'Sign and save' }))
    expect(await screen.findByRole('row', { name: /Ida/ })).toBeInTheDocument()
  })
  it('Add member offers people by name, never ids; Enter picks the highlighted one, a second Enter adds', async () => {
    const { user } = renderApp('/settings/members')
    await user.click(await screen.findByRole('button', { name: 'Add member' }))
    const box = screen.getByRole('combobox', { name: 'Person' })
    await user.type(box, 'Ida')
    expect(await screen.findByRole('option', { name: /Ida/ })).toBeInTheDocument()
    expect(screen.queryByText(/p_ida/)).toBeNull()
    expect(screen.getByText(/Maintainer: can approve plans and verdicts, cannot change settings/)).toBeInTheDocument()
    // The note about email addresses is there before anything is pressed, and the list does not cover the Role field.
    expect(screen.getByText(/Adding by an email address that is not in it comes later/)).toBeInTheDocument()
    expect(screen.getByLabelText('Role')).toBeVisible()
    await user.keyboard('{Enter}')
    expect(screen.queryByRole('button', { name: 'Sign and save' })).toBeNull() // Enter only picked
    expect(box).toHaveValue('Ida')
    expect(screen.queryByRole('listbox')).toBeNull()
    await user.keyboard('{Enter}')
    expect(await screen.findByRole('button', { name: 'Sign and save' })).toBeInTheDocument()
  })
  it('an email that is not in the directory is refused, a known one is found', async () => {
    const { user } = renderApp('/settings/members')
    await user.click(await screen.findByRole('button', { name: 'Add member' }))
    await user.type(screen.getByRole('combobox', { name: 'Person' }), 'ida@other.org')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/No one with this email in your directory yet/)
    expect(screen.queryByRole('button', { name: 'Sign and save' })).toBeNull()
    await user.clear(screen.getByRole('combobox', { name: 'Person' }))
    await user.type(screen.getByRole('combobox', { name: 'Person' }), 'ida@example.test')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByText(/Ida \(ida@example.test, p_ida\)/)).toBeInTheDocument()
  })
  it('the people directory is for owners of a workspace they are in', async () => {
    const ws = mockStore.workspaces[0].id
    expect((await api.listPeople(ws)).some((p) => p.name === 'Ida')).toBe(true)
    mockStore.setViewer('p_tom')
    await expect(api.listPeople(ws)).rejects.toMatchObject({ status: 403 })
    mockStore.setViewer('p_mara')
    await expect(api.listPeople(ws)).rejects.toMatchObject({ status: 403 })
    await expect(api.listPeople('nope')).rejects.toMatchObject({ status: 404 })
  })
  it('the host refuses a policy that can never be met, and an approval given before a stricter policy stays', async () => {
    resetMockStoreForTests()
    mockStore.setViewer('p_sev')
    const ws = mockStore.workspaces[0].id
    await expect(api.postSettings(ws, { op: 'gate.policy', gate: 'plan', approvers: 'owner', count: 2 })).rejects.toMatchObject({ status: 409 })
    const key = 'DEMO-0044'
    const t0 = await api.getTicket(key)
    expect(t0.gates.plan.state).toBe('pending')
    await api.postAction(key, { action: 'approve', gate: 'plan' })
    expect((await api.getTicket(key)).gates.plan.state).toBe('approved')
    await api.postSettings(ws, { op: 'gate.policy', gate: 'plan', approvers: 'maintainer', count: 2 })
    expect((await api.getTicket(key)).gates.plan.state).toBe('approved')
  })
  it('"Affects N" counts tickets waiting at the gate, like Today', async () => {
    const { user } = renderApp('/settings/gates')
    const ws = mockStore.workspaces[0].id
    const today = await api.getToday(ws)
    const plans = today.needs_you.filter((i) => i.kind === 'approval' && i.ref === 'plan').length
    expect(plans).toBeGreaterThan(0)
    const section = (await screen.findByRole('heading', { name: 'Plan' })).closest('section')!
    // Within the Plan section: another gate may affect the same number of tickets (Verify does since N9).
    await within(section as HTMLElement).findByText(new RegExp(`Affects ${plans} open ticket`), {}, { timeout: 10000 })
    expect(section.textContent).toMatch(new RegExp(`Affects ${plans} open ticket`))
    void user
  })
  it('an empty Add member submit explains itself', async () => {
    const { user } = renderApp('/settings/members')
    await user.click(await screen.findByRole('button', { name: 'Add member' }))
    await user.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Choose a person/)
  })
  it('refuses a gate policy that can never be met', async () => {
    const { user } = renderApp('/settings/gates')
    await user.click(await screen.findByRole('button', { name: 'Plan: 2 approvals' }))
    expect(await screen.findByText(/can never be met/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign and save' })).toBeNull()
  })
  it('an approved gate stays approved after the policy asks for more', async () => {
    const { user } = renderApp('/settings/gates', { setup: (s) => ['requirements', 'plan', 'verify'].forEach((g) => s.appendWs(s.workspaces[0].id, { type: 'gate.policy_set', gate: g, approvers: 'maintainer', count: 1 })) })
    const key = 'DEMO-0043'
    const before = await api.getTicket(key)
    const gate = (['requirements', 'plan', 'verify'] as const).find((g) => before.gates[g].state === 'approved')
    expect(gate).toBeDefined()
    await user.click(await screen.findByRole('button', { name: `${gate![0].toUpperCase()}${gate!.slice(1)}: 2 approvals` }))
    await user.click(await screen.findByRole('button', { name: 'Sign and save' }))
    await screen.findByText(/needs 2 approvals/)
    expect((await api.getTicket(key)).gates[gate!].state).toBe('approved')
  })
  it('a maintainer sees why controls are disabled', async () => {
    renderApp('/settings/general', { viewer: 'p_mara' })
    expect((await screen.findAllByText('Only owners can save.')).length).toBe(2) // the name and the agent grant length
    expect(screen.getByRole('button', { name: /Export workspace/ })).toBeDisabled()
    expect(screen.getByText('Only owners can export.')).toBeInTheDocument()
  })
  it('/settings/addons/<name> redirects to the addon settings route', async () => {
    renderApp('/settings/addons/estimate')
    expect(await screen.findByRole('group', { name: /Estimate settings/ })).toBeInTheDocument()
  })
  it('saves an addon settings form and the value is in the addon state', async () => {
    const { user } = renderApp('/settings/addon/estimate')
    await user.selectOptions(await screen.findByLabelText(/Scale/), 'fibonacci')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/Settings saved/)).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('Unsaved changes')).toBeNull()) // saved: nothing to discard
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
    expect(within(screen.getByRole('group', { name: /Estimate settings/ })).getByText('Only owners change settings.')).toBeInTheDocument() // the reason, in the panel
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
