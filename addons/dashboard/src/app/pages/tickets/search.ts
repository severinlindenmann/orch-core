import { STATUSES, type Priority, type Status } from '@/api/types'

export const PRIORITIES: Priority[] = ['urgent', 'high', 'medium', 'low']
export const SORTS = ['updated', 'priority', 'key', 'status'] as const
export type SortKey = (typeof SORTS)[number]
const NEEDS = ['me', 'agent', 'nobody'] as const

/** Router search params of /tickets. Task 6's saved views are exactly this object. */
export interface TicketsSearch {
  repo?: string
  q?: string
  status?: Status[]
  type?: string
  priority?: Priority[]
  person?: string
  needs?: (typeof NEEDS)[number]
  label?: string
  sort?: SortKey
}

const str = (v: unknown) => (typeof v === 'string' && v ? v : undefined)
function oneOf<T extends string>(all: readonly T[], v: unknown): T | undefined {
  return all.includes(v as T) ? (v as T) : undefined
}
function listOf<T extends string>(all: readonly T[], v: unknown): T[] | undefined {
  const raw = Array.isArray(v) ? v : typeof v === 'string' ? v.split(',') : []
  const out = raw.filter((x): x is T => all.includes(x as T))
  return out.length ? out : undefined
}

/** Tolerant parser: unknown or malformed params are dropped instead of failing the route. */
/** The filters the host applies (status is filtered in the page, so its chips can count the other filters). */
export function ticketsServerParams(s: TicketsSearch) {
  return { repo: s.repo, q: s.q, type: s.type, priority: s.priority, person: s.person, needs: s.needs, label: s.label, sort: s.sort }
}

export function validateTicketsSearch(raw: Record<string, unknown>): TicketsSearch {
  const out: TicketsSearch = {
    repo: str(raw.repo),
    q: str(raw.q),
    status: listOf(STATUSES, raw.status),
    type: str(raw.type),
    priority: listOf(PRIORITIES, raw.priority),
    person: str(raw.person),
    needs: oneOf(NEEDS, raw.needs),
    label: str(raw.label),
    sort: oneOf(SORTS, raw.sort),
  }
  return Object.fromEntries(Object.entries(out).filter(([, v]) => v !== undefined)) as TicketsSearch
}

/** True when any filter (not the sort) is set. */
export const hasFilters = (s: TicketsSearch) => Object.keys(s).some((k) => k !== 'sort')
