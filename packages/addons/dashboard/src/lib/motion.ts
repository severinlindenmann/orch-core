// Motion helpers. No library: CSS transitions for most things, the Web Animations API for the few that need the old
// position (FLIP). Rules: durations at most 200 ms, ease-out, nothing animates on a plain live refresh (only an item
// that is new or has moved), and everything stands still under prefers-reduced-motion.
import { useEffect, useRef, useState } from 'react'

export const DURATION = { fast: 120, base: 160, settle: 180, slow: 200 } as const
export const EASE_OUT = 'cubic-bezier(0.22, 1, 0.36, 1)'

export function prefersReducedMotion(): boolean {
  return typeof window === 'undefined' || typeof window.matchMedia !== 'function' ? true : window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** True when `el` may be animated through WAAPI right now (motion allowed and the browser has it). */
export function canAnimate(el: Element | null | undefined): el is HTMLElement {
  return !!el && !prefersReducedMotion() && typeof el.animate === 'function'
}

/**
 * FLIP: `el` is already at its new place; play it from `from` (where it was) to here. Position only, with an optional
 * start scale (a card that was lifted settles from 1.02). Returns the animation, or null when nothing ran.
 */
export function flip(el: Element | null, from: { left: number; top: number }, opts: { scaleFrom?: number; duration?: number } = {}): Animation | null {
  if (!canAnimate(el)) return null
  const to = el.getBoundingClientRect()
  const dx = from.left - to.left
  const dy = from.top - to.top
  if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && !opts.scaleFrom) return null
  const scale = opts.scaleFrom ? ` scale(${opts.scaleFrom})` : ''
  return el.animate([{ transform: `translate(${dx}px, ${dy}px)${scale}` }, { transform: 'none' }], {
    duration: Math.min(opts.duration ?? DURATION.settle, DURATION.slow),
    easing: EASE_OUT,
  })
}

/** A one-off fade-in for an element that has just appeared because something new arrived. */
export function fadeIn(el: Element | null): Animation | null {
  if (!canAnimate(el)) return null
  return el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: DURATION.base, easing: EASE_OUT })
}

/**
 * Collapses a detached element (a row that was resolved) in place: height and opacity to zero, then `done`.
 * `el` must already be in the document at its old spot. With motion off, `done` runs at once.
 */
export function collapseOut(el: HTMLElement, done: () => void): void {
  if (!canAnimate(el)) return done()
  const h = el.getBoundingClientRect().height
  el.style.overflow = 'hidden'
  el.style.pointerEvents = 'none'
  const a = el.animate([{ height: `${h}px`, opacity: 1 }, { height: '0px', opacity: 0 }], { duration: DURATION.base, easing: EASE_OUT, fill: 'forwards' })
  void a.finished.then(done, done)
}

// -- Where a ticket card was: a moved card plays from there once it exists in its new place. -------------------------

interface Noted {
  left: number
  top: number
  scaleFrom?: number
  /** `mount`: the card will appear as a new element (another column). `settle`: the same element drops back in place. */
  when: 'mount' | 'settle'
  at: number
}
const noted = new Map<string, Noted>()
const NOTE_TTL = 1500

export function noteRect(key: string, rect: { left: number; top: number }, when: Noted['when'], scaleFrom?: number): void {
  if (prefersReducedMotion()) return
  noted.set(key, { left: rect.left, top: rect.top, when, scaleFrom, at: Date.now() })
}

/** Takes the noted rect for `key` once (and only if recent and of the wanted kind). */
export function takeNoted(key: string, when: Noted['when'] | 'any'): Noted | null {
  const n = noted.get(key)
  if (!n) return null
  if (Date.now() - n.at > NOTE_TTL) {
    noted.delete(key)
    return null
  }
  if (when !== 'any' && n.when !== when) return null
  noted.delete(key)
  return n
}

// -- Numbers ------------------------------------------------------------------------------------------------------

/**
 * `value`, tweened for a moment when it changes by more than one. The first value is shown as is, and under reduced
 * motion every value is. A live refresh that does not change the number does nothing.
 */
export function useTween(value: number): number {
  const [shown, setShown] = useState(value)
  const from = useRef(value)
  useEffect(() => {
    if (prefersReducedMotion() || Math.abs(value - from.current) <= 1 || typeof requestAnimationFrame !== 'function') {
      from.current = value
      setShown(value)
      return
    }
    const start = from.current
    const t0 = performance.now()
    let raf = 0
    const step = (now: number) => {
      const p = Math.min(1, (now - t0) / DURATION.base)
      const eased = 1 - (1 - p) ** 3
      from.current = Math.round(start + (value - start) * eased)
      setShown(from.current)
      if (p < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [value])
  return shown
}
