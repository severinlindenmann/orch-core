import { useEffect, useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { prefersReducedMotion } from '@/lib/motion'

/**
 * Height transition for a panel that opens and closes. Closed content is not rendered (it unmounts once the close
 * transition has ended); under reduced motion it opens and closes at once. The id goes on the outer element so
 * aria-controls points at something that exists while open.
 */
export function Collapse({ open, id, className, children }: { open: boolean; id?: string; className?: string; children: ReactNode }) {
  const instant = prefersReducedMotion()
  const [mounted, setMounted] = useState(open)
  const [expanded, setExpanded] = useState(open)
  // Once fully open the clip is lifted, so focus rings at the edge are not cut off.
  const [settled, setSettled] = useState(open)
  if (open && !mounted) setMounted(true)
  useEffect(() => {
    setSettled(false)
    if (open) {
      const raf = requestAnimationFrame(() => setExpanded(true))
      return () => cancelAnimationFrame(raf)
    }
    setExpanded(false)
    // If no transition ends (hidden tab, interrupted), still unmount.
    const t = setTimeout(() => setMounted(false), 260)
    return () => clearTimeout(t)
  }, [open])
  if (instant ? !open : !mounted) return null
  return (
    <div
      id={id}
      className={cn('grid transition-[grid-template-rows] duration-200 ease-out', className)}
      style={{ gridTemplateRows: (instant ? open : expanded && open) ? '1fr' : '0fr' }}
      onTransitionEnd={(e) => {
        if (e.target !== e.currentTarget) return
        if (open) setSettled(true)
        else setMounted(false)
      }}
    >
      <div className={cn('min-h-0', !(settled || instant) && 'overflow-hidden')}>{children}</div>
    </div>
  )
}
