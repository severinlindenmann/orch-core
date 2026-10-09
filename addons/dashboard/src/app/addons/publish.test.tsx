import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { api, mockStore } from '@/api/client'
import { moreAction } from '@/test/rowActions'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

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
    const item = (await screen.findByText('Tariff API notes', {}, T)).closest('li')!
    await user.click(await moreAction(user, item, 'Revoke'))
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Revoke link' }))
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
  it('stopping an app acts at once and the toast offers Undo', async () => {
    const success = vi.spyOn(toast, 'success')
    const { user } = renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    const row = (await screen.findByText('Billing explorer', {}, T)).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Stop' }))
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
  it('a show-once row says "Shown once" instead of Copy link', async () => {
    const { user } = renderApp('/addon/publish/shares', { viewer: 'p_sev' })
    await user.click(await screen.findByRole('button', { name: 'New show-once link' }, T))
    await user.click(await screen.findByRole('button', { name: 'I saved it' }, T))
    const item = (await screen.findByText('One-time link', {}, T)).closest('li')!
    expect(within(item).getByRole('button', { name: 'Shown once' })).toBeInTheDocument()
    expect(within(item).queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument()
  })
  it('viewer sees the page with disabled actions', async () => {
    renderApp('/addon/publish/shares', { viewer: 'p_tom' })
    const row = (await screen.findByText('Energy dashboard', {}, T)).closest('tr')!
    expect(within(row).getByRole('button', { name: 'Start' })).toBeDisabled()
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
    await user.click(await screen.findByRole('button', { name: 'Revoke link' }))
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
