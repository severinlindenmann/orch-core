import { act, screen, waitFor, within } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', async (orig) => {
  const actual = await orig<typeof import('sonner')>()
  return { ...actual, toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }) }
})
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

import { renderApp } from '@/test/renderApp'

const T = { timeout: 5000 }
const realMatchMedia = window.matchMedia
const realRect = Element.prototype.getBoundingClientRect
const ORDER = ['backlog', 'open', 'in-progress', 'waiting', 'testing', 'done']

/** Motion on; a stub Element.animate; layout where a card's x depends on its status, so a move changes its place. */
function motionOn() {
  window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  const animate = vi.fn(() => ({ finished: Promise.resolve(), cancel() {} }))
  Element.prototype.animate = animate as unknown as typeof Element.prototype.animate
  Element.prototype.getBoundingClientRect = function (this: Element) {
    const st = (this as HTMLElement).dataset?.status ?? this.closest('[data-status]')?.getAttribute('data-status') ?? ''
    const left = Math.max(0, ORDER.indexOf(st)) * 300
    // Outside the columns (the board's own frame) a wide page, so the Board adds no rails (N11).
    const width = st ? 200 : 2400
    return { left, top: 100, right: left + width, bottom: 160, width, height: 60, x: left, y: 100, toJSON() {} } as DOMRect
  }
  return animate
}
const transforms = (animate: ReturnType<typeof vi.fn>) => animate.mock.calls.map((c) => String(((c as unknown as [Keyframe[]])[0][0] as Keyframe).transform ?? ''))

describe('board motion (motion allowed)', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(() => {
    window.matchMedia = realMatchMedia
    Element.prototype.getBoundingClientRect = realRect
    delete (Element.prototype as { animate?: unknown }).animate
  })

  it('a card dropped in another column settles by FLIP from where it was let go', async () => {
    const animate = motionOn()
    renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    animate.mockClear()
    const t = { key: 'DEMO-0043', status: 'in-progress' }
    act(() => dnd.onDragEnd!({ active: { data: { current: { ticket: t } }, rect: { current: { translated: { left: 640, top: 120 } } } }, over: { id: 'testing' } }))
    await waitFor(() => expect(transforms(animate).some((x) => x.startsWith('translate(') && x.includes('scale(1.02)'))).toBe(true), T)
  })

  it('a drop that changes nothing settles in place; a cancelled drag does not animate a card that never left', async () => {
    const animate = motionOn()
    renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    animate.mockClear()
    const t = { key: 'DEMO-0043', status: 'in-progress' }
    // Dropped onto nothing: no move, no animation until the card itself comes out of dragging.
    act(() => dnd.onDragEnd!({ active: { data: { current: { ticket: t } }, rect: { current: { translated: { left: 640, top: 120 } } } }, over: null }))
    expect(animate).not.toHaveBeenCalled()
    // Dropped on its own column: still no status change and no animation of the unchanged card (it never was in dragging here).
    act(() => dnd.onDragEnd!({ active: { data: { current: { ticket: t } }, rect: { current: { translated: { left: 640, top: 120 } } } }, over: { id: 'in-progress' } }))
    expect(animate).not.toHaveBeenCalled()
  })

  it('a card moved from the menu plays from its old column to the new one', async () => {
    const animate = motionOn()
    const { user } = renderApp('/board')
    const card = await screen.findByTestId('card-DEMO-0043', {}, T)
    card.focus()
    animate.mockClear()
    await user.keyboard('m')
    const menu = await screen.findByRole('menu', { name: 'Move to' })
    await user.click(within(menu).getByRole('menuitem', { name: 'Open' }))
    await waitFor(() => expect(transforms(animate).some((x) => x.startsWith('translate(') && !x.includes('scale('))).toBe(true), T)
  })
})
