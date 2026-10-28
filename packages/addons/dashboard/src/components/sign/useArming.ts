// Sign is enabled only after the View has been on screen, visible, for ARM_MS (1 s). The timer restarts when the window
// loses focus or the page is not visible (host contract section 0.8.6 item 7). A new View (new `key`) starts over.
// It reduces accidental activation; it proves neither review nor presence and does not constrain a compromised client.
//
// C3 (one press): `usePressSign` is the one rule every sign button uses (ClickSignPanel, the answer strip):
// - A View prepared by a trusted press (`pressed`) and without risk lines runs the ARM: the button shows the signer's
//   headline with a progress fill, and after 1 s of visible, focused View the signature is asked for. During the arm
//   Esc, a second press, a press elsewhere, window blur and a hidden page cancel (the caller tombstones the request);
//   opening "i" stops the arm, and a new trusted press (inside "i" or on the button) starts a new one.
// - A View WITH risk lines never signs at the end of an arm: the lines are shown under the button, and a SECOND trusted
//   press that begins at least 1 s after the lines are on screen (visible, focused) signs. Lines that are not in the page
//   never count: no lines rendered, no press accepted.
// - Any other View (not prepared by a press here, or a re-read) keeps the press-after-1-s rule.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { ARM_MS } from '@/api/click'
import { trustedPress } from './trustedPress'

const focusedNow = () => (typeof document.hasFocus === 'function' ? document.hasFocus() : true)

/** The attribute of a region outside the panel that still counts as inside for the arm (the "i" drawer's content). */
export const ARM_INSIDE = 'data-arm-inside'
/** The attribute every rendered risk line carries (counted before any press is accepted). */
export const RISK_LINE = 'data-risk-line'

/** The risk lines are in the page: the box is in the document and holds one rendered line per signer's line. */
export function riskShown(box: HTMLElement | null, lines: readonly string[]): boolean {
  if (lines.length === 0) return true
  return !!box && box.isConnected && box.querySelectorAll(`[${RISK_LINE}]`).length === lines.length
}

/**
 * True once the View has been visible and focused for `ms` while `active`; `gate` must hold at the start and at the end
 * (the risk lines are on screen). A blur or a hidden page restarts it; the gate is looked at again on every DOM change.
 */
