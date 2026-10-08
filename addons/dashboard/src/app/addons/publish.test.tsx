import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

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
    await user.click(within(item).getByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(screen.queryByText('Tariff API notes')).not.toBeInTheDocument(), T)
  })
  it('viewer sees the page with disabled actions', async () => {
    renderApp('/addon/publish/shares', { viewer: 'p_tom' })
    const row = (await screen.findByText('Energy dashboard', {}, T)).closest('tr')!
    expect(within(row).getByRole('button', { name: 'Start' })).toBeDisabled()
  })
})

describe('publish ticket panel', () => {
  it('lists this ticket shares and revoking removes one', async () => {
    const { user } = renderApp('/ticket/DEMO-0041', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    const item = (await within(panel).findByText('Before/after report', {}, T)).closest('li')!
    expect(within(panel).queryByText('UTC migration summary')).not.toBeInTheDocument()
    expect(within(panel).getByRole('button', { name: 'Share report…' })).toBeInTheDocument()
    await user.click(within(item).getByRole('button', { name: 'Revoke' }))
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
