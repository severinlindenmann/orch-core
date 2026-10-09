import { useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useDraggable } from '@dnd-kit/core'
import { Bot, CircleAlert, Lock } from 'lucide-react'
import { STATUSES, type Status, type TicketSummary } from '@/api/types'
import { AddonContributionView, useSlot } from '@/addon-ui'
import { can } from '@/api/permissions'
import { useRole } from '@/app/useRole'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { initials, PriorityMarker, STATUS_LABEL, TypeIcon, type BoardDisplay } from './lib'

export interface BoardPeople {
  name: (id: string | null | undefined) => string
  epicTitle: (key: string) => string | undefined
}

/** The addon fields of a card (estimate...). `own`: an epic's own value, drawn as one dashed pill with an "own" label, so it never reads as part of a sum beside it. */
export function CardFields({ ticket, own }: { ticket: TicketSummary; own?: boolean }) {
  const items = useSlot('board.card_field', { ticket })
  const readOnly = !can(useRole(), 'addon.action')
  const shown = items.filter((c) => {
    if (c.waiting) return false // a field that reads addon state appears once it has loaded
    if (c.guarded) return true // its `when` held (e.g. a landing chip read from the addon's state)
    const data = ticket.addons?.[c.addon]
    return data && Object.keys(data).length > 0
  })
  if (shown.length === 0) return null
  const views = shown.map((c) => <AddonContributionView key={`${c.addon}/${c.id}`} c={c} ctx={{ ticket }} compact readOnly={readOnly} />)
  if (!own) return <>{views}</>
  return (
    <span title="The epic's own estimate, not a sum of its children" className="inline-flex items-center gap-1 rounded-full border border-dashed border-border-strong pl-1.5">
      <span className="text-[11px] text-text-faint">own</span>
      {views}
    </span>
  )
}

export function People({ ticket, people, max }: { ticket: TicketSummary; people: BoardPeople; max?: number }) {
  const all = [ticket.owner, ...ticket.assignees].filter((x, i, a): x is string => !!x && a.indexOf(x) === i)
  const ids = max ? all.slice(0, max) : all
  if (ids.length === 0) return null
  return (
    <span className="flex -space-x-1.5">
      {ids.map((id) => (
        <Tooltip key={id}>
          <TooltipTrigger asChild>
            <Avatar size="sm" className="size-5 ring-2 ring-surface" aria-label={people.name(id)}>
              <AvatarFallback className="bg-surface-3 text-[9px] font-medium text-text-muted">{initials(people.name(id))}</AvatarFallback>
            </Avatar>
          </TooltipTrigger>
          <TooltipContent>
            {people.name(id)}
            {id === ticket.owner ? ' (owner)' : ''}
          </TooltipContent>
        </Tooltip>
      ))}
      {max !== undefined && all.length > max && (
        <span className="z-10 grid size-5 place-items-center rounded-full bg-surface-3 text-[9px] text-text-muted ring-2 ring-surface">+{all.length - max}</span>
      )}
    </span>
  )
}

export function ProgressBar({ ticket }: { ticket: TicketSummary }) {
  const { tasks_done, tasks_total, ac_proven, ac_total } = ticket.progress
  if (tasks_total === 0 && ac_total === 0) return null
  const done = tasks_total ? tasks_done : ac_proven
  const total = tasks_total || ac_total
  const label = tasks_total ? 'tasks' : 'acceptance criteria'
  return (
    <div className="flex items-center gap-2" title={`${done} of ${total} ${label}`}>
      <div
        role="progressbar"
        aria-label={`${label} progress`}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done}
        className="h-1 flex-1 overflow-hidden rounded-full bg-surface-3"
      >
        <div className={cn('h-full rounded-full', done === total ? 'bg-success' : 'bg-brand')} style={{ width: `${(done / total) * 100}%` }} />
      </div>
      <span className="font-mono text-[10px] text-text-faint">
        {done}/{total}
      </span>
    </div>
  )
}

const agentTitle = (id: string) => id.split('-').map((w) => w[0].toUpperCase() + w.slice(1)).join(' ')

/** The one state glyph on line 1: needs you, blocked, or an agent working on it. */
function StateGlyph({ ticket, people, me, task }: { ticket: TicketSummary; people: BoardPeople; me: string | undefined; task?: string }) {
  if (me && ticket.turn.who === me && ticket.status !== 'done') {
    return <span role="img" aria-label="Needs you" title={`Needs you: ${ticket.turn.why}`} className="size-1.5 rounded-full bg-brand" />
  }
  if (ticket.blocking_questions > 0) {
    const n = ticket.blocking_questions
    return (
      <span title={`Blocked by ${n} open question${n > 1 ? 's' : ''}`} className="inline-flex">
        <CircleAlert role="img" aria-label="Blocked" className="size-3.5 text-danger" />
      </span>
    )
  }
  if (ticket.claim) {
    const tip = `${agentTitle(ticket.claim.agent)} for ${people.name(ticket.claim.for)}${task ? ` · ${task}` : ''}`
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={-1} className="inline-flex">
            <Bot role="img" aria-label="Agent working" className="size-3.5 text-brand" />
          </span>
        </TooltipTrigger>
        <TooltipContent>{tip}</TooltipContent>
      </Tooltip>
    )
  }
  return null
}

