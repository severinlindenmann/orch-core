import { useEffect, useRef } from 'react'
import { canAnimate, DURATION, EASE_OUT, useTween } from '@/lib/motion'

/** A count that ticks when it changes (a short slide, and a tween when it jumps). Static on first render and when it stays the same. */
export function Num({ value }: { value: number }) {
  const shown = useTween(value)
  const ref = useRef<HTMLSpanElement>(null)
  const last = useRef(value)
  useEffect(() => {
    if (last.current === value) return
    last.current = value
    if (canAnimate(ref.current)) ref.current.animate([{ transform: 'translateY(-35%)', opacity: 0.35 }, { transform: 'none', opacity: 1 }], { duration: DURATION.base, easing: EASE_OUT })
  }, [value])
  return (
    <span ref={ref} className="inline-block tabular-nums">
      {shown}
    </span>
  )
}
