// The open terminal dock (lazy chunk: xterm comes with it). Header with the context (ticket or workspace) and addon
// chips, the session picker, the terminal (core's TerminalView in its dock placement) and a tmux-style status line
// whose window list is the dock's tabs. The terminals addon's rules hold unchanged: TerminalView decides who may type.

import { useQuery } from '@tanstack/react-query'
import { useRouter } from '@tanstack/react-router'
import { ChevronDown, ChevronRight, ExternalLink, List, PanelBottom, PanelRight } from 'lucide-react'
import { useEffect, useRef, useState, type KeyboardEvent, type MutableRefObject, type PointerEvent } from 'react'
import { api } from '@/api/client'
import type { HarnessId } from '@/api/harnesses'
import type { TerminalSessionView } from '@/api/terminals'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { addonHairline, addonRule } from '@/addon-ui/addonClasses'
import { useAddonStates } from '@/addon-ui/slots'
import { useRunAddonAction } from '@/addon-ui/useRunAddonAction'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import TerminalView from '../TerminalView'
import { useWorkspace } from '../../workspace'
import { DOCK_ADDON, DOCK_KEYS, sessionsIn, useDockTicket } from './context'
import { DockChips } from './DockChips'
import { clampDock, DOCK_LIMITS, dockMax, type DockPrefs } from './prefs'
import { SessionPicker } from './SessionPicker'
import { TmuxStatus } from './TmuxStatus'

const STEP = 16
const BIG_STEP = 64

