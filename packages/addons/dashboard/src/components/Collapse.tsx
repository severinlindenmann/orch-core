import { useEffect, useRef, useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { prefersReducedMotion } from '@/lib/motion'

/**
 * Height transition for a panel that opens and closes. Closed content is not rendered (it unmounts once the close
 * transition has ended); under reduced motion it opens and closes at once. The id goes on the outer element so
 * aria-controls points at something that exists while open. Content that is closing is inert.
 */
export function Collapse({ open, id, className, children }: { open: boolean; id?: string; className?: string; children: ReactNode }) {
  const instant = prefersReducedMotion()
  const ref = useRef<HTMLDivElement>(null)
  const [mounted, setMounted] = useState(open)
  const [expanded, setExpanded] = useState(open)
  // Fully open: the clip is lifted so focus rings at the edge are not cut off. A panel that mounts open starts that way.
  const [settled, setSettled] = useState(open)
  const first = useRef(true)
  if (open && !mounted) setMounted(true)
  useEffect(() => {
    const initial = first.current
    first.current = false
    if (initial) return // mounted open (or closed): nothing to animate
    setSettled(false)
    if (open) {
      // Read layout so the closed (0fr) state is painted before the open state: the first frame is not skipped.
      void ref.current?.offsetHeight
      const raf = requestAnimationFrame(() => setExpanded(true))
      // If no transition ends (hidden tab, interrupted), still lift the clip.
      const t = setTimeout(() => setSettled(true), 220)
      return () => {
        cancelAnimationFrame(raf)
        clearTimeout(t)
      }
    }
    setExpanded(false)
    // If no transition ends (hidden tab, interrupted), still unmount.
    const t = setTimeout(() => setMounted(false), 260)
    return () => clearTimeout(t)
  }, [open])
  if (instant ? !open : !mounted) return null
  return (
    <div
      ref={ref}
      id={id}
      className={cn('grid transition-[grid-template-rows] duration-200 ease-out', className)}
      style={{ gridTemplateRows: (instant ? open : expanded && open) ? '1fr' : '0fr' }}
      onTransitionEnd={(e) => {
        if (e.target !== e.currentTarget) return
        if (open) setSettled(true)
        else setMounted(false)
      }}
    >
      <div inert={!open} className={cn('min-h-0', !(settled || instant) && 'overflow-hidden')}>
        {children}
      </div>
    </div>
  )
}
