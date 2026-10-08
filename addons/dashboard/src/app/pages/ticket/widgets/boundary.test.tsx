import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ErrorBoundary, BlockProblem, PageProblem } from '@/components/ErrorBoundary'
import { renderApp } from '@/test/renderApp'
import { AddonContributionView } from '@/addon-ui/AddonSlot'
import type { ResolvedContribution } from '@/addon-ui/slots'

vi.mock('./parse', async (orig) => ({
  ...(await orig<typeof import('./parse')>()),
  resolveTicketWidgets: () => {
    throw new Error('boom')
  },
}))

// AddonNode validates nodes, so nothing real throws: a test-only node type stands in for a renderer bug.
vi.mock('@/addon-ui/AddonNode', async (orig) => {
  const real = await orig<typeof import('@/addon-ui/AddonNode')>()
  return {
    ...real,
    AddonNode: (p: Parameters<typeof real.AddonNode>[0]) => {
      if ((p.node as { type?: string } | null)?.type === 'bomb') throw new Error('renderer bug')
      return real.AddonNode(p)
    },
  }
})

describe('error boundaries', () => {
  it('a page that throws shows the calm panel and keeps the sidebar usable', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    renderApp('/ticket/DEMO-0043')
    expect(await screen.findByRole('heading', { name: 'This page hit a problem' }, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: 'Tickets' }).length).toBeGreaterThan(0)
    // Leaving the page clears the error.
    await userEvent.click(screen.getAllByRole('link', { name: 'Board' })[0])
    await waitFor(() => expect(screen.queryByText('This page hit a problem')).toBeNull(), { timeout: 5000 })
  })
  it('a failing block shows a small note and leaves its siblings', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const Bomb = () => {
      throw new Error('x')
    }
    render(
      <div>
        <ErrorBoundary fallback={() => <BlockProblem what="This widget" />}>
          <Bomb />
        </ErrorBoundary>
        <p>sibling</p>
      </div>,
    )
    expect(screen.getByText(/This widget could not be drawn/)).toBeInTheDocument()
    expect(screen.getByText('sibling')).toBeInTheDocument()
  })
  it('the page panel says what happened and its Reload retries', async () => {
    const retry = vi.fn()
    render(<PageProblem retry={retry} />)
    const panel = screen.getByRole('alert')
    expect(panel).toHaveTextContent('This page hit a problem')
    expect(panel).toHaveTextContent('Nothing was lost.')
    await userEvent.click(screen.getByRole('button', { name: 'Reload' }))
    expect(retry).toHaveBeenCalledOnce()
  })
  it('one addon contribution that throws shows its own note; the next contribution still renders', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const c = (id: string, node: unknown): ResolvedContribution => ({ addon: 'estimate', addonTitle: 'Estimate', slot: 'ticket.panel', id, title: id, node })
    render(
      <div>
        <AddonContributionView c={c('bad', { type: 'bomb' })} readOnly />
        <AddonContributionView c={c('good', { type: 'markdown', text: 'still here' })} readOnly />
      </div>,
    )
    expect(screen.getByText(/This estimate panel could not be drawn/)).toBeInTheDocument()
    expect(screen.getByText('still here')).toBeInTheDocument()
  })
})
