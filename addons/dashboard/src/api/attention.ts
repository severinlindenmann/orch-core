import type { AddonDecision, NeedsYouItem } from './types'
import { isBlocking, type ConnectionInfo } from './connections'

/** Connections whose last check means the owner must log in again: Today's "Connections" rows. */
export const reloginItems = (connections: ConnectionInfo[]) => connections.filter((c) => c.last_check && isBlocking(c.last_check.status))

/**
 * What "needs you" counts (R-c): exactly what Today lists. The Today header, the sidebar badge, the workspace
 * switcher and the host's `needs_you` all use this one function. `connections` are passed for the owner only.
 */
export function countAttention(items: NeedsYouItem[], decisions: AddonDecision[], connections: ConnectionInfo[] = []) {
  const core = items.filter((i) => i.kind === 'question' || i.kind === 'approval' || i.kind === 'verdict').length
  const addon = decisions.length
  const relogin = reloginItems(connections).length
  return { core, addon, connections: relogin, total: core + addon + relogin }
}
