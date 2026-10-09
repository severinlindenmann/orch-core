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

  it('the board scrolls inside its own region: overflow-x-auto and min-w-0, and its parents do not overflow', async () => {
    renderApp('/board', { viewer: 'p_tom', setup: (s) => s.reset('busy'), storage: { 'orch.board.display': JSON.stringify({ group: 'none' }) } })
    const col = await screen.findByRole('region', { name: 'Backlog' }, T)
    const scroller = col.parentElement!
    expect(scroller.className).toContain('overflow-x-auto')
    expect(scroller.className).toContain('min-w-0')
    for (let el = scroller.parentElement; el && el !== document.body; el = el.parentElement) {
      expect(getComputedStyle(el).overflowX, el.className).not.toBe('scroll')
    }
    mockStore.sim.stopAll()
  })

  it('the grouped board scrolls inside its own region too', async () => {
    renderApp('/board', { viewer: 'p_tom', setup: (s) => s.reset('busy') })
    await screen.findByRole('region', { name: 'Backlog' }, T)
    const scroller = document.querySelector<HTMLElement>('[data-grouped="epic"]')!
    expect(scroller.className).toContain('overflow-x-auto')
    expect(scroller.className).toContain('min-w-0')
    for (let el = scroller.parentElement; el && el !== document.body; el = el.parentElement) {
      expect(getComputedStyle(el).overflowX, el.className).not.toBe('scroll')
    }
    mockStore.sim.stopAll()
  })

  it('the jump nav lists the addon lanes, so they are discoverable at every width', async () => {
    renderApp('/board')
    const nav = await screen.findByRole('navigation', { name: 'Jump to column' }, T)
    expect(await within(nav).findByRole('button', { name: /GitHub issues/ }, T)).toBeInTheDocument()
  })

  it('m on a focused card opens Move to, choosing Open moves it, the toast offers Undo', async () => {
    const { user } = renderApp('/board')
    const card = await screen.findByTestId('card-DEMO-0043', {}, T)
    card.focus()
    await user.keyboard('m')
    const menu = await screen.findByRole('menu', { name: 'Move to' })
    await user.click(within(menu).getByRole('menuitem', { name: 'Open' }))
    const open = screen.getByRole('group', { name: 'DEMO-0040 · Open' })
    await waitFor(() => expect(within(open).getByTestId('card-DEMO-0043')).toBeInTheDocument(), T)
    await waitFor(() => expect(document.activeElement).toBe(within(open).getByTestId('card-DEMO-0043')), T)
    expect(await screen.findByText('Moved DEMO-0043 to Open', {}, T)).toBeInTheDocument()
    await user.click(await screen.findByRole('button', { name: 'Undo' }))
    const prog = screen.getByRole('group', { name: 'DEMO-0040 · In progress' })
    await waitFor(() => expect(within(prog).getByTestId('card-DEMO-0043')).toBeInTheDocument(), T)
  })
})
