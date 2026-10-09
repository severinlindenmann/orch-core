import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { api, mockStore } from '@/api/client'
import { moreAction } from '@/test/rowActions'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'
import { installAndGrant } from '@/test/installAddon'

// Ticket-rail tests render at 1440 px (the rail is a column from 1280 px; below, the Panels sheet).
afterEach(() => vi.unstubAllGlobals())

const T = { timeout: 4000 }
const ws = () => mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
const setup = (store: typeof mockStore) => installAndGrant(store, store.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'worktrees')
const wts = async () => (await api.getAddonState(ws(), 'worktrees')).worktrees as { ticket: string; path: string; dirty: number }[]

afterEach(() => vi.restoreAllMocks())

describe('worktrees page', () => {
  it('lists worktrees per repo with path, branch, files and ahead/behind', async () => {
    renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('tr')!
    expect(within(row).getByText(/feat\/load-tariff-tables-as-dbt-seeds/)).toBeInTheDocument()
    expect(within(row).getByText(/3 changed files/)).toBeInTheDocument()
    expect(within(row).getByText(/ahead 2/)).toBeInTheDocument()
  })
  it('Remove on the dirty worktree is refused in place: an alert in the row names the ticket, no toast', async () => {
    const error = vi.spyOn(toast, 'error')
    const success = vi.spyOn(toast, 'success')
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('tr')!
    await user.click(await moreAction(user, row, 'Remove'))
    await user.click(await screen.findByRole('button', { name: 'Remove worktree' }))
    const alert = await screen.findByRole('alert', {}, T)
    expect(alert).toHaveTextContent(/DEMO-0043 has 3 changed files/)
    expect(alert).toHaveTextContent(/Commit or stash them first/)
    expect(alert.closest('tr')!.previousElementSibling).toBe(row)
    expect(error).not.toHaveBeenCalled()
    expect(success).not.toHaveBeenCalled()
    expect((await wts()).some((w) => w.path === 'wt/DEMO-0043-energy-dbt')).toBe(true)
  })
  it('Remove on a clean worktree removes it from the page', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0041-energy-dbt', {}, T)).closest('tr')!
    await user.click(await moreAction(user, row, 'Remove'))
    // A destructive confirm whose button names the consequence.
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Remove worktree' }))
    await waitFor(() => expect(screen.queryByText('wt/DEMO-0041-energy-dbt')).not.toBeInTheDocument(), T)
  })
  it('the add form creates a worktree', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const sel = await screen.findByLabelText(/^Ticket\b/, {}, T)
    await waitFor(() => expect(within(sel).getByRole('option', { name: /^DEMO-0044 · \S/ })).toBeInTheDocument(), T)
    await user.selectOptions(sel, 'DEMO-0044')
    await user.selectOptions(screen.getByLabelText(/^Repository/), 'acme-energy/billing-api')
    await user.click(screen.getByRole('button', { name: 'Add worktree' }))
    await screen.findByText('wt/DEMO-0044-billing-api', {}, T)
  })
  it('an empty Add worktree focuses Ticket and says Fill in Ticket', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    await screen.findByLabelText(/^Ticket\b/, {}, T)
    await user.click(screen.getByRole('button', { name: 'Add worktree' }))
    expect(await screen.findByText('Fill in Ticket')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('combobox', { name: /^Ticket\b/ })).toHaveFocus())
  })
  it('Open terminal here is offered while terminals is active', async () => {
    renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('tr')!
    expect(within(row).getByRole('button', { name: 'Open terminal here' })).toBeInTheDocument()
  })
  it('no Open terminal here when terminals is off', async () => {
    renderApp('/addon/worktrees/worktrees', {
      viewer: 'p_sev',
      setup: (st) => {
        setup(st)
        st.addonOp(ws(), 'terminals', { op: 'disable' }, { kind: 'person', id: 'p_sev' })
      },
    })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('tr')!
    expect(within(row).queryByRole('button', { name: 'Open terminal here' })).not.toBeInTheDocument()
  })
  it('viewer sees disabled actions', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_tom', setup })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('tr')!
    expect(await moreAction(user, row, 'Remove')).toHaveAttribute('aria-disabled', 'true')
  })
})

describe('worktrees ticket panel', () => {
  it('shows this ticket worktrees and adds one for this ticket', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev', setup })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    await openTicketPanel(user, 'Worktrees')
    expect(await within(panel).findByText('wt/DEMO-0043-energy-dbt', {}, T)).toBeInTheDocument()
    await user.selectOptions(within(panel).getByLabelText(/^Repository/), 'acme-energy/ingest')
    await user.click(within(panel).getByRole('button', { name: 'Add worktree for this ticket' }))
    await within(panel).findByText('wt/DEMO-0043-ingest', {}, T)
  })
})
