import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createAppRouter } from '@/app/router'
import { describe, expect, it } from 'vitest'
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
})
