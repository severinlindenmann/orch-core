// Gate policy wording shared by Settings and the ticket page (part of the API contract).
import type { BodySections, GateName, TicketDefinition, Workspace } from './types'

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
