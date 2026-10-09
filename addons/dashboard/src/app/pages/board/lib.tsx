import { useCallback, useEffect, useState } from 'react'
import { Bug, ChevronDown, ChevronUp, ChevronsUp, Equal, FlaskConical, Layers, Sparkles, Wrench, type LucideIcon } from 'lucide-react'
import { STATUSES, type Priority, type Status, type TicketSummary, type TicketType } from '@/api/types'
import { cn } from '@/lib/utils'
import type { GroupBy } from './grouping'

export const STATUS_LABEL: Record<Status, string> = {
  backlog: 'Backlog',
  open: 'Open',
  'in-progress': 'In progress',
  waiting: 'Waiting',
  testing: 'Testing',
  done: 'Done',
}

/** Done lists its latest tickets only, until "Show all". */
export const DONE_LIMIT = 5

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
  high: { icon: ChevronUp, cls: 'text-text-muted' },
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


export interface BoardDisplay {
  density: 'comfortable' | 'compact'
  labels: boolean
  estimate: boolean
  progress: boolean
  collapsed: Status[]
  /** Swimlanes per epic (default) or one flat set of columns. */
  group: GroupBy
  /** Lanes the person folded (true) or unfolded (false) themselves; absent = the default for that epic. */
  lanes: Record<string, boolean>
}
export const DEFAULT_DISPLAY: BoardDisplay = { density: 'comfortable', labels: true, estimate: true, progress: false, collapsed: ['done'], group: 'epic', lanes: {} }
/** Remembered per viewer: Tom does not inherit what Severin chose. */
export const displayKey = (person: string) => `orch.board.display.${person}`

function loadDisplay(person: string | undefined): BoardDisplay {
  if (!person) return DEFAULT_DISPLAY
  try {
    const raw = JSON.parse(localStorage.getItem(displayKey(person)) ?? 'null') as Partial<BoardDisplay> | null
    if (!raw || typeof raw !== 'object') return DEFAULT_DISPLAY
    return {
      density: raw.density === 'compact' ? 'compact' : 'comfortable',
      labels: raw.labels ?? DEFAULT_DISPLAY.labels,
      estimate: raw.estimate ?? DEFAULT_DISPLAY.estimate,
      progress: raw.progress ?? DEFAULT_DISPLAY.progress,
      collapsed: Array.isArray(raw.collapsed) ? raw.collapsed.filter((s): s is Status => STATUSES.includes(s)) : DEFAULT_DISPLAY.collapsed,
      group: raw.group === 'none' ? 'none' : 'epic',
      lanes: raw.lanes && typeof raw.lanes === 'object' ? Object.fromEntries(Object.entries(raw.lanes).filter(([, v]) => typeof v === 'boolean')) : {},
    }
  } catch {
    return DEFAULT_DISPLAY
  }
}

/** The board's Display options, remembered per viewer (nothing is stored until we know who is viewing). */
export function useBoardDisplay(person: string | undefined) {
  const [state, setState] = useState<{ person: string | undefined; display: BoardDisplay }>(() => ({ person, display: loadDisplay(person) }))
  useEffect(() => {
    setState((s) => (s.person === person ? s : { person, display: loadDisplay(person) }))
  }, [person])
  const display = state.person === person ? state.display : loadDisplay(person)
  const update = useCallback(
    (patch: Partial<BoardDisplay>) => {
      setState((s) => {
        const next = { person, display: { ...(s.person === person ? s.display : loadDisplay(person)), ...patch } }
        if (person) {
          try {
            localStorage.setItem(displayKey(person), JSON.stringify(next.display))
          } catch {
            /* storage unavailable: the choice lasts for this visit */
          }
        }
        return next
      })
    },
    [person],
  )
  return [display, update] as const
}
