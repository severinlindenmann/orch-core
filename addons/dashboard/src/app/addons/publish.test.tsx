import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { api, mockStore } from '@/api/client'
import { moreAction } from '@/test/rowActions'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'
import { installAndGrant } from '@/test/installAddon'

// Ticket-rail tests render at 1440 px (the rail is a column from 1280 px; below, the Panels sheet).
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const T = { timeout: 4000 }
const ws = () => mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
const apps = async () => ((await api.getAddonState(ws(), 'publish')).apps as { id: string; status: string }[])

describe('publish page', () => {
  it('lists apps and shares; Start flips the stopped app to running', async () => {
    const { user } = renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    const row = (await screen.findByText('Energy dashboard', {}, T)).closest('tr')!
    expect(within(row).getByText('stopped')).toBeInTheDocument()
    expect(screen.getByText('Billing explorer')).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Start' }))
    await waitFor(async () => expect((await apps()).find((a) => a.id === 'app_energy')!.status).toBe('running'), T)
    await waitFor(() => expect(within(screen.getByText('Energy dashboard').closest('tr')!).getByText('running')).toBeInTheDocument(), T)
  })
  it('revoking a share removes it from the page', async () => {
    const { user } = renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    await user.click(await screen.findByRole('tab', { name: /Shares/ }, T))
    const item = (await screen.findByText('Tariff API notes', {}, T)).closest('tr')!
    await user.click(await moreAction(user, item, 'Revoke'))
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Confirm: Revoke (revoke)' }))
    await waitFor(() => expect(screen.queryByText('Tariff API notes')).not.toBeInTheDocument(), T)
  })
  it('a running app has no Start and a stopped one has no Stop', async () => {
    renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    const running = (await screen.findByText('Billing explorer', {}, T)).closest('tr')!
    expect(within(running).queryByRole('button', { name: 'Start' })).not.toBeInTheDocument()
    expect(within(running).getByRole('button', { name: 'Stop' })).toBeInTheDocument()
    const stopped = screen.getByText('Energy dashboard').closest('tr')!
    expect(within(stopped).getByRole('button', { name: 'Start' })).toBeInTheDocument()
    expect(within(stopped).queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument()
  })
  it('stopping an app asks first (people are using it), then the toast offers Undo', async () => {
    const success = vi.spyOn(toast, 'success')
    const { user } = renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    const row = (await screen.findByText('Billing explorer', {}, T)).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Stop' }))
    const ask = await screen.findByRole('alertdialog')
    expect(ask).toHaveTextContent(/Anyone using the app loses it/)
    expect(within(ask).getByTestId('consequence')).toHaveTextContent('The addon offers an undo right after.') // core's line: stop declares a plain undo (start)
    expect((await apps()).find((a) => a.id === 'app_billing')!.status).toBe('running') // nothing yet
    // While it runs only this action's button says "Stopping…" (the row's other buttons keep their names).
    let release: () => void = () => {}
    const real = api.runAddonAction.bind(api)
    vi.spyOn(api, 'runAddonAction').mockImplementationOnce((...a) => new Promise((res) => (release = () => void real(...a).then(res))))
    await user.click(within(ask).getByRole('button', { name: 'Confirm: Stop (stop)' }))
    expect(await within(row).findByRole('button', { name: 'Stopping…' })).toBeDisabled()
    expect(within(row).queryByRole('button', { name: 'Stop' })).toBeNull() // only the pressed button changed its text
    release()
    await waitFor(() => expect(success).toHaveBeenCalledWith('Billing explorer stopped.', expect.objectContaining({ action: expect.objectContaining({ label: 'Undo' }) })), T)
    const opts = success.mock.calls[0][1] as unknown as { action: { onClick: () => void } }
    opts.action.onClick()
    await waitFor(async () => expect((await apps()).find((a) => a.id === 'app_billing')!.status).toBe('running'), T)
  })
  it('sharing shows the link in core\'s dialog, not in a toast; it stays until "I saved it"', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const success = vi.spyOn(toast, 'success')
    const { user } = renderApp('/ticket/DEMO-0041', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    await openTicketPanel(user, 'Shares')
    await user.click(await within(panel).findByRole('button', { name: 'Share report…' }))
    const dialog = await screen.findByRole('dialog', { name: /Copy this link now/ }, T)
    expect((within(dialog).getByRole('textbox') as HTMLInputElement).value).toMatch(/^https:/)
    for (const c of success.mock.calls) expect(String(c[0])).not.toMatch(/https:/)
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog', { name: /Copy this link now/ })).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'I saved it' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /Copy this link now/ })).not.toBeInTheDocument())
  })
  // A show-once link is always to one ticket: the button lives in the ticket's Shares panel.
  const showOnce = async () => {
    vi.stubGlobal('innerWidth', 1440)
    const r = renderApp('/ticket/DEMO-0041', { viewer: 'p_sev' })
    const panel = await openTicketPanel(r.user, 'Shares')
    return { ...r, panel, press: async () => r.user.click(await within(panel).findByRole('button', { name: 'New show-once link' }, T)) }
  }
  it('a show-once row says "Shown once" instead of Copy link', async () => {
    const { user, panel, press } = await showOnce()
    await press()
    await user.click(await within(await screen.findByRole('dialog', { name: 'Choose: Share once (share_once) · Publish (publish)' }, T)).findByRole('button', { name: 'Create show-once link' }))
    await user.click(await screen.findByRole('button', { name: 'I saved it' }, T))
    const item = (await within(panel).findByText('DEMO-0041 ticket page one-time link', {}, T)).closest('li')!
    expect(within(item).getByRole('button', { name: 'Shown once' })).toBeInTheDocument()
    expect(within(item).queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument()
  })
  it('a show-once link asks first what to share, how long it works and how often it opens; Cancel makes nothing', async () => {
    const { user, panel, press } = await showOnce()
    await press()
    const ask = await screen.findByRole('dialog', { name: 'Choose: Share once (share_once) · Publish (publish)' }, T)
    expect(screen.queryByRole('dialog', { name: /Copy this link now/ })).toBeNull() // the secret dialog comes after
    await user.click(within(ask).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(within(panel).queryByText(/one-time link/)).toBeNull()
    await press()
    const ask2 = await screen.findByRole('dialog', { name: 'Choose: Share once (share_once) · Publish (publish)' }, T)
    await user.selectOptions(within(ask2).getByLabelText('What to share'), 'report')
    await user.selectOptions(within(ask2).getByLabelText('Works for'), '3')
    await user.selectOptions(within(ask2).getByLabelText('Opens'), '3')
    await user.click(within(ask2).getByRole('button', { name: 'Create show-once link' }))
    const secret = await screen.findByRole('dialog', { name: /Copy this link now/ }, T)
    expect(secret).toHaveTextContent(/before\/after report: opens 3 times, works for 3 days/i)
    await user.click(within(secret).getByRole('button', { name: 'I saved it' }))
    const item = (await within(panel).findByText('DEMO-0041 before/after report one-time link', {}, T)).closest('li')!
    expect(item).toHaveTextContent('3 days')
  })
  it('viewer sees the page with disabled actions', async () => {
    renderApp('/addon/publish/shares', { viewer: 'p_tom' })
    const row = (await screen.findByText('Energy dashboard', {}, T)).closest('tr')!
    expect(within(row).getByRole('button', { name: 'Start' })).toBeDisabled()
  })
})

describe('publish tabs', () => {
  it('opens on Apps, counts both tabs, and remembers Shares for this viewer', async () => {
    const first = renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    expect(await screen.findByRole('tab', { name: /Apps\s*3/ }, T)).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByText('Tariff API notes')).not.toBeInTheDocument() // the Shares tab is not rendered
    await first.user.click(screen.getByRole('tab', { name: /Shares/ }))
    expect(await screen.findByText('Tariff API notes')).toBeInTheDocument()
    expect(screen.queryByText('Billing explorer')).not.toBeInTheDocument()
  })
})

describe('publish failed builds', () => {
  it('a failed build is said once, above the tabs, on both tabs: one line, Redeploy, Show log', async () => {
    const { user } = renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    expect(await screen.findByText('Ops notebook failed to build', {}, T)).toBeInTheDocument()
    expect(screen.getByText(/Redeploy rebuilds it from its folder/)).toBeInTheDocument()
    expect(screen.getAllByText(/ModuleNotFoundError/)).toHaveLength(1) // the error line is in the app's row only
    expect(screen.getAllByText('Ops notebook failed to build')).toHaveLength(1)
    expect(screen.queryByText(/Traceback/)).not.toBeInTheDocument() // the log is behind Show log
    await user.click(screen.getByRole('button', { name: 'Show log' }))
    expect(await screen.findByText(/Traceback \(most recent call last\)/)).toBeInTheDocument() // the whole log, not three lines
    expect(screen.getByText(/Installing requirements\.txt/)).toBeInTheDocument()
    // The app row has a short note instead of a second copy of the alert.
    const row = screen.getByText('Ops notebook').closest('tr')!
    expect(within(row).getByText(/ModuleNotFoundError/)).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /Shares/ }))
    expect(await screen.findByText('Tariff API notes')).toBeInTheDocument()
    expect(screen.getByText('Ops notebook failed to build')).toBeInTheDocument() // still there on the Shares tab
  })
  it('Redeploy rebuilds and the alert goes away', async () => {
    const { user } = renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    await user.click(await screen.findByRole('button', { name: 'Redeploy' }, T))
    await waitFor(() => expect(screen.queryByText('Ops notebook failed to build')).not.toBeInTheDocument(), T)
  })
})

