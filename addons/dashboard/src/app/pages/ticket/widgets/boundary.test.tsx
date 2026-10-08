import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ErrorBoundary, BlockProblem, PageProblem } from '@/components/ErrorBoundary'
import { renderApp } from '@/test/renderApp'

vi.mock('./parse', async (orig) => ({
  ...(await orig<typeof import('./parse')>()),
  resolveTicketWidgets: () => {
    throw new Error('boom')
  },
}))

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
    render(<PageProblem retry={() => {}} />)
  })
})
