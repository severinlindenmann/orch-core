// Sign is enabled only after the View has been on screen, visible, for ARM_MS (1 s). The timer restarts when the window
// loses focus or the page is not visible (host contract section 0.8.6 item 7). A new View (new `key`) starts over.
// It reduces accidental activation; it proves neither review nor presence and does not constrain a compromised client.
import { useEffect, useRef, useState } from 'react'
import { ARM_MS } from '@/api/click'
import { trustedPress } from './trustedPress'

export function useArming(key: string, ms = ARM_MS): boolean {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    setArmed(false)
    let timer: ReturnType<typeof setTimeout> | undefined
    // Focused means the window has focus: no shortcut for "nothing is focused" (that is the blurred case too).
    let focused = typeof document.hasFocus === 'function' ? document.hasFocus() : true
    const stop = () => {
      clearTimeout(timer)
      timer = undefined
      setArmed(false)
    }
    const start = () => {
      stop()
      if (document.visibilityState !== 'visible' || !focused) return
      timer = setTimeout(() => setArmed(true), ms)
    }
    const onBlur = () => {
      focused = false
      stop()
    }
    const onFocus = () => {
      focused = true
      start()
    }
    const onVisibility = () => (document.visibilityState === 'visible' ? start() : stop())
    window.addEventListener('blur', onBlur)
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
    start()
    return () => {
      clearTimeout(timer)
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [key, ms])
  return armed
}

/**
 * The guard for the Sign button: an activation counts only if it BEGAN while armed (pointer down or key down, not an
 * auto-repeat). A click whose press started before the View appeared, or before the button was armed, is ignored.
 * The click itself must be a trusted, non-repeat press (trustedPress): a synthetic click or a key auto-repeat never signs.
 */
export function useActivation(armed: boolean) {
  const began = useRef(false)
  useEffect(() => {
    if (!armed) began.current = false
  }, [armed])
  return {
    begin: (e: { type: string; key?: string; repeat?: boolean }) => {
      if (e.type === 'keydown' && (e.repeat || (e.key !== 'Enter' && e.key !== ' '))) return
      began.current = armed
    },
    /** True once per trusted activation that began armed. `e` is the click's native event. */
    take: (e: { isTrusted: boolean; repeat?: boolean }) => {
      const ok = began.current && armed && trustedPress(e)
      began.current = false
      return ok
    },
  }
}
