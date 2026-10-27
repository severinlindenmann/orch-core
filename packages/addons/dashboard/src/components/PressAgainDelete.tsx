import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { trustedPress } from '@/components/sign/trustedPress'

/** Irreversible unsigned actions need two presses on this control within five seconds. */
export function PressAgainDelete({ onDelete, disabled, children = 'Delete work folder' }: { onDelete: () => void; disabled?: boolean; children?: React.ReactNode }) {
  const [until, setUntil] = useState(0)
  const deadline = useRef(0)
  const reset = () => { deadline.current = 0; setUntil(0) }
  useEffect(() => {
    if (!until) return
    const timer = setTimeout(reset, Math.max(0, until - Date.now()))
    return () => clearTimeout(timer)
  }, [until])
  return <Button size="sm" variant={until ? 'destructive' : 'ghost'} disabled={disabled} onBlur={reset} onPointerLeave={reset} onKeyDown={e => { if (e.key === 'Escape') reset() }} onClick={e => {
    if (!trustedPress(e.nativeEvent) || disabled) return
    if (deadline.current > Date.now()) { reset(); onDelete(); return }
    deadline.current = Date.now() + 5000
    setUntil(deadline.current)
  }}>{until ? 'Press again to delete' : children}</Button>
}
