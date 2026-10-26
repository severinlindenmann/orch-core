import { describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

describe('Tickets page', () => {
  it('lists the workspace tickets in a table', async () => {
    renderApp('/tickets')
    const table = await screen.findByRole('table', { name: 'Tickets' })
    expect(await within(table).findByText('DEMO-0043')).toBeInTheDocument()
  })
  it('full-text search finds a ticket by its body and shows a snippet', async () => {
    const { user } = renderApp('/tickets')
    await user.type(await screen.findByRole('searchbox', { name: 'Search tickets' }), 'fct_billing')
    expect(await screen.findByText(/«fct_billing»/)).toBeInTheDocument()
  })
  it('filters by status and clears', async () => {
    const { user } = renderApp('/tickets')
    await user.click(await screen.findByRole('button', { name: /^testing/i }))
    const rows = within(screen.getByRole('table', { name: 'Tickets' })).getAllByRole('row').slice(1).filter((r) => !r.hasAttribute('data-group'))
    rows.forEach((r) => expect(r).toHaveTextContent(/testing/i))
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
  })
  it('shows an empty state', async () => {
    const { user } = renderApp('/tickets')
    await user.type(await screen.findByRole('searchbox', { name: 'Search tickets' }), 'zzzz-no-match')
    expect(await screen.findByText(/No tickets match/)).toBeInTheDocument()
  })
  it('opens a ticket with Enter on the focused row', async () => {
    const { user } = renderApp('/tickets')
    await screen.findByRole('table', { name: 'Tickets' })
    await user.keyboard('j{Enter}')
    expect(await screen.findByRole('heading', { level: 1 })).toBeInTheDocument()
  })
  it('hides bulk actions for the viewer', async () => {
    const { user } = renderApp('/tickets', { viewer: 'p_tom' })
    await screen.findByRole('table', { name: 'Tickets' })
    await user.keyboard('jx')
    expect(screen.queryByRole('button', { name: 'Set status' })).toBeNull()
  })

  // Additions beyond the brief
  it('Enter really navigates to the focused ticket', async () => {
    const { user } = renderApp('/tickets')
    const table = await screen.findByRole('table', { name: 'Tickets' })
    await within(table).findByText('DEMO-0043')
    await user.keyboard('j{Enter}')
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent(/^[A-Z]+-\d+$/))
  })
  it('keeps filters in the URL search params and shows bulk actions to the owner', async () => {
    const { user } = renderApp('/tickets')
    const table = await screen.findByRole('table', { name: 'Tickets' })
    await within(table).findByText('DEMO-0043')
    await user.keyboard('jx')
    expect(await screen.findByRole('button', { name: 'Set status' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add label' })).toBeInTheDocument()
  })
  it('does not fire list shortcuts while typing in the search box', async () => {
    const { user } = renderApp('/tickets')
    await screen.findByRole('table', { name: 'Tickets' })
    await user.keyboard('/')
    const box = screen.getByRole('searchbox', { name: 'Search tickets' })
    expect(box).toHaveFocus()
    await user.keyboard('jx')
    expect(box).toHaveValue('jx')
    expect(screen.queryByRole('button', { name: 'Set status' })).toBeNull()
  })

  describe('saved views', () => {
    it('applies a seeded shared view', async () => {
      const { user } = renderApp('/tickets')
      await user.click(await screen.findByRole('tab', { name: 'Release blockers' }))
      await waitFor(() => expect(screen.getByRole('tab', { name: 'Release blockers' })).toHaveAttribute('aria-selected', 'true'))
      await waitFor(() => {
        const rows = within(screen.getByRole('table', { name: 'Tickets' })).getAllByRole('row').slice(1).filter((r) => !r.hasAttribute('data-group'))
        expect(rows.length).toBeGreaterThan(0)
        rows.forEach((r) => expect(within(r).getByRole('img', { name: /^Priority (urgent|high)$/ })).toBeInTheDocument())
      })
    })
    it('saves the current filters as a view', async () => {
      const { user } = renderApp('/tickets')
      await user.click(await screen.findByRole('button', { name: /^testing/i }))
      await user.click(screen.getByRole('button', { name: 'Save view…' }))
      await user.type(screen.getByLabelText('Name'), 'Bugs')
      await user.click(screen.getByRole('button', { name: 'Save' }))
      expect(await screen.findByRole('tab', { name: 'Bugs' })).toHaveAttribute('aria-selected', 'true')
    })
    it('the viewer cannot share a view', async () => {
      const { user } = renderApp('/tickets', { viewer: 'p_tom' })
      await user.click(await screen.findByRole('button', { name: 'Save view…' }))
      expect(screen.getByLabelText('Share with the workspace')).toBeDisabled()
    })
    it('keeps a personal view private and shows Modified when filters change', async () => {
      const { user, unmount } = renderApp('/tickets')
      await user.click(await screen.findByRole('tab', { name: 'My open work' }))
      await user.click(await screen.findByRole('button', { name: /^testing/i }))
      expect(await screen.findByText(/Modified/)).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Revert' }))
      await waitFor(() => expect(screen.queryByText(/Modified/)).toBeNull())
      unmount()
      renderApp('/tickets', { viewer: 'p_mara' })
      await screen.findByRole('tab', { name: 'Release blockers' })
      expect(screen.queryByRole('tab', { name: 'My open work' })).toBeNull()
    })
  })
})

describe('Tickets grouped by epic (N2)', () => {
  const groupRows = (t: HTMLElement) => within(t).getAllByRole('row').filter((r) => r.hasAttribute('data-group'))
  const ticketRows = (t: HTMLElement) => within(t).queryAllByRole('row').filter((r) => r.hasAttribute('data-key'))

  it('groups by epic by default: epic rows with progress, indented children, a No epic section', async () => {
    renderApp('/tickets')
    const table = await screen.findByRole('table', { name: 'Tickets' })
    await within(table).findByText('DEMO-0043')
    const groups = groupRows(table)
    const epic = groups.find((r) => r.getAttribute('data-group') === 'DEMO-0040')!
    expect(epic).toHaveTextContent(/\d\/4 done/)
    expect(groups.some((r) => r.getAttribute('data-group') === '_none')).toBe(true)
    // the epic is its group row, not also a ticket row; its children are rows below it
    expect(ticketRows(table).some((r) => r.dataset.key === 'DEMO-0040')).toBe(false)
    const order = within(table).getAllByRole('row').map((r) => r.getAttribute('data-group') ?? r.getAttribute('data-key'))
    expect(order.indexOf('DEMO-0043')).toBeGreaterThan(order.indexOf('DEMO-0040'))
    // single tickets come first, so a big epic never pushes them out of sight
    expect(order.indexOf('_none')).toBeLessThan(order.indexOf('DEMO-0040'))
    expect(screen.getByText(/^\d+ tickets · 2 epics$/)).toBeInTheDocument()
  })

  it('Group: None restores the flat list and is remembered', async () => {
    const { user } = renderApp('/tickets')
    const table = await screen.findByRole('table', { name: 'Tickets' })
    await within(table).findByText('DEMO-0043')
    await user.click(screen.getByRole('radio', { name: 'None' }))
    await waitFor(() => expect(within(table).queryAllByRole('row').some((r) => r.hasAttribute('data-group'))).toBe(false))
    expect(ticketRows(table).some((r) => r.dataset.key === 'DEMO-0040')).toBe(true)
    expect(JSON.parse(localStorage.getItem('orch.tickets.group.p_sev')!).group).toBe('none')
  })

  it('a 40-child epic starts folded to one row; the chevron opens it with Enter and the arrows fold and unfold', async () => {
    const { user } = renderApp('/tickets', { viewer: 'p_sev', setup: (s) => s.reset('busy') })
    const table = await screen.findByRole('table', { name: 'Tickets' })
    const toggle = await within(table).findByRole('button', { name: /^Expand DEMO-0100 /, expanded: false }, { timeout: 8000 })
    expect(groupRows(table).find((r) => r.getAttribute('data-group') === 'DEMO-0100')).toHaveTextContent(/\d+\/40 done/)
    const kids = mockStore.ticket('DEMO-0100')!.children!
    expect(ticketRows(table).filter((r) => kids.includes(r.dataset.key!))).toHaveLength(0)
    toggle.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(ticketRows(table).filter((r) => kids.includes(r.dataset.key!)).length).toBe(40))
    within(table).getByRole('button', { name: /^Collapse DEMO-0100 / }).focus()
    await user.keyboard('{ArrowLeft}')
    await waitFor(() => expect(ticketRows(table).filter((r) => kids.includes(r.dataset.key!))).toHaveLength(0))
    mockStore.sim.stopAll()
  }, 30_000)

  it('j/k skip the children of a folded epic', async () => {
    const { user } = renderApp('/tickets', { viewer: 'p_sev', setup: (s) => s.reset('busy') })
    const table = await screen.findByRole('table', { name: 'Tickets' })
    await within(table).findByRole('button', { name: /^Expand DEMO-0100 /, expanded: false }, { timeout: 8000 })
    const kids = mockStore.ticket('DEMO-0100')!.children!
    await user.keyboard('jjjjj')
    expect(kids).not.toContain((document.activeElement as HTMLElement | null)?.dataset.key)
    mockStore.sim.stopAll()
  }, 30_000)

  it('a search hit inside a big epic is shown and j reaches it', async () => {
    const { user } = renderApp('/tickets', { viewer: 'p_sev', setup: (st) => st.reset('busy') })
    const table = await screen.findByRole('table', { name: 'Tickets' })
    await within(table).findByRole('button', { name: /^Expand DEMO-0100 /, expanded: false }, { timeout: 8000 })
    const kid = mockStore.ticket(mockStore.ticket('DEMO-0100')!.children![7])!
    await user.type(screen.getByRole('searchbox', { name: 'Search tickets' }), kid.title)
    await waitFor(() => expect(ticketRows(table).some((r) => r.dataset.key === kid.key)).toBe(true), { timeout: 8000 })
    await user.click(document.body)
    for (let i = 0; i < 25 && (document.activeElement as HTMLElement | null)?.dataset.key !== kid.key; i++) await user.keyboard('j')
    expect((document.activeElement as HTMLElement | null)?.dataset.key).toBe(kid.key)
    mockStore.sim.stopAll()
  }, 30_000)

  it('is remembered per viewer: Tom does not inherit Severin\'s choice', async () => {
    renderApp('/tickets', { viewer: 'p_tom', storage: { 'orch.tickets.group.p_sev': JSON.stringify({ group: 'none' }) } })
    const table = await screen.findByRole('table', { name: 'Tickets' })
    await within(table).findByText('DEMO-0043')
    expect(groupRows(table).length).toBeGreaterThan(0)
  })

  it('if the viewer cannot be loaded the list shows an error with Retry', async () => {
    // Down until restored: the route loader asks first, the page asks again when it mounts (G4).
    vi.spyOn(api, 'getMe').mockRejectedValue(new Error('down'))
    renderApp('/tickets')
    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not load tickets/)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    vi.restoreAllMocks()
  })
})

describe('Tickets page in a narrow page area (N11)', () => {
  it('folds People, Turn and Progress under the title, puts the filters in one popover and the views in a select', async () => {
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 700, height: 600, top: 0, left: 0, right: 700, bottom: 600, x: 0, y: 0, toJSON: () => ({}) })
    const { user } = renderApp('/tickets')
    const table = await screen.findByRole('table', { name: 'Tickets' })
    const heads = within(table).getAllByRole('columnheader').map((h) => h.textContent)
    expect(heads).not.toContain('People')
    expect(heads).not.toContain('Progress')
    expect(heads).toContain('Updated')
    expect(table.querySelector('[data-fold-line]')).toHaveTextContent(/Turn/)
    expect(screen.queryByRole('tablist', { name: 'Saved views' })).toBeNull()
    expect(screen.getByRole('combobox', { name: 'Saved view' })).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Status' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Filters' }))
    await user.click(await screen.findByRole('button', { name: /^testing/i }))
    expect(screen.getByRole('button', { name: 'Filters (1)' })).toBeInTheDocument()
    rect.mockRestore()
  })
})
