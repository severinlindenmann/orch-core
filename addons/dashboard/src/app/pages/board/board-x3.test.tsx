import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 5000 }

describe('board X3: 3-line cards, Display popover, role-aware moves', () => {
  beforeEach(() => {
    localStorage.clear()
    // jsdom has no pointer capture; sonner's toast calls it on pointerdown.
    Element.prototype.setPointerCapture = () => {}
  })

  it('a card has no progress bar until Show progress is switched on', async () => {
    const { user } = renderApp('/board')
    const card = await screen.findByTestId('card-DEMO-0043', {}, T)
    expect(within(card).queryByRole('progressbar')).toBeNull()
    expect(screen.queryByRole('progressbar')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Display' }))
    await user.click(await screen.findByRole('switch', { name: 'Show progress' }))
    expect(screen.getAllByRole('progressbar').length).toBeGreaterThan(0)
    expect(JSON.parse(localStorage.getItem('orch.board.display')!).progress).toBe(true)
  })

  it('Done is a collapsed rail by default and expands on click', async () => {
    const { user } = renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    const rail = screen.getByRole('button', { name: /Expand Done/ })
    expect(screen.queryByRole('region', { name: 'Done' })).toBeNull()
    await user.click(rail)
    expect(await screen.findByRole('region', { name: 'Done' })).toBeInTheDocument()
  })

  it('a viewer sees Read only and cannot drag', async () => {
    renderApp('/board', { viewer: 'p_tom' })
    const card = await screen.findByTestId('card-DEMO-0043', {}, T)
    expect(screen.getByText('Read only')).toBeInTheDocument()
    expect(card).not.toHaveAttribute('aria-roledescription')
    expect(card).not.toHaveAttribute('aria-describedby')
  })

  it('a viewer on the busy day has no page-level horizontal scroll', async () => {
    renderApp('/board', { viewer: 'p_tom', setup: (s) => s.reset('busy') })
    await screen.findAllByTestId(/^card-/, {}, T)
    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(window.innerWidth)
    mockStore.sim.stopAll()
  })

  it('m on a focused card opens Move to, choosing Open moves it, the toast offers Undo', async () => {
    const { user } = renderApp('/board')
    const card = await screen.findByTestId('card-DEMO-0043', {}, T)
    card.focus()
    await user.keyboard('m')
    const menu = await screen.findByRole('menu', { name: 'Move to' })
    await user.click(within(menu).getByRole('menuitem', { name: 'Open' }))
    const open = screen.getByRole('region', { name: 'Open' })
    await waitFor(() => expect(within(open).getByTestId('card-DEMO-0043')).toBeInTheDocument(), T)
    expect(await screen.findByText('Moved DEMO-0043 to Open', {}, T)).toBeInTheDocument()
    await user.click(await screen.findByRole('button', { name: 'Undo' }))
    const prog = screen.getByRole('region', { name: 'In progress' })
    await waitFor(() => expect(within(prog).getByTestId('card-DEMO-0043')).toBeInTheDocument(), T)
  })
})
