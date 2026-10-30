import './lib/zodJitless'
import { bootstrapSetup, isSetupPage } from './api/setup'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { createAppRouter } from './app/router'
import './lib/cssom'
import './styles/tokens.css'
import { applyChoice, watchSystem } from './lib/theme'

applyChoice() // the boot script did this already; this keeps the page right if it was blocked
watchSystem()

if (isSetupPage()) {
  const session = bootstrapSetup()
  void session.catch(() => {})
  void import('./setup/Setup').then(({mount}) => mount(session))
} else {
const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, retry: false, refetchOnWindowFocus: false } } })
const router = createAppRouter(undefined, queryClient)
// Dev only: the layout guard (scripts/layout-guard.mjs) moves between routes through this without reloading.
if (import.meta.env.DEV) Object.assign(window, { __orchRouter: router })

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)

}
