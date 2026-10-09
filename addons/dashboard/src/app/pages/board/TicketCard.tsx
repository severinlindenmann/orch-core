import { useDraggable } from '@dnd-kit/core'
import { Bot, Layers, Lock } from 'lucide-react'
import type { TicketSummary } from '@/api/types'
import { AddonContributionView, useSlot } from '@/addon-ui'
import { can } from '@/api/permissions'
import { useRole } from '@/app/useRole'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { initials, PriorityMarker, TypeIcon } from './lib'

export interface BoardPeople {
  name: (id: string | null | undefined) => string
  epicTitle: (key: string) => string | undefined
}

export function CardFields({ ticket }: { ticket: TicketSummary }) {
  const items = useSlot('board.card_field', { ticket })
  const readOnly = !can(useRole(), 'addon.action')
  const shown = items.filter((c) => {
    if (c.waiting) return false // a field that reads addon state appears once it has loaded
    const data = ticket.addons?.[c.addon]
    return data && Object.keys(data).length > 0
  })
  if (shown.length === 0) return null
  return (
    <>
      {shown.map((c) => (
        <AddonContributionView key={`${c.addon}/${c.id}`} c={c} ctx={{ ticket }} compact readOnly={readOnly} />
      ))}
    </>
  )
}

export function People({ ticket, people }: { ticket: TicketSummary; people: BoardPeople }) {
  const ids = [ticket.owner, ...ticket.assignees].filter((x, i, a): x is string => !!x && a.indexOf(x) === i)
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

export function ClaimChip({ ticket, task, people }: { ticket: TicketSummary; task?: string; people: BoardPeople }) {
  if (!ticket.claim) return null
  return (
    <span className="inline-flex items-center gap-1 rounded-md bg-brand-soft px-1.5 py-0.5 text-[11px] text-brand">
      <Bot className="size-3" aria-hidden />
      <span>for {people.name(ticket.claim.for)}</span>
      {task && <span className="font-mono">{task}</span>}
    </span>
  )
}

interface CardProps {
  ticket: TicketSummary
  people: BoardPeople
  me: string | undefined
  task?: string
  canMove: boolean
  onOpen: (key: string) => void
}

export function TicketCardBody({
  ticket,
  people,
  me,
  task,
  overlay,
}: {
  ticket: TicketSummary
  people: BoardPeople
  me: string | undefined
  task?: string
  overlay?: boolean
}) {
  const needsYou = !!me && ticket.turn.who === me && ticket.status !== 'done'
  return (
    <div
      className={cn(
        'flex flex-col gap-2 rounded-lg border border-border bg-surface p-2.5 text-left',
        overlay && 'border-brand shadow-lg shadow-black/40',
      )}
    >
      <div className="flex items-center gap-1.5">
        <TypeIcon type={ticket.type} />
        <span className="font-mono text-[11px] text-text-muted">{ticket.key}</span>
        {needsYou && <span role="img" aria-label="Needs you" title={`Needs you: ${ticket.turn.why}`} className="size-1.5 rounded-full bg-brand" />}
        <span className="flex-1" />
        {ticket.restricted && <Lock role="img" aria-label="Restricted" className="size-3 text-text-faint" />}
        {ticket.size && <span className="rounded bg-surface-3 px-1 font-mono text-[10px] uppercase text-text-muted">{ticket.size}</span>}
        <PriorityMarker priority={ticket.priority} />
      </div>
      <div className="line-clamp-2 text-[13px] leading-snug text-text">{ticket.title}</div>
      {ticket.parent && (
        <span className="inline-flex max-w-full items-center gap-1 self-start rounded-md border border-border px-1.5 py-0.5 text-[11px] text-text-muted">
          <Layers className="size-3 shrink-0" aria-hidden />
          <span className="truncate">{people.epicTitle(ticket.parent) ?? ticket.parent}</span>
        </span>
      )}
      <ProgressBar ticket={ticket} />
      {(ticket.labels.length > 0 || ticket.claim) && (
        <div className="flex flex-wrap items-center gap-1">
          <ClaimChip ticket={ticket} task={task} people={people} />
          {ticket.labels.slice(0, 3).map((l) => (
            <span key={l} className="rounded-full border border-border px-1.5 text-[10px] leading-4 text-text-muted">
              {l}
            </span>
          ))}
          {ticket.labels.length > 3 && <span className="text-[10px] text-text-faint">+{ticket.labels.length - 3}</span>}
        </div>
      )}
      <div className="flex items-center gap-1.5">
        <People ticket={ticket} people={people} />
        <span className="flex-1" />
        <CardFields ticket={ticket} />
      </div>
    </div>
  )
}

export function TicketCard({ ticket, people, me, task, canMove, onOpen }: CardProps) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: ticket.key, data: { ticket }, disabled: !canMove })
  return (
    <div
      ref={setNodeRef}
      {...attributes}
      {...listeners}
      data-testid={`card-${ticket.key}`}
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
        listeners?.onKeyDown?.(e)
      }}
      className={cn(
        'rounded-lg outline-none transition-colors hover:[&>div]:border-border-strong focus-visible:ring-2 focus-visible:ring-brand',
        canMove ? 'cursor-grab active:cursor-grabbing' : 'cursor-pointer',
        isDragging && 'opacity-40',
      )}
    >
      <TicketCardBody ticket={ticket} people={people} me={me} task={task} />
    </div>
  )
}
