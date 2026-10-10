// Gate policy wording shared by Settings and the ticket page (part of the API contract).
import type { BodySections, CodeReviewApplies, GateName, TicketBranch, TicketDefinition, TicketDocument, TicketType, Workspace } from './types'

export type GatePolicy = Pick<Workspace['gates'][GateName], 'approvers' | 'count' | 'not'>

/** The gates in order: the code review (opt-in) follows the verdict. */
export const GATE_ORDER: GateName[] = ['requirements', 'plan', 'verify', 'code']

/** The code review gate when a workspace never set it: off, one approval from owners or maintainers, never an assignee. */
export const DEFAULT_CODE_POLICY: Workspace['gates']['code'] = { approvers: 'maintainer', count: 1, not: 'assignees', applies: 'off' }

/** Does the code review gate apply to a ticket of `type` under `applies`? */
export const codeReviewApplies = (applies: CodeReviewApplies | undefined, type: TicketType): boolean =>
  applies === 'all' || (Array.isArray(applies) && applies.includes(type))

/** "Off", "On for every ticket", "On for feature and bug tickets". */
export function appliesText(applies: CodeReviewApplies | undefined): string {
  if (applies === 'all') return 'On for every ticket'
  if (!Array.isArray(applies) || applies.length === 0) return 'Off'
  const list = applies.length === 1 ? applies[0] : `${applies.slice(0, -1).join(', ')} and ${applies[applies.length - 1]}`
  return `On for ${list} tickets`
}

/** "+12 −3" with a real minus sign. */
export const diffstat = (b: Pick<TicketBranch, 'additions' | 'deletions'>) => `+${b.additions} \u2212${b.deletions}`

/**
 * The commit line a verdict or code review signs, in core's words, with the full head sha:
 * "Commit 5be3d10 on feat/DEMO-0041-reconciliation: +40 −12 in 3 files against develop".
 */
export const commitCover = (b: TicketBranch) => `Commit ${b.head} on ${b.name}: ${diffstat(b)} in ${b.files} file${b.files === 1 ? '' : 's'} against ${b.base}`

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

/** The fields of a ticket that a requirements or plan gate hash covers. */
export type GateSource = Pick<TicketDefinition, 'acceptance' | 'tasks' | 'type' | 'size'> & { body: BodySections }

export interface SignedSection {
  label: string
  text: string
  /** Shown because the hash covers it, but not content a person wrote (type and size always exist). */
  meta?: boolean
}

/**
 * What an approval of the requirements or plan gate covers: the words for the hash, the material the mock hashes, and
 * the sections the dialog shows. One function feeds both the hash (mocks/derive.ts) and the signing dialog, so what is
 * shown cannot drift from what is signed.
 */
export function gateSignedContent(gate: 'requirements' | 'plan', t: GateSource, personName: (id: string) => string = (id) => id): { covers: string[]; material: string; sections: SignedSection[] } {
  const text = (k: keyof BodySections) => t.body[k]?.trim() ?? ''
  if (gate === 'requirements')
    return {
      covers: ['Section: Requirements', 'Section: Out of scope', `Acceptance criteria (${t.acceptance.length})`, 'Type and size'],
      material: JSON.stringify([t.body.requirements, t.body.out_of_scope, t.acceptance, t.type, t.size]),
      sections: [
        { label: 'Requirements', text: text('requirements') },
        { label: 'Out of scope', text: text('out_of_scope') },
        { label: 'Acceptance criteria', text: t.acceptance.map((a) => `${a.id}  ${a.text}`).join('\n') },
        { label: 'Type and size', text: `Type: ${t.type} · Size: ${t.size ?? 'not set'}`, meta: true },
      ],
    }
  return {
    covers: ['Section: Plan', `Tasks (${t.tasks.length}): ids, text, assignee, verify, proves`, 'Section: Decisions'],
    material: JSON.stringify([t.body.plan, t.tasks, t.body.decisions]),
    sections: [
      { label: 'Plan', text: text('plan') },
      {
        label: 'Tasks',
        text: t.tasks
          .map((k) => [`${k.id}  ${k.text}`, `    Assignee: ${k.assignee ? personName(k.assignee) : 'not assigned'}`, k.verify ? `    Verify: ${k.verify.cmd}` : '    Verify: by a person', k.proves.length ? `    Proves: ${k.proves.join(', ')}` : ''].filter(Boolean).join('\n'))
          .join('\n'),
      },
      { label: 'Decisions', text: text('decisions') },
    ],
  }
}

/** How many people could ever approve under `approvers`, or null when it depends on each ticket (its reviewers). */
function eligibleCount(members: { role: string }[], approvers: string): number | null {
  if (approvers === 'owner') return members.filter((m) => m.role === 'owner').length
  if (approvers === 'maintainer') return members.filter((m) => m.role === 'owner' || m.role === 'maintainer').length
  return null
}

/** "Only 1 owner exists; 2 approvals from owners can never be met." or null when the policy can be met. The host applies the same rule. */
export function unmeetablePolicy(workspace: { members: { role: string }[] }, p: Pick<GatePolicy, 'approvers' | 'count'>): string | null {
  const n = eligibleCount(workspace.members, p.approvers)
  if (n === null || p.count <= n) return null
  const group = approversText(p.approvers)
  const one = p.approvers === 'owner' ? 'owner' : 'owner or maintainer'
  return `Only ${n} ${n === 1 ? one : group} ${n === 1 ? 'exists' : 'exist'}; ${p.count} approvals from ${group} can never be met.`
}

/** A pass verdict stands and the code review gate applies to this ticket and is not approved yet (the host's rule too). */
export const codeReviewWaits = (t: Pick<TicketDocument, 'status' | 'verdict' | 'gates'>): boolean =>
  t.status === 'testing' && t.verdict?.result === 'pass' && !!t.gates.code.required && t.gates.code.state !== 'approved'
