// N11: the Board in a narrow page area (the terminal docked on the right) rails columns instead of scrolling sideways.
import { screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 5000 }

describe('board in a narrow page area (N11)', () => {
  beforeEach(() => {
    localStorage.clear()
    Element.prototype.setPointerCapture = () => {}
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 700, height: 600, top: 0, left: 0, right: 700, bottom: 600, x: 0, y: 0, toJSON: () => ({}) })
  })
  afterEach(() => vi.restoreAllMocks())

  it('rails columns until the board fits; a column opened from a rail stays open and the choice is not stored', async () => {
    const { user } = renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    // Backlog rails early (order: addon lanes, Done, Backlog, Testing, Waiting, Open, In progress).
    const backlog = await screen.findByRole('button', { name: /^Expand Backlog/ }, T)
    expect(screen.queryByRole('button', { name: /^Expand In progress/ })).toBeNull()
    await user.click(backlog)
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Expand Backlog/ })).toBeNull())
    const stored = JSON.parse(localStorage.getItem('orch.board.display.p_sev') ?? '{"collapsed":[]}')
    expect(stored.collapsed ?? []).not.toContain('backlog')
  })
})
