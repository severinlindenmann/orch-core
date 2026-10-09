import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', async (orig) => {
  const actual = await orig<typeof import('sonner')>()
  return { ...actual, toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }) }
})

import { toast } from 'sonner'
import { ApiError } from '@/api/types'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

describe('Today page', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('answers Q2 from the Today card, removes it and toasts', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
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
    const { user } = renderApp('/', { viewer: 'p_tom' })
    expect(await screen.findByText('viewer · read only', {}, { timeout: 4000 })).toBeInTheDocument()
    const card = await screen.findByTestId('card-question:DEMO-0043:Q2', {}, { timeout: 4000 })
    const buttons = await within(card).findAllByRole('button', {}, { timeout: 4000 })
    expect(buttons.length).toBeGreaterThan(0)
    buttons.forEach((b) => expect(b).toBeDisabled())
    buttons.forEach((b) => expect(b).toHaveAccessibleDescription('Viewers cannot change tickets.'))
    await user.hover(buttons[0].parentElement!)
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Viewers cannot change tickets.')
  })

  it('addon decisions post the current workspace id', async () => {
    const spy = vi.spyOn(api, 'runAddonAction')
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const card = (await screen.findAllByTestId(/^card-addon:/, {}, { timeout: 4000 }))[0]
    await user.click((await within(card).findAllByRole('button'))[0])
    await user.click(await screen.findByRole('button', { name: 'Sign with Touch ID' }))
    await waitFor(() => expect(spy).toHaveBeenCalled())
    expect(spy.mock.calls[0][0]).toBe(mockStore.workspaces[0].id)
    spy.mockRestore()
  })

  it('an addon decision is signed in core\'s prompt before it is posted, and core records it', async () => {
    const spy = vi.spyOn(api, 'runAddonAction')
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const card = await screen.findByTestId('card-addon:dec_publish_failed_build', {}, { timeout: 4000 })
    const option = (await within(card).findAllByRole('button'))[0]
    const label = option.textContent!
    await user.click(option)
    const prompt = await screen.findByRole('dialog', { name: /^Decide: / })
    expect(prompt).toHaveTextContent(`Your answer: ${label}`)
    expect(spy).not.toHaveBeenCalled()
    await user.click(within(prompt).getByRole('button', { name: 'Sign with Touch ID' }))
    await waitFor(() => expect(spy).toHaveBeenCalled())
    const ws = mockStore.workspaces[0].id
    await waitFor(() => expect(mockStore.wsEventsOf(ws).some((e) => e.type === 'addon.decided' && e.id === 'dec_publish_failed_build' && e.presence === 'touchid')).toBe(true))
    spy.mockRestore()
  })

  it('cancelling the signing prompt posts nothing', async () => {
    const spy = vi.spyOn(api, 'runAddonAction')
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const card = await screen.findByTestId('card-addon:dec_publish_failed_build', {}, { timeout: 4000 })
    await user.click((await within(card).findAllByRole('button'))[0])
    await user.click(within(await screen.findByRole('dialog', { name: /^Decide: / })).getByRole('button', { name: 'Cancel' }))
    expect(spy).not.toHaveBeenCalled()
    expect(screen.getByTestId('card-addon:dec_publish_failed_build')).toBeInTheDocument()
    spy.mockRestore()
  })

  it('a failed card action toasts the API message with its hint', async () => {
    const spy = vi.spyOn(api, 'postAction').mockRejectedValueOnce(new ApiError(403, { code: 'forbidden', message: 'Not yours to answer', hint: 'Ask the owner', retryable: false }))
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const card = await screen.findByTestId('card-question:DEMO-0043:Q2', {}, { timeout: 4000 })
    const buttons = await within(card).findAllByRole('button', {}, { timeout: 4000 })
    await user.click(buttons[0])
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Not yours to answer', { description: 'Ask the owner' }), { timeout: 4000 })
    spy.mockRestore()
  })
})
