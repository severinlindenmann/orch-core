import { act, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { setClock } from '@/lib/time'
import { Ago } from '../ticket/shared'

afterEach(() => vi.useRealTimers())

it('a list time counts up on the shared clock without a reload (P12)', () => {
  vi.useFakeTimers()
  let now = Date.parse('2026-10-29T12:00:30Z')
  setClock(() => now)
  render(<span data-testid="t"><Ago at="2026-10-29T12:00:00Z" /></span>)
  const before = screen.getByTestId('t').textContent
  act(() => { now += 3 * 60_000; vi.advanceTimersByTime(3 * 60_000) })
  expect(screen.getByTestId('t').textContent).not.toBe(before)
})
