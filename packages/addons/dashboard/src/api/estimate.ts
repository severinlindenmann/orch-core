// Estimate add-on (real host). A ticket's points ARE its core size field, so nothing new is stored or signed:
// setting points is the ordinary signed change of the size. The scale is fixed: XS 1, S 2, M 3, L 5, XL 8 (the same
// weights the mock's t-shirt scale used). "Not estimated" is a ticket with no size.
import { hasFeature, isRealHost } from './features'
import type { TicketSummary, Workspace } from './types'

export type SizeName = 'xs' | 's' | 'm' | 'l' | 'xl'

export const ESTIMATE_SCALE: readonly { size: SizeName; points: number; label: string }[] = [
  { size: 'xs', points: 1, label: '1 point' },
  { size: 's', points: 2, label: '2 points' },
  { size: 'm', points: 3, label: '3 points' },
  { size: 'l', points: 5, label: '5 points' },
  { size: 'xl', points: 8, label: '8 points' },
]

const POINTS: Record<string, number> = Object.fromEntries(ESTIMATE_SCALE.map((s) => [s.size, s.points]))

/** The points of a size, or `null` when the ticket is not estimated (no size, or one this build does not know). */
export const pointsOf = (size: string | null | undefined): number | null => (size && Object.hasOwn(POINTS, size) ? POINTS[size] : null)

export const pointsText = (points: number) => `${points} ${points === 1 ? 'point' : 'points'}`

/** Real host only: the mock keeps its own estimate add-on (slots and addon data), so the two never draw twice. */
export function estimateOn(workspace: Pick<Workspace, 'id'> | undefined): boolean {
  return isRealHost() && !!workspace && hasFeature('estimate', workspace.id)
}

export interface Rollup {
  /** Sum of the estimated tickets. */
  total: number
  estimated: number
  notEstimated: number
}

/** Epics are not summed into their own children's total: callers pass the children (or a column's cards). */
export function rollup(tickets: readonly Pick<TicketSummary, 'size'>[]): Rollup {
  let total = 0
  let estimated = 0
  for (const t of tickets) {
    const p = pointsOf(t.size)
    if (p === null) continue
    total += p
    estimated += 1
  }
  return { total, estimated, notEstimated: tickets.length - estimated }
}

export const notEstimatedText = (n: number) => `${n} not estimated`
