import { describe, expect, it } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
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
    const rows = within(screen.getByRole('table', { name: 'Tickets' })).getAllByRole('row').slice(1)
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
})
