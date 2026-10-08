import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
const ws = () => mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
const prs = async () => (await api.getAddonState(ws(), 'github')).prs as { id: string; review: string; checks: { status: string }[] }[]

afterEach(() => vi.restoreAllMocks())

describe('github code reviews page', () => {
  it('lists the pull requests with repo, number, ticket, checks and review', async () => {
    renderApp('/addon/github/reviews', { viewer: 'p_sev' })
    const row = (await screen.findByText('Add billing reconciliation tests', {}, T)).closest('tr')!
    expect(within(row).getByText('acme-energy/energy-dbt')).toBeInTheDocument()
    expect(within(row).getByText('#29')).toBeInTheDocument()
    expect(within(row).getByText('DEMO-0041')).toBeInTheDocument()
    expect(within(row).getByText('pass')).toBeInTheDocument()
    expect(within(row).getByText('requested')).toBeInTheDocument()
  })
  it('Approve on GitHub marks the review approved', async () => {
    const { user } = renderApp('/addon/github/reviews', { viewer: 'p_sev' })
    const row = (await screen.findByText('Add billing reconciliation tests', {}, T)).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Approve on GitHub' }))
    await waitFor(async () => expect((await prs()).find((p) => p.id === 'acme-energy/energy-dbt#29')!.review).toBe('approved'), T)
    await waitFor(() => expect(within(screen.getByText('Add billing reconciliation tests').closest('tr')!).getByText('approved')).toBeInTheDocument(), T)
  })
  it('Open opens the pull request on github.com', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const { user } = renderApp('/addon/github/reviews', { viewer: 'p_sev' })
    const row = (await screen.findByText('Add billing reconciliation tests', {}, T)).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Open' }))
    await waitFor(() => expect(open).toHaveBeenCalledWith('https://github.com/acme-energy/energy-dbt/pull/29', '_blank', 'noopener,noreferrer'), T)
  })
  it('viewer sees the table with disabled actions', async () => {
    renderApp('/addon/github/reviews', { viewer: 'p_tom' })
    const row = (await screen.findByText('Add billing reconciliation tests', {}, T)).closest('tr')!
    expect(within(row).getByRole('button', { name: 'Approve on GitHub' })).toBeDisabled()
  })
})

describe('github ticket panel', () => {
  it('shows the PR on a ticket that has one and Refresh flips pending checks', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    await within(panel).findByText('Pull request', {}, T)
    expect(await within(panel).findByText('#31', {}, T)).toBeInTheDocument()
    expect(within(panel).getAllByText('pending').length).toBeGreaterThan(0)
    await user.click(within(panel).getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(within(panel).queryAllByText('pending')).toHaveLength(0), T)
  })
  it('is hidden on a ticket without a PR', async () => {
    renderApp('/ticket/DEMO-0045', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    await waitFor(() => expect(within(panel).getByText(/Usage/i)).toBeInTheDocument(), T).catch(() => undefined)
    expect(within(panel).queryByText('Pull request')).not.toBeInTheDocument()
  })
})

describe('github issues lane', () => {
  it('Import as ticket (a list item action) creates a ticket and removes the card', async () => {
    const { user } = renderApp('/board', { viewer: 'p_sev' })
    const lane = await screen.findByRole('region', { name: /GitHub issues/ }, T)
    const buttons = await within(lane).findAllByRole('button', { name: 'Import as ticket' }, T)
    expect(buttons).toHaveLength(8)
    const card = buttons[0].closest('[data-testid="lane-card"]') as HTMLElement
    const title = card.querySelector('div > div')!.textContent!
    await user.click(buttons[0])
    await waitFor(() => expect(within(lane).queryByText(title)).not.toBeInTheDocument(), T)
    expect(within(lane).getAllByRole('button', { name: 'Import as ticket' })).toHaveLength(7)
  })
})