const DEFAULT_CARD_DISPLAY: Pick<BoardDisplay, 'density' | 'labels' | 'estimate' | 'progress'> = { density: 'comfortable', labels: true, estimate: true, progress: false }

export type CardVariant = 'card' | 'lane'

interface CardProps {
  ticket: TicketSummary
  people: BoardPeople
  me: string | undefined
  task?: string
  canMove: boolean
  display?: Pick<BoardDisplay, 'density' | 'labels' | 'estimate' | 'progress'>
  /** `lane`: the two-line card of an epic's child. */
  variant?: CardVariant
  onOpen: (key: string) => void
  onMove?: (key: string, status: Status) => void
}

/** Two lines for a child under its epic: key, state, priority; then the title with the estimate at its end. The epic is the lane, so no chip. */
function LaneCardBody({ ticket, people, me, task, overlay, showEstimate, showProgress }: { ticket: TicketSummary; people: BoardPeople; me: string | undefined; task?: string; overlay?: boolean; showEstimate: boolean; showProgress: boolean }) {
  return (
    <div className={cn('flex flex-col gap-0.5 rounded-md border border-border bg-surface px-2 py-1.5 text-left', overlay && 'border-brand shadow-lg shadow-black/40')}>
      <div className="flex items-center gap-1.5">
        <TypeIcon type={ticket.type} />
        <span className="shrink-0 whitespace-nowrap font-mono text-[11px] text-text-muted">{ticket.key}</span>
        <StateGlyph ticket={ticket} people={people} me={me} task={task} />
        {ticket.restricted && <Lock role="img" aria-label="Restricted" className="size-3 text-text-faint" />}
        <span className="min-w-0 flex-1" />
        <PriorityMarker priority={ticket.priority} />
      </div>
      <div className="flex min-w-0 items-center gap-1.5">
        <div className="min-w-0 flex-1 truncate text-[12px] leading-snug text-text" title={ticket.title}>
          {ticket.title}
        </div>
        {showEstimate && <CardFields ticket={ticket} />}
      </div>
      {showProgress && <ProgressBar ticket={ticket} />}
    </div>
  )
}

/** Three lines: key and state, title, people / chip / estimate. Progress only when Display asks for it. */
export function TicketCardBody({
  ticket,
  people,
  me,
  task,
  overlay,
  display = DEFAULT_CARD_DISPLAY,
  variant = 'card',
}: {
  ticket: TicketSummary
  people: BoardPeople
  me: string | undefined
  task?: string
  overlay?: boolean
  display?: CardProps['display']
  variant?: CardVariant
}) {
  const d = display ?? DEFAULT_CARD_DISPLAY
  if (variant === 'lane') return <LaneCardBody ticket={ticket} people={people} me={me} task={task} overlay={overlay} showEstimate={d.estimate} showProgress={d.progress} />
  const compact = d.density === 'compact'
  const chip = ticket.parent ? (people.epicTitle(ticket.parent) ?? ticket.parent) : ticket.labels[0]
  const more = ticket.labels.length - (ticket.parent ? 0 : 1)
  return (
    <div
      className={cn(
        'flex flex-col rounded-lg border border-border bg-surface text-left',
        compact ? 'gap-1 p-2' : 'gap-1.5 p-2.5',
        overlay && 'border-brand shadow-lg shadow-black/40',
      )}
    >
      <div className="flex items-center gap-1.5">
        <TypeIcon type={ticket.type} />
        <span className="font-mono text-[11px] text-text-muted">{ticket.key}</span>
        <StateGlyph ticket={ticket} people={people} me={me} task={task} />
        {ticket.restricted && <Lock role="img" aria-label="Restricted" className="size-3 text-text-faint" />}
        <span className="flex-1" />
        <PriorityMarker priority={ticket.priority} />
      </div>
      <div className={cn('text-[13px] leading-snug text-text', compact ? 'line-clamp-1' : 'line-clamp-2')}>{ticket.title}</div>
      {d.progress && <ProgressBar ticket={ticket} />}
      <div className="flex min-w-0 items-center gap-1.5">
        <People ticket={ticket} people={people} max={2} />
        <span className="flex min-w-0 flex-1 items-center gap-1">
          {d.labels && chip && (
            <span title={chip} className="inline-flex min-w-0 items-center rounded-full border border-border px-1.5 text-[10px] leading-4 text-text-muted">
              <span className="truncate">{chip}</span>
            </span>
          )}
          {d.labels && more > 0 && (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="shrink-0 text-[10px] text-text-faint">+{more}</span>
              </TooltipTrigger>
              <TooltipContent>
                {more} more label{more > 1 ? 's' : ''}
              </TooltipContent>
            </Tooltip>
          )}
        </span>
        {d.estimate && <CardFields ticket={ticket} />}
      </div>
    </div>
  )
}

