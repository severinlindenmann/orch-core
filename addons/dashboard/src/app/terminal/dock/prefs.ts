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
}

/** Size limits: bottom 160 px to 70% of the window height, right 320 px to 60% of the window width. */
export const DOCK_LIMITS = {
  bottom: { min: 160, ratio: 0.7 },
  right: { min: 320, ratio: 0.6 },
} as const

/** The collapsed bar, px. */
export const DOCK_BAR = 32

export const DEFAULT_PREFS: DockPrefs = { side: 'bottom', open: false, bottom: 280, right: 440 }

const keyOf = (viewer: string) => `orch.dock.${viewer}`

export function dockMax(side: DockSide, view: { width: number; height: number }): number {
  const l = DOCK_LIMITS[side]
  return Math.max(l.min, Math.floor((side === 'bottom' ? view.height : view.width) * l.ratio))
}

export function clampDock(side: DockSide, px: number, view: { width: number; height: number }): number {
  return Math.round(Math.min(dockMax(side, view), Math.max(DOCK_LIMITS[side].min, Number.isFinite(px) ? px : DEFAULT_PREFS[side])))
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
