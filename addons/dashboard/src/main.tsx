import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { createAppRouter } from './app/router'
import './styles/tokens.css'

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
