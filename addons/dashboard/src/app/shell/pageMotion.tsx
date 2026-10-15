// Page changes in <main> (G4): where the scroll goes, and the short fade-in of the new page.
//
// - Scroll: a new page starts at the top. Back and Forward return to where that page was left. A change of the search
//   only (a filter, a ticket's tab) keeps the scroll, and so does anything that is not a page change.
// - Fade: the new page fades in over 160 ms (opacity only, `--ease-out`), once, when the router shows it (skeleton or
//   page). Not on tab, filter or settings-section changes, not on live refreshes, and never under reduced motion.

import { useRouter, useRouterState } from '@tanstack/react-router'
import { useEffect, useLayoutEffect, useRef, type ReactNode, type RefObject } from 'react'
import { canAnimate, DURATION, EASE_OUT } from '@/lib/motion'
import { splitWorkspacePath } from '../urls'

export const FADE_MS = DURATION.base

/** The workspace part of an address (`/w/DEMO/board` → DEMO): the same in-app path in another workspace is another page. */
const prefixOf = (publicHref: string) => splitWorkspacePath(new URL(publicHref, 'http://x').pathname).prefix ?? ''

/** The page the router shows (skeleton or page): the leaf match's path and the address's workspace, without the search. */
function useShownPath(): string {
  return useRouterState({ select: (s) => `${prefixOf(s.location.publicHref)}|${s.matches.at(-1)?.pathname ?? s.location.pathname}` })
}

/** The settings sections are one page with a sub-nav: switching them is not a page change for the fade. */
const fadeKeyOf = (key: string) => key.replace(/\|\/settings.*$/, '|/settings')

/**
 * Whether two `<prefix>|<path>` keys are the same page. The workspace counts only when both addresses name one: an
 * address the app completes (`/board` → `/w/DEMO/board`) is the same page, a switch DEMO → OPS is another.
 */
export function samePage(a: string, b: string): boolean {
  const [pa, ...ra] = a.split('|')
  const [pb, ...rb] = b.split('|')
  return ra.join('|') === rb.join('|') && (!pa || !pb || pa === pb)
}

type HistoryMove = 'PUSH' | 'REPLACE' | 'BACK' | 'FORWARD' | 'GO'

/**
 * Resets or restores `#main`'s scroll when the page changes (see the top of this file). The offset a page was left at
 * is saved when the navigation starts: while the next page loads, the old one may be hidden (React keeps it mounted
 * under the skeleton) and the browser clamps the scroll, which must not overwrite it.
 */
export function usePageScroll(main: RefObject<HTMLElement | null>) {
  const router = useRouter()
  // The page on screen (skeleton or page): a new one starts at the top as soon as it shows.
  const shownPath = useShownPath()
  // The location the page on screen was loaded for (its path and history entry): Back restores once it is in.
  const resolved = useRouterState({
    select: (s) => {
      const l = s.resolvedLocation ?? s.location
      const st = l.state as { __TSR_key?: string; key?: string } | undefined
      return `${prefixOf(l.publicHref)}|${l.pathname}\n${st?.__TSR_key ?? st?.key ?? l.href}`
    },
  })
  const saved = useRef(new Map<string, number>())
  const lastMove = useRef<HistoryMove>('PUSH')
  const shown = useRef<{ path: string; entry: string } | null>(null)
  /** Between a navigation's start and its page being in: scroll events belong to no entry. */
  const moving = useRef(false)
  const back = () => lastMove.current === 'BACK' || lastMove.current === 'FORWARD' || lastMove.current === 'GO'

  useEffect(() => router.history.subscribe(({ action }) => void (lastMove.current = action.type as HistoryMove)), [router])
  useEffect(() => {
    const offStart = router.subscribe('onBeforeNavigate', () => {
      const el = main.current
      if (el && shown.current && !moving.current) saved.current.set(shown.current.entry, el.scrollTop)
      moving.current = true
    })
    const offDone = router.subscribe('onResolved', () => void (moving.current = false))
    return () => {
      offStart()
      offDone()
    }
  }, [router, main])

  // Remember each history entry's scroll as the person scrolls (read again on Back/Forward).
  useEffect(() => {
    const el = main.current
    if (!el) return
    const onScroll = () => {
      if (shown.current && !moving.current && router.state.status !== 'pending') saved.current.set(shown.current.entry, el.scrollTop)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [main, router])

  // Another page shows (its skeleton too): the top, unless Back/Forward brings a page back (restored below).
  const lastShown = useRef(shownPath)
  useLayoutEffect(() => {
    const was = lastShown.current
    lastShown.current = shownPath
    if (samePage(was, shownPath)) return
    if (!back() && main.current) main.current.scrollTop = 0
  }, [shownPath, main])

  useLayoutEffect(() => {
    const [path, entry] = resolved.split('\n')
    const el = main.current
    const prev = shown.current
    shown.current = { path, entry }
    moving.current = false
    if (!el || !prev || prev.entry === entry) return
    // The same page with another search (a filter, a tab): the new entry keeps the scroll.
    if (samePage(prev.path, path)) {
      saved.current.set(entry, el.scrollTop)
      return
    }
    el.scrollTop = back() ? (saved.current.get(entry) ?? 0) : 0
  }, [resolved, main])
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
    if (prev === null || samePage(prev, fadeKey) || !canAnimate(el)) return
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
