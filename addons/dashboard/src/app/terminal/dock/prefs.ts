// The terminal dock's per-viewer layout: which side, how big, open or collapsed. Kept in localStorage (per person,
// per browser); storage that throws or is empty falls back to the defaults.

import { useCallback, useEffect, useState } from 'react'

export type DockSide = 'bottom' | 'right'

export interface DockPrefs {
  side: DockSide
  open: boolean
  /** Height when docked at the bottom, px. */
  bottom: number
  /** Width when docked on the right, px. */
  right: number
  /** The harness last started from the dock (the New session form starts with it). */
  harness: string
}

/** Size limits: bottom 160 px to 70% of the window height, right 320 px to 60% of the window width. */
export const DOCK_LIMITS = {
  bottom: { min: 160, ratio: 0.7 },
  right: { min: 320, ratio: 0.6 },
} as const

/** The collapsed bar, px. */
export const DOCK_BAR = 32

export const DEFAULT_PREFS: DockPrefs = { side: 'bottom', open: false, bottom: 280, right: 440, harness: 'claude' }

/** The page keeps at least this much width beside a right-hand dock (N11: every page is laid out to work down to 720). */
export const PAGE_MIN = 720

const keyOf = (viewer: string) => `orch.dock.${viewer}`

/**
 * The largest dock: bottom 70% of the window height; right 60% of the window width, and never so wide that the page
 * area (`area`: page plus dock, i.e. the window minus the sidebar) drops under PAGE_MIN. Can be under the minimum: then
 * the right side does not fit (see rightFits).
 */
export function dockMax(side: DockSide, view: { width: number; height: number }, area = view.width): number {
  const l = DOCK_LIMITS[side]
  if (side === 'bottom') return Math.max(l.min, Math.floor(view.height * l.ratio))
  return Math.min(Math.floor(view.width * l.ratio), area - PAGE_MIN)
}

/** Room a right-hand dock needs to be worth having; with less it docks at the bottom. */
export const RIGHT_ROOM = 400

/** Is there room for a right-hand dock (at least RIGHT_ROOM px while the page keeps PAGE_MIN)? */
export const rightFits = (view: { width: number; height: number }, area = view.width) => dockMax('right', view, area) >= RIGHT_ROOM

export function clampDock(side: DockSide, px: number, view: { width: number; height: number }, area = view.width): number {
  const max = Math.max(DOCK_LIMITS[side].min, dockMax(side, view, area))
  return Math.round(Math.min(max, Math.max(DOCK_LIMITS[side].min, Number.isFinite(px) ? px : DEFAULT_PREFS[side])))
}

export function readDockPrefs(viewer: string | undefined): DockPrefs {
  if (!viewer) return DEFAULT_PREFS
  try {
    const raw = JSON.parse(localStorage.getItem(keyOf(viewer)) ?? 'null') as Partial<DockPrefs> | null
    if (!raw || typeof raw !== 'object') return DEFAULT_PREFS
    return {
      side: raw.side === 'right' ? 'right' : 'bottom',
      open: raw.open === true,
      bottom: typeof raw.bottom === 'number' ? raw.bottom : DEFAULT_PREFS.bottom,
      right: typeof raw.right === 'number' ? raw.right : DEFAULT_PREFS.right,
      harness: typeof raw.harness === 'string' && raw.harness ? raw.harness : DEFAULT_PREFS.harness,
    }
  } catch {
    return DEFAULT_PREFS
  }
}

export function writeDockPrefs(viewer: string | undefined, prefs: DockPrefs) {
  if (!viewer) return
  try {
    localStorage.setItem(keyOf(viewer), JSON.stringify(prefs))
  } catch {
    /* storage unavailable: the layout lasts for this page only */
  }
}

/** The viewer's dock layout and a setter that saves it. Re-reads when the viewer changes. */
export function useDockPrefs(viewer: string | undefined): [DockPrefs, (change: (p: DockPrefs) => DockPrefs) => void] {
  const [state, setState] = useState(() => ({ viewer, prefs: readDockPrefs(viewer) }))
  const prefs = state.viewer === viewer ? state.prefs : readDockPrefs(viewer)
  useEffect(() => {
    if (state.viewer !== viewer) setState({ viewer, prefs: readDockPrefs(viewer) })
  }, [viewer, state.viewer])
  const update = useCallback(
    (change: (p: DockPrefs) => DockPrefs) =>
      setState((cur) => {
        const base = cur.viewer === viewer ? cur.prefs : readDockPrefs(viewer)
        const next = change(base)
        writeDockPrefs(viewer, next)
        return { viewer, prefs: next }
      }),
    [viewer],
  )
  return [prefs, update]
}

/** The window size, kept current (the dock's limits follow it). */
export function useViewport() {
  const read = () => ({ width: window.innerWidth, height: window.innerHeight })
  const [view, setView] = useState(read)
  useEffect(() => {
    const on = () => setView(read())
    window.addEventListener('resize', on)
    return () => window.removeEventListener('resize', on)
  }, [])
  return view
}

/**
 * Does the dock, open on the right, squeeze the page so much that the sidebar should be the rail (N11)? Yes when,
 * beside the wide sidebar, the page area would drop under `squeeze` px, or when the right side fits only beside the
 * rail. Worked out for the wide sidebar whatever the sidebar is now, so the answer does not flip when it collapses.
 */
export function dockSqueezesSidebar(prefs: Pick<DockPrefs, 'side' | 'open' | 'right'>, view: { width: number; height: number }, widths: { wide: number; rail: number; squeeze: number }): boolean {
  if (prefs.side !== 'right' || !prefs.open) return false
  const area = view.width - widths.wide
  if (!rightFits(view, area)) return rightFits(view, view.width - widths.rail)
  return area - clampDock('right', prefs.right, view, area) < widths.squeeze
}
