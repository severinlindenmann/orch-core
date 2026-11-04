import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import * as gesture from '@/components/sign/trustedPress'
import { PressAgainDelete } from './PressAgainDelete'
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })
function mount() {
  vi.useFakeTimers()
  // jsdom cannot create trusted browser events. Positive cases inject the boundary;
  // the synthetic case below exercises the real guard.
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
  const remove = vi.fn()
  render(<PressAgainDelete onDelete={remove} />)
  return { remove, button: screen.getByRole('button') }
}
it('one press does not delete; a second within five seconds deletes once', () => {
  const { remove, button } = mount()
  fireEvent.click(button)
  expect(remove).not.toHaveBeenCalled()
  expect(button).toHaveTextContent('Press again to delete')
  act(() => vi.advanceTimersByTime(4000))
  fireEvent.click(button)
  expect(remove).toHaveBeenCalledTimes(1)
})
it('two presses six seconds apart do not delete', () => {
  const { remove, button } = mount()
  fireEvent.click(button)
  act(() => vi.advanceTimersByTime(6000))
  expect(button).toHaveTextContent('Delete work folder')
  fireEvent.click(button)
  expect(remove).not.toHaveBeenCalled()
})
it('moving away resets the second press', () => {
  const { remove, button } = mount()
  fireEvent.click(button)
  fireEvent.blur(button)
  fireEvent.click(button)
  expect(remove).not.toHaveBeenCalled()
})
it('synthetic presses never arm or delete', () => {
  const remove = vi.fn()
  render(<PressAgainDelete onDelete={remove} />)
  fireEvent.click(screen.getByRole('button'))
  fireEvent.click(screen.getByRole('button'))
  expect(remove).not.toHaveBeenCalled()
  expect(screen.getByRole('button')).toHaveTextContent('Delete work folder')
})
