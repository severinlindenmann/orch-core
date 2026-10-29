import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { COLD } from '@/test/cold'

const row = (key: string, title: string) => ({ workspace_id: 'a'.repeat(32), key, title, shared_by: 'Anna Test', status: 'open', type: 'feature', deadline: null, description: null })

describe('Today: Shared with you card', () => {
  afterEach(() => vi.restoreAllMocks())
  it('shows up to 3 shared tickets with See all, and Today stays quiet about decisions', async () => {
    vi.spyOn(api, 'getHubShared').mockResolvedValue({ items: [row('D-1', 'One'), row('D-2', 'Two'), row('D-3', 'Three'), row('D-4', 'Four')] })
    renderApp('/')
    const card = await screen.findByTestId('shared-today', {}, COLD)
    expect(within(card).getByRole('heading')).toHaveTextContent('Shared with you · 4')
    expect(within(card).getAllByTestId('shared-today-row')).toHaveLength(3)
    expect(within(card).getByRole('link', { name: 'See all in Tickets' })).toHaveAttribute('href', expect.stringMatching(/\/tickets$/))
  })
  it('is absent when nothing is shared or the hub is away', async () => {
    const shared = vi.spyOn(api, 'getHubShared').mockResolvedValue({ items: [] })
    renderApp('/')
    await waitFor(() => expect(shared).toHaveBeenCalled())
    expect(screen.queryByTestId('shared-today')).toBeNull()
  })
  it('is absent when the hub read fails', async () => {
    const shared = vi.spyOn(api, 'getHubShared').mockRejectedValue(new Error('hub away'))
    renderApp('/')
    await waitFor(() => expect(shared).toHaveBeenCalled())
    expect(screen.queryByTestId('shared-today')).toBeNull()
  })
  it('Read carries the shared ticket in the address', async () => {
    vi.spyOn(api, 'getHubShared').mockResolvedValue({ items: [row('D-1', 'One')] })
    renderApp('/')
    const card = await screen.findByTestId('shared-today')
    expect(decodeURIComponent(within(card).getByRole('link', { name: 'Read' }).getAttribute('href')!)).toContain(`shared=${'a'.repeat(32)}:D-1`)
  })
})
