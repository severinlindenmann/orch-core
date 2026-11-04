import { act, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { setClock } from '@/lib/time'
import { ClosedNotice } from './Header'

afterEach(() => vi.useRealTimers())

it('the closed notice time counts up on the shared clock without a reload', () => {
  vi.useFakeTimers()
  let now = Date.parse('2026-10-29T12:00:30Z')
  setClock(() => now)
  render(<ClosedNotice closed={{ resolution: 'obsolete', at: '2026-10-29T12:00:00Z' } as never} name={(id) => id} />)
  const before = screen.getByTestId('closed-notice').textContent
  act(() => { now += 10 * 60_000; vi.advanceTimersByTime(10 * 60_000) })
  const after = screen.getByTestId('closed-notice').textContent
  expect(after).not.toBe(before)
  expect(after).toMatch(/1\d min ago|10 min ago/)
})
