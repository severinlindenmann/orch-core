import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { api, mockStore } from '@/api/client'
import { moreAction } from '@/test/rowActions'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

// Ticket-rail tests render at 1440 px (the rail is a column from 1280 px; below, the Panels sheet).
afterEach(() => vi.unstubAllGlobals())

const T = { timeout: 4000 }
const ws = () => mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
const prs = async () => (await api.getAddonState(ws(), 'github')).prs as { id: string; review: string; checks: { status: string }[] }[]

afterEach(() => vi.restoreAllMocks())

describe('github code reviews page', () => {
  it('lists the open pull requests with repo, number, ticket link, and checks and review as chips', async () => {
    renderApp('/addon/github/reviews', { viewer: 'p_sev' })
    const row = (await screen.findByText('Add billing reconciliation tests', {}, T)).closest('tr')!
    expect(within(row).getByText('acme-energy/energy-dbt')).toBeInTheDocument()
    expect(within(row).getByText('#29')).toBeInTheDocument()
    expect(within(row).getByRole('link', { name: 'DEMO-0041' })).toHaveAttribute('href', '/ticket/DEMO-0041')
    expect(within(row).getByText('pass').className).toMatch(/rounded-full/)
    expect(within(row).getByText('requested').className).toMatch(/rounded-full/)
    expect(screen.queryByText('Normalize meter reading timestamps to UTC')).not.toBeInTheDocument() // merged: in the fold
  })
  it('Recently merged is a closed fold with the merged pull requests', async () => {
    const { user } = renderApp('/addon/github/reviews', { viewer: 'p_sev' })
    const fold = await screen.findByRole('button', { name: /Recently merged\s*1/ }, T)
    expect(fold).toHaveAttribute('aria-expanded', 'false')
    await user.click(fold)
    expect(await screen.findByText('Normalize meter reading timestamps to UTC')).toBeInTheDocument()
  })
  it('the row button is Review on GitHub; Approve is in More, and absent when checks fail or it is approved', async () => {
    const { user } = renderApp('/addon/github/reviews', { viewer: 'p_sev' })
    const ok = (await screen.findByText('Add billing reconciliation tests', {}, T)).closest('tr')!
    expect(within(ok).getByRole('button', { name: 'Review on GitHub ↗' })).toBeInTheDocument()
    expect(within(ok).queryByRole('button', { name: 'Approve on GitHub' })).not.toBeInTheDocument()
    await user.click(within(ok).getByRole('button', { name: /^More actions for / }))
    expect(await screen.findByRole('menuitem', { name: 'Approve on GitHub' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    const failing = screen.getByText('Add freshness checks to sources').closest('tr')! // checks fail
    expect(within(failing).getByRole('button', { name: 'Review on GitHub ↗' })).toBeInTheDocument()
    expect(within(failing).queryByRole('button', { name: /^More actions for / })).not.toBeInTheDocument()
  })
  it('Approve on GitHub marks the review approved, and the action is gone afterwards', async () => {
    const { user } = renderApp('/addon/github/reviews', { viewer: 'p_sev' })
    const row = (await screen.findByText('Add billing reconciliation tests', {}, T)).closest('tr')!
    await user.click(await moreAction(user, row, 'Approve on GitHub'))
    await waitFor(async () => expect((await prs()).find((p) => p.id === 'acme-energy/energy-dbt#29')!.review).toBe('approved'), T)
    await waitFor(() => expect(within(screen.getByText('Add billing reconciliation tests').closest('tr')!).getByText('approved')).toBeInTheDocument(), T)
    expect(within(screen.getByText('Add billing reconciliation tests').closest('tr')!).queryByRole('button', { name: /^More actions for / })).not.toBeInTheDocument()
  })
  it('Review on GitHub opens the pull request on github.com', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const { user } = renderApp('/addon/github/reviews', { viewer: 'p_sev' })
    const row = (await screen.findByText('Add billing reconciliation tests', {}, T)).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Review on GitHub ↗' }))
    await waitFor(() => expect(open).toHaveBeenCalledWith('https://github.com/acme-energy/energy-dbt/pull/29', '_blank', 'noopener,noreferrer'), T)
  })
  it('viewer sees the table; Approve is disabled, Review (a read) is not', async () => {
    const { user } = renderApp('/addon/github/reviews', { viewer: 'p_tom' })
    const row = (await screen.findByText('Add billing reconciliation tests', {}, T)).closest('tr')!
    await waitFor(() => expect(within(row).getByRole('button', { name: 'Review on GitHub ↗' })).toBeEnabled(), T)
    await user.click(within(row).getByRole('button', { name: /^More actions for / }))
    expect(await screen.findByRole('menuitem', { name: 'Approve on GitHub' })).toHaveAttribute('aria-disabled', 'true')
  })
})

describe('github page tabs', () => {
  it('Pull requests first with the open count, Issues second with its own table and Import as ticket', async () => {
    const { user } = renderApp('/addon/github/reviews', { viewer: 'p_sev' })
    expect(await screen.findByRole('tab', { name: /^Pull requests\s*5/ }, T)).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByText('Seed loader fails on BOM files')).not.toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /^Issues\s*8/ }))
    const row = (await screen.findByText('Seed loader fails on BOM files', {}, T)).closest('tr')!
    expect(within(row).getByText('bug')).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Import as ticket' }))
    await waitFor(() => expect(screen.queryByText('Seed loader fails on BOM files')).not.toBeInTheDocument(), T)
    expect(screen.getByRole('tab', { name: /^Issues\s*7/ })).toBeInTheDocument()
  })
})

describe('github ticket panel', () => {
  it('shows the PR on a ticket that has one and Refresh flips pending checks', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    await within(panel).findByText('Pull request', {}, T)
    await openTicketPanel(user, 'Pull request')
    expect(await within(panel).findByText('#31', {}, T)).toBeInTheDocument()
    expect(within(panel).getAllByText('pending').length).toBeGreaterThan(0)
    await user.click(within(panel).getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(within(panel).queryAllByText('pending')).toHaveLength(0), T)
  })
  it('is hidden on a ticket without a PR', async () => {
    vi.stubGlobal('innerWidth', 1440)
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
    const success = vi.spyOn(toast, 'success')
    await user.click(buttons[0])
    await waitFor(() => expect(within(lane).queryByText(title)).not.toBeInTheDocument(), T)
    expect(within(lane).getAllByRole('button', { name: 'Import as ticket' })).toHaveLength(7)
    // The confirmation names the new ticket and opens it.
    const [msg, opts] = success.mock.calls.find(([m]) => /^Imported GH-\d+ as DEMO-\d+ \(Backlog\)\.$/.test(String(m)))!
    const key = /as (DEMO-\d+)/.exec(String(msg))![1]
    const action = (opts as { action: { label: string; onClick: () => void } }).action
    expect(action.label).toBe('Open')
    action.onClick()
    expect(await screen.findByRole('heading', { level: 1, name: title.split('acme-energy/')[0] }, T)).toBeInTheDocument()
    expect(key).toMatch(/^DEMO-/)
  })
})
