import { act, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { renderApp } from '@/test/renderApp'

// The page "measures" 1200 px (a 1440 px window with the sidebar open); see preview.test.tsx.
vi.mock('@/lib/useElementWidth', () => ({ useElementWidth: () => [() => {}, 1200] }))

const T = { timeout: 6000 }
const original = window.matchMedia
const wide = (on: boolean) => {
  window.matchMedia = ((query: string) => ({
    matches: (on && query === '(min-width: 1280px)') || query.includes('prefers-reduced-motion: reduce'),
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia
}
beforeEach(() => {
  localStorage.clear()
  // Toasts are global: one from an earlier test must not count here.
  toast.dismiss()
})
afterEach(() => {
  window.matchMedia = original
  vi.restoreAllMocks()
})

// tariff-export.log on DEMO-0043 (sha256 9b1e44c07ad2) and seeds-in-warehouse.png (3f9ab0c4e7d8).
const TARIFF = 'DEMO-0043.9b1e44c07ad2'
const SEEDS = 'DEMO-0043.3f9ab0c4e7d8'
const pane = (name: string) => screen.findByRole('complementary', { name: `Preview of ${name}` }, T)
const search = (address: string) => new URL(address, 'http://x').searchParams

// G2 × G3: the address is the one source of the Artifacts page's view and shown artifact.
describe('Artifacts page: view and shown artifact in the address', () => {
  it('a cold load of ?view=grid&a=… shows the grid and that artifact in the pane (wide)', async () => {
    wide(true)
    renderApp(`/w/DEMO/artifacts?view=grid&a=${TARIFF}`)
    expect(await screen.findByRole('list', { name: 'Artifacts' }, T)).toBeInTheDocument()
    await pane('tariff-export.log')
    expect(document.querySelector('li[data-artifact][aria-current="true"]')).toHaveTextContent('tariff-export.log')
  })
  it('a cold load of ?a=… on a narrow page opens the drawer', async () => {
    wide(false)
    renderApp(`/w/DEMO/artifacts?a=${TARIFF}`)
    const sheet = await screen.findByRole('dialog', {}, T)
    expect(within(sheet).getByRole('heading', { name: 'tariff-export.log' })).toBeInTheDocument()
    expect(screen.getByRole('table', { hidden: true })).toBeInTheDocument()
  })
  it('?view= wins over the remembered layout; without it the remembered one applies', async () => {
    wide(true)
    const r = renderApp('/w/DEMO/artifacts?view=list', { storage: { 'orch.artifacts.view.p_sev': 'grid' } })
    expect(await screen.findByRole('table', {}, T)).toBeInTheDocument()
    r.unmount()
    renderApp('/w/DEMO/artifacts', { storage: { 'orch.artifacts.view.p_sev': 'grid' } })
    expect(await screen.findByRole('list', { name: 'Artifacts' }, T)).toBeInTheDocument()
  })
  it('Preview and Close replace the entry; choosing a view pushes one and keeps ?a=', async () => {
    wide(true)
    const { user, router, address } = renderApp('/w/DEMO/artifacts')
    const start = router.history.length
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    await pane('tariff-export.log')
    await waitFor(() => expect(search(address()).get('a')).toBe(TARIFF))
    expect(router.history.length).toBe(start)
    await user.click(screen.getByRole('radio', { name: 'Grid' }))
    await waitFor(() => expect(search(address()).get('view')).toBe('grid'))
    expect(router.history.length).toBe(start + 1)
    expect(search(address()).get('a')).toBe(TARIFF)
    await pane('tariff-export.log')
    await user.click(screen.getByRole('button', { name: 'Close the preview' }))
    await waitFor(() => expect(search(address()).get('a')).toBeNull())
    expect(router.history.length).toBe(start + 1)
  })
  it('Back and Forward restore the view and the shown artifact', async () => {
    wide(true)
    const { user, router, address } = renderApp('/w/DEMO/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    await pane('tariff-export.log')
    await user.click(screen.getByRole('radio', { name: 'Grid' }))
    await screen.findByRole('list', { name: 'Artifacts' }, T)
    await user.click(screen.getByRole('button', { name: 'Preview seeds-in-warehouse.png' }))
    await pane('seeds-in-warehouse.png')
    await waitFor(() => expect(search(address()).get('a')).toBe(SEEDS))
    await act(async () => router.history.back())
    expect(await screen.findByRole('table', {}, T)).toBeInTheDocument()
    await pane('tariff-export.log')
    await act(async () => router.history.forward())
    expect(await screen.findByRole('list', { name: 'Artifacts' }, T)).toBeInTheDocument()
    await pane('seeds-in-warehouse.png')
  })
  it('a stale ?a= (not in these results) is cleared with replace and says why', async () => {
    wide(true)
    const { router, address } = renderApp('/w/DEMO/artifacts?view=list&a=DEMO-0043.000000000000')
    const start = router.history.length
    await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T)
    await waitFor(() => expect(search(address()).get('a')).toBeNull(), T)
    expect(search(address()).get('view')).toBe('list')
    expect(router.history.length).toBe(start)
    expect(await screen.findByText('The linked artifact is not in this list', {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('complementary', { name: /Preview of/ })).toBeNull()
  })
  it('a new search clears ?a= with replace', async () => {
    wide(true)
    const { user, router, address } = renderApp(`/w/DEMO/artifacts?a=${TARIFF}`)
    await pane('tariff-export.log')
    const start = router.history.length
    await user.type(screen.getByRole('searchbox', { name: 'Search artifacts' }), 'tolerance')
    expect(await screen.findByText('1 artifact', {}, T)).toBeInTheDocument()
    await waitFor(() => expect(search(address()).get('a')).toBeNull(), T)
    expect(router.history.length).toBe(start)
    expect(screen.queryByText('The linked artifact is not in this list')).toBeNull()
  })
})
