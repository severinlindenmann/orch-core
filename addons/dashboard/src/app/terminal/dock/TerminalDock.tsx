// The open terminal dock (lazy chunk: xterm comes with it). One toolbar — the session switcher, New session,
// Sessions, ticket info and a menu — then the session browser when asked for, the terminal (core's TerminalView in
// its dock placement) and its strip. The terminals addon's rules hold unchanged: TerminalView decides who may type.

import { useQuery } from '@tanstack/react-query'
import { useRouter } from '@tanstack/react-router'
import { List, MoreHorizontal, PanelBottomClose, PanelRightClose, SquareTerminal } from 'lucide-react'
import { useEffect, useRef, useState, type KeyboardEvent, type MutableRefObject, type PointerEvent } from 'react'
import type { TerminalSessionView } from '@/api/terminals'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { addonHairline, addonRule } from '@/addon-ui/addonClasses'
import { useAddonStates } from '@/addon-ui/slots'
import { useRunAddonAction } from '@/addon-ui/useRunAddonAction'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuShortcut, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import TerminalView from '../TerminalView'
import { useWorkspace } from '../../workspace'
import { DOCK_ADDON, DOCK_KEYS, dockName, scopeKey, sessionsIn, useDockTicket, type DockMemory } from './context'
import { NewSessionButton, type NewSessionChoice } from './NewSession'
import { DOCK_LIMITS, dockMax, type DockPrefs, type DockSide } from './prefs'
import { SessionBrowser } from './SessionBrowser'
import { SessionTabs, type DockWindow } from './SessionTabs'
import { TicketInfo } from './TicketInfo'
import { queries } from '@/api/queries'

const STEP = 16
const BIG_STEP = 64
const NO_ROOM = 'Not enough room — dock at the bottom'

export interface TerminalDockProps {
  prefs: DockPrefs
  /** The side drawn (bottom when the right does not fit, whatever is stored). */
  side: DockSide
  size: number
  view: { width: number; height: number }
  /** Page plus dock width. */
  area: number
  rightFits: boolean
  setPrefs: (change: (p: DockPrefs) => DockPrefs) => void
  focus: MutableRefObject<'dock' | 'bar' | null>
  memory: DockMemory
  collapse: () => void
  /** A session an action opened (Worktrees' "Open terminal here"): select it and put the keyboard in it. */
  request?: string | null
  requestDone?: () => void
}

export default function TerminalDock(props: TerminalDockProps) {
  const { workspace } = useWorkspace()
  const page = useDockTicket()
  // A session started "in the workspace" from a ticket page belongs to the workspace: the dock follows it there
  // (and offers the way back). Navigating to another page drops the switch.
  const [wide, setWide] = useState<{ page: string | undefined; follow: string } | null>(null)
  const inWorkspace = !!wide && wide.page === page
  const ticket = inWorkspace ? undefined : page
  // One body per scope (workspace + ticket): its selection comes from, and goes back to, the dock's memory.
  return <DockBody key={scopeKey(workspace?.id, ticket)} {...props} ticket={ticket} pageTicket={page} followFrom={inWorkspace ? wide!.follow : null}
    toWorkspace={(follow) => setWide({ page, follow })} backToTicket={() => setWide(null)} />
}

