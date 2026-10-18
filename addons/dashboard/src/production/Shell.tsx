// The production app: the mockup's shell and pages on the fetch transport (the build aliases `@/api/client` to the
// real api). This file owns what only a real host has: the login screen, the features the server lists, the query
// cache that is dropped when the session ends. Everything else is the mockup (src/app).
import { StrictMode, useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { OrbitMark } from '../brand/OrbitMark'
import { CHECKLIST_PATH, createAppRouter } from '../app/router'
import { getToken, isForeignNavigation, onSessionChange } from '../api/bootstrap'
import { setFeatures } from '../api/features'
import { session } from '../api/prodApi'
import './shell.css'

export const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, retry: false, refetchOnWindowFocus: false } } })

/** Lists the features the server serves, then draws the app. The router is built after them: its guards read them. */
function App() {
  // The tester's checklist needs no host and asks it for nothing; its link back to the dashboard is a full page load.
  const onChecklist = location.pathname === CHECKLIST_PATH
  const features = useQuery({ queryKey: ['session'], queryFn: session, enabled: !onChecklist })
  // The tester's checklist needs no host: before `orch host enable` or `orch setup` the feature list can be refused
  // (no host registry yet), and the checklist still opens, with no feature on.
  const checklistOnly = onChecklist || (!!features.error && location.pathname === CHECKLIST_PATH)
  const router = useMemo(() => {
    if (!features.data && !checklistOnly) return null
    setFeatures(features.data ?? { features: [] })
    return createAppRouter(undefined, queryClient)
  }, [features.data, checklistOnly])
  if (features.error && !checklistOnly) return <main className="prod-login" role="alert"><OrbitMark /><h1>The dashboard server did not answer</h1><p>{features.error.message}</p></main>
  if (!router) return <div aria-label="Loading session" role="status" className="prod-skeleton" />
  return <RouterProvider router={router} />
}

export function Shell() {
  const [token, setToken] = useState(getToken)
  useEffect(() => onSessionChange(() => { queryClient.clear(); setToken(getToken()) }), [])
  if (!token && isForeignNavigation()) return <main className="prod-login"><OrbitMark /><h1>This page was opened from another site.</h1><p>Open the dashboard from the terminal (<code>orch-dashboard open</code>) or a bookmark.</p></main>
  if (!token) return <main className="prod-login"><OrbitMark /><h1>Open your dashboard</h1><p>Run <code>orch dashboard</code> in your terminal.</p><p>Addon launcher: <code>python -m orch_dashboard open</code></p></main>
  return <App />
}

export function mount() {
  createRoot(document.getElementById('root')!).render(<StrictMode><QueryClientProvider client={queryClient}><Shell /></QueryClientProvider></StrictMode>)
}
