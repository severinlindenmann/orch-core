import { act, screen, waitFor, within } from '@testing-library/react'
import { createElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', async (orig) => {
  const actual = await orig<typeof import('sonner')>()
  return { ...actual, toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }) }
})
// jsdom has no layout for real dragging: capture the board's drag-end handler.
const dnd = vi.hoisted(() => ({ onDragEnd: undefined as undefined | ((e: unknown) => void) }))
vi.mock('@dnd-kit/core', async (orig) => {
  const actual = await orig<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: Parameters<typeof actual.DndContext>[0]) => {
      dnd.onDragEnd = props.onDragEnd as (e: unknown) => void
      return createElement(actual.DndContext, props)
    },
  }
})

import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }
const DISPLAY = 'orch.board.display'

describe('board grouped by epic (N2)', () => {
  beforeEach(() => {
    Element.prototype.setPointerCapture = () => {}
  })

  it('groups by epic by default: an epic lane with its children, no epic card, and a No epic lane', async () => {
    renderApp('/board')
    const lane = await screen.findByRole('button', { name: /^Collapse DEMO-0040 /, expanded: true }, T)
    expect(lane).toBeInTheDocument()
    expect(screen.queryByTestId('card-DEMO-0040')).toBeNull()
    expect(screen.getByRole('button', { name: /^Collapse No epic/ })).toBeInTheDocument()
    expect(within(screen.getByRole('group', { name: 'DEMO-0040 · In progress' })).getByTestId('card-DEMO-0043')).toBeInTheDocument()
    // a single ticket keeps its normal card; the child is the compact two-line card
    expect(screen.getByTestId('card-DEMO-0044').closest('[role=group]')).toHaveAttribute('aria-label', expect.stringMatching(/^No epic · /))
    expect(screen.getByRole('img', { name: /^DEMO-0040 .*: \d+\/4 done$/ })).toBeInTheDocument()
  })

  it('Display has Group: Epic / None; None restores the flat columns, and the choice is remembered', async () => {
    const { user, unmount } = renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    await user.click(screen.getByRole('button', { name: 'Display' }))
    const group = await screen.findByRole('radiogroup', { name: 'Group by' })
    expect(within(group).getByRole('radio', { name: 'Epic' })).toBeChecked()
    await user.click(within(group).getByRole('radio', { name: 'None' }))
    // flat: the epic is a card again and the columns hold their cards directly
    expect(await screen.findByTestId('card-DEMO-0040')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'In progress' })).getByTestId('card-DEMO-0043')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Collapse DEMO-0040/ })).toBeNull()
    expect(JSON.parse(localStorage.getItem(DISPLAY)!).group).toBe('none')
    unmount()
    renderApp('/board', { storage: { [DISPLAY]: localStorage.getItem(DISPLAY)! } })
    expect(await screen.findByTestId('card-DEMO-0040', {}, T)).toBeInTheDocument()
  })

  it('the estimate sum of a column is the same grouped and flat (the epic counts too)', async () => {
    const { user } = renderApp('/board')
    const sumOf = async () => (await within(await screen.findByRole('region', { name: 'In progress' })).findByLabelText(/^Sum of /)).textContent
    const grouped = await sumOf()
    await user.click(screen.getByRole('button', { name: 'Display' }))
    await user.click(await screen.findByRole('radio', { name: 'None' }))
    await waitFor(async () => expect(await sumOf()).toBe(grouped))
  })

  it('a big epic (40 children) starts folded: one lane header, no child cards; Enter unfolds, the arrow keys fold and unfold', async () => {
    const { user } = renderApp('/board', { viewer: 'p_sev', setup: (s) => s.reset('busy') })
    const folded = await screen.findByRole('button', { name: /^Expand DEMO-0100 /, expanded: false }, T)
    const children = mockStore.ticket('DEMO-0100')!.children!
    expect(children).toHaveLength(40)
    for (const k of children) expect(screen.queryByTestId(`card-${k}`)).toBeNull()
    expect(screen.getByText(/\d+\/40 done/)).toBeInTheDocument()
    folded.focus()
    await user.keyboard('{Enter}')
    const open = await screen.findByRole('button', { name: /^Collapse DEMO-0100 / })
    expect(open).toHaveAttribute('aria-expanded', 'true')
    expect(JSON.parse(localStorage.getItem(DISPLAY)!).lanes['DEMO-0100']).toBe(false)
    await waitFor(() => expect(screen.getAllByTestId(/^card-/).length).toBeGreaterThan(5), T)
    open.focus()
    await user.keyboard('{ArrowLeft}')
    expect(await screen.findByRole('button', { name: /^Expand DEMO-0100 / })).toHaveAttribute('aria-expanded', 'false')
    screen.getByRole('button', { name: /^Expand DEMO-0100 / }).focus()
    await user.keyboard('{ArrowRight}')
    expect(await screen.findByRole('button', { name: /^Collapse DEMO-0100 / })).toBeInTheDocument()
    mockStore.sim.stopAll()
  }, 30_000)

  it('dropping a child on another status cell of its lane moves its status', async () => {
    renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    act(() => dnd.onDragEnd!({ active: { data: { current: { ticket: { key: 'DEMO-0043', status: 'in-progress' } } } }, over: { id: 'DEMO-0040|open' } }))
    const cell = screen.getByRole('group', { name: 'DEMO-0040 · Open' })
    await waitFor(() => expect(within(cell).getByTestId('card-DEMO-0043')).toBeInTheDocument(), T)
  })
})
