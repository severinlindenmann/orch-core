import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { spies } from '@/test/handoffHarness'
import { scheduleKey } from '@/api/schedules'
import { SignPopover } from './SignPopover'
import * as gesture from './trustedPress'

// run 13: a schedule the host refuses as too soon says the host's own lead (the server's min_lead_s), not always "1 hour".
let s: ReturnType<typeof spies>
const realTrusted = gesture.trustedPress
beforeEach(() => {
  s = spies()
  // fireEvent is untrusted: every press stands in for a trusted one, judged by the real rule (a key auto-repeat still blocks it)
  vi.spyOn(gesture, 'trustedPress').mockImplementation((e) => realTrusted({ isTrusted: true, repeat: e.repeat }))
  const real = api.getWorkspaces.bind(api)
  vi.spyOn(api, 'getWorkspaces').mockImplementation(async () => {
    const [w] = await real()
    return [{ ...w!, id: 'w_1', click: { custody: 'convenience' as const, enabled: true, available: true } }]
  })
})
afterEach(() => vi.restoreAllMocks())

const RAW = 'first occurrence must be after the minimum scheduling lead'
const action = { action: 'schedule_add', key: 'DEMO-0044', brief: 'b', at: '2026-10-28 09:30', tz: 'UTC' } as never

async function refused(props: { minLeadS?: number }, cached?: number) {
  const { ApiError } = await import('@/api/types')
  const prepare = vi.spyOn(api, 'prepareClick').mockRejectedValue(new ApiError(400, { code: 'schedule.denied', message: `schedule.denied: ${RAW}`, retryable: false }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  if (cached !== undefined) client.setQueryData(scheduleKey('w_1'), { available: true, role: 'member', can: { add: true, cancel: true }, min_lead_s: cached, rows: [] })
  render(
    <QueryClientProvider client={client}>
      <SignPopover title="Schedule on DEMO-0044" ws="w_1" action={action} onClose={vi.fn()} {...props} />
    </QueryClientProvider>,
  )
  // a render grants no intent: the trusted press of the outcome button prepares, and the host refuses it
  const dialog = await screen.findByRole('dialog', {}, { timeout: 5000 })
  const outcome = await within(dialog).findByRole('button', { name: 'Schedule on DEMO-0044' }, { timeout: 5000 })
  await waitFor(() => expect(outcome).toBeEnabled(), { timeout: 5000 })
  fireEvent.click(outcome)
  await waitFor(() => expect(prepare).toHaveBeenCalledTimes(1), { timeout: 5000 })
  expect(s.postAction).not.toHaveBeenCalled()
}

it('says the lead the page passes', async () => {
  await refused({ minLeadS: 30 })
  expect(await screen.findByText(/^A schedule must start at least 30 seconds from now\./, {}, { timeout: 5000 })).toBeInTheDocument()
  expect(document.body).not.toHaveTextContent('1 hour')
  expect(document.body).not.toHaveTextContent('minimum scheduling lead')
})

it('says the lead of the schedules list already read when the page passes none', async () => {
  await refused({}, 7200)
  expect(await screen.findByText(/^A schedule must start at least 2 hours from now\./, {}, { timeout: 5000 })).toBeInTheDocument()
})

it('says an hour when the lead is not known', async () => {
  await refused({})
  expect(await screen.findByText(/^A schedule must start at least 1 hour from now\./, {}, { timeout: 5000 })).toBeInTheDocument()
})
