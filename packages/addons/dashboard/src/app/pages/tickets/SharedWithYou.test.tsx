import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within, act } from '@testing-library/react'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const t = (key: string, title: string) => ({ workspace_id: 'a'.repeat(32), key, title, shared_by: 'Anna', status: 'open', type: 'feature', deadline: null, description: null })

describe('SharedWithYou stays current', () => {
  beforeEach(() => sessionStorage.clear())
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('polls: a rename shows within 15 s, a vanished ticket becomes a "stopped sharing" line that Dismiss removes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const get = vi.spyOn(api, 'getHubShared').mockResolvedValue({ items: [t('D-7', 'Invoice export for Ben')] })
    const { user } = renderApp('/tickets')
    const box = await screen.findByTestId('shared-with-you')
    expect(within(box).getByText('Invoice export for Ben')).toBeInTheDocument()
    get.mockResolvedValue({ items: [t('D-7', 'Invoice export for Ben v2')] })
    await act(async () => { await vi.advanceTimersByTimeAsync(15_500) })
    expect(await screen.findByText('Invoice export for Ben v2')).toBeInTheDocument()
    expect(screen.queryByTestId('shared-ended')).toBeNull() // a rename needs no note
    get.mockResolvedValue({ items: [] })
    await act(async () => { await vi.advanceTimersByTimeAsync(15_500) })
    const note = await screen.findByTestId('shared-ended')
    expect(note).toHaveTextContent('Anna stopped sharing Invoice export for Ben v2')
    expect(screen.queryByTestId('shared-row')).toBeNull()
    vi.useRealTimers()
    await user.click(within(note).getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByTestId('shared-with-you')).toBeNull())
  })

  it('a failed read gives no "stopped sharing" note and keeps the last list', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const get = vi.spyOn(api, 'getHubShared').mockResolvedValue({ items: [t('D-7', 'Invoice export')] })
    renderApp('/tickets')
    await screen.findByTestId('shared-row')
    get.mockRejectedValue(new Error('hub away'))
    await act(async () => { await vi.advanceTimersByTimeAsync(15_500) })
    expect(screen.queryByTestId('shared-ended')).toBeNull()
    expect(screen.getByText('Invoice export')).toBeInTheDocument()
  })
})
