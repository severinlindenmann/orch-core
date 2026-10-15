import { useRouterState } from '@tanstack/react-router'
import { lazy, Suspense, useEffect, useRef } from 'react'
import { useShellState } from './ShellUi'

// The overlay and its form load with the New ticket page's chunk, the first time it is opened.
const NewTicketOverlay = lazy(() => import('../pages/new-ticket/NewTicketOverlay').then((m) => ({ default: m.NewTicketOverlay })))

/** Mounts the New ticket overlay while it is open (`c`, the topbar button, the palette). A route change closes it. */
export function NewTicketHost() {
  const { newTicketOpen, setNewTicketOpen, newTicketOpener } = useShellState()
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const openedOn = useRef(pathname)
  useEffect(() => {
    if (newTicketOpen) openedOn.current = pathname
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newTicketOpen])
  useEffect(() => {
    // Reached only when the overlay let the navigation through (nothing typed, or the person discarded it).
    if (newTicketOpen && pathname !== openedOn.current) setNewTicketOpen(false)
  }, [pathname, newTicketOpen, setNewTicketOpen])
  if (!newTicketOpen) return null
  return (
    <Suspense fallback={null}>
      <NewTicketOverlay opener={newTicketOpener.current} onClose={() => setNewTicketOpen(false)} />
    </Suspense>
  )
}
