// Page changes in <main> (G4): where the scroll goes, and the short fade-in of the new page.
//
// - Scroll: a new page starts at the top. Back and Forward return to where that page was left. A change of the search
//   only (a filter, a ticket's tab) keeps the scroll, and so does anything that is not a page change.
// - Fade: the new page fades in over 180 ms and rises 6 px (`--ease-out`), once, when the router shows it (skeleton or
//   page); its first eight rows follow 20 ms apart. Not on tab, filter or settings-section changes, not on live
//   refreshes, and never under reduced motion.
// - Bar: a 2 px indeterminate bar at the top while a page loads (it never claims a measured progress).

import { useRouter, useRouterState } from '@tanstack/react-router'
import { useEffect, useLayoutEffect, useRef, type ReactNode, type RefObject } from 'react'
import { canAnimate, DURATION, EASE_OUT } from '@/lib/motion'
import { splitWorkspacePath } from '../urls'

export const FADE_MS = DURATION.settle
export const RISE_PX = 6
export const STAGGER_MS = 20
export const STAGGER_ROWS = 8
const HIDDEN_FRAME = { opacity: 0, transform: `translateY(${RISE_PX}px)` }
const SHOWN_FRAME = { opacity: 1, transform: 'none' }

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

/** The rows that follow the page fade: opted-in rows, cards and list items. Never table rows (a transform on a `tr` shifts the table's layout). */
export const STAGGER_SELECTOR = '[data-row], article, ul[aria-label] > li'

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
    el.animate([HIDDEN_FRAME, SHOWN_FRAME], { duration: FADE_MS, easing: EASE_OUT })
    // The first rows of the page follow one after the other (at most eight, only those on screen).
    const rows = [...el.querySelectorAll<HTMLElement>(STAGGER_SELECTOR)].filter((r) => r.offsetParent !== null).slice(0, STAGGER_ROWS)
    rows.forEach((row, i) => row.animate?.([HIDDEN_FRAME, SHOWN_FRAME], { duration: FADE_MS, delay: (i + 1) * STAGGER_MS, easing: EASE_OUT, fill: 'backwards' }))
  }, [fadeKey])
  return (
    // h-full: pages that fill <main> (Board, Tickets) size against it as before.
    <div ref={box} data-page-fade className="h-full">
      {children}
    </div>
  )
}

/** What a page is called while it loads ("Loading Board..."), from its in-app path. */
export function pageNameOf(pathname: string): string {
  const seg = pathname.split('/').filter(Boolean)
  if (seg.length === 0) return 'Today'
  if (seg[0] === 'ticket' && seg[1]) return `ticket ${decodeURIComponent(seg[1])}`
  const name = seg[0].replaceAll('-', ' ')
  return name.charAt(0).toUpperCase() + name.slice(1)
}

/**
 * A 2 px bar at the top of the page area while the router loads a page. Indeterminate on purpose (it moves, it never
 * fills): nothing here knows how long the page will take. Screen readers hear "Loading Board". Under reduced motion
 * the bar stands still.
 */
export function NavProgress() {
  const loading = useRouterState({ select: (s) => (s.status === 'pending' ? s.location.pathname : null) })
  if (loading === null) return null
  return (
    <div role="progressbar" aria-label={`Loading ${pageNameOf(loading)}`} aria-busy="true" data-nav-progress className="orch-nav-bar pointer-events-none absolute inset-x-0 top-0 z-40 h-0.5 overflow-hidden bg-brand/15">
      <span className="orch-nav-bar-fill block h-full w-1/3 bg-brand" />
    </div>
  )
}
