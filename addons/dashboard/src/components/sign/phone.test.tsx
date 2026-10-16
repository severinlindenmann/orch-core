import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { usePhone } from './usePhone'
import { ApiError } from '@/api/types'
import type { PhoneRequest } from '@/api/phone'
import { api } from '@/api/client'
import type { PhoneHint } from '@/api/phone'
import { HandoffDialog } from './HandoffDialog'
import { PhoneWaiting } from './SignMethods'

const request = { ref: 'DEMO-0044', gate: 'plan' as const, gate_gen: 7 }
const hint: PhoneHint = { ...request, id: 'ph_01JABCDEFGHJKMNPQRSTVWXYZ01', uid: 'uid', created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 600000).toISOString(), state: 'open' }
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })
beforeEach(() => {
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: ['terminal', 'phone'] } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'phone', is_default: false, allowed: ['terminal', 'phone'] })
  vi.spyOn(api, 'phoneAvailable').mockResolvedValue({ phones: 1 })
  vi.spyOn(api, 'phoneRequest').mockResolvedValue({ ...hint, phones: 1 })
  vi.spyOn(api, 'phoneStatus').mockResolvedValue({ hints: [hint] })
  vi.spyOn(api, 'phoneCancel').mockResolvedValue({ id: hint.id, state: 'cancelled' })
})
function mount(phoneRequest = request) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><HandoffDialog ws="ws" title="Approve plan" action={{ action: 'approve', key: request.ref, gate: 'plan' }} phoneRequest={phoneRequest} onClose={vi.fn()} /></QueryClientProvider>)
  return userEvent.setup()
}
it('uses the phone default and posts only the displayed gate generation, then waits', async () => {
  const user = mount()
  await user.click(await screen.findByRole('button', { name: 'Approve on your phone' }))
  await screen.findByText('Waiting for your phone…')
  expect(api.phoneRequest).toHaveBeenCalledWith('ws', { ...request, idem: expect.any(String) })
  expect(screen.getByText(/remaining/)).toBeVisible()
})
it('cancels and shows the outcome if a decision won the race', async () => {
  const user = mount()
  await user.click(await screen.findByRole('button', { name: 'Approve on your phone' }))
  await screen.findByText('Waiting for your phone…')
  vi.mocked(api.phoneStatus).mockResolvedValue({ hints: [{ ...hint, state: 'done', outcome: { kind: 'changes_requested', seq: 1, event_id: 'e', at: hint.created_at } }] })
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  await screen.findByText('Changes requested.')
  expect(api.phoneCancel).toHaveBeenCalledWith('ws', hint.id)
})
it('does not offer an enabled phone button without a paired phone', async () => {
  vi.mocked(api.phoneAvailable).mockResolvedValue({ phones: 0 })
  mount()
  await waitFor(() => expect(screen.getByRole('button', { name: /pair a phone first/ })).toBeDisabled())
  expect(api.phoneRequest).not.toHaveBeenCalled()
})
it.each(['stale', 'expired', 'cancelled', 'done'] as const)('explains %s without an auth label', state => {
  const { container } = render(<PhoneWaiting hint={{ ...hint, state, outcome: { kind: 'approved', seq: 1, event_id: 'e', at: hint.created_at } }} />)
  expect(container.textContent).not.toMatch(/verified|signed on phone|secure|trusted/i)
  expect(container.textContent).not.toContain('Waiting for your phone')
})
it('polls the host and displays an approval result', async () => {
  const user = mount()
  await user.click(await screen.findByRole('button', { name: 'Approve on your phone' }))
  await screen.findByText('Waiting for your phone…')
  vi.mocked(api.phoneStatus).mockResolvedValue({ hints: [{ ...hint, state: 'done', outcome: { kind: 'approved', seq: 1, event_id: 'e', at: hint.created_at } }] })
  await screen.findByText('Approved.', {}, { timeout: 4000 })
})
it('keeps phase two phone methods disabled', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><HandoffDialog ws="ws" title="Approve code" action={{ action: 'approve', key: request.ref, gate: 'code' }} onClose={vi.fn()} /></QueryClientProvider>)
  expect(await screen.findByRole('button', { name: /Approve on your phone · coming soon/ })).toBeDisabled()
  expect(api.phoneRequest).not.toHaveBeenCalled()
})
it('puts phone under Other ways when terminal is the default', async () => {
  vi.mocked(api.getSignDefault).mockResolvedValue({ method: 'terminal', is_default: false, allowed: ['terminal', 'phone'] })
  const user = mount()
  await user.click(await screen.findByText('Other ways'))
  const button = screen.getByRole('button', { name: 'Approve on your phone' })
  expect(button.closest('details')).not.toBeNull()
  await user.click(button)
  await screen.findByText('Waiting for your phone…')
})

