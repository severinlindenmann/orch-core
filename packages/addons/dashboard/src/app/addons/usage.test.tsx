import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

// Ticket-rail tests render at 1440 px (the rail is a column from 1280 px; below, the Panels sheet).
afterEach(() => vi.unstubAllGlobals())

const T = { timeout: 4000 }
const ws = () => mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id

describe('usage page', () => {
  it('opens on Overview: every number states its period; budget, cost by model and the daily trend; tables are in their tabs', async () => {
    renderApp('/addon/usage/overview', { viewer: 'p_sev' })
    expect(await screen.findByText('CHF 31.40', {}, T)).toBeInTheDocument()
    // One measure and one period per tile, both in the label (B m4).
    expect(screen.getByText('Cost · last 7 days')).toBeInTheDocument()
    expect(screen.queryByText('This week')).not.toBeInTheDocument()
    expect(screen.getByText('Cost · this month')).toBeInTheDocument()
    expect(screen.getByText(/^1–9 Oct · \d+% of the CHF 150 budget$/)).toBeInTheDocument()
    expect(screen.getByText('Cost · last 30 days')).toBeInTheDocument()
    expect(screen.getByText('Tokens · last 30 days')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: /Monthly budget used/ })).toBeInTheDocument()
    expect(await screen.findByRole('img', { name: 'Cost by model · last 30 days' }, T)).toBeInTheDocument()
    expect(await screen.findByRole('img', { name: /^Cost per day · last 30 days/ }, T)).toBeInTheDocument()
    for (const name of ['Overview', 'By model', 'By ticket', 'By agent']) expect(screen.getByRole('tab', { name })).toBeInTheDocument()
    expect(screen.queryByText('DEMO-0040')).not.toBeInTheDocument()
    expect(screen.queryByText(/^Budget \d+% used$/)).not.toBeInTheDocument()
  })
  it('By model: cost first, then share, sessions and tokens in one unit; the total is the 30-day cost and stands out', async () => {
    const { user } = renderApp('/addon/usage/overview', { viewer: 'p_sev' })
    await user.click(await screen.findByRole('tab', { name: 'By model' }, T))
    const row = (await screen.findByText('claude-opus-5-5', {}, T)).closest('tr')!
    expect(within(row).getAllByRole('cell')).toHaveLength(7)
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Model', 'Cost (CHF)', 'Share of cost', 'Sessions', 'Input (M tokens)', 'Output (M tokens)', 'Cache-read (M tokens)'])
    for (const name of ['claude-sonnet-5-5', 'claude-haiku-4-5', 'gpt-5-codex']) expect(screen.getByText(name)).toBeInTheDocument()
    for (const h of ['Cost (CHF)', 'Share of cost', 'Sessions', 'Input (M tokens)']) expect(screen.getByRole('columnheader', { name: h })).toHaveClass('text-right')
    expect(await screen.findByText(/Input and output tokens exclude cache reads/, {}, T)).toBeInTheDocument()
    expect(screen.getByText(/^Last 30 days\./)).toBeInTheDocument()
    const total = screen.getByText('Total').closest('tr')!
    expect(total.className).toMatch(/font-semibold/)
    const st = (await api.getAddonState(ws(), 'usage')) as { cost30Cents: number }
    expect(within(total).getByText((st.cost30Cents / 100).toFixed(2))).toBeInTheDocument()
    expect(within(total).getByText('100.0 %')).toBeInTheDocument()
  })
  it('By ticket and By agent state their period and have their own tables', async () => {
    const { user } = renderApp('/addon/usage/overview', { viewer: 'p_sev' })
    await user.click(await screen.findByRole('tab', { name: 'By ticket' }, T))
    const row = (await screen.findByRole('link', { name: 'DEMO-0040' }, T)).closest('tr')!
    expect(within(row).getAllByRole('cell')[1]).toHaveTextContent(/^\d+\.\d\d$/)
    expect(await within(screen.getByRole('tabpanel')).findByText('Last 30 days', {}, T)).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'By agent' }))
    expect(await screen.findByText('Claude Code (subagents)')).toBeInTheDocument()
    expect(await within(screen.getByRole('tabpanel')).findByText('Last 30 days', {}, T)).toBeInTheDocument()
    expect(screen.queryByText('DEMO-0040')).not.toBeInTheDocument()
  })
  it('shows the warn alert after the budget is lowered to CHF 30', async () => {
    renderApp('/addon/usage/overview', {
      viewer: 'p_sev',
      setup: (s) => {
        s.addonState(s.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'usage').settings = { budget_chf: 30 }
      },
    })
    const alert = await screen.findByText(/^Budget \d+% used$/, {}, T)
    expect(alert.closest('[role="status"]')!.className).toMatch(/warning/)
  })
})

describe('usage ticket panel and Today card', () => {
  it('the panel for DEMO-0043 shows CHF, tokens, sessions and time', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    await openTicketPanel(user, 'Usage')
    const frame = await waitFor(() => {
      const f = panel.querySelector('[data-addon="usage"]')
      expect(f).not.toBeNull()
      return f as HTMLElement
    }, T)
    expect(within(frame).getByText(/^CHF \d+\.\d\d$/)).toBeInTheDocument()
    expect(within(frame).getByText(/\d+k$|\d+(\.\d)? M$/)).toBeInTheDocument()
    expect(within(frame).getByText('3')).toBeInTheDocument()
  })
  it('the Today glance says CHF 31.40 last 7 days', async () => {
    renderApp('/', { viewer: 'p_sev' })
    const card = (await screen.findByText('CHF 31.40', {}, T)).closest('[data-addon="usage"]') as HTMLElement
    expect(card).toHaveTextContent('CHF 31.40 last 7 days')
  })
})
