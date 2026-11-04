import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { toast } from 'sonner'
import { expect, it, vi } from 'vitest'
import { queryClient } from '@/production/Shell'
import type { TicketDocument } from '@/api/types'
import { HandoffSignDialog } from '@/app/pages/ticket/HandoffSignDialog'
import type { HumanAction } from '@/app/pages/ticket/shared'
import { DO, READS, clickOn, trustedPresses, useFakeHost, workspace } from '@/test/hostHarness'

// P25/W03: every ticket action click ends in exactly one visible confirmation, in the decided words.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@tanstack/react-router', async (orig) => ({ ...(await orig<typeof import('@tanstack/react-router')>()), useNavigate: () => vi.fn(), useRouter: () => null }))
vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, ...DO, 'actions', 'click'] })])
const ticket = { key: 'DEMO-0001', title: 'T', head: { seq: 7 }, gates: { plan: { hash: null }, requirements: { hash: null }, code: { hash: null }, verify: { hash: null } }, questions_state: [], acceptance_state: [], tasks_state: [], artifacts: [], branch: {} } as unknown as TicketDocument

async function run(action: HumanAction) {
  clickOn(state.host.workspaces[0])
  trustedPresses()
  vi.mocked(toast.success).mockClear()
  render(
    <QueryClientProvider client={queryClient}>
      <HandoffSignDialog ticket={ticket} action={action} ws={A} onClose={() => {}} />
    </QueryClientProvider>,
  )
}
const toasts = () => vi.mocked(toast.success).mock.calls
const signed = (n = 1) => waitFor(() => expect(state.host.clickSigned).toHaveLength(n), { timeout: 8000 })

it('approve both: exactly one toast, with a View plan action', async () => {
  await run({ kind: 'approve_both', pressed: true })
  await signed()
  await waitFor(() => expect(toasts()).toHaveLength(1), T)
  expect(toasts()[0][0]).toBe('You approved the requirements and plan')
  expect((toasts()[0][1] as { action: { label: string } }).action.label).toBe('View plan')
})
it('approve plan: "You approved the plan" with View plan', async () => {
  await run({ kind: 'approve', gate: 'plan', pressed: true })
  await signed()
  await waitFor(() => expect(toasts()).toHaveLength(1), T)
  expect(toasts()[0][0]).toBe('You approved the plan')
  expect((toasts()[0][1] as { action: { label: string } }).action.label).toBe('View plan')
})
it('approve requirements and code: their own words, no action', async () => {
  await run({ kind: 'approve', gate: 'requirements', pressed: true })
  await signed()
  await waitFor(() => expect(toasts()).toHaveLength(1), T)
  expect(toasts()[0][0]).toBe('You approved the requirements')
  expect(toasts()[0][1]).toBeUndefined()
})
it('answer: "Answer sent"', async () => {
  await run({ kind: 'answer', question: 'Q1', option: 'a' })
  const b = await screen.findByRole('button', { name: 'Send answer' }, T)
  b.click()
  await signed()
  await waitFor(() => expect(toasts()).toHaveLength(1), T)
  expect(toasts()[0][0]).toBe('Answer sent')
})
it('a refused sign shows no success toast', async () => {
  const orig = state.host.fetch
  vi.stubGlobal('fetch', vi.fn(async (u: RequestInfo | URL, i?: RequestInit) => (String(u).endsWith('/click/sign') ? new Response(JSON.stringify({ ok: false, error: { code: 'human.click_refused', message: 'no', retryable: false } }), { status: 409, headers: { 'content-type': 'application/json' } }) : orig(String(u), i))))
  await run({ kind: 'approve', gate: 'plan', pressed: true })
  await new Promise((r) => setTimeout(r, 2500))
  expect(toasts()).toHaveLength(0)
})
