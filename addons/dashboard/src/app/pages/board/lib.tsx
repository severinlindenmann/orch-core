import { Bug, ChevronDown, ChevronUp, ChevronsUp, Equal, FlaskConical, Layers, Sparkles, Wrench, type LucideIcon } from 'lucide-react'
import type { Priority, Status, TicketSummary, TicketType } from '@/api/types'
import { cn } from '@/lib/utils'

export const STATUS_LABEL: Record<Status, string> = {
  backlog: 'Backlog',
  open: 'Open',
  'in-progress': 'In progress',
  waiting: 'Waiting',
  testing: 'Testing',
  done: 'Done',
}

export const TYPE_ICON: Record<TicketType, LucideIcon> = {
  feature: Sparkles,
  bug: Bug,
  chore: Wrench,
  spike: FlaskConical,
  epic: Layers,
}

const PRIORITY: Record<Priority, { icon: LucideIcon; cls: string }> = {
  low: { icon: ChevronDown, cls: 'text-text-faint' },
  medium: { icon: Equal, cls: 'text-text-muted' },
  high: { icon: ChevronUp, cls: 'text-warning' },
  urgent: { icon: ChevronsUp, cls: 'text-danger' },
}
export const PRIORITY_RANK: Record<Priority, number> = { low: 0, medium: 1, high: 2, urgent: 3 }

export function PriorityMarker({ priority }: { priority: Priority }) {
  const { icon: Icon, cls } = PRIORITY[priority]
  return <Icon role="img" aria-label={`Priority ${priority}`} className={cn('size-3.5 shrink-0', cls)} />
}

export function TypeIcon({ type, className }: { type: TicketType; className?: string }) {
  const Icon = TYPE_ICON[type]
  return <Icon role="img" aria-label={`Type ${type}`} className={cn('size-3.5 shrink-0 text-text-muted', className)} />
}

export interface Filters {
  mine: boolean
  type: string
  label: string
  person: string
  epic: string
  q: string
}
export const NO_FILTERS: Filters = { mine: false, type: 'all', label: 'all', person: 'all', epic: 'all', q: '' }

export function isOwnedBy(t: TicketSummary, person: string | undefined) {
  return !!person && (t.owner === person || t.assignees.includes(person))
}

export function applyFilters(tickets: TicketSummary[], f: Filters, me: string | undefined): TicketSummary[] {
  const q = f.q.trim().toLowerCase()
  return tickets.filter(
    (t) =>
      (!f.mine || isOwnedBy(t, me)) &&
      (f.type === 'all' || t.type === f.type) &&
      (f.label === 'all' || t.labels.includes(f.label)) &&
      (f.person === 'all' || isOwnedBy(t, f.person)) &&
      (f.epic === 'all' || t.parent === f.epic) &&
      (!q || t.key.toLowerCase().includes(q) || t.title.toLowerCase().includes(q) || t.labels.some((l) => l.toLowerCase().includes(q))),
  )
}

export const initials = (name: string) =>
  name
    .split(/\s+/)
    .map((p) => p[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
