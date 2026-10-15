// The dock's drag edge (shared by the mockup's dock and the real-host dock).

import { useRef, type KeyboardEvent, type PointerEvent } from 'react'
import { cn } from '@/lib/utils'
import { DOCK_LIMITS, type DockSide } from './prefs'

const STEP = 16
const BIG_STEP = 64

/** The drag edge with a visible grip: pointer drag, or arrow keys (Shift for bigger steps), Home/End for the limits. */
export function ResizeHandle({ side, size, max, onResize }: { side: DockSide; size: number; max: number; onResize: (px: number) => void }) {
  const right = side === 'right'
  const min = DOCK_LIMITS[side].min
  const drag = useRef<{ start: number; size: number } | null>(null)
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.currentTarget.setPointerCapture?.(e.pointerId)
    drag.current = { start: right ? e.clientX : e.clientY, size }
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    const delta = drag.current.start - (right ? e.clientX : e.clientY) // dragging up / left makes the dock bigger
    onResize(drag.current.size + delta)
  }
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? BIG_STEP : STEP
    const grow = right ? 'ArrowLeft' : 'ArrowUp'
    const shrink = right ? 'ArrowRight' : 'ArrowDown'
    const next = e.key === grow ? size + step : e.key === shrink ? size - step : e.key === 'Home' ? min : e.key === 'End' ? max : null
    if (next === null) return
    e.preventDefault()
    onResize(next)
  }
  return (
    <div role="separator" tabIndex={0} aria-label="Resize terminal dock" title="Drag to resize · Arrow keys" aria-orientation={right ? 'vertical' : 'horizontal'} aria-valuemin={min} aria-valuemax={max} aria-valuenow={size}
      onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={() => (drag.current = null)} onPointerCancel={() => (drag.current = null)} onKeyDown={onKey}
      className={cn('group absolute z-10 flex items-center justify-center outline-none', right ? '-left-1.5 top-0 h-full w-3 cursor-col-resize' : '-top-1.5 left-0 h-3 w-full cursor-row-resize')}>
      <span aria-hidden="true" className={cn('rounded-full bg-border-strong group-hover:bg-brand group-focus-visible:bg-brand', right ? 'h-10 w-1' : 'h-1 w-10')} />
      <span className="pointer-events-none absolute hidden whitespace-nowrap rounded bg-surface-3 px-1.5 py-0.5 text-[11px] text-text group-hover:block group-focus-visible:block" style={right ? { left: 12 } : { top: -22 }}>
        Drag to resize · Arrow keys
      </span>
    </div>
  )
}
