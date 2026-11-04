import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { agentsApi, RUNNER_LINE } from '@/api/agents'
import { ApiError } from '@/api/types'
import * as gesture from './sign/trustedPress'
import { RunnerSwitch } from './RunnerSwitch'

const D1 = 'sha256:' + '1'.repeat(64)
const D2 = 'sha256:' + '2'.repeat(64)
const review = (on: boolean, digest = D1, lines?: string[], terminals = false) => ({
  view: { op: on ? 'runner.on' : 'runner.off', on, terminals, lines: lines ?? (on ? ['Turn on dashboard execution for Demo.', RUNNER_LINE, 'It can read your keys.'] : ['Turn off dashboard execution for Demo.']) },
  view_digest: digest,
})
const mount = (on: boolean, initialTerminals = false) => render(<QueryClientProvider client={new QueryClient()}><RunnerSwitch ws="w1" on={on} initialTerminals={initialTerminals} /></QueryClientProvider>)
beforeEach(() => { vi.spyOn(document, 'hasFocus').mockReturnValue(true); vi.spyOn(gesture, 'trustedPress').mockReturnValue(true) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('shows core\'s review first and switches on only that review\'s digest', async () => {
  const user = userEvent.setup()
  const rv = vi.spyOn(agentsApi, 'runnerReview').mockResolvedValue(review(true))
  const sw = vi.spyOn(agentsApi, 'runnerSwitch').mockResolvedValue({ enabled: true, terminals: true })
  mount(true)
  await user.click(screen.getByTestId('runner-switch'))
  expect(await screen.findByText(RUNNER_LINE)).toBeInTheDocument()
  expect(sw).not.toHaveBeenCalled() // reading is not switching
  await waitFor(() => expect(screen.getByTestId('runner-switch-confirm')).toBeEnabled(), { timeout: 2000 })
  await user.click(screen.getByTestId('runner-switch-confirm'))
  await waitFor(() => expect(sw).toHaveBeenCalledWith('w1', { on: true, terminals: false, view_digest: D1 }))
  expect(rv).toHaveBeenCalledWith('w1', { on: true, terminals: false })
})

it('checking terminals reads the review again and sends the new digest', async () => {
  const user = userEvent.setup()
  const rv = vi.spyOn(agentsApi, 'runnerReview').mockResolvedValueOnce(review(true)).mockResolvedValueOnce(review(true, D2, undefined, true))
  const sw = vi.spyOn(agentsApi, 'runnerSwitch').mockResolvedValue({ enabled: true, terminals: true })
  mount(true)
  await user.click(screen.getByTestId('runner-switch'))
  await screen.findByText(RUNNER_LINE)
  await user.click(screen.getByTestId('runner-switch-terminals'))
  await waitFor(() => expect(rv).toHaveBeenLastCalledWith('w1', { on: true, terminals: true }))
  await waitFor(() => expect(screen.getByTestId('runner-switch-confirm')).toBeEnabled())
  await waitFor(() => expect(screen.getByTestId('runner-switch-confirm')).toBeEnabled(), { timeout: 2000 })
  await user.click(screen.getByTestId('runner-switch-confirm'))
  await waitFor(() => expect(sw).toHaveBeenCalledWith('w1', { on: true, terminals: true, view_digest: D2 }))
})

it('a review of turning it on that lacks core\'s sentence is not shown and cannot be confirmed', async () => {
  const user = userEvent.setup()
  vi.spyOn(agentsApi, 'runnerReview').mockResolvedValue(review(true, D1, ['Turn on something.']))
  const sw = vi.spyOn(agentsApi, 'runnerSwitch')
  mount(true)
  await user.click(screen.getByTestId('runner-switch'))
  expect(await screen.findByTestId('runner-switch-error')).toHaveTextContent('Nothing changed')
  expect(screen.getByTestId('runner-switch-confirm')).toBeDisabled()
  expect(sw).not.toHaveBeenCalled()
})

it('a remote session is told why turning it on is refused', async () => {
  const user = userEvent.setup()
  vi.spyOn(agentsApi, 'runnerReview').mockRejectedValue(new ApiError(409, { code: 'human.click_refused', reason: 'check_failed', message: 'Turning on dashboard execution needs a dashboard opened on this computer.', retryable: false }))
  mount(true)
  await user.click(screen.getByTestId('runner-switch'))
  expect(await screen.findByTestId('runner-switch-error')).toHaveTextContent('opened on this computer')
})

it('turning it off is the same two steps without the terminals choice', async () => {
  const user = userEvent.setup()
  vi.spyOn(agentsApi, 'runnerReview').mockResolvedValue(review(false))
  const sw = vi.spyOn(agentsApi, 'runnerSwitch').mockResolvedValue({ enabled: false, terminals: false })
  mount(false)
  await user.click(screen.getByTestId('runner-switch'))
  await screen.findByText('Turn off dashboard execution for Demo.')
  expect(screen.queryByTestId('runner-switch-terminals')).toBeNull()
  await waitFor(() => expect(screen.getByTestId('runner-switch-confirm')).toBeEnabled(), { timeout: 2000 })
  await user.click(screen.getByTestId('runner-switch-confirm'))
  await waitFor(() => expect(sw).toHaveBeenCalledWith('w1', { on: false, terminals: false, view_digest: D1 }))
})

it('initialTerminals: the box is ticked, the review is read with terminals, and that is what is switched', async () => {
  const user = userEvent.setup()
  const rv = vi.spyOn(agentsApi, 'runnerReview').mockResolvedValue(review(true, D2, undefined, true))
  const sw = vi.spyOn(agentsApi, 'runnerSwitch').mockResolvedValue({ enabled: true, terminals: true })
  mount(true, true)
  await user.click(screen.getByTestId('runner-switch'))
  await screen.findByText(RUNNER_LINE)
  expect(screen.getByTestId('runner-switch-terminals')).toBeChecked()
  expect(rv).toHaveBeenCalledWith('w1', { on: true, terminals: true })
  await waitFor(() => expect(screen.getByTestId('runner-switch-confirm')).toBeEnabled(), { timeout: 2000 })
  await user.click(screen.getByTestId('runner-switch-confirm'))
  await waitFor(() => expect(sw).toHaveBeenCalledWith('w1', { on: true, terminals: true, view_digest: D2 }))
})

it('the box change re-reads the review and Confirm stays disabled until the new review is there', async () => {
  const user = userEvent.setup()
  let release: (v: ReturnType<typeof review>) => void = () => {}
  const rv = vi.spyOn(agentsApi, 'runnerReview').mockResolvedValueOnce(review(true)).mockImplementationOnce(() => new Promise((r) => { release = r }))
  const sw = vi.spyOn(agentsApi, 'runnerSwitch').mockResolvedValue({ enabled: true, terminals: true })
  mount(true)
  await user.click(screen.getByTestId('runner-switch'))
  await screen.findByText(RUNNER_LINE)
  await user.click(screen.getByTestId('runner-switch-terminals'))
  await waitFor(() => expect(rv).toHaveBeenCalledTimes(2))
  expect(screen.getByTestId('runner-switch-confirm')).toBeDisabled() // the old review is gone while the new one is read
  release(review(true, D2, undefined, true))
  await waitFor(() => expect(screen.getByTestId('runner-switch-confirm')).toBeEnabled())
  await waitFor(() => expect(screen.getByTestId('runner-switch-confirm')).toBeEnabled(), { timeout: 2000 })
  await user.click(screen.getByTestId('runner-switch-confirm'))
  await waitFor(() => expect(sw).toHaveBeenCalledWith('w1', { on: true, terminals: true, view_digest: D2 }))
})

it('a failed re-read shows the error, keeps Confirm disabled and switches nothing', async () => {
  const user = userEvent.setup()
  vi.spyOn(agentsApi, 'runnerReview').mockResolvedValueOnce(review(true)).mockRejectedValueOnce(new ApiError(502, { code: 'host.unavailable', message: 'The signing helper did not answer. Try again.', retryable: true }))
  const sw = vi.spyOn(agentsApi, 'runnerSwitch')
  mount(true)
  await user.click(screen.getByTestId('runner-switch'))
  await screen.findByText(RUNNER_LINE)
  await user.click(screen.getByTestId('runner-switch-terminals'))
  expect(await screen.findByTestId('runner-switch-error')).toHaveTextContent('did not answer')
  expect(screen.getByTestId('runner-switch-confirm')).toBeDisabled()
  expect(sw).not.toHaveBeenCalled()
})

it('a review for other terminals than asked is not shown', async () => {
  const user = userEvent.setup()
  vi.spyOn(agentsApi, 'runnerReview').mockResolvedValue(review(true, D1, undefined, true)) // asked for none
  mount(true)
  await user.click(screen.getByTestId('runner-switch'))
  expect(await screen.findByTestId('runner-switch-error')).toHaveTextContent('Nothing changed')
})

it('Undo review does not switch on until a fresh trusted press after the dwell', async () => {
  const user = userEvent.setup()
  vi.spyOn(agentsApi, 'runnerReview').mockResolvedValue(review(true))
  const sw = vi.spyOn(agentsApi, 'runnerSwitch').mockResolvedValue({ enabled: true, terminals: false })
  render(<QueryClientProvider client={new QueryClient()}><RunnerSwitch ws="w1" on initiallyOpen /></QueryClientProvider>)
  await screen.findByText(RUNNER_LINE)
  expect(screen.getByTestId('runner-switch-confirm')).toBeDisabled()
  await waitFor(() => expect(screen.getByTestId('runner-switch-confirm')).toBeEnabled(), { timeout: 2000 })
  expect(sw).not.toHaveBeenCalled()
  vi.mocked(gesture.trustedPress).mockReturnValue(false)
  await user.click(screen.getByTestId('runner-switch-confirm'))
  expect(sw).not.toHaveBeenCalled()
  vi.mocked(gesture.trustedPress).mockReturnValue(true)
  await user.click(screen.getByTestId('runner-switch-confirm'))
  await waitFor(() => expect(sw).toHaveBeenCalledOnce())
})
