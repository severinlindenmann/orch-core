import { policySentence } from '@/api/gates'
import type { GateName, GateStatus, QuestionStatus, TicketDocument } from '@/api/types'
import { roleMeets } from '@/api/roles'
import { can } from '@/api/permissions'
import type { Viewer } from './shared'

/** Mirrors the gate policy the mock host enforces: who may approve a gate. */
export function canApproveGate(t: TicketDocument, gate: GateName, viewer: Viewer): boolean {
  const g = t.gates[gate]
  if (!can(viewer.role, 'ticket.act')) return false
  if (g.approvers === 'reviewers' ? !t.people.reviewers.includes(viewer.person) : !roleMeets(viewer.role, g.approvers)) return false
  if (g.not === 'assignees' && t.people.assignees.includes(viewer.person)) return false
  if (g.approvals.some((a) => a.by === viewer.person)) return false
  return true
}

export function canAnswer(q: QuestionStatus, viewer: Viewer): boolean {
  if (q.state !== 'open') return false
  if (!can(viewer.role, 'ticket.act')) return false
  return q.to === viewer.person || can(viewer.role, 'question.answer.any')
}

export interface Available {
  approve: ('requirements' | 'plan')[]
  requestChanges: GateName[]
  verdict: boolean
  answer: QuestionStatus[]
}

/** Human actions the viewer can take now, only where the gate or the needs say so. */
export function availableActions(t: TicketDocument, viewer: Viewer): Available {
  const approve: Available['approve'] = []
  const requestChanges: GateName[] = []
  if (t.status !== 'done') {
    const req = t.gates.requirements
    if (req.state !== 'approved' && !!t.body.requirements?.trim() && canApproveGate(t, 'requirements', viewer)) {
      approve.push('requirements')
      requestChanges.push('requirements')
    }
    const plan = t.gates.plan
    if (plan.state !== 'approved' && req.state === 'approved' && t.tasks.length > 0 && canApproveGate(t, 'plan', viewer)) {
      approve.push('plan')
      requestChanges.push('plan')
    }
  }
  const verdict = t.status === 'testing' && !t.verdict && canApproveGate(t, 'verify', viewer)
  if (verdict) requestChanges.push('verify')
  return { approve, requestChanges, verdict, answer: t.questions_state.filter((q) => canAnswer(q, viewer)) }
}

export const GATE_LABEL: Record<GateName, string> = { requirements: 'Requirements', plan: 'Plan', verify: 'Verification' }

/** The gate's policy in the same words as Settings: "Plan needs 1 approval from owners or maintainers." */
export function policyText(gate: GateName, g: Pick<GateStatus, 'approvers' | 'needed' | 'not'>): string {
  return policySentence(GATE_LABEL[gate], { approvers: g.approvers, count: g.needed, not: g.not })
}
