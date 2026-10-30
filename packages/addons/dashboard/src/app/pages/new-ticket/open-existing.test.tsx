// Same text as a ticket that already exists: the box names it, and "Open it" leaves for that ticket without the
// "Discard unsaved changes?" question (the same text is saved there).
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore } from '@/api/client'
import { ApiError } from '@/api/types'
import * as gesture from '@/components/sign/trustedPress'
import { clearMark } from '@/app/pages/ticket/progress'
import { renderApp } from '@/test/renderApp'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))

const T = { timeout: 8000 }

beforeEach(() => {
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
  clearMark(mockStore.workspaces[0].id, 'DEMO-0044')
  const real = api.getWorkspaces.bind(api)
  vi.spyOn(api, 'getWorkspaces').mockImplementation(async () => (await real()).map((w) => ({ ...w, click: { custody: 'convenience' as const, enabled: true, available: true } })))
  vi.spyOn(api, 'prepareClick').mockRejectedValue(new ApiError(409, { code: 'intent.done', message: 'x', retryable: false, ticket: { key: 'DEMO-0044', title: 'Export CSV' } }))
  vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: 'hr_x', state: 'cancelled' })
})
afterEach(async () => {
  cleanup()
  await new Promise((r) => setTimeout(r, 20))
  vi.restoreAllMocks()
})

describe('same-text ticket', () => {
  it('Open it goes to the existing ticket with no discard question', async () => {
    const r = renderApp('/board')
    await waitFor(() => expect(screen.getByTestId('topbar-title')).not.toHaveTextContent(/^$/), T)
    await r.user.click(await screen.findByRole('button', { name: /New ticket/ }, T))
    const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
    await r.user.type(await within(sheet).findByLabelText('Title', {}, T), 'Export CSV')
    await r.user.type(within(sheet).getByLabelText(/^What should be true/), 'Empty months export a header row')
    await r.user.click(await within(sheet).findByRole('button', { name: /^Create/ }, T))
    const open = await screen.findByTestId('open-existing-ticket', {}, T)
    expect(await screen.findByText('You already have this ticket', {}, T)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create another' })).toBeInTheDocument()
    fireEvent.click(open)
    await waitFor(() => expect(r.router.state.location.pathname).toMatch(/\/ticket\/DEMO-0044$/), T)
    expect(screen.queryByText('Discard unsaved changes?')).toBeNull()
  })
})
