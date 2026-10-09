import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

// Ticket-rail tests render at 1440 px (the rail is a column from 1280 px; below, the Panels sheet).
afterEach(() => vi.unstubAllGlobals())

const T = { timeout: 4000 }

describe('usage page', () => {
  it('shows this week, month to date, the budget bar, both charts and the top tickets', async () => {
    renderApp('/addon/usage/overview', { viewer: 'p_sev' })
    expect(await screen.findByText('CHF 31.40', {}, T)).toBeInTheDocument()
    expect(screen.getByText('Month to date')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: /Budget used/ })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'CHF per day' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'CHF per model' })).toBeInTheDocument()
    const row = screen.getByText('DEMO-0040').closest('tr')!
    expect(within(row).getByText(/^CHF \d+\.\d\d$/)).toBeInTheDocument()
    expect(screen.queryByText(/^Budget \d+% used$/)).not.toBeInTheDocument()
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
  it('the Today card says This week CHF 31.40', async () => {
    renderApp('/', { viewer: 'p_sev' })
    const card = (await screen.findByText('CHF 31.40', {}, T)).closest('[data-addon="usage"]') as HTMLElement
    expect(within(card).getByText('This week')).toBeInTheDocument()
  })
})