function DockBody({ prefs, side, size, view, area, rightFits, setPrefs, focus, memory, collapse, request, requestDone, ticket, pageTicket, followFrom, toWorkspace, backToTicket }: TerminalDockProps & {
  ticket?: string
  /** The ticket of the page (may differ from `ticket` after switching to the workspace scope). */
  pageTicket?: string
  /** Mounted after that switch: select the new session once the addon has opened it (differs from this id). */
  followFrom: string | null
  toWorkspace: (follow: string) => void
  backToTicket: () => void
}) {
  const { workspace } = useWorkspace()
  const router = useRouter()
  const me = useQuery(queries.me())
  const { [DOCK_ADDON]: state } = useAddonStates(workspace?.id, [DOCK_ADDON])
  const inTicket = useRunAddonAction(ticket)
  const inWorkspace = useRunAddonAction()
  const region = useRef<HTMLElement>(null)
  const strip = useRef<HTMLDivElement>(null)
  const right = side === 'right'
  const key = scopeKey(workspace?.id, ticket)
  const remembered = memory.get(key)

  const sessions = (state?.sessions as TerminalSessionView[] | undefined) ?? []
  const { running, ended } = sessionsIn(sessions, ticket)
  const [selected, setSelected] = useState<string | null>(remembered?.selected ?? null)
  const [opened, setOpened] = useState<string[]>(remembered?.opened ?? [])
  const [browser, setBrowser] = useState(!remembered && !!ticket)
  const [newOpen, setNewOpen] = useState(false)
  const [focusId, setFocusId] = useState<string | null>(null)
  const follow = useRef<string | null>(followFrom) // after start/continue: select the session the addon opened
  const name = (s: TerminalSessionView) => dockName(s, ticket)
  const list = [...running, ...ended.filter((s) => opened.includes(s.id))]
  const windows: DockWindow[] = list.map((s, index) => ({ session: s, index, name: name(s) }))
  const mine = running.find((s) => s.interactive)
  const current = list.find((s) => s.id === selected)?.id ?? mine?.id ?? running[0]?.id ?? null
  const currentSession = list.find((s) => s.id === current)
  const currentId = (state?.current as { id?: string } | undefined)?.id

  useEffect(() => {
    memory.set(key, { selected, opened })
  }, [memory, key, selected, opened])
  useEffect(() => {
    if (follow.current === null || !currentId || currentId === follow.current) return
    follow.current = null
    setSelected(currentId)
    setFocusId(currentId)
  }, [currentId])
  const requestSeen = useRef<{ id: string; state: unknown } | null>(null)
  // A requested session: select it here, or switch to the workspace scope when this ticket's list does not hold it.
  useEffect(() => {
    if (!request) return
    if (list.some((s) => s.id === request) || ended.some((s) => s.id === request)) {
      requestDone?.()
      select(request)
      // The keyboard goes to the dock now, and into the terminal once xterm is up (select's focus step).
      region.current?.focus({ preventScroll: true })
    } else if (ticket && sessions.some((s) => s.id === request)) toWorkspace(currentId ?? '')
    // Not a session of this viewer (or gone) once the sessions were read again after the request: drop it, the dock
    // stays as it is (no focus grab, no waiting).
    else if (requestSeen.current && requestSeen.current.id === request && requestSeen.current.state !== state) requestDone?.()
    else if (!requestSeen.current || requestSeen.current.id !== request) requestSeen.current = { id: request, state }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request, sessions, state])
  // Opening the dock puts focus on the session strip (or the dock), so the keyboard is where the eye is.
  useEffect(() => {
    if (focus.current !== 'dock') return
    focus.current = null
    let tries = 0
    const timer = setInterval(() => {
      const target = region.current?.querySelector<HTMLElement>('[data-session-strip]')
      if (target || ++tries > 20) {
        clearInterval(timer)
        ;(target ?? region.current)?.focus({ preventScroll: true })
      }
    }, 50)
    return () => clearInterval(timer)
  }, [focus])
  // Joining or starting a session puts the keyboard in its terminal once xterm has mounted.
  useEffect(() => {
    if (!focusId) return
    let tries = 0
    const timer = setInterval(() => {
      // The last one: a terminal replaced a moment ago (StrictMode remount) is still in the DOM until it is disposed.
      const input = Array.from(region.current?.querySelectorAll<HTMLTextAreaElement>(`[data-terminal-session="${CSS.escape(focusId)}"] textarea`) ?? []).at(-1)
      if (input || ++tries > 100) {
        clearInterval(timer)
        input?.focus({ preventScroll: true })
        setFocusId(null)
      }
    }, 50)
    return () => clearInterval(timer)
  }, [focusId])

  const select = (id: string) => {
    if (ended.some((s) => s.id === id)) setOpened((o) => (o.includes(id) ? o : [...o, id]))
    setSelected(id)
    setFocusId(id)
    setBrowser(false)
  }
  const closeTranscript = (id: string) => {
    setOpened((o) => o.filter((x) => x !== id))
    if (selected === id) setSelected(null)
  }
  const followNext = (ok: boolean) => {
    if (!ok) follow.current = null // refused: nothing new to select
  }
  const start = (c: NewSessionChoice) => {
    const from = currentId ?? ''
    follow.current = from
    setBrowser(false)
    setPrefs((p) => ({ ...p, harness: c.harness }))
    // Started in the workspace from a ticket page: the session has no ticket, so show the workspace scope with it.
    const leaving = !c.inTicket && !!ticket
    ;(c.inTicket ? inTicket : inWorkspace).run(DOCK_ADDON, 'start', { harness: c.harness, context: c.summary }, undefined, {
      onDone: (ok) => {
        followNext(ok)
        if (ok && leaving) toWorkspace(from)
      },
    })
  }
  const continueFrom = (id: string) => {
    follow.current = currentId ?? ''
    setBrowser(false)
    inTicket.run(DOCK_ADDON, 'resume', { session: id }, undefined, { onDone: followNext })
  }
  const openFull = () => {
    if (current) inTicket.run(DOCK_ADDON, 'open', { session: current })
    void router.navigate({ to: '/addon/$name/$page', params: { name: DOCK_ADDON, page: 'sessions' } })
  }
  const resize = (px: number) => setPrefs((p) => ({ ...p, [side]: Math.round(Math.min(dockMax(side, view, area), Math.max(DOCK_LIMITS[side].min, px))) }))

  const canStart = inTicket.allowed(DOCK_ADDON, 'start')
  const now = sessions[0]?.ctx.now
  const scope = ticket ?? 'Workspace'
  const width = right ? size : area
  const visibleTabs = Math.max(1, Math.floor((width - (right ? 60 : 470)) / 150)) // right: the tabs have a row of their own
  const pane = right ? 'max-h-[60%] border-b border-border' : 'w-80 shrink-0 border-r border-border'
  const watching = currentSession && currentSession.status === 'running' && !currentSession.interactive
  const footer = watching ? (
    <div className="flex shrink-0 items-center gap-2 border-t border-border bg-surface px-2 py-1 text-xs text-text-muted">
      <span>Read only — this is the agent's session.</span>
      {canStart && <Button variant="link" size="xs" onClick={() => setNewOpen(true)}>Start your own session</Button>}
    </div>
  ) : undefined

  return (
    <section ref={region} tabIndex={-1} aria-label="Terminal dock" data-addon={DOCK_ADDON} data-dock-side={side}
      className={cn('@container/dock relative flex min-w-0 shrink-0 flex-col bg-bg outline-none', addonHairline, right ? 'border-l' : 'border-t')}
      style={right ? { width: size } : { height: size }}>
      <ResizeHandle side={side} size={size} max={dockMax(side, view, area)} onResize={resize} />
      <header className={cn('flex h-9 shrink-0 items-center gap-2 border-b bg-surface-2 px-2 text-xs', addonRule)}>
        <AddonBadge name={DOCK_ADDON} title="Terminals" />
        <SquareTerminal aria-hidden="true" className="size-3.5 shrink-0 text-text-muted" />
        {/* At the narrowest right-hand dock (320 px) the header still fits: the scope truncates, "New session" is an icon. */}
        <h2 className="shrink-0 text-xs font-semibold">Terminal</h2>
        <span className="min-w-0 truncate font-mono text-[11px] text-text-muted" title={ticket ? `Sessions of ${ticket}` : 'Sessions of this workspace'} data-dock-scope>{scope}</span>
        {!ticket && pageTicket && (
          <Button variant="link" size="xs" className="shrink-0 px-0" onClick={backToTicket}>Back to {pageTicket}</Button>
        )}
        {right ? <span className="flex-1" /> : <SessionTabs windows={windows} current={current} visible={visibleTabs} onSelect={select} onClose={closeTranscript} />}
        <NewSessionButton open={newOpen} onOpenChange={setNewOpen} ticket={ticket} lastHarness={prefs.harness} canStart={canStart} onStart={start} />
        <Button variant="ghost" size={right ? 'icon-xs' : 'xs'} aria-label="Sessions" title="Sessions" aria-pressed={browser} onClick={() => setBrowser(!browser)}>
          <List />{!right && 'Sessions'}
        </Button>
        <TicketInfo ticket={ticket} compact={right} />
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-xs" aria-label="Dock menu"><MoreHorizontal /></Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={openFull}>Open in Terminals</DropdownMenuItem>
            {right ? (
              <DropdownMenuItem onSelect={() => setPrefs((p) => ({ ...p, side: 'bottom' }))}>Move to the bottom</DropdownMenuItem>
            ) : (
              <DropdownMenuItem disabled={!rightFits} onSelect={() => setPrefs((p) => ({ ...p, side: 'right' }))}>{rightFits ? 'Move to the right' : NO_ROOM}</DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={collapse}>Collapse<DropdownMenuShortcut>{DOCK_KEYS}</DropdownMenuShortcut></DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon-xs" aria-label="Collapse the dock" aria-keyshortcuts="Control+Backquote" onClick={collapse}>
              {right ? <PanelRightClose /> : <PanelBottomClose />}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Collapse · {DOCK_KEYS}</TooltipContent>
        </Tooltip>
      </header>
      {right && (
        <div className="flex h-8 shrink-0 items-center border-b border-border bg-surface-2 px-2">
          <SessionTabs windows={windows} current={current} visible={visibleTabs} onSelect={select} onClose={closeTranscript} />
        </div>
      )}
      <div className={cn('flex min-h-0 flex-1', right ? 'flex-col' : 'flex-row')}>
        {(browser || !current) && state && (
          <SessionBrowser className={current ? pane : 'flex-1'} title={ticket ? `Sessions for ${ticket}` : 'Sessions in this workspace'} running={running} ended={ended} names={name}
            current={current} now={now} canStart={canStart} onSelect={select} onContinue={continueFrom} />
        )}
        {current && (
          <div className="min-h-0 min-w-0 flex-1">
            <TerminalView key={current} addon={DOCK_ADDON} session={current} placement="dock" dock={{ name: `${windows.find((w) => w.session.id === current)?.index ?? 0} ${name(currentSession!)}`, footer, stripRef: strip, compact: right }}
              fallback={<p className="p-3 text-xs text-text-muted">This session is no longer available.</p>} />
          </div>
        )}
        {(!state || !me.data) && <Skeleton className="m-3 h-24 flex-1" />}
      </div>
      {inTicket.dialog}
      {inWorkspace.dialog}
    </section>
  )
}

/** The drag edge with a visible grip: pointer drag, or arrow keys (Shift for bigger steps), Home/End for the limits. */
function ResizeHandle({ side, size, max, onResize }: { side: DockSide; size: number; max: number; onResize: (px: number) => void }) {
  const right = side === 'right'
  const min = DOCK_LIMITS[side].min
  const drag = useRef<{ start: number; size: number } | null>(null)
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.currentTarget.setPointerCapture?.(e.pointerId)
    drag.current = { start: right ? e.clientX : e.clientY, size }
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    const delta = drag.current.start - (right ? e.clientX : e.clientY) // dragging up / left makes the dock bigger
    onResize(drag.current.size + delta)
  }
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? BIG_STEP : STEP
    const grow = right ? 'ArrowLeft' : 'ArrowUp'
    const shrink = right ? 'ArrowRight' : 'ArrowDown'
    const next = e.key === grow ? size + step : e.key === shrink ? size - step : e.key === 'Home' ? min : e.key === 'End' ? max : null
    if (next === null) return
    e.preventDefault()
    onResize(next)
  }
  return (
    <div role="separator" tabIndex={0} aria-label="Resize terminal dock" title="Drag to resize · Arrow keys" aria-orientation={right ? 'vertical' : 'horizontal'} aria-valuemin={min} aria-valuemax={max} aria-valuenow={size}
      onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={() => (drag.current = null)} onPointerCancel={() => (drag.current = null)} onKeyDown={onKey}
      className={cn('group absolute z-10 flex items-center justify-center outline-none', right ? '-left-1.5 top-0 h-full w-3 cursor-col-resize' : '-top-1.5 left-0 h-3 w-full cursor-row-resize')}>
      <span aria-hidden="true" className={cn('rounded-full bg-border-strong group-hover:bg-brand group-focus-visible:bg-brand', right ? 'h-10 w-1' : 'h-1 w-10')} />
      <span className="pointer-events-none absolute hidden whitespace-nowrap rounded bg-surface-3 px-1.5 py-0.5 text-[11px] text-text group-hover:block group-focus-visible:block" style={right ? { left: 12 } : { top: -22 }}>
        Drag to resize · Arrow keys
      </span>
    </div>
  )
}
