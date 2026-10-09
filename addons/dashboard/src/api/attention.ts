import type { AddonDecision, NeedsYouItem } from './types'
import { isBlocking, type ConnectionInfo } from './connections'

/** The items Today can list. Shared by group headings, the header, badge and workspace counts. */
export function countAttention(items: NeedsYouItem[], decisions: AddonDecision[], connections: ConnectionInfo[] = []) {
  const questions = items.filter(i => i.kind === 'question').length
  const approvals = items.filter(i => i.kind === 'approval').length
  const verdicts = items.filter(i => i.kind === 'verdict').length
  const addon = decisions.length
  const connection = reloginItems(connections).length
  const core = questions + approvals + verdicts
  return { questions, approvals, verdicts, addons: addon, connections: connection, core, addon, total: core + addon + connection }
}

export const reloginItems = (connections: ConnectionInfo[]) => connections.filter(c => c.last_check && isBlocking(c.last_check.status))
