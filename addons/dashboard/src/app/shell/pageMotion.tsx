// Page changes in <main> (G4): where the scroll goes, and the short fade-in of the new page.
//
// - Scroll: a new page starts at the top. Back and Forward return to where that page was left. A change of the search
//   only (a filter, a ticket's tab) keeps the scroll, and so does anything that is not a page change.
// - Fade: the new page fades in over 160 ms (opacity only, `--ease-out`), once, when the router shows it (skeleton or
//   page). Not on tab, filter or settings-section changes, not on live refreshes, and never under reduced motion.

import { useRouter, useRouterState } from '@tanstack/react-router'
import { useEffect, useLayoutEffect, useRef, type ReactNode, type RefObject } from 'react'
import { canAnimate, DURATION, EASE_OUT } from '@/lib/motion'

export const FADE_MS = DURATION.base

/** The page the router shows (skeleton or page): the leaf match's path, without the search. */
function useShownPath(): string {
  return useRouterState({ select: (s) => s.matches.at(-1)?.pathname ?? s.location.pathname })
}

/** The settings sections are one page with a sub-nav: switching them is not a page change for the fade. */
const fadeKeyOf = (path: string) => (path.startsWith('/settings') ? '/settings' : path)

type HistoryMove = 'PUSH' | 'REPLACE' | 'BACK' | 'FORWARD' | 'GO'

/** Resets or restores `#main`'s scroll when the page changes (see the top of this file). */
export function usePageScroll(main: RefObject<HTMLElement | null>) {
  const router = useRouter()
  // The location the page on screen was loaded for (its path and its history entry), not the one being loaded.
  const shownLoc = useRouterState({
    select: (s) => {
      const l = s.resolvedLocation ?? s.location
      const st = l.state as { __TSR_key?: string; key?: string } | undefined
      return `${l.pathname}\n${st?.__TSR_key ?? st?.key ?? l.href}`
    },
  })
  const saved = useRef(new Map<string, number>())
  const lastMove = useRef<HistoryMove>('PUSH')
  const shown = useRef<{ path: string; entry: string } | null>(null)

  useEffect(() => router.history.subscribe(({ action }) => void (lastMove.current = action.type as HistoryMove)), [router])

  // Remember each history entry's scroll as the person scrolls (read again on Back/Forward).
  useEffect(() => {
    const el = main.current
    if (!el) return
    const onScroll = () => {
      if (shown.current) saved.current.set(shown.current.entry, el.scrollTop)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [main])

  useLayoutEffect(() => {
    const [path, entry] = shownLoc.split('\n')
    const el = main.current
    const prev = shown.current
    shown.current = { path, entry }
    if (!el || !prev || prev.entry === entry) return
    // The same page with another search (a filter, a tab): the new entry keeps the scroll.
    if (prev.path === path) {
      saved.current.set(entry, el.scrollTop)
      return
    }
    const back = lastMove.current === 'BACK' || lastMove.current === 'FORWARD' || lastMove.current === 'GO'
    el.scrollTop = back ? (saved.current.get(entry) ?? 0) : 0
  }, [shownLoc, main])
}

/** Wraps the page: fades it in when another page is shown (see the top of this file). */
export function PageFade({ children }: { children: ReactNode }) {
  const fadeKey = fadeKeyOf(useShownPath())
  const box = useRef<HTMLDivElement>(null)
  const last = useRef<string | null>(null)
  useLayoutEffect(() => {
    const prev = last.current
    last.current = fadeKey
    // Not on the first page of the session (the app's own first paint), nor without a page change.
    const el = box.current
    if (prev === null || prev === fadeKey || !canAnimate(el)) return
    el.getAnimations?.().forEach((a) => a.cancel())
    el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: FADE_MS, easing: EASE_OUT })
  }, [fadeKey])
  return (
    // h-full: pages that fill <main> (Board, Tickets) size against it as before.
    <div ref={box} data-page-fade className="h-full">
      {children}
    </div>
  )
}