function mountHook() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const hook = renderHook(() => usePhone('ws'), {
    wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  })
  return { ...hook, client }
}
async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}
it.each(['cancelled', 'expired', 'stale'] as const)('stops polling 60 seconds after %s, including ticket updates', async state => {
  vi.useFakeTimers()
  const { result, client } = mountHook()
  await act(async () => { await result.current.start(request) })
  vi.mocked(api.phoneStatus).mockResolvedValue({ hints: [{ ...hint, state }] })
  await advance(2000)
  expect(result.current.hint?.state).toBe(state)
  await advance(58_000)
  expect(api.phoneStatus).toHaveBeenCalledTimes(30)
  await advance(2000)
  const calls = vi.mocked(api.phoneStatus).mock.calls.length
  await advance(120_000)
  await act(async () => { client.setQueryData(['ticket', 'ws', request.ref], {}) })
  expect(api.phoneStatus).toHaveBeenCalledTimes(calls)
})
it.each(['cancelled', 'expired', 'stale'] as const)('accepts a late decision after %s and stops immediately on done', async state => {
  vi.useFakeTimers()
  const { result, client } = mountHook()
  await act(async () => { await result.current.start(request) })
  vi.mocked(api.phoneStatus).mockResolvedValue({ hints: [{ ...hint, state }] })
  await advance(2000)
  vi.mocked(api.phoneStatus).mockResolvedValue({ hints: [{ ...hint, state: 'done' }] })
  await advance(2000)
  expect(result.current.hint?.state).toBe('done')
  await act(async () => { client.setQueryData(['ticket', 'ws', request.ref], {}) })
  await advance(120_000)
  expect(api.phoneStatus).toHaveBeenCalledTimes(2)
})
it.each([403, 404])('stops polling with plain copy after phone.status returns %s', async status => {
  vi.useFakeTimers()
  const { result, client } = mountHook()
  await act(async () => { await result.current.start(request) })
  vi.mocked(api.phoneStatus).mockRejectedValue(new ApiError(status, { code: 'internal.code', message: 'technical detail', retryable: false }))
  await advance(2000)
  expect(result.current.error).toBe(status === 404 ? 'This phone request is no longer available.' : 'You no longer have access to this phone request.')
  await act(async () => { client.setQueryData(['ticket', 'ws', request.ref], {}) })
  await advance(120_000)
  expect(api.phoneStatus).toHaveBeenCalledTimes(1)
})
it.each<PhoneRequest>([{ ...request, gate: 'requirements' }, { ...request, gate_gen: 8 }])('uses a new idem key for a changed request: %j', async changed => {
  const { result } = mountHook()
  vi.mocked(api.phoneRequest).mockRejectedValue(new Error('Connection lost'))
  await act(async () => { await result.current.start(request) })
  await act(async () => { await result.current.start(request) })
  await act(async () => { await result.current.start(changed) })
  const calls = vi.mocked(api.phoneRequest).mock.calls
  expect(calls[1][1].idem).toBe(calls[0][1].idem)
  expect(calls[2][1].idem).not.toBe(calls[0][1].idem)
})
