import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { RECHECK_MAX, RECHECK_MS, useRecheck } from './WorkspaceChoice'

// Right after a hub join the dashboard is "not switched on yet" for a few seconds: the page asks again by itself.
const client = new QueryClient()
const wrap = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

test('while the dashboard is not on, the workspace list is read again on a timer, a bounded number of times', () => {
  const spy = vi.spyOn(client, 'invalidateQueries').mockResolvedValue()
  renderHook(() => useRecheck(true), { wrapper: wrap })
  vi.advanceTimersByTime(RECHECK_MS * 3)
  expect(spy).toHaveBeenCalledTimes(3)
  expect(spy).toHaveBeenCalledWith({ queryKey: ['workspaces'] })
  vi.advanceTimersByTime(RECHECK_MS * (RECHECK_MAX + 5))
  expect(spy).toHaveBeenCalledTimes(RECHECK_MAX)
})

test('nothing is polled when the page is not waiting, and leaving the page stops it', () => {
  const spy = vi.spyOn(client, 'invalidateQueries').mockResolvedValue()
  renderHook(() => useRecheck(false), { wrapper: wrap })
  vi.advanceTimersByTime(RECHECK_MS * 3)
  expect(spy).not.toHaveBeenCalled()
  const { unmount } = renderHook(() => useRecheck(true), { wrapper: wrap })
  unmount()
  vi.advanceTimersByTime(RECHECK_MS * 3)
  expect(spy).not.toHaveBeenCalled()
})
