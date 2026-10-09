import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { QueueGroup } from './groups'
import type { Entry, Group, Row } from './queue'

const row = (id: string): Row => ({ kind: 'one', id, entry: { id } as unknown as Entry })
const group = (ids: string[]): Group => ({ id: 'questions', label: 'Questions', count: ids.length, oldest: undefined, rows: ids.map(row) })
const renderRow = (r: Row) => (
  <li key={r.id} data-testid={`row-${r.id}`}>
    {r.id}
  </li>
)
const ui = (ids: string[]) => <QueueGroup group={group(ids)} now="2026-10-09T11:30:00Z" scope="t" renderRow={renderRow} />

const realMatchMedia = window.matchMedia
afterEach(() => {
  window.matchMedia = realMatchMedia
  delete (Element.prototype as { animate?: unknown }).animate
  sessionStorage.clear()
})

describe('Today rows motion', () => {
  it('under reduced motion a resolved row is gone at once and nothing animates', () => {
    const animate = vi.fn()
    Element.prototype.animate = animate as unknown as typeof Element.prototype.animate
    const { rerender } = render(ui(['a', 'b', 'c']))
    rerender(ui(['a', 'c', 'd']))
    expect(screen.queryByTestId('row-b')).toBeNull()
    expect(screen.getByTestId('row-d')).toBeInTheDocument()
    expect(animate).not.toHaveBeenCalled()
  })

  it('with motion allowed a resolved row collapses out as an inert ghost, a new row fades in once, the rest stay still', async () => {
    window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
    const finishers: Array<() => void> = []
    const animate = vi.fn(function (this: Element) {
      return { finished: new Promise<void>((res) => finishers.push(res)), cancel() {} } as unknown as Animation
    })
    Element.prototype.animate = animate as unknown as typeof Element.prototype.animate
    const { rerender } = render(ui(['a', 'b', 'c']))
    expect(animate).not.toHaveBeenCalled() // first render: nothing animates
    rerender(ui(['a', 'b', 'c']))
    expect(animate).not.toHaveBeenCalled() // a refresh with the same rows: nothing animates
    rerender(ui(['a', 'c', 'd']))
    const ghost = screen.getByTestId('row-b')
    expect(ghost).toHaveAttribute('data-ghost')
    expect(ghost).toHaveAttribute('aria-hidden', 'true')
    expect(ghost.nextElementSibling).toBe(screen.getByTestId('row-c')) // collapses where it stood
    const kinds = animate.mock.calls.map((c) => ((c as unknown as [Keyframe[]])[0][0] as Keyframe).opacity)
    expect(kinds.sort()).toEqual([0, 1]) // one fade-in (d), one collapse-out (b)
    finishers.forEach((f) => f())
    await vi.waitFor(() => expect(screen.queryByTestId('row-b')).toBeNull())
  })
})
