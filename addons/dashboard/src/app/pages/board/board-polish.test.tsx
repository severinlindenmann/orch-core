import { act, screen, waitFor, within } from '@testing-library/react'
import { createElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', async (orig) => {
  const actual = await orig<typeof import('sonner')>()
  return { ...actual, toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }) }
})
// Pointer/keyboard dragging needs real layout rects, which jsdom lacks: capture the board's drag-end handler.
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

import { toast } from 'sonner'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 5000 }

describe('board polish', () => {
  beforeEach(() => vi.clearAllMocks())

  it('dragging a card to done toasts the verdict message and hint', async () => {
    renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    act(() => dnd.onDragEnd!({ active: { data: { current: { ticket: { key: 'DEMO-0043', status: 'in-progress' } } } }, over: { id: 'done' } }))
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Done is reached by a verdict', { description: 'Give the verdict on the ticket page' }), T)
  })

  it('an empty column says Nothing here, and backlog offers to create a ticket', async () => {
    const { user } = renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    await user.click(screen.getByRole('button', { name: 'Switch workspace' }))
    await user.click(await screen.findByRole('menuitem', { name: /CLI/ }))
    const backlog = await screen.findByRole('region', { name: 'Backlog' })
    await waitFor(() => expect(screen.queryByTestId('card-DEMO-0043')).toBeNull(), T)
    const empties = screen.getAllByText('Nothing here')
    expect(empties.length).toBeGreaterThan(0)
    await waitFor(() => expect(within(backlog).queryByTestId(/^card-/)).toBeNull(), T)
    expect(within(backlog).getByText('Nothing here')).toBeInTheDocument()
    expect(within(backlog).getByText('Create a ticket (c)')).toBeInTheDocument()
    const open = screen.getByRole('region', { name: 'Open' })
    expect(within(open).queryByText('Create a ticket (c)')).toBeNull()
  })
})
