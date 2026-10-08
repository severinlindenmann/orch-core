// Gate policy wording shared by Settings and the ticket page (part of the API contract).
import type { GateName, Workspace } from './types'

export type GatePolicy = Pick<Workspace['gates'][GateName], 'approvers' | 'count' | 'not'>

/** The approver groups a gate policy may name, in words. */
export const APPROVER_GROUPS = [
  { value: 'owner', who: 'owners', label: 'Owners' },
  { value: 'maintainer', who: 'owners or maintainers', label: 'Owners or maintainers' },
  { value: 'reviewers', who: "the ticket's reviewers", label: "The ticket's reviewers" },
] as const

/** "owners or maintainers" for 'maintainer'; an unknown value is shown as is. */
export const approversText = (approvers: string): string => APPROVER_GROUPS.find((a) => a.value === approvers)?.who ?? approvers

/** "Plan needs 1 approval from owners or maintainers, not the assignees." */
export function policySentence(label: string, p: GatePolicy): string {
  return `${label} needs ${p.count} approval${p.count === 1 ? '' : 's'} from ${approversText(p.approvers)}${p.not === 'assignees' ? ', not the assignees' : ''}.`
}
