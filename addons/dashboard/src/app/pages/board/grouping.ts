import type { TicketSummary } from '@/api/types'

/** A lane or section is folded away at first when its epic has more open children than this. */
export const COLLAPSE_OVER = 8
/** Collapse-state key of the "No epic" lane / section. */
export const NO_EPIC = '_none'

export type GroupBy = 'epic' | 'none'

export interface EpicLane {
  /** The epic ticket (from the unfiltered list, so it is there even when a filter hides it). */
  epic: TicketSummary
  /** Its children that pass the filters. */
  children: TicketSummary[]
  /** All children, filtered or not: the progress "12/40 done". */
  total: number
  done: number
  open: number
}

export interface EpicGroups {
  lanes: EpicLane[]
  /** Tickets that are not an epic and have no epic parent. */
  none: TicketSummary[]
}

/** True when the workspace has any epic: only then is there something to group by. */
export const hasEpics = (all: TicketSummary[]) => all.some((t) => t.type === 'epic')

/**
 * Epics become groups, their children go under them, the rest is "No epic". `all` is the unfiltered list (epics and
 * progress come from it), `shown` the filtered one. While filtering, a group without a visible child is left out
 * unless the epic itself matched.
 */
export function groupByEpic(all: TicketSummary[], shown: TicketSummary[], filtering: boolean): EpicGroups {
  const epics = new Map(all.filter((t) => t.type === 'epic').map((t) => [t.key, t]))
  const kids = new Map<string, TicketSummary[]>()
  for (const t of all) if (t.parent && epics.has(t.parent) && t.type !== 'epic') kids.set(t.parent, [...(kids.get(t.parent) ?? []), t])
  const shownEpics = new Set(shown.filter((t) => t.type === 'epic').map((t) => t.key))
  const children = new Map<string, TicketSummary[]>()
  const none: TicketSummary[] = []
  for (const t of shown) {
    if (t.type === 'epic') continue
    if (t.parent && epics.has(t.parent)) children.set(t.parent, [...(children.get(t.parent) ?? []), t])
    else none.push(t)
  }
  const lanes: EpicLane[] = []
  for (const epic of [...epics.values()].sort((a, b) => a.key.localeCompare(b.key))) {
    const all = kids.get(epic.key) ?? []
    const visible = children.get(epic.key) ?? []
    if (filtering && visible.length === 0 && !shownEpics.has(epic.key)) continue
    const done = all.filter((t) => t.status === 'done').length
    lanes.push({ epic, children: visible, total: all.length, done, open: all.length - done })
  }
  return { lanes, none }
}

/**
 * An explicit choice wins; otherwise a big epic starts folded. While a filter or search is on, "big" counts the open
 * children that are shown, so a hit is never hidden inside a fold.
 */
export function isCollapsed(lane: EpicLane, overrides: Record<string, boolean>, filtering = false): boolean {
  const open = filtering ? lane.children.filter((t) => t.status !== 'done').length : lane.open
  return overrides[lane.epic.key] ?? open > COLLAPSE_OVER
}

/** The lane a ticket sits in: its epic's key, or NO_EPIC. */
export const laneOf = (t: TicketSummary, epicKeys: ReadonlySet<string>) => (t.parent && epicKeys.has(t.parent) && t.type !== 'epic' ? t.parent : NO_EPIC)

export const progressLabel = (lane: Pick<EpicLane, 'done' | 'total'>) => `${lane.done}/${lane.total} done`
