import { useRouterState } from '@tanstack/react-router'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { Toaster } from '@/components/ui/sonner'

const GUTTER = 16
const WIDE_RAIL = 232
const NARROW_RAIL = 56

/**
 * Toasts confirm the person's own successful action, quietly: bottom-left, at most two, gone after 6 s, and gone as
 * soon as the route changes. They start right of the sidebar so they never cover the rail, whatever its width.
 */
export function ShellToaster() {
  const path = useRouterState({ select: (s) => s.resolvedLocation?.pathname ?? s.location.pathname })
  const last = useRef(path)
  useEffect(() => {
    if (last.current === path) return
    last.current = path
    toast.dismiss()
  }, [path])

  const [rail, setRail] = useState(WIDE_RAIL)
  useEffect(() => {
    const aside = document.querySelector('aside[data-collapsed]')
    if (!aside) return
    const read = () => setRail(aside.getAttribute('data-collapsed') === 'true' ? NARROW_RAIL : WIDE_RAIL)
    read()
    const mo = new MutationObserver(read)
    mo.observe(aside, { attributes: true, attributeFilter: ['data-collapsed'] })
    return () => mo.disconnect()
  }, [])

  return <Toaster position="bottom-left" visibleToasts={2} duration={6000} offset={{ left: rail + GUTTER, bottom: GUTTER }} />
}
