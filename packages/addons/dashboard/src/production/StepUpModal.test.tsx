import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { StepUpModal } from './StepUpModal'
import { requestStepUp, settleStepUp } from '../api/stepUp'

vi.mock('../api/trustedKey', () => ({ upgradeSession: vi.fn() }))
vi.mock('../api/bootstrap', () => ({ getToken: () => 'switch-token', adoptToken: vi.fn() }))
import { upgradeSession } from '../api/trustedKey'
import { adoptToken } from '../api/bootstrap'

afterEach(() => { cleanup(); settleStepUp(false); vi.clearAllMocks() })

it('asks to confirm on this computer, and a confirmed browser resumes the action', async () => {
  vi.mocked(upgradeSession).mockResolvedValueOnce(null).mockResolvedValueOnce('full')
  render(<StepUpModal />)
  expect(screen.queryByTestId('stepup-modal')).toBeNull()
  let result: Promise<boolean> = Promise.resolve(false)
  act(() => { result = requestStepUp({ where: 'this_computer', message: 'Confirm it\'s you on this computer to make this change.' }) })
  expect(screen.getByRole('dialog').textContent).toContain("Confirm it's you on this computer")
  await act(async () => { fireEvent.click(screen.getByTestId('stepup-retry')) })
  expect(screen.getByRole('alert').textContent).toContain('not confirmed yet') // no key yet: stays read-mostly
  await act(async () => { fireEvent.click(screen.getByTestId('stepup-retry')) })
  expect(adoptToken).toHaveBeenCalledWith('full')
  expect(await result).toBe(true)
  expect(screen.queryByTestId('stepup-modal')).toBeNull()
})

it('a session from another computer is told where the change can be made, with no retry', async () => {
  render(<StepUpModal />)
  let result: Promise<boolean> = Promise.resolve(true)
  act(() => { result = requestStepUp({ where: 'host', message: 'Changes can be made on the computer this workspace runs on.' }) })
  expect(screen.queryByTestId('stepup-retry')).toBeNull()
  await act(async () => { fireEvent.click(screen.getByText('Close')) })
  expect(await result).toBe(false)
})