function useDwell(active: boolean, key: string, ms: number, gate: () => boolean): boolean {
  const [ready, setReady] = useState(false)
  const gateRef = useRef(gate)
  gateRef.current = gate
  useEffect(() => {
    setReady(false)
    if (!active) return
    let timer: ReturnType<typeof setTimeout> | undefined
    let on = false
    // Focused means the window has focus: no shortcut for "nothing is focused" (that is the blurred case too).
    let focused = focusedNow()
    const stop = () => {
      clearTimeout(timer)
      timer = undefined
      on = false
      setReady(false)
    }
    const start = () => {
      stop()
      if (document.visibilityState !== 'visible' || !focused || !gateRef.current()) return
      timer = setTimeout(() => {
        timer = undefined
        if (!gateRef.current()) return
        on = true
        setReady(true)
      }, ms)
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
    // The risk lines left the page: never ready until they are back for a full `ms` again.
    const watch = typeof MutationObserver === 'function' ? new MutationObserver(() => {
      const shown = gateRef.current()
      if (!shown && (on || timer)) stop()
      else if (shown && !on && !timer) start()
    }) : null
    watch?.observe(document.body, { childList: true, subtree: true })
    window.addEventListener('blur', onBlur)
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
    start()
    return () => {
      clearTimeout(timer)
      watch?.disconnect()
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [active, key, ms])
  return ready
}

const always = () => true

export function useArming(key: string, ms = ARM_MS): boolean {
  return useDwell(true, key, ms, always)
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

/**
 * - `arm`: the 1 s arm runs; the signature is asked for at its end; any press cancels.
 * - `held`: "i" was opened during the arm; a trusted press starts a new arm.
 * - `press`: a trusted press that began after `ready` signs (risk lines, a View not prepared by a press, or after a refusal).
 * - `off`: the arm was cancelled; nothing more is accepted on this View.
 */
export type PressMode = 'arm' | 'held' | 'press' | 'off'

export interface PressSignOptions {
  /** The View's digest: a new View is a new confirmation. */
  key: string
  /** The signer's risk lines (contract 4). Non-empty: never signed at the end of an arm. */
  riskLines: readonly string[]
  /** The View is the first one of a request prepared by a trusted press (`ClickRun.fresh`): it may run the arm. */
  pressed: boolean
  /** The caller allows a signature now (not signing, no rate limit, pictures checked, fields filled). */
  can: boolean
  onSign: () => void
  /** The arm was cancelled (Esc, a second press, a press elsewhere, blur, hidden page): tombstone the request. */
  onCancel: () => void
  ms?: number
}

export interface PressSign {
  mode: PressMode
  /** A counter of arms: the progress fill restarts with it. */
  round: number
  /** `press` mode: the View (and its risk lines) has been visible and focused for 1 s. */
  ready: boolean
  /** The panel: a press outside it (and outside `ARM_INSIDE` regions) cancels the arm. */
  root: (el: HTMLElement | null) => void
  /** The box of the rendered risk lines (each line with `RISK_LINE`). */
  riskBox: (el: HTMLElement | null) => void
  /** Spread on the sign button (and on the "press again" button inside "i"). */
  button: {
    onPointerDown: (e: ReactPointerEvent) => void
    onKeyDown: (e: ReactKeyboardEvent) => void
    onClick: (e: ReactMouseEvent) => void
  }
  /** "i" opened or closed: opening it during the arm stops the arm (`held`). */
  info: (open: boolean) => void
}

export function usePressSign({ key, riskLines, pressed, can, onSign, onCancel, ms = ARM_MS }: PressSignOptions): PressSign {
  const risky = riskLines.length > 0
  // Client assert: a View with risk lines never arms; only the second-press rule applies.
  const [mode, setModeState] = useState<PressMode>(() => (pressed && !risky ? 'arm' : 'press'))
  const modeRef = useRef(mode)
  const setMode = useCallback((m: PressMode) => {
    modeRef.current = m
    setModeState(m)
  }, [])
  const [round, setRound] = useState(0)
  const [elapsed, setElapsed] = useState(false)
  const rootEl = useRef<HTMLElement | null>(null)
  const riskEl = useRef<HTMLElement | null>(null)
  const lines = useRef(riskLines)
  lines.current = riskLines
  const latest = useRef({ onSign, onCancel, can })
  latest.current = { onSign, onCancel, can }
  const gate = useCallback(() => riskShown(riskEl.current, lines.current), [])
  const ready = useDwell(mode === 'press', key, ms, gate)
  const activation = useActivation(ready)

  const cancelArm = useCallback(() => {
    if (modeRef.current !== 'arm') return
    setMode('off')
    latest.current.onCancel()
  }, [setMode])

  // The arm: 1 s of visible, focused View; every cancel trigger is live while it runs.
  useEffect(() => {
    if (mode !== 'arm') return
    setElapsed(false)
    if (risky || document.visibilityState !== 'visible' || !focusedNow()) {
      cancelArm() // not on screen at the start: nothing is signed
      return
    }
    const timer = setTimeout(() => setElapsed(true), ms)
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') cancelArm()
    }
    const onDown = (e: Event) => {
      const t = e.target
      if (t instanceof Node && rootEl.current?.contains(t)) return
      if (t instanceof Element && t.closest(`[${ARM_INSIDE}]`)) return
      cancelArm()
    }
    const onVisibility = () => {
      if (document.visibilityState !== 'visible') cancelArm()
    }
    window.addEventListener('blur', cancelArm)
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('pointerdown', onDown, true)
    document.addEventListener('mousedown', onDown, true)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      clearTimeout(timer)
      window.removeEventListener('blur', cancelArm)
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('pointerdown', onDown, true)
      document.removeEventListener('mousedown', onDown, true)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [mode, round, ms, risky, cancelArm])

  // The end of the arm: the signature is asked for once (as soon as the caller allows it, if it did not yet).
  useEffect(() => {
    if (mode !== 'arm' || !elapsed || !can || risky) return
    setElapsed(false)
    setMode('press') // a refusal that leaves the View open needs a new press
    latest.current.onSign()
  }, [mode, elapsed, can, risky, setMode])

  const info = useCallback((open: boolean) => {
    if (open && modeRef.current === 'arm') setMode('held')
  }, [setMode])

  const button = {
    onPointerDown: (e: ReactPointerEvent) => activation.begin(e),
    onKeyDown: (e: ReactKeyboardEvent) => activation.begin(e),
    onClick: (e: ReactMouseEvent) => {
      const m = modeRef.current
      if (m === 'arm') return cancelArm() // a second press during the arm cancels
      if (m === 'held') {
        if (!trustedPress(e.nativeEvent) || risky) return
        setMode('arm')
        setRound((r) => r + 1)
        return
      }
      if (m !== 'press') return
      // A synthetic click or a key repeat never signs; risk lines that are not on screen never count.
      if (!activation.take(e.nativeEvent) || !latest.current.can || !gate()) return
      latest.current.onSign()
    },
  }

  const root = useCallback((el: HTMLElement | null) => {
    rootEl.current = el
  }, [])
  const riskBox = useCallback((el: HTMLElement | null) => {
    riskEl.current = el
  }, [])
  return { mode, round, ready, root, riskBox, button, info }
}