/** "Move to…" for the keyboard: a menu of statuses beside the card. */
function MoveMenu({
  ticket,
  anchor,
  onPick,
  onClose,
}: {
  ticket: TicketSummary
  anchor: HTMLElement | null
  onPick: (s: Status) => void
  /** `refocus`: the person dismissed it (Esc); false when focus already went elsewhere. */
  onClose: (refocus: boolean) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  // Drawn in a portal at the card's corner so the column's scroller cannot clip it; flips up near the bottom of the window.
  useLayoutEffect(() => {
    const menu = ref.current
    if (!menu || !anchor) return
    const a = anchor.getBoundingClientRect()
    const h = menu.offsetHeight
    const top = a.top + 28 + h > window.innerHeight - 8 ? Math.max(8, a.bottom - 28 - h) : a.top + 28
    setPos({ left: Math.min(a.left + 8, window.innerWidth - menu.offsetWidth - 8), top })
  }, [anchor])
  useLayoutEffect(() => {
    if (pos) ref.current?.querySelector<HTMLElement>('[role=menuitem]:not([aria-disabled=true])')?.focus()
  }, [pos])
  const items = STATUSES.filter((s) => s !== ticket.status)
  return createPortal(
    <div
      ref={ref}
      role="menu"
      aria-label="Move to"
      style={{ position: 'fixed', left: pos?.left ?? 0, top: pos?.top ?? 0, visibility: pos ? 'visible' : 'hidden' }}
      className="z-50 flex min-w-40 flex-col rounded-md border border-border-strong bg-popover p-1 shadow-lg shadow-black/40"
      onKeyDown={(e) => {
        e.stopPropagation()
        const els = [...(ref.current?.querySelectorAll<HTMLElement>('[role=menuitem]:not([aria-disabled=true])') ?? [])]
        const at = els.indexOf(document.activeElement as HTMLElement)
        if (e.key === 'Escape') {
          e.preventDefault()
          onClose(true)
        } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault()
          els[(at + (e.key === 'ArrowDown' ? 1 : -1) + els.length) % els.length]?.focus()
        } else if (e.key === 'Tab') onClose(false)
      }}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) onClose(false)
      }}
    >
      <div className="px-2 py-1 text-[11px] text-text-faint">Move {ticket.key} to</div>
      {items.map((s) => {
        const verdict = s === 'done'
        return (
          <button
            key={s}
            type="button"
            role="menuitem"
            aria-disabled={verdict || undefined}
            tabIndex={-1}
            onClick={(e) => {
              e.stopPropagation()
              if (!verdict) onPick(s)
            }}
            className="flex items-center justify-between gap-3 rounded px-2 py-1 text-left text-[12px] text-text outline-none hover:bg-accent focus:bg-accent aria-disabled:cursor-not-allowed aria-disabled:text-text-faint"
          >
            {STATUS_LABEL[s]}
            {verdict && <span className="text-[10px]">by verdict</span>}
          </button>
        )
      })}
    </div>,
    document.body,
  )
}

export function TicketCard({ ticket, people, me, task, canMove, display, variant, onOpen, onMove }: CardProps) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: ticket.key, data: { ticket }, disabled: !canMove })
  const [menu, setMenu] = useState(false)
  const cardRef = useRef<HTMLDivElement | null>(null)
  const closeMenu = (refocus: boolean) => {
    setMenu(false)
    if (refocus) cardRef.current?.focus()
  }
  return (
    <div className="relative">
      <div
        ref={(el) => {
          cardRef.current = el
          setNodeRef(el)
        }}
        {...(canMove ? attributes : {})}
        {...(canMove ? listeners : {})}
        data-testid={`card-${ticket.key}`}
        data-ticket={ticket.key}
        data-status={ticket.status}
        aria-label={`${ticket.key} ${ticket.title}`}
        tabIndex={0}
        role="button"
        onClick={() => onOpen(ticket.key)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            onOpen(ticket.key)
            return
          }
          if (e.key === 'm' && canMove && onMove && !e.metaKey && !e.ctrlKey && !e.altKey) {
            e.preventDefault()
            setMenu(true)
            return
          }
          if (canMove) listeners?.onKeyDown?.(e)
        }}
        className={cn(
          'rounded-lg outline-none transition-colors hover:[&>div]:border-border-strong focus-visible:ring-2 focus-visible:ring-brand',
          canMove ? 'cursor-grab active:cursor-grabbing' : 'cursor-pointer',
          isDragging && 'opacity-40',
        )}
      >
        <TicketCardBody ticket={ticket} people={people} me={me} task={task} display={display} variant={variant} />
      </div>
      {menu && (
        <MoveMenu
          ticket={ticket}
          anchor={cardRef.current}
          onClose={closeMenu}
          onPick={(s) => {
            setMenu(false)
            onMove?.(ticket.key, s)
          }}
        />
      )}
    </div>
  )
}
