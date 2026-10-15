import { useRouterState } from '@tanstack/react-router'
import { useEffect, useRef } from 'react'
import { toast } from 'sonner'
import { Toaster } from '@/components/ui/sonner'
import { useShellState } from './ShellUi'

const GUTTER = 16
const WIDE_RAIL = 232
const NARROW_RAIL = 56

/**
 * Toasts confirm the person's own successful action, quietly: bottom-left, at most two, gone after 6 s, and gone as
 * soon as the route changes (errors stay until dismissed). They start right of the sidebar so they never cover the rail, whatever its width.
 */
export function ShellToaster() {
  const path = useRouterState({ select: (s) => s.resolvedLocation?.pathname ?? s.location.pathname })
  const last = useRef(path)
  useEffect(() => {
    if (last.current === path) return
    last.current = path
    // Confirmations go; a failure stays until dismissed, and a pending sign (loading) is not cut off.
    for (const t of toast.getToasts()) if ('type' in t && t.type !== 'error' && t.type !== 'loading') toast.dismiss(t.id)
  }, [path])

  const { railCollapsed } = useShellState()
  const rail = railCollapsed ? NARROW_RAIL : WIDE_RAIL

  return <Toaster position="bottom-left" visibleToasts={2} duration={6000} offset={{ left: rail + GUTTER, bottom: `calc(var(--dock-bottom, 0px) + ${GUTTER}px)` }} />
}