describe('publish ticket panel', () => {
  it('lists this ticket shares and revoking removes one', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0041', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    await openTicketPanel(user, 'Shares')
    const item = (await within(panel).findByText('Before/after report', {}, T)).closest('li')!
    expect(within(panel).queryByText('UTC migration summary')).not.toBeInTheDocument()
    expect(within(panel).getByRole('button', { name: 'Share report…' })).toBeInTheDocument()
    await user.click(await moreAction(user, item, 'Revoke'))
    await user.click(await screen.findByRole('button', { name: 'Confirm: Revoke (revoke)' }))
    await waitFor(() => expect(within(panel).queryByText('Before/after report')).not.toBeInTheDocument(), T)
  })
})

describe('publish decisions on Today', () => {
  it('Severin sees the failed-build decision, Tom does not', async () => {
    const a = renderApp('/', { viewer: 'p_sev' })
    expect(await screen.findByText(/Ops notebook failed to build/, {}, T)).toBeInTheDocument()
    a.unmount()
    renderApp('/', { viewer: 'p_tom' })
    await screen.findByText('viewer · read only', {}, T)
    expect(screen.queryByText(/Ops notebook failed to build/)).not.toBeInTheDocument()
  })
})

describe('Apps & shares and Drop point at each other (R-e)', () => {
  const T = { timeout: 5000 }
  it('says what the page is for; "Open Drop" only while Drop is active, and it opens Drop', async () => {
    const { unmount } = renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    expect(await screen.findByText(/Live apps, and read-only links to tickets and artifacts\. Files you send go through Drop\./, {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open Drop' })).toBeNull() // Drop is in the catalog, not installed
    unmount()
    const { user } = renderApp('/addon/publish/shares', { viewer: 'p_sev', setup: (s) => installAndGrant(s, s.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'drop') })
    await user.click(await screen.findByRole('link', { name: 'Open Drop' }, T))
    expect(await screen.findByText(/Files between devices, people and workspaces\./, {}, T)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Apps & shares' })).toBeInTheDocument()
  })
})
