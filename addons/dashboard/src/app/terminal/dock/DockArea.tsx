// The terminal dock's place in the shell: the page area plus the dock beside it (bottom or right), so the page
// shrinks and the dock never covers it. This file is in the entry chunk and stays light: the collapsed bar, the
// layout prefs, the page-width context and the Ctrl+` shortcut. The open dock (and xterm with it) is a lazy chunk.
// The dock is the terminals addon's surface, mounted by core: it shows only where that addon may use `pty`.

import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronUp, SquareTerminal } from 'lucide-react'
import { lazy, Suspense, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { api } from '@/api/client'
import type { TerminalSessionView } from '@/api/terminals'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { addonHairline } from '@/addon-ui/addonClasses'
import { canUsePty } from '@/addon-ui/capabilities'
import { useAddons, useAddonStates } from '@/addon-ui/slots'
import { cn } from '@/lib/utils'
import { PageWidthContext } from '../../pageWidth'
import { useWorkspace } from '../../workspace'
import { DOCK_ADDON, DOCK_KEYS, sessionsIn, useDockTicket, type DockMemory } from './context'
import { clampDock, DOCK_BAR, rightFits, useDockPrefs, useViewport } from './prefs'

const TerminalDock = lazy(() => import('./TerminalDock'))

/** CSS variable on <html>: the height a bottom dock takes (px), for things pinned to the bottom such as toasts. */
export const DOCK_BOTTOM_VAR = '--dock-bottom'

/** May the dock show here? The terminals addon must hold `pty` in this workspace (same gate as the terminal node). */
export function useDockAllowed(): boolean {
  const { data } = useAddons()
  const { workspace } = useWorkspace()
  return canUsePty(data?.find((a) => a.name === DOCK_ADDON), workspace?.addons[DOCK_ADDON])
}

/** Ctrl+` opens or collapses the dock, from anywhere (also from inside a terminal), unless a dialog is open. */
function useDockShortcut(enabled: boolean, toggle: () => void) {
  const latest = useRef(toggle)
  latest.current = toggle
  useEffect(() => {
    if (!enabled) return
    const onKey = (e: KeyboardEvent) => {
      if (!e.ctrlKey || e.metaKey || e.altKey || (e.key !== '`' && e.code !== 'Backquote')) return
      if (document.querySelector('[role="dialog"]')) return
      e.preventDefault()
      e.stopPropagation()
      latest.current()
    }
    // Capture: xterm stops the keys it handles from bubbling.
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [enabled])
}

/** The width of the page-plus-dock area (the window minus the sidebar); measured, with a fallback where layout is not real. */
function useAreaWidth(ref: React.RefObject<HTMLDivElement | null>, viewWidth: number): number {
  const [w, setW] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => setW(Math.round(el.getBoundingClientRect().width))
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])
  return w > 0 ? w : viewWidth - (viewWidth >= 1280 ? 232 : 56)
}

export function DockArea({ children }: { children: ReactNode }) {
  const allowed = useDockAllowed()
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const [prefs, setPrefs] = useDockPrefs(me.data?.person)
  const view = useViewport()
  const root = useRef<HTMLDivElement>(null)
  const area = useAreaWidth(root, view.width)
  const focus = useRef<'dock' | 'bar' | null>(null)
  // What the dock had selected, per workspace and ticket scope: survives collapse and navigation.
  const memory = useRef<DockMemory>(new Map())
  useDockShortcut(allowed, () => {
    setPrefs((p) => {
      focus.current = p.open ? 'bar' : 'dock'
      return { ...p, open: !p.open }
    })
  })
  // Not enough room on the right: the dock sits at the bottom until there is (the stored choice is kept).
  const fits = rightFits(view, area)
  const side = prefs.side === 'right' && fits ? 'right' : 'bottom'
  const right = side === 'right'
  const size = clampDock(side, prefs[side], view, area)
  const pageWidth = allowed && right ? view.width - (prefs.open ? size : DOCK_BAR) : view.width
  // Toasts sit above a bottom dock: the shell's toaster reads this variable (0 when the dock is not at the bottom).
  const bottom = allowed && !right ? (prefs.open ? size : DOCK_BAR) : 0
  useEffect(() => {
    document.documentElement.style.setProperty(DOCK_BOTTOM_VAR, `${bottom}px`)
  }, [bottom])
  useEffect(() => () => void document.documentElement.style.removeProperty(DOCK_BOTTOM_VAR), [])
  return (
    <div ref={root} className={cn('flex min-h-0 min-w-0 flex-1', right ? 'flex-row' : 'flex-col')}>
      <PageWidthContext.Provider value={pageWidth}>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">{children}</div>
      </PageWidthContext.Provider>
      {allowed &&
        (prefs.open ? (
          <Suspense fallback={<div aria-hidden="true" className={cn('shrink-0 bg-surface', addonHairline, right ? 'border-l' : 'border-t')} style={right ? { width: size } : { height: size }} />}>
            <TerminalDock prefs={prefs} side={side} size={size} view={view} area={area} rightFits={fits} setPrefs={setPrefs} focus={focus} memory={memory.current}
              collapse={() => {
                focus.current = 'bar'
                setPrefs((p) => ({ ...p, open: false }))
              }} />
          </Suspense>
        ) : (
          <DockBar right={right} focus={focus} open={() => {
            focus.current = 'dock'
            setPrefs((p) => ({ ...p, open: true }))
          }} />
        ))}
    </div>
  )
}

/** The collapsed dock: one 32 px button with the context and a count of running sessions. */
function DockBar({ right, open, focus }: { right: boolean; open: () => void; focus: React.MutableRefObject<'dock' | 'bar' | null> }) {
  const { workspace } = useWorkspace()
  const ticket = useDockTicket()
  const { [DOCK_ADDON]: state } = useAddonStates(workspace?.id, [DOCK_ADDON])
  const running = sessionsIn((state?.sessions as TerminalSessionView[] | undefined) ?? [], ticket).running.length
  const button = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (focus.current !== 'bar') return
    focus.current = null
    button.current?.focus()
  }, [focus])
  const label = `Open terminal dock${ticket ? ` for ${ticket}` : ''} (${DOCK_KEYS}) · ${running} running`
  return (
    <aside aria-label="Terminal dock" data-addon={DOCK_ADDON} className={cn('flex shrink-0 bg-surface', addonHairline, right ? 'border-l' : 'border-t')} style={right ? { width: DOCK_BAR } : { height: DOCK_BAR }}>
      <button ref={button} type="button" aria-label={label} aria-expanded={false} title={label} onClick={open}
        className={cn('flex flex-1 items-center gap-2 text-xs text-text-muted outline-none hover:bg-surface-2 hover:text-text focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand', right ? 'flex-col py-2' : 'px-3')}>
        <AddonBadge name={DOCK_ADDON} title="Terminals" />
        {right ? (
          <>
            <ChevronLeft aria-hidden="true" className="size-3.5" />
            <SquareTerminal aria-hidden="true" className="size-4" />
            {running > 0 && <span className="text-[11px] tabular-nums">{running}</span>}
          </>
        ) : (
          <>
            <SquareTerminal aria-hidden="true" className="size-3.5" />
            <span className="font-medium text-text">Terminal</span>
            <span className="truncate">
              {ticket ?? 'Workspace'} · {running} running
            </span>
            <span className="flex-1" />
            <kbd className="rounded border border-border px-1 font-mono text-[10px] text-text-faint">{DOCK_KEYS}</kbd>
            <ChevronUp aria-hidden="true" className="size-3.5" />
          </>
        )}
      </button>
    </aside>
  )
}
