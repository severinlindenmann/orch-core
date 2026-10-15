// G3 re-review nits, fixed in the G4 fix round: identical content on one ticket (N1), focus when the shown artifact
// leaves the results while focus is in the wide pane (N2), and the view when the viewer cannot be read (N3).
import { act, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { api } from '@/api/client'
import type { ArtifactItem } from '@/api/types'
import { renderApp } from '@/test/renderApp'

vi.mock('@/lib/useElementWidth', () => ({ useElementWidth: () => [() => {}, 1200] }))

const T = { timeout: 6000 }
const original = window.matchMedia
beforeEach(() => {
  localStorage.clear()
  toast.dismiss()
  window.matchMedia = ((query: string) => ({
    matches: query === '(min-width: 1280px)' || query.includes('prefers-reduced-motion: reduce'),
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia
})
afterEach(() => {
  window.matchMedia = original
  vi.restoreAllMocks()
})

describe('Artifacts: G3 nits', () => {
  it('N1: of two files with the same content on one ticket, Preview shows the one chosen', async () => {
    const real = api.listArtifacts.bind(api)
    vi.spyOn(api, 'listArtifacts').mockImplementation(async (ws, q) => {
      const page = await real(ws, q)
      const first = page.items.find((a) => a.name === 'tariff-export.log') as ArtifactItem
      return { ...page, items: [...page.items, { ...first, name: 'tariff-export-copy.log' }], total: page.total + 1 }
    })
    const { user } = renderApp('/w/DEMO/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export-copy.log' }, T))
    expect(await screen.findByRole('complementary', { name: 'Preview of tariff-export-copy.log' }, T)).toBeInTheDocument()
    expect(document.querySelector('[data-artifact][aria-current="true"]')).toHaveTextContent('tariff-export-copy.log')
  })

  it('N2: when the shown artifact leaves the results while focus is in the pane, focus does not fall to the page', async () => {
    let hide = false
    const real = api.listArtifacts.bind(api)
    vi.spyOn(api, 'listArtifacts').mockImplementation(async (ws, q) => {
      const page = await real(ws, q)
      return hide ? { ...page, items: page.items.filter((a) => a.name !== 'tariff-export.log'), total: page.total - 1 } : page
    })
    const { user, client } = renderApp('/w/DEMO/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    await screen.findByRole('complementary', { name: 'Preview of tariff-export.log' }, T)
    screen.getByRole('button', { name: 'Close the preview' }).focus()
    hide = true
    await act(() => client.invalidateQueries({ queryKey: ['artifacts'] }))
    await waitFor(() => expect(screen.queryByRole('complementary', { name: 'Preview of tariff-export.log' })).toBeNull(), T)
    await waitFor(() => expect(document.activeElement).not.toBe(document.body), T)
    expect(document.activeElement?.id).toBe('artifact-results')
  })

  it('N3: without ?view= and without the viewer, the page shows the list instead of waiting for good', async () => {
    vi.spyOn(api, 'getMe').mockRejectedValue(new Error('down'))
    renderApp('/w/DEMO/artifacts')
    expect(await screen.findByRole('table', {}, T)).toBeInTheDocument()
  })
})
