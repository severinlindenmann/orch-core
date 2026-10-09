// The terminal dock's place in the shell: the page area plus the dock beside it (bottom or right), so the page
// shrinks and the dock never covers it. This file is in the entry chunk and stays light: the collapsed bar, the
// layout prefs and the ⌃` shortcut. The open dock (and xterm with it) is a lazy chunk.
// The dock is the terminals addon's surface, mounted by core: it shows only where that addon may use `pty`.

import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronUp, SquareTerminal } from 'lucide-react'
import { lazy, Suspense, useEffect, useRef, type ReactNode } from 'react'
import { api } from '@/api/client'
import type { TerminalSessionView } from '@/api/terminals'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { addonHairline } from '@/addon-ui/addonClasses'
import { canUsePty } from '@/addon-ui/capabilities'
import { useAddons, useAddonStates } from '@/addon-ui/slots'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useWorkspace } from '../../workspace'
import { DOCK_ADDON, DOCK_KEYS, sessionsIn, useDockTicket } from './context'
import { clampDock, DOCK_BAR, useDockPrefs, useViewport, type DockPrefs } from './prefs'

const TerminalDock = lazy(() => import('./TerminalDock'))

/** May the dock show here? The terminals addon must hold `pty` in this workspace (same gate as the terminal node). */
export function useDockAllowed(): boolean {
  const { data } = useAddons()
  const { workspace } = useWorkspace()
  return canUsePty(data?.find((a) => a.name === DOCK_ADDON), workspace?.addons[DOCK_ADDON])
}

/** ⌃` opens or collapses the dock, from anywhere (also from inside a terminal), unless a dialog is open. */
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

export function DockArea({ children }: { children: ReactNode }) {
  const allowed = useDockAllowed()
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const [prefs, setPrefs] = useDockPrefs(me.data?.person)
  const view = useViewport()
  const focusOnOpen = useRef(false)
  useDockShortcut(allowed, () => {
    setPrefs((p) => {
      focusOnOpen.current = !p.open
      return { ...p, open: !p.open }
    })
  })
  const right = prefs.side === 'right'
  const size = clampDock(prefs.side, prefs[prefs.side], view)
  return (
    <div className={cn('flex min-h-0 min-w-0 flex-1', right ? 'flex-row' : 'flex-col')}>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">{children}</div>
      {allowed &&
        (prefs.open ? (
          <Suspense fallback={<div aria-hidden="true" className={cn('shrink-0 bg-surface', addonHairline, right ? 'border-l' : 'border-t')} style={right ? { width: size } : { height: size }} />}>
            <TerminalDock prefs={prefs} size={size} view={view} setPrefs={setPrefs} focusOnOpen={focusOnOpen} />
          </Suspense>
        ) : (
          <DockBar prefs={prefs} open={() => setPrefs((p) => ({ ...p, open: true }))} />
        ))}
    </div>
  )
}

/** The collapsed dock: a 32 px bar with the context and a count of running sessions. */
function DockBar({ prefs, open }: { prefs: DockPrefs; open: () => void }) {
  const { workspace } = useWorkspace()
  const ticket = useDockTicket()
  const { [DOCK_ADDON]: state } = useAddonStates(workspace?.id, [DOCK_ADDON])
  const running = sessionsIn((state?.sessions as TerminalSessionView[] | undefined) ?? [], ticket).running.length
  const label = `Open terminal dock${ticket ? ` for ${ticket}` : ''} (${DOCK_KEYS})`
  if (prefs.side === 'right')
    return (
      <aside aria-label="Terminal dock" data-addon={DOCK_ADDON} className={cn("flex shrink-0 flex-col items-center gap-2 border-l bg-surface py-2", addonHairline)} style={{ width: DOCK_BAR }}>
        <AddonBadge name={DOCK_ADDON} title="Terminals" />
        <Button variant="ghost" size="icon-xs" aria-label={label} aria-expanded={false} title={label} onClick={open}>
          <ChevronLeft />
        </Button>
        <SquareTerminal aria-hidden="true" className="size-4 text-text-muted" />
        {running > 0 && <span className="text-[11px] tabular-nums text-text-muted" title={`${running} running`}>{running}</span>}
      </aside>
    )
  return (
    <aside aria-label="Terminal dock" data-addon={DOCK_ADDON} className={cn("flex shrink-0 items-center gap-2 border-t bg-surface px-3 text-xs", addonHairline)} style={{ height: DOCK_BAR }}>
      <AddonBadge name={DOCK_ADDON} title="Terminals" />
      <SquareTerminal aria-hidden="true" className="size-3.5 text-text-muted" />
      <span className="font-medium">Terminal</span>
      <span className="truncate text-text-muted">
        {ticket ?? 'Workspace'} · {running} running
      </span>
      <span className="flex-1" />
      <kbd className="rounded border border-border px-1 font-mono text-[10px] text-text-faint">⌃`</kbd>
      <Button variant="ghost" size="icon-xs" aria-label={label} aria-expanded={false} title={label} onClick={open}>
        <ChevronUp />
      </Button>
    </aside>
  )
}
