import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import * as gesture from './trustedPress'
import { SignPopover } from './SignPopover'
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspaces: [{ id: 'w_1', prefix: 'DEMO', name: 'Demo', click: { enabled: true, available: true, custody: 'convenience' } }] }) }))
vi.mock('@/app/useMe', () => ({ useMe: () => ({ data: { person: 'p_test' } }) }))
vi.mock('@tanstack/react-router', () => ({ useRouter: () => null, Link: () => null }))

// B11 (#14): a gate.incomplete refusal reads "Add Context, Out of scope and one acceptance check first.", never the raw
// code, and a CLI pointer ("orch describe approve") is not shown on the page.
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('says what an incomplete gate lacks in words and never shows orch describe', async () => {
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([])
  const prepare = vi.spyOn(api, 'prepareClick').mockRejectedValue(
    new ApiError(409, { code: 'gate.incomplete', message: 'missing: context, out_of_scope, acceptance', fix: 'orch describe approve', retryable: false }),
  )
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <SignPopover title="Approve requirements" ws="w_1" action={{ action: 'approve', key: 'DEMO-0044', gate: 'requirements' } as never} inline onClose={vi.fn()} />
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Sign requirements' }))
  expect(await screen.findByText('Add Context, Out of scope and one acceptance check first.', {}, { timeout: 5000 })).toBeInTheDocument()
  expect(prepare).toHaveBeenCalledTimes(1)
  expect(document.body).not.toHaveTextContent('orch describe')
  expect(document.body).not.toHaveTextContent('gate.incomplete')
})
