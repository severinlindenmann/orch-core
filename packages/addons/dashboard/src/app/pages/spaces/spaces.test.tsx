// Shared spaces (run-15c P03): the sidebar entry only with at least one space, the read-only page, and Today's
// "New shared space" row that clears after Open. The server's answers are faked.
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { setFeatures } from '@/api/features'
import { renderApp } from '@/test/renderApp'
import { COLD } from '@/test/cold'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  setFeatures(null)
})

const SID = 'b'.repeat(32)
const space = { id: SID, name: 'Team notes', kind: 'wiki', members: 2 }
const note = 'from the hub, not signed'
const real = (extra: string[] = ['hub']) => setFeatures({ features: ['today', 'tickets', 'board', 'ticket', ...extra] })
const SLOW = 40_000
const hasEntry = () => !!screen.queryByRole('link', { name: 'Shared spaces' })

describe('Shared spaces: sidebar entry', () => {
  it('is hidden without a space', async () => {
    real()
    const get = vi.spyOn(api, 'getSpaces').mockResolvedValue({ spaces: [], note })
    renderApp('/', { viewer: 'p_sev' })
    await waitFor(() => expect(get).toHaveBeenCalled())
    await screen.findByRole('link', { name: 'Board' }, COLD)
    expect(hasEntry()).toBe(false)
  }, SLOW)
  it('is shown with one space', async () => {
    real()
    vi.spyOn(api, 'getSpaces').mockResolvedValue({ spaces: [space], note })
    renderApp('/', { viewer: 'p_sev' })
    expect(await screen.findByRole('link', { name: 'Shared spaces' }, COLD)).toBeInTheDocument()
  }, SLOW)
  it('is hidden when the hub is not served, without even asking', async () => {
    real([])
    const get = vi.spyOn(api, 'getSpaces').mockResolvedValue({ spaces: [space], note })
    renderApp('/', { viewer: 'p_sev' })
    await screen.findByRole('link', { name: 'Board' }, COLD)
    expect(hasEntry()).toBe(false)
    expect(get).not.toHaveBeenCalled()
  }, SLOW)
  it('is hidden when the hub read fails', async () => {
    real()
    const get = vi.spyOn(api, 'getSpaces').mockRejectedValue(new Error('hub away'))
    renderApp('/', { viewer: 'p_sev' })
    await waitFor(() => expect(get).toHaveBeenCalled())
    await screen.findByRole('link', { name: 'Board' }, COLD)
    expect(hasEntry()).toBe(false)
  }, SLOW)
})

describe('Shared spaces: the page', () => {
  it('lists each space with its items as plain text and sends nothing', async () => {
    real()
    vi.spyOn(api, 'getSpaces').mockResolvedValue({ spaces: [space], note })
    const items = vi.spyOn(api, 'getSpaceItems').mockResolvedValue({
      id: SID, note, refused: 1,
      items: [{ name: 'plan.md', size: 12 }, { name: '<img src=x onerror=alert(1)>', size: 2048 }],
    })
    renderApp('/spaces', { viewer: 'p_sev' })
    const card = await screen.findByTestId('space-card', {}, COLD)
    expect(within(card).getByRole('heading', { name: 'Team notes' })).toBeInTheDocument()
    expect(await within(card).findByText('plan.md')).toBeInTheDocument()
    expect(card).toHaveTextContent('Wiki · 2 workspaces · 2 items')
    expect(card.querySelector('img')).toBeNull() // an item name is text, never markup
    expect(within(card).getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(card).toHaveTextContent('1 item was left out')
    expect(within(card).getByRole('button', { name: 'Add or edit in the hub' })).toBeInTheDocument()
    expect(items).toHaveBeenCalledWith(expect.any(String), SID)
  }, SLOW)
  it('says so when there is no space', async () => {
    real()
    vi.spyOn(api, 'getSpaces').mockResolvedValue({ spaces: [], note })
    renderApp('/spaces', { viewer: 'p_sev' })
    expect(await screen.findByTestId('spaces-none', {}, COLD)).toBeInTheDocument()
  }, SLOW)
})

describe('Shared spaces: Today row', () => {
  it('appears for a space not opened yet and clears after Open', async () => {
    real()
    vi.spyOn(api, 'getSpaces').mockResolvedValue({ spaces: [space], note })
    vi.spyOn(api, 'getSpaceItems').mockResolvedValue({ id: SID, note, refused: 0, items: [] })
    const { user, router } = renderApp('/', { viewer: 'p_sev' })
    const row = await screen.findByTestId('spaces-today-row', {}, COLD)
    expect(row).toHaveTextContent('New shared space: Team notes')
    await user.click(within(row).getByRole('link', { name: 'Open' }))
    await waitFor(() => expect(router.history.location.pathname).toMatch(/\/spaces$/))
    await screen.findByTestId('space-card')
    await router.navigate({ to: '/' })
    await screen.findByRole('link', { name: 'Board' })
    await waitFor(() => expect(screen.queryByTestId('spaces-today')).toBeNull())
  }, SLOW)
  it('is absent without a space', async () => {
    real()
    const get = vi.spyOn(api, 'getSpaces').mockResolvedValue({ spaces: [], note })
    renderApp('/', { viewer: 'p_sev' })
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.queryByTestId('spaces-today')).toBeNull()
  }, SLOW)
})
