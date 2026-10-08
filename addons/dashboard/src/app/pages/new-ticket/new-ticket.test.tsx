import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createAppRouter } from '@/app/router'
import { describe, expect, it, vi } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

describe('new ticket page', () => {
  it('shows the sections a bug needs and hides absent ones', async () => {
    const { user } = renderApp('/tickets/new')
    await user.click(await screen.findByRole('radio', { name: 'chore' }))
    expect(screen.queryByLabelText('Out of scope')).toBeNull()
    await user.click(screen.getByRole('radio', { name: 'bug' }))
    expect(screen.getByLabelText('Out of scope')).toBeInTheDocument()
  })
  it('keeps shared section text when switching type', async () => {
    const { user } = renderApp('/tickets/new')
    await user.type(await screen.findByLabelText(/^Requirements/), 'Must not loop')
    await user.click(screen.getByRole('radio', { name: 'bug' }))
    expect(screen.getByLabelText(/^Requirements/)).toHaveValue('Must not loop')
  })
  it('creates the ticket and opens it', async () => {
    const { user } = renderApp('/tickets/new')
    await user.type(await screen.findByLabelText('Title'), 'Export fails on empty month')
    await user.type(screen.getByLabelText(/^Requirements/), 'Empty months export a header row')
    await user.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByRole('heading', { level: 1, name: /Export fails on empty month/ })).toBeInTheDocument()
    // The ticket page renders the status chip as "Backlog" (STATUS_LABEL), not the raw id.
    expect(await screen.findByText('Backlog')).toBeInTheDocument()
  })
  it('blocks Create and says why when requirements are empty', async () => {
    const { user } = renderApp('/tickets/new')
    await user.type(await screen.findByLabelText('Title'), 'No reqs')
    await user.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText(/Requirements are needed/)).toBeInTheDocument()
  })
  it('the viewer sees why they cannot create', async () => {
    renderApp('/tickets/new', { viewer: 'p_tom' })
    expect(await screen.findByText(/Viewers cannot create tickets/)).toBeInTheDocument()
  })
  it('asks before discarding a draft when leaving by a link', async () => {
    const { user } = renderApp('/tickets/new')
    await user.type(await screen.findByLabelText('Title'), 'Half done')
    await user.click(screen.getByRole('link', { name: /Board/ }))
    expect(await screen.findByText('Discard this draft?')).toBeInTheDocument()
  })
  it('restores an autosaved draft with a discard bar', async () => {
    const first = renderApp('/tickets/new')
    await first.user.type(await screen.findByLabelText('Title'), 'Kept draft')
    await waitFor(() => expect(Object.keys(localStorage).filter((k) => k.includes('draft'))).toHaveLength(1))
    first.unmount()
    const user = userEvent.setup()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={createAppRouter('/tickets/new')} />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/Draft restored/)).toBeInTheDocument()
    expect(screen.getByLabelText('Title')).toHaveValue('Kept draft')
    await user.click(screen.getByRole('button', { name: 'Discard' }))
    expect(screen.getByLabelText('Title')).toHaveValue('')
  })
  it('a double Cmd+Enter creates only one ticket', async () => {
    const spy = vi.spyOn(api, 'createTicket')
    const { user } = renderApp('/tickets/new')
    const title = await screen.findByLabelText('Title')
    await user.type(title, 'Only once')
    await user.type(screen.getByLabelText(/^Requirements/), 'One ticket please')
    fireEvent.keyDown(title, { key: 'Enter', metaKey: true })
    fireEvent.keyDown(title, { key: 'Enter', metaKey: true })
    expect(await screen.findByRole('heading', { level: 1, name: /Only once/ })).toBeInTheDocument()
    expect(spy).toHaveBeenCalledTimes(1)
    spy.mockRestore()
  })
  it('"Restricted to…" starts with the creator and the owners', async () => {
    const { user } = renderApp('/tickets/new', { viewer: 'p_mara' })
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Visibility' }), 'restricted')
    const group = screen.getByRole('group', { name: 'Can see this ticket' })
    expect(within(group).getByRole('button', { name: 'Mara' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(group).getByRole('button', { name: 'Severin' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(group).getByRole('button', { name: 'Tom' })).toHaveAttribute('aria-pressed', 'false')
  })
  it.each([
    ['an unknown type', { type: 'saga', title: 'Old draft' }],
    ['an inherited property as type', { type: 'toString', title: 'Old draft' }],
    ['null sections', { type: 'bug', title: 'Old draft', sections: null }],
    ['a non-string title', { title: 42 }],
    ['not an object', 'just text'],
  ])('discards a stored draft with %s instead of crashing', async (_what, stored) => {
    renderApp('/').unmount()
    const key = Object.keys(localStorage).find((k) => k.includes('draft')) ?? `orch.dashboard.new-ticket.draft.p_sev.${mockStore.workspaces[0].id}`
    localStorage.setItem(key, JSON.stringify(stored))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={createAppRouter('/tickets/new')} />
      </QueryClientProvider>,
    )
    expect(await screen.findByLabelText('Title')).toHaveValue('')
    expect(screen.queryByText(/Draft restored/)).toBeNull()
    expect(localStorage.getItem(key)).toBeNull()
  })
})
