import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

const T = { timeout: 5000 }

afterEach(() => vi.unstubAllGlobals())

describe('Landing page', () => {
  it('shows each queue by remote and target, the attempt being checked and its checks, with a Preview chip', async () => {
    renderApp('/addon/land/landing', { viewer: 'p_sev' })
    const h1 = await screen.findByRole('heading', { level: 1, name: /Landing/ }, T)
    expect(within(h1).getByText('Preview')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'github.com/acme-energy/billing-api → develop' }, T)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'github.com/acme-energy/energy-dbt → develop' })).toBeInTheDocument()
    expect(screen.getByText(/Worker: checking DEMO-0052 \(attempt #14\)/)).toBeInTheDocument()
    expect(screen.getByText(/Attempt #14 · DEMO-0052/)).toBeInTheDocument()
    expect(await screen.findByText('T2 integration checks', {}, T)).toBeInTheDocument()
    expect(screen.getByText(/main \(D33: people merge to main by hand\)/)).toBeInTheDocument()
    expect(screen.getByText('1 from another workspace on this machine')).toBeInTheDocument()
  })

  it('History lists land.attempt records with source, target and candidate; the tab is per viewer', async () => {
    const { user } = renderApp('/addon/land/landing', { viewer: 'p_sev' })
    await user.click(await screen.findByRole('button', { name: 'History' }, T))
    expect(await screen.findByRole('columnheader', { name: 'Candidate' }, T)).toBeInTheDocument()
    const rows = screen.getAllByRole('row')
    expect(rows.some((r) => /DEMO-0053.*failed: conflict/.test(r.textContent ?? ''))).toBe(true)
    expect(rows.some((r) => /DEMO-0053.*requeued: target moved/.test(r.textContent ?? ''))).toBe(true)
    const ws = mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
    mockStore.setViewer('p_mara')
    expect(JSON.stringify(mockStore.addonStateView(ws, 'land')!.body)).not.toContain('"columns"')
  })
})

describe('Landing in the ticket rail', () => {
  it('DEMO-0053: the conflict resolution voided the approval — back to review, with the resolution diff and the invalidated gate', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0053', { viewer: 'p_sev' })
    const panel = await openTicketPanel(user, 'Landing')
    expect(await within(panel).findByText('Conflict resolution voids the approval — back to review', {}, T)).toBeInTheDocument()
    expect(within(panel).getByText('Verify: invalidated by orch')).toBeInTheDocument()
    await waitFor(() => expect(panel.querySelector('figure[data-widget="land-diff-13"]')).toBeTruthy(), T)
  })

  it('DEMO-0052: checking, with the candidate and the checks', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0052', { viewer: 'p_sev' })
    const panel = await openTicketPanel(user, 'Landing')
    expect(await within(panel).findByText('Checking (attempt #14)', {}, T)).toBeInTheDocument()
    expect(within(panel).getByText('Clean: the approval stands')).toBeInTheDocument()
    await waitFor(() => expect(panel.querySelector('figure[data-widget="land-checks-14"]')).toBeTruthy(), T)
  })

  it('DEMO-0042: approved, tested and merged are the same candidate', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0042', { viewer: 'p_sev' })
    const panel = await openTicketPanel(user, 'Landing')
    expect(await within(panel).findByText(/^Approved, tested and merged: the same candidate [0-9a-f]{7}$/, {}, T)).toBeInTheDocument()
  })
})

describe('Landing on the board and Today', () => {
  it('a card in the testing → done path carries a landing chip (no new column)', async () => {
    renderApp('/board', { viewer: 'p_sev' })
    const card = await screen.findByTestId('card-DEMO-0053', {}, T)
    expect(await within(card).findByText('back to review', {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /Landing/ })).toBeNull()
  })

  it('busy day: Today shows the needs as core-signed decisions in "From addons"', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup: (s) => s.reset('busy') })
    const group = await screen.findByRole('button', { name: /^From addons · \d+/ }, { timeout: 8000 })
    const region = group.closest('section') as HTMLElement
    const more = within(region).queryByRole('button', { name: /^Show \d+ more$/ })
    if (more) await user.click(more)
    expect(await within(region).findByText(/Needs: red checks/, {}, T)).toBeInTheDocument()
    expect(within(region).getByText(/Needs: conflict, a person must resolve/)).toBeInTheDocument()
  })
})
