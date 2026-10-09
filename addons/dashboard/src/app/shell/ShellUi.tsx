import { useNavigate } from '@tanstack/react-router'
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

interface ShellUi {
  paletteOpen: boolean
  setPaletteOpen: (open: boolean) => void
  /** Text the palette starts with the next time it opens (a shortcut that wants to show one entry). */
  paletteSeed: string
  setPaletteSeed: (q: string) => void
  header: PageHeaderState
  setHeader: (h: PageHeaderState) => void
  /** The sidebar is the icon rail (the viewer's choice wins; without one, below 1280 px). */
  railCollapsed: boolean
  toggleRail: () => void
}

type RailPref = 'auto' | 'wide' | 'narrow'
const RAIL_KEY = 'orch.sidebar'

function readRailPref(): RailPref {
  try {
    const v = localStorage.getItem(RAIL_KEY)
    return v === 'wide' || v === 'narrow' ? v : 'auto'
  } catch {
    return 'auto'
  }
}

/** Wide or narrow (icon rail). Lives in the shell so the sidebar and the toaster agree on the rail width. */
function useRailState() {
  const [pref, setPref] = useState<RailPref>(readRailPref)
  const [narrowWindow, setNarrowWindow] = useState(() => typeof window !== 'undefined' && window.innerWidth < 1280)
  useEffect(() => {
    const onResize = () => setNarrowWindow(window.innerWidth < 1280)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const collapsed = pref === 'auto' ? narrowWindow : pref === 'narrow'
  const toggle = useCallback(() => {
    const next: RailPref = collapsed ? 'wide' : 'narrow'
    setPref(next)
    try {
      localStorage.setItem(RAIL_KEY, next)
    } catch {
      /* storage unavailable: the choice lasts for this page only */
    }
  }, [collapsed])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '[' || e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target as HTMLElement | null
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
      e.preventDefault()
      toggle()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggle])
  return { collapsed, toggle }
}

export interface PageHeaderState {
  /** Overrides the default title derived from the route. */
  title?: ReactNode
  /** Rendered before the title (a shadcn Breadcrumb, typically). */
  breadcrumb?: ReactNode
}

const Ctx = createContext<ShellUi | null>(null)

export function ShellUiProvider({ children }: { children: ReactNode }) {
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [paletteSeed, setPaletteSeed] = useState('')
  const [header, setHeader] = useState<PageHeaderState>({})
  const rail = useRailState()
  const value = useMemo(
    () => ({ paletteOpen, setPaletteOpen, paletteSeed, setPaletteSeed, header, setHeader, railCollapsed: rail.collapsed, toggleRail: rail.toggle }),
    [paletteOpen, paletteSeed, header, rail.collapsed, rail.toggle],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

function useShellUi() {
  const v = useContext(Ctx)
  if (!v) throw new Error('ShellUiProvider missing')
  return v
}

/** Pages and components open the global overlays through this. */
export function useShellActions() {
  const { setPaletteOpen } = useShellUi()
  const navigate = useNavigate()
  return { openPalette: () => setPaletteOpen(true), openNewTicket: () => void navigate({ to: '/tickets/new' }) }
}

export const useShellState = useShellUi

/**
 * Sets the topbar title and breadcrumb for the current page (reset on unmount).
 * Pass a stable `breadcrumb` element (or memoize it) to avoid re-registering on every render.
 */
export function usePageHeader(title?: ReactNode, breadcrumb?: ReactNode) {
  const { setHeader } = useShellUi()
  useEffect(() => {
    setHeader({ title, breadcrumb })
    return () => setHeader({})
  }, [setHeader, title, breadcrumb])
}
