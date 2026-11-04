import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { expect, it, vi } from 'vitest'
import { useLiveUpdates } from './useLiveUpdates'

vi.mock('./bootstrap', () => ({ getToken: () => 'tok', clearSession: vi.fn() }))

// B7: a tab that was hidden (pushes throttled) reads its data again the moment it is visible.
it('reads the page data again when the tab becomes visible or the network returns', () => {
  vi.stubGlobal('fetch', () => new Promise(() => {}))
  const client = new QueryClient()
  const spy = vi.spyOn(client, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const view = renderHook(() => useLiveUpdates('ws-1'), { wrapper })
  spy.mockClear()
  vi.useFakeTimers()
  document.dispatchEvent(new Event('visibilitychange'))
  window.dispatchEvent(new Event('online')) // fires together with the first: one read, ~1 s later
  expect(spy).toHaveBeenCalledTimes(0)
  vi.advanceTimersByTime(1000)
  expect(spy).toHaveBeenCalledTimes(1)
  view.unmount()
  window.dispatchEvent(new Event('online'))
  vi.advanceTimersByTime(2000)
  expect(spy).toHaveBeenCalledTimes(1)
  vi.useRealTimers()
  vi.unstubAllGlobals()
})
