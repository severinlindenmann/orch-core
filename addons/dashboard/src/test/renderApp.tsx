import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { mockStore, resetMockStoreForTests } from '@/api/client'
import { createAppRouter } from '@/app/router'
import type { MockStore } from '@/mocks/store'

export function renderApp(path = '/', opts: { viewer?: string; setup?: (store: MockStore) => void } = {}) {
  resetMockStoreForTests()
  if (opts.viewer) mockStore.setViewer(opts.viewer)
  opts.setup?.(mockStore)
  try {
    localStorage.clear()
  } catch {
    /* jsdom */
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const user = userEvent.setup()
  const r = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={createAppRouter(path)} />
    </QueryClientProvider>,
  )
  return { ...r, user }
}
