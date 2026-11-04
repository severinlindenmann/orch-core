import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { StepUpModal } from './StepUpModal'
import { requestStepUp, settleStepUp } from '../api/stepUp'

let current = 'switch-token'
vi.mock('../api/bootstrap', () => ({ getToken: () => current }))
const fetchSpy = vi.fn()

afterEach(() => { current = 'switch-token'; cleanup(); settleStepUp(false); vi.clearAllMocks(); vi.unstubAllGlobals() })

it('asks to confirm on this computer; a read-mostly session has no way up of its own', async () => {
  vi.stubGlobal('fetch', fetchSpy)
  render(<StepUpModal />)
  expect(screen.queryByTestId('stepup-modal')).toBeNull()
  let result: Promise<boolean> = Promise.resolve(true)
  act(() => { result = requestStepUp({ where: 'this_computer', message: 'Confirm it\'s you on this computer to make this change.' }) })
  expect(screen.getByRole('dialog').textContent).toContain("Confirm it's you on this computer")
  await act(async () => { fireEvent.click(screen.getByTestId('stepup-retry')) })
  expect(screen.getByRole('alert').textContent).toContain('not confirmed yet') // still the hub's read-mostly session
  expect(fetchSpy).not.toHaveBeenCalled() // no upgrade request: E4's proof before the redeem is the only way to full
  await act(async () => { fireEvent.click(screen.getByText('Not now')) })
  expect(await result).toBe(false)
})

it('a session from another computer is told where the change can be made, with no retry', async () => {
  render(<StepUpModal />)
  let result: Promise<boolean> = Promise.resolve(true)
  act(() => { result = requestStepUp({ where: 'host', message: 'Changes can be made on the computer this workspace runs on.' }) })
  expect(screen.queryByTestId('stepup-retry')).toBeNull()
  await act(async () => { fireEvent.click(screen.getByText('Close')) })
  expect(await result).toBe(false)
})

it('adopts the new token a launch link gave this tab: Try again runs the action', async () => {
  vi.stubGlobal('fetch', fetchSpy)
  render(<StepUpModal />)
  let result: Promise<boolean> = Promise.resolve(false)
  act(() => { result = requestStepUp({ where: 'this_computer', message: 'x' }) })
  current = 'local-token' // the person opened the terminal's link; the tab got its session
  await act(async () => { fireEvent.click(screen.getByTestId('stepup-retry')) })
  expect(await result).toBe(true)
  expect(screen.queryByTestId('stepup-modal')).toBeNull()
  expect(fetchSpy).not.toHaveBeenCalled()
})
