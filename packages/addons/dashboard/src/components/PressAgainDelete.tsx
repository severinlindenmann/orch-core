import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { trustedPress } from '@/components/sign/trustedPress'

/**
 * Irreversible unsigned actions (C5) need two presses on this control within five seconds: the first arms it, the
 * second acts once. Only trusted presses count; moving away, blur or Escape disarms. `again` is the armed label.
 */
export function PressAgainDelete({ onDelete, disabled, children = 'Delete work folder', again = 'Press again to delete', testId }: { onDelete: () => void; disabled?: boolean; children?: React.ReactNode; again?: React.ReactNode; testId?: string }) {
  const [until, setUntil] = useState(0)
  const deadline = useRef(0)
  const reset = () => { deadline.current = 0; setUntil(0) }
  useEffect(() => {
    if (!until) return
    const timer = setTimeout(reset, Math.max(0, until - Date.now()))
    return () => clearTimeout(timer)
  }, [until])
  return <Button data-testid={testId} size="sm" variant={until ? 'destructive' : 'ghost'} disabled={disabled} onBlur={reset} onPointerLeave={reset} onKeyDown={e => { if (e.key === 'Escape') reset() }} onClick={e => {
    if (!trustedPress(e.nativeEvent) || disabled) return
    if (deadline.current > Date.now()) { reset(); onDelete(); return }
    deadline.current = Date.now() + 5000
    setUntil(deadline.current)
  }}>{until ? again : children}</Button>
}
