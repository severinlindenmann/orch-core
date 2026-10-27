import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { spies } from '@/test/handoffHarness'
import { SignPopover } from './SignPopover'

// B11 (#14): a gate.incomplete refusal reads "Add Context, Out of scope and one acceptance check first.", never the raw
// code, and a CLI pointer ("orch describe approve") is not shown on the page.
let s: ReturnType<typeof spies>
beforeEach(() => {
  s = spies()
})
afterEach(() => vi.restoreAllMocks())

it('says what an incomplete gate lacks in words and never shows orch describe', async () => {
  const { ApiError } = await import('@/api/types')
  s.prepare.mockRejectedValue(
    new ApiError(409, { code: 'gate.incomplete', message: 'missing: context, out_of_scope, acceptance', fix: 'orch describe approve', retryable: false }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <SignPopover title="Approve requirements" ws="w_1" action={{ action: 'approve', key: 'DEMO-0044', gate: 'requirements' } as never} onClose={vi.fn()} />
    </QueryClientProvider>,
  )
  expect(await screen.findByText('Add Context, Out of scope and one acceptance check first.', {}, { timeout: 5000 })).toBeInTheDocument()
  expect(document.body).not.toHaveTextContent('orch describe')
  expect(document.body).not.toHaveTextContent('gate.incomplete')
})
