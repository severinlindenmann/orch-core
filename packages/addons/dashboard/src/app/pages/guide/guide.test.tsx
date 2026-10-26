// The guide on a real host: fixed pages in the bundle, no host call; the sidebar lists it and `?` opens the page for the route.
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { setFeatures } from '@/api/features'
import { helpPageFor } from '@/api/guide'
import { GUIDE_PAGES, GUIDE_ROUTES } from '@/api/guideContent'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
const real = () => setFeatures({ features: ['today', 'board', 'tickets', 'agents', 'actions', 'addons'] })
afterEach(() => {
  cleanup()
  setFeatures(null)
})

describe('guide on a real host', () => {
  it('shows the page the address asks for, the first page otherwise, and lists coming-later things as later', async () => {
    real()
    const { user } = renderApp('/guide?page=approving', { viewer: 'p_sev' })
    expect(await screen.findByRole('heading', { name: 'Approving things' }, T)).toBeInTheDocument()
    expect(screen.getByText(/orch sign CODE/)).toBeInTheDocument()
    await user.click(within(screen.getByRole('navigation', { name: 'Guide pages' })).getByRole('link', { name: 'Coming later' }))
    expect(await screen.findByRole('heading', { name: 'Coming later' }, T)).toBeInTheDocument()
  })
  it('the sidebar has a Guide link', async () => {
    real()
    renderApp('/board', { viewer: 'p_sev' })
    expect(await screen.findByRole('link', { name: 'Guide' }, T)).toBeInTheDocument()
  })
  it('? on the board shows the guide page for it without any add-on, and the link opens the full guide', async () => {
    real()
    const { user } = renderApp('/board', { viewer: 'p_sev' })
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board'), T)
    await user.keyboard('?')
    const sheet = await screen.findByRole('dialog', { name: 'Help' }, T)
    expect(await within(sheet).findByRole('heading', { name: 'Today, Board and Tickets' }, T)).toBeInTheDocument()
    expect(sheet.querySelector('section[data-addon="guide"]')).toBeNull()
    await user.click(within(sheet).getByRole('link', { name: 'Open the guide' }))
    expect(await screen.findByRole('heading', { name: 'Today, Board and Tickets' }, T)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Guide'), T)
  })
  it('has pages for Repositories, Worktrees and Shares, and ? on those routes shows them (E-6)', () => {
    const titleOf = (path: string) => GUIDE_PAGES.find((p) => p.slug === helpPageFor(GUIDE_ROUTES, path))?.title
    expect(titleOf('/repos')).toBe('Repositories')
    expect(titleOf('/worktrees')).toBe('Worktrees')
    expect(titleOf('/shares')).toBe('Shares and Publish')
    for (const r of GUIDE_ROUTES) expect(GUIDE_PAGES.some((p) => p.slug === r.page), r.page).toBe(true)
  })
})
