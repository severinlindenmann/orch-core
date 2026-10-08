import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

interface ShellUi {
  paletteOpen: boolean
  setPaletteOpen: (open: boolean) => void
  newTicketOpen: boolean
  setNewTicketOpen: (open: boolean) => void
  header: PageHeaderState
  setHeader: (h: PageHeaderState) => void
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
  const [newTicketOpen, setNewTicketOpen] = useState(false)
  const [header, setHeader] = useState<PageHeaderState>({})
  const value = useMemo(
    () => ({ paletteOpen, setPaletteOpen, newTicketOpen, setNewTicketOpen, header, setHeader }),
    [paletteOpen, newTicketOpen, header],
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
  const { setPaletteOpen, setNewTicketOpen } = useShellUi()
  return { openPalette: () => setPaletteOpen(true), openNewTicket: () => setNewTicketOpen(true) }
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
