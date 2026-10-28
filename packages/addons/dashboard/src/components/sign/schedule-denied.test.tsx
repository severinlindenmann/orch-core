import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { spies } from '@/test/handoffHarness'
import { scheduleKey } from '@/api/schedules'
import { SignPopover } from './SignPopover'

// run 13: a schedule the host refuses as too soon says the host's own lead (the server's min_lead_s), not always "1 hour".
let s: ReturnType<typeof spies>
beforeEach(() => {
  s = spies()
})
afterEach(() => vi.restoreAllMocks())

const RAW = 'first occurrence must be after the minimum scheduling lead'
const action = { action: 'schedule_add', key: 'DEMO-0044', brief: 'b', at: '2026-10-28 09:30', tz: 'UTC' } as never

async function refused(props: { minLeadS?: number }, cached?: number) {
  const { ApiError } = await import('@/api/types')
  s.prepare.mockRejectedValue(new ApiError(400, { code: 'schedule.denied', message: `schedule.denied: ${RAW}`, retryable: false }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  if (cached !== undefined) client.setQueryData(scheduleKey('w_1'), { available: true, role: 'member', can: { add: true, cancel: true }, min_lead_s: cached, rows: [] })
  render(
    <QueryClientProvider client={client}>
      <SignPopover title="Schedule on DEMO-0044" ws="w_1" action={action} autoStart onClose={vi.fn()} {...props} />
    </QueryClientProvider>,
  )
}

it('says the lead the page passes', async () => {
  await refused({ minLeadS: 30 })
  expect(await screen.findByText('A schedule must start at least 30 seconds from now.', {}, { timeout: 5000 })).toBeInTheDocument()
  expect(document.body).not.toHaveTextContent('1 hour')
  expect(document.body).not.toHaveTextContent('minimum scheduling lead')
})

it('says the lead of the schedules list already read when the page passes none', async () => {
  await refused({}, 7200)
  expect(await screen.findByText('A schedule must start at least 2 hours from now.', {}, { timeout: 5000 })).toBeInTheDocument()
})

it('says an hour when the lead is not known', async () => {
  await refused({})
  expect(await screen.findByText('A schedule must start at least 1 hour from now.', {}, { timeout: 5000 })).toBeInTheDocument()
})
