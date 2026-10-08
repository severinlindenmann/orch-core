import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', async (orig) => {
  const actual = await orig<typeof import('sonner')>()
  return { ...actual, toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }) }
})

import { toast } from 'sonner'
import { api } from '@/api/client'
import { createAppRouter } from '../../router'

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={createAppRouter('/')} />
    </QueryClientProvider>,
  )
}

describe('Today page', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await api.resetDemo()
    await api.setViewer('p_sev')
  })

  it('answers Q2 from the Today card, removes it and toasts', async () => {
    const user = userEvent.setup()
    renderApp()
    const card = await screen.findByTestId('card-question:DEMO-0043:Q2', {}, { timeout: 4000 })
    expect(within(card).getByText('blocking')).toBeInTheDocument()
    expect(await within(card).findByText(/recommended/, {}, { timeout: 4000 })).toBeInTheDocument()
    const buttons = await within(card).findAllByRole('button', {}, { timeout: 4000 })
    await user.click(buttons[0])
    await waitFor(() => expect(toast.success).toHaveBeenCalled(), { timeout: 4000 })
    await waitFor(() => expect(screen.queryByTestId('card-question:DEMO-0043:Q2')).not.toBeInTheDocument(), { timeout: 4000 })
    expect(await screen.findByText(/undo isn't possible, it's signed/)).toBeInTheDocument()
  })

  it('shows Tom read-only cards with disabled actions', async () => {
    await api.setViewer('p_tom')
    renderApp()
    expect(await screen.findByText('viewer · read only', {}, { timeout: 4000 })).toBeInTheDocument()
    const card = await screen.findByTestId('card-question:DEMO-0043:Q2', {}, { timeout: 4000 })
    const buttons = await within(card).findAllByRole('button', {}, { timeout: 4000 })
    expect(buttons.length).toBeGreaterThan(0)
    buttons.forEach((b) => expect(b).toBeDisabled())
  })
})
