import { act, cleanup, render, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import { api } from '@/api/client'
import { CLICK_RISK } from '@/api/click'
import { HoldToasts, trackHold } from './holdToasts'
import { useClickSign } from './useClickSign'

const mocks = vi.hoisted(() => ({ dismiss: vi.fn(), loading: vi.fn(), success: vi.fn(), info: vi.fn(), error: vi.fn() }))
vi.mock('sonner', () => ({ toast: mocks }))
const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
beforeEach(() => { localStorage.clear(); vi.clearAllMocks() })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('sends the unsigned hold to the signer and leaving does not cancel it', async () => {
  vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: 'r1', op: 'log', confirm: 'click', expires_at: new Date(Date.now() + 600000).toISOString() })
  vi.spyOn(api, 'viewClick').mockResolvedValue({ view_digest: 'digest', view: { risk: CLICK_RISK, op: 'log', hold_allowed: true, workspace: { id: 'w1', name: 'Demo', prefix: 'DEMO' }, fingerprint: 'x', lines: [], checks: [], confirm: { kind: 'click' }, expires_at: '', auth: 'convenience', device: 'd1', artifacts: null } })
  const sign = vi.spyOn(api, 'signClick').mockResolvedValue({ state: 'held', deadline_ms: Date.now() + 10000, duplicate: false })
  const cancel = vi.spyOn(api, 'cancelClick')
  const done = vi.fn()
  const { result, unmount } = renderHook(() => useClickSign('w1', done), { wrapper })
  await act(() => result.current.start({ action: 'comment', key: 'DEMO-0001', message: 'Hi' }))
  await act(() => result.current.sign())
  expect(sign).toHaveBeenCalledWith('w1', 'r1', 'digest', undefined, undefined, 10)
  expect(result.current.phase).toBe('held')
  expect(done).not.toHaveBeenCalled()
  unmount()
  expect(cancel).not.toHaveBeenCalled()
  expect(mocks.loading).toHaveBeenCalledWith('Sending in 10 s; closing this page still sends', expect.anything())
  expect(JSON.parse(localStorage.getItem('orch-unsigned-holds')!)).toEqual([{ ws: 'w1', id: 'r1', deadline: expect.any(Number) }])
  vi.spyOn(api, 'stateClick').mockResolvedValue({ id: 'r1', state: 'submitted' })
  render(<HoldToasts />, { wrapper })
  await waitFor(() => expect(done).toHaveBeenCalledOnce())
})

it('Undo calls cancel and only reports not sent after the signer confirms', async () => {
  const cancel = vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: 'r2', state: 'cancelled' })
  trackHold('w2', 'r2', Date.now() / 1000 + 10)
  mocks.loading.mock.lastCall![1].action.onClick()
  expect(cancel).toHaveBeenCalledWith('w2', 'r2')
  await waitFor(() => expect(mocks.info).toHaveBeenCalledWith('Not sent', expect.anything()))
})

it('reconnect reports a hold lost on signer restart as not sent', async () => {
  localStorage.setItem('orch-unsigned-holds', JSON.stringify([{ ws: 'w3', id: 'r3', deadline: 1 }]))
  vi.spyOn(api, 'stateClick').mockResolvedValue({ id: 'r3', state: 'not_sent' })
  render(<HoldToasts />, { wrapper })
  await waitFor(() => expect(mocks.info).toHaveBeenCalledWith('Not sent', expect.anything()))
  expect(JSON.parse(localStorage.getItem('orch-unsigned-holds')!)).toEqual([])
})
