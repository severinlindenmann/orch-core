// The production app: the mockup's shell and pages on the fetch transport (the build aliases `@/api/client` to the
// real api). This file owns what only a real host has: the login screen, the features the server lists, the query
// cache that is dropped when the session ends. Everything else is the mockup (src/app).
import { StrictMode, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { OrbitMark } from '../brand/OrbitMark'
import { CHECKLIST_PATH, createAppRouter } from '../app/router'
import { getToken, isForeignNavigation, onSessionChange } from '../api/bootstrap'
import { setFeatures } from '../api/features'
import { onWorkspaceNotReady, session } from '../api/prodApi'
import { StepUpModal } from './StepUpModal'
import './shell.css'

export const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, retry: false, refetchOnWindowFocus: false } } })

/**
 * How long the first screen may wait for the server before it says so and offers "Try again". The boot screen is never
 * a place to wait for good: within this time the person sees the app, the session-ended screen, or an error with a
 * button (`boot-never-stuck.test.tsx` holds it). The wait goes on behind the notice: an answer still opens the app.
 */
export const START_PATIENCE_MS = 10_000

function useElapsed(waiting: boolean, ms: number): boolean {
  const [late, setLate] = useState(false)
  useEffect(() => {
    if (!waiting) return setLate(false)
    const t = setTimeout(() => setLate(true), ms)
    return () => clearTimeout(t)
  }, [waiting, ms])
  return waiting && late
}

/** Lists the features the server serves, then draws the app. The router is built after them: its guards read them. */
function App() {
  // The tester's checklist needs no host and asks it for nothing; its link back to the dashboard is a full page load.
  const onChecklist = location.pathname === CHECKLIST_PATH
  const features = useQuery({ queryKey: ['session'], queryFn: ({ signal }) => session(signal), enabled: !onChecklist })
  // The tester's checklist needs no host: before `orch host enable` or `orch setup` the feature list can be refused
  // (no host registry yet), and the checklist still opens, with no feature on.
  const checklistOnly = onChecklist || (!!features.error && location.pathname === CHECKLIST_PATH)
  const router = useMemo(() => {
    if (!features.data && !checklistOnly) return null
    setFeatures(features.data ?? { features: [] })
    return createAppRouter(undefined, queryClient)
  }, [features.data, checklistOnly])
  const slow = useElapsed(!router && !features.error, START_PATIENCE_MS)
  const again = () => { void queryClient.cancelQueries({ queryKey: ['session'] }).then(() => features.refetch()) }
  if (features.error && !checklistOnly) return <main className="prod-login" role="alert"><OrbitMark /><h1>The dashboard server did not answer</h1><p>{features.error.message}</p><p><button type="button" className="prod-retry" onClick={again}>Try again</button></p></main>
  if (!router && slow) return <main className="prod-login" role="alert"><OrbitMark /><h1>The dashboard is slow to answer</h1><p>Your computer or the orch host may be busy. This page keeps trying; you can also ask again now.</p><p><button type="button" className="prod-retry" onClick={again}>Try again</button></p></main>
  if (!router) return <div aria-label="Loading session" role="status" className="prod-skeleton" />
  return <RouterProvider router={router} />
}

export function Shell() {
  // A workspace read refused because its dashboard add-on went off or paused after the list was read: read the list again.
  useEffect(() => onWorkspaceNotReady(() => void queryClient.invalidateQueries({ queryKey: ['workspaces'] })), [])
  const [token, setToken] = useState(getToken)
  const hadSession = useRef(token !== null)
  if (token) hadSession.current = true
  useEffect(() => onSessionChange(() => { queryClient.clear(); setToken(getToken()) }), [])
  if (!token && isForeignNavigation()) return <main className="prod-login"><OrbitMark /><h1>This page was opened from another site.</h1><p>Open the dashboard from the terminal (<code>orch-dashboard open</code>) or a bookmark.</p></main>
  if (!token && hadSession.current) return <main className="prod-login" role="alert"><OrbitMark /><h1>Your dashboard session ended</h1><p>Run <code>orch dashboard open</code> in your terminal to open it again.</p></main>
  if (!token) return <main className="prod-login"><OrbitMark /><h1>Open your dashboard</h1><p>Run <code>orch dashboard</code> in your terminal.</p><p>Addon launcher: <code>python -m orch_dashboard open</code></p></main>
  return <><App /><StepUpModal /></>
}

export function mount() {
  createRoot(document.getElementById('root')!).render(<StrictMode><QueryClientProvider client={queryClient}><Shell /></QueryClientProvider></StrictMode>)
}
