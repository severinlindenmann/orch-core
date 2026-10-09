import { useQuery } from '@tanstack/react-query'
import { useRouterState } from '@tanstack/react-router'
import { api } from '@/api/client'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { railCollapsed, railToggle, type RailPref } from './railRule'

interface ShellUi {
  paletteOpen: boolean
  setPaletteOpen: (open: boolean) => void
  /** Text the palette starts with the next time it opens (a shortcut that wants to show one entry). */
  paletteSeed: string
  setPaletteSeed: (q: string) => void
  header: PageHeaderState
  setHeader: (h: PageHeaderState) => void
  /** The sidebar is the icon rail (the viewer's choice wins; without one, below 1280 px or beside a squeezing dock). */
  railCollapsed: boolean
  toggleRail: () => void
  /** The terminal dock reports whether it squeezes the page (open on the right, little room): see railRule.ts. */
  setDockSqueeze: (squeezed: boolean) => void
  /** The New ticket overlay (a right-hand sheet over the current page). */
  newTicketOpen: boolean
  setNewTicketOpen: (open: boolean) => void
  /** What had the focus when the overlay was asked for; it gets the focus back on close. */
  newTicketOpener: { current: HTMLElement | null }
}

/** The usual sidebar choice and the one made while the right-hand dock squeezes the page (N11), per viewer. */
const railKey = (viewer: string) => `orch.sidebar.${viewer}`
const railDockKey = (viewer: string) => `orch.sidebar.docked.${viewer}`
/** The earlier, browser-wide key: moved to the first viewer who loads the app, then removed. */
const LEGACY_RAIL_KEY = 'orch.sidebar'

function readPref(key: string, legacy?: string): RailPref {
  try {
    let v = localStorage.getItem(key)
    if (v === null && legacy) {
      const old = localStorage.getItem(legacy)
      if (old !== null) {
        localStorage.setItem(key, old)
        localStorage.removeItem(legacy)
        v = old
      }
    }
    return v === 'wide' || v === 'narrow' ? v : 'auto'
  } catch {
    return 'auto'
  }
}

function readRailPrefs(viewer: string | undefined): { viewer: string | undefined; pref: RailPref; dockPref: RailPref } {
  if (!viewer) return { viewer, pref: 'auto', dockPref: 'auto' }
  return { viewer, pref: readPref(railKey(viewer), LEGACY_RAIL_KEY), dockPref: readPref(railDockKey(viewer)) }
}

/** Wide or narrow (icon rail). Lives in the shell so the sidebar, the toaster and the dock agree on the rail width. */
function useRailState() {
  const viewer = useQuery({ queryKey: ['me'], queryFn: api.getMe }).data?.person
  const [stored, setStored] = useState(() => readRailPrefs(viewer))
  const current = stored.viewer === viewer ? stored : readRailPrefs(viewer)
  useEffect(() => {
    if (stored.viewer !== viewer) setStored(readRailPrefs(viewer))
  }, [viewer, stored.viewer])
  const { pref, dockPref } = current
  // Set by the terminal dock: open on the right and squeezing the page (see dockSqueezesSidebar).
  const [squeezed, setSqueezed] = useState(false)
  const [windowWidth, setWindowWidth] = useState(() => (typeof window === 'undefined' ? 1440 : window.innerWidth))
  useEffect(() => {
    const onResize = () => setWindowWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const inputs = { pref, dockPref, windowWidth, squeezed }
  const collapsed = railCollapsed(inputs)
  const latest = useRef(inputs)
  latest.current = inputs
  const latestViewer = useRef(viewer)
  latestViewer.current = viewer
  const toggle = useCallback(() => {
    const { which, value } = railToggle(latest.current)
    const who = latestViewer.current
    setStored((cur) => ({ ...cur, viewer: who, [which]: value }))
    try {
      if (who) localStorage.setItem(which === 'pref' ? railKey(who) : railDockKey(who), value)
    } catch {
      /* storage unavailable: the choice lasts for this page only */
    }
  }, [])
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
  return { collapsed, toggle, setSqueezed }
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
  const [newTicketOpen, setNewTicketOpen] = useState(false)
  const newTicketOpener = useRef<HTMLElement | null>(null)
  const value = useMemo(
    () => ({ paletteOpen, setPaletteOpen, paletteSeed, setPaletteSeed, header, setHeader, railCollapsed: rail.collapsed, toggleRail: rail.toggle, setDockSqueeze: rail.setSqueezed, newTicketOpen, setNewTicketOpen, newTicketOpener }),
    [paletteOpen, paletteSeed, header, rail.collapsed, rail.toggle, rail.setSqueezed, newTicketOpen],
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
  const { setPaletteOpen, setNewTicketOpen, newTicketOpener } = useShellUi()
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const openNewTicket = useCallback(() => {
    // On the full page already: the form is right there.
    if (pathname === '/tickets/new') return void document.getElementById('nt-title')?.focus()
    const active = document.activeElement as HTMLElement | null
    // Opened from the palette (or another dialog): that element is about to go, so the focus returns to the page.
    newTicketOpener.current = active && active !== document.body && !active.closest('[role="dialog"],[role="alertdialog"]') ? active : null
    setNewTicketOpen(true)
  }, [pathname, setNewTicketOpen, newTicketOpener])
  return { openPalette: () => setPaletteOpen(true), openNewTicket }
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