export default function TerminalDock({ prefs, size, view, setPrefs, focusOnOpen }: {
  prefs: DockPrefs
  size: number
  view: { width: number; height: number }
  setPrefs: (change: (p: DockPrefs) => DockPrefs) => void
  focusOnOpen: MutableRefObject<boolean>
}) {
  const { workspace } = useWorkspace()
  const ticket = useDockTicket()
  const router = useRouter()
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const { [DOCK_ADDON]: state } = useAddonStates(workspace?.id, [DOCK_ADDON])
  const actions = useRunAddonAction(ticket)
  const region = useRef<HTMLElement>(null)
  const right = prefs.side === 'right'

  const sessions = (state?.sessions as TerminalSessionView[] | undefined) ?? []
  const { running, ended } = sessionsIn(sessions, ticket)
  const [selected, setSelected] = useState<string | null>(null)
  const [opened, setOpened] = useState<string[]>([]) // ended sessions opened as windows (transcripts)
  const [picker, setPicker] = useState<boolean>(!!ticket)
  const [focusId, setFocusId] = useState<string | null>(null)
  const follow = useRef<string | null>(null) // after start/resume: select the session the addon opened
  const windows = [...running, ...ended.filter((s) => opened.includes(s.id))]
  const mine = running.find((s) => s.interactive)
  const current = windows.find((s) => s.id === selected)?.id ?? mine?.id ?? running[0]?.id ?? null
  const currentId = (state?.current as { id?: string } | undefined)?.id

  // Another page: start again from that page's sessions.
  useEffect(() => {
    setSelected(null)
    setOpened([])
    setPicker(!!ticket)
  }, [ticket, workspace?.id])
  useEffect(() => {
    if (follow.current === null || !currentId || currentId === follow.current) return
    follow.current = null
    setSelected(currentId)
    setFocusId(currentId)
  }, [currentId])
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
  useEffect(() => {
    if (!focusOnOpen.current) return
    focusOnOpen.current = false
    region.current?.focus()
  }, [focusOnOpen])

  const select = (id: string) => {
    if (ended.some((s) => s.id === id)) setOpened((o) => (o.includes(id) ? o : [...o, id]))
    setSelected(id)
    setFocusId(id)
  }
  const start = (harness: HarnessId, context: boolean) => {
    follow.current = currentId ?? ''
    actions.run(DOCK_ADDON, 'start', { harness, context })
  }
  const resume = (id: string) => {
    follow.current = currentId ?? ''
    actions.run(DOCK_ADDON, 'resume', { session: id })
  }
  const openFull = () => {
    if (current) actions.run(DOCK_ADDON, 'open', { session: current })
    void router.navigate({ to: '/addon/$name/$page', params: { name: DOCK_ADDON, page: 'sessions' } })
  }
  const collapse = () => setPrefs((p) => ({ ...p, open: false }))
  const resize = (px: number) => setPrefs((p) => ({ ...p, [p.side]: clampDock(p.side, px, view) }))

  const canStart = actions.allowed(DOCK_ADDON, 'start')
  const now = sessions[0]?.ctx.now
  const title = ticket ?? 'Workspace'
  const pane = right ? 'max-h-[55%] border-b border-border' : 'w-80 shrink-0 border-r border-border'

  return (
    <section ref={region} tabIndex={-1} aria-label="Terminal dock" data-addon={DOCK_ADDON} data-dock-side={prefs.side}
      className={cn('relative flex shrink-0 flex-col bg-bg outline-none', addonHairline, right ? 'border-l' : 'border-t')}
      style={right ? { width: size } : { height: size }}>
      <ResizeHandle side={prefs.side} size={size} max={dockMax(prefs.side, view)} onResize={resize} />
      <header className={cn("flex h-8 shrink-0 items-center gap-2 border-b bg-surface-2 px-2 text-xs", addonRule)}>
        <AddonBadge name={DOCK_ADDON} title="Terminals" />
        <h2 className="shrink-0 text-[13px] font-semibold">Terminal</h2>
        <span className="min-w-0 truncate text-text-muted" title={title}>{ticket ? <TicketTitle ticket={ticket} /> : 'Workspace'}</span>
        {!right && <DockChips ticket={ticket} />}
        <span className="flex-1" />
        <Button variant="ghost" size="xs" aria-pressed={picker} onClick={() => setPicker(!picker)}><List />Sessions</Button>
        <Button variant="ghost" size="icon-xs" aria-label="Open in Terminals" title="Open in Terminals" onClick={openFull}><ExternalLink /></Button>
        <Button variant="ghost" size="icon-xs" aria-label={right ? 'Move dock to the bottom' : 'Move dock to the right'} title={right ? 'Move dock to the bottom' : 'Move dock to the right'}
          onClick={() => setPrefs((p) => ({ ...p, side: right ? 'bottom' : 'right' }))}>
          {right ? <PanelBottom /> : <PanelRight />}
        </Button>
        <Button variant="ghost" size="icon-xs" aria-label="Collapse terminal dock" title={`Collapse (${DOCK_KEYS})`} aria-expanded={true} onClick={collapse}>
          {right ? <ChevronRight /> : <ChevronDown />}
        </Button>
      </header>
      {right && <DockChips ticket={ticket} row />}
      <div className={cn('flex min-h-0 flex-1', right ? 'flex-col' : 'flex-row')}>
        {(picker || !current) && me.data && (
          <SessionPicker className={current ? pane : 'flex-1'} ticket={ticket} running={running} ended={ended} current={current} me={me.data.person} now={now} canStart={canStart}
            onSelect={select} onStart={start} onResume={resume} />
        )}
        {current && (
          <div className="min-h-0 min-w-0 flex-1">
            <TerminalView key={current} addon={DOCK_ADDON} session={current} placement="dock" fallback={<p className="p-3 text-xs text-text-muted">This session is no longer available.</p>} />
          </div>
        )}
        {!state && <Skeleton className="m-3 h-24 flex-1" />}
      </div>
      <TmuxStatus name={workspace?.prefix ?? 'orch'} windows={windows} current={current} ticket={ticket} now={now} canNew={canStart} onSelect={select} onNew={() => setPicker(true)} />
      {actions.dialog}
    </section>
  )
}

/** "DEMO-0043 · Load tariff tables" from the ticket query the page already holds. */
function TicketTitle({ ticket }: { ticket: string }) {
  const t = useQuery({ queryKey: ['ticket', ticket], queryFn: () => api.getTicket(ticket), staleTime: 10_000 })
  return <>{ticket}{t.data ? ` · ${t.data.title}` : ''}</>
}

/** The drag edge: pointer drag, or arrow keys (Shift for bigger steps), Home/End for the limits. */
function ResizeHandle({ side, size, max, onResize }: { side: DockPrefs['side']; size: number; max: number; onResize: (px: number) => void }) {
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
    <div role="separator" tabIndex={0} aria-label="Resize terminal dock" aria-orientation={right ? 'vertical' : 'horizontal'} aria-valuemin={min} aria-valuemax={max} aria-valuenow={size}
      onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={() => (drag.current = null)} onPointerCancel={() => (drag.current = null)} onKeyDown={onKey}
      className={cn('absolute z-10 outline-none hover:bg-brand/40 focus-visible:bg-brand/60', right ? '-left-1 top-0 h-full w-2 cursor-col-resize' : '-top-1 left-0 h-2 w-full cursor-row-resize')} />
  )
}
