import { act, screen, waitFor, within } from '@testing-library/react'
import { openDockOn } from '@/app/terminal/dock/request'
import { afterEach, describe, expect, it, vi } from 'vitest'
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
  it('is one list for all repositories, with the repository as a column and filter chips above', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    expect(await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('tab')).not.toBeInTheDocument()
    expect(screen.getByText('wt/DEMO-0046-billing-api')).toBeInTheDocument()
    expect(screen.getByText('wt/DEMO-0037-ingest')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'All repositories' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByRole('button', { name: 'ingest' }))
    await waitFor(() => expect(screen.queryByText('wt/DEMO-0043-energy-dbt')).not.toBeInTheDocument(), T)
    expect(screen.getByText('wt/DEMO-0037-ingest')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'ingest' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByRole('button', { name: 'All repositories' }))
    await user.click(screen.getByRole('button', { name: 'With changes' }))
    await waitFor(() => expect(screen.queryByText('wt/DEMO-0041-energy-dbt')).not.toBeInTheDocument(), T)
    expect(screen.getByText('wt/DEMO-0043-energy-dbt')).toBeInTheDocument() // the one with 3 changed files
  })
  it('nothing matching the filter says so', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    await user.click(await screen.findByRole('button', { name: 'ingest' }, T))
    await user.click(screen.getByRole('button', { name: 'With changes' }))
    expect(await screen.findByText('No worktrees match. Clear the filter, or add one for a ticket.', {}, T)).toBeInTheDocument()
  })
  it('Remove is disabled on a worktree with changes, and says why in the menu', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('tr')!
    const item = await moreAction(user, row, /^Remove/)
    expect(item).toHaveAttribute('aria-disabled', 'true')
    expect(item).toHaveTextContent('Commit or stash the changes first')
    expect((await wts()).some((w) => w.path === 'wt/DEMO-0043-energy-dbt')).toBe(true)
  })
  it('Add worktree stands apart from the filters: its own primary button above the Show chips', async () => {
    renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const add = await screen.findByRole('button', { name: 'Add worktree' }, T)
    const filter = screen.getByRole('button', { name: 'With changes' })
    expect(add.compareDocumentPosition(filter) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(filter).toHaveAttribute('aria-pressed')
    expect(add).not.toHaveAttribute('aria-pressed')
    expect(add.parentElement).not.toBe(filter.parentElement)
  })
  it('Remove on a clean worktree removes it from the page', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0041-energy-dbt', {}, T)).closest('tr')!
    await user.click(await moreAction(user, row, 'Remove'))
    // Core's destructive confirm (its own button words; the addon's "Remove worktree" is in the addon region).
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Confirm: Remove (remove)' }))
    await waitFor(() => expect(screen.queryByText('wt/DEMO-0041-energy-dbt')).not.toBeInTheDocument(), T)
  })
  it('the add form creates a worktree', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    expect(screen.queryByLabelText(/^Ticket\b/)).not.toBeInTheDocument() // creation sits behind the button
    await user.click(await screen.findByRole('button', { name: 'Add worktree' }, T))
    const sel = await screen.findByLabelText(/^Ticket\b/, {}, T)
    await waitFor(() => expect(within(sel).getByRole('option', { name: /^DEMO-0044 · \S/ })).toBeInTheDocument(), T)
    await user.selectOptions(sel, 'DEMO-0044')
    await user.selectOptions(screen.getByLabelText(/^Repository/), 'acme-energy/billing-api')
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add worktree' }))
    await screen.findByText('wt/DEMO-0044-billing-api', {}, T)
    await waitFor(() => expect(screen.queryByLabelText(/^Ticket\b/)).not.toBeInTheDocument(), T) // the panel closes once it went through
  })
  it('an empty Add worktree focuses Ticket and says Fill in Ticket', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    await user.click(await screen.findByRole('button', { name: 'Add worktree' }, T))
    await screen.findByLabelText(/^Ticket\b/, {}, T)
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add worktree' }))
    expect(await screen.findByText('Fill in Ticket')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('combobox', { name: /^Ticket\b/ })).toHaveFocus())
  })
  it('the add panel preselects the repository that is filtered', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    await user.click(await screen.findByRole('button', { name: 'billing-api' }, T))
    await waitFor(() => expect(screen.getByRole('button', { name: 'billing-api' })).toHaveAttribute('aria-pressed', 'true'), T)
    await user.click(screen.getByRole('button', { name: 'Add worktree' }))
    expect(await screen.findByLabelText(/^Repository/, {}, T)).toHaveValue('acme-energy/billing-api')
  })
  it('Open terminal here is offered while terminals is active', async () => {
    renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('tr')!
    expect(within(row).getByRole('button', { name: 'Open terminal here' })).toBeInTheDocument()
  })
  it('Open terminal here opens the dock on the new session, with the keyboard in the dock', async () => {
    vi.stubGlobal('innerWidth', 1440)
    vi.stubGlobal('innerHeight', 900)
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0041-energy-dbt', {}, T)).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Open terminal here' }))
    const dock = await screen.findByRole('region', { name: 'Terminal dock' }, T)
    expect(await within(dock).findByRole('group', { name: /^Session: \d+ DEMO-0041 Shell · Your shell/ }, { timeout: 8000 })).toBeInTheDocument()
    // The keyboard goes to the dock (the session strip, then the terminal once xterm is up).
    await waitFor(() => expect(dock.contains(document.activeElement)).toBe(true), { timeout: 8000 })
  }, 25_000)
  it('a dock request for a session that is not in the viewer\'s view is dropped: no focus grab, nothing selected', async () => {
    vi.stubGlobal('innerWidth', 1440)
    vi.stubGlobal('innerHeight', 900)
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const add = await screen.findByRole('button', { name: 'Add worktree' }, T)
    await user.click(screen.getByRole('button', { name: /^Open terminal dock/ }))
    const dock = await screen.findByRole('region', { name: 'Terminal dock' }, T)
    // Opening the dock puts the keyboard on its strip; then the person goes back to the page.
    await waitFor(() => expect(document.activeElement).toHaveAttribute('data-session-strip'), T)
    const before = dock.querySelector('[data-session-strip]')?.getAttribute('aria-label')
    add.focus()
    act(() => openDockOn('no-such-session'))
    await new Promise((r) => setTimeout(r, 600))
    expect(document.activeElement).toBe(add)
    expect(dock.querySelector('[data-session-strip]')?.getAttribute('aria-label')).toBe(before)
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
    expect(await moreAction(user, row, /^Remove/)).toHaveAttribute('aria-disabled', 'true')
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
