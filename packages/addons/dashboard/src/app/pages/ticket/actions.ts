import { addFirst, codeReviewWaits, missingWords, policySentence } from '@/api/gates'
import type { GateName, GateStatus, QuestionStatus, TicketDocument } from '@/api/types'
import { roleMeets } from '@/api/roles'
import { can } from '@/api/permissions'
import { isRealHost } from '@/api/features'
import type { Viewer } from './shared'

/** Tickets whose combined approval could not be prepared (the host refused or failed): their cards fall back to the two
 * separate approvals, so the combined card is never a dead end. Per page load; a reload tries the combined card again. */
const bothBlocked = new Set<string>()
export const blockBoth = (key: string) => void bothBlocked.add(key)
export const bothIsBlocked = (key: string) => bothBlocked.has(key)
export const unblockBoth = (key: string) => void bothBlocked.delete(key)

/** Mirrors the gate policy the mock host enforces: who may approve a gate. */
export function canApproveGate(t: TicketDocument, gate: GateName, viewer: Viewer): boolean {
  const g = t.gates[gate]
  if (!can(viewer.role, 'ticket.act')) return false
  if (Array.isArray(g.eligible)) {
    // The host's own list (core's may_decide, incl. the solo-owner rule) decides who may; the mirror below only runs without it.
    if (!g.eligible.includes(viewer.person)) return false
    // The independence rule of the code review is not relaxed by the list.
    if (gate === 'code' && t.people.assignees.includes(viewer.person)) return false
  } else {
    if (g.approvers === 'reviewers' ? !t.people.reviewers.includes(viewer.person) : !roleMeets(viewer.role, g.approvers)) return false
    if ((g.not === 'assignees' || gate === 'code') && t.people.assignees.includes(viewer.person)) return false
  }
  if (g.approvals.some((a) => a.by === viewer.person)) return false
  // On a real host an approval binds the gate hash; with none (it cannot be computed now) nothing is offered to sign.
  if (isRealHost() && !g.hash) return false
  return true
}

const WHO: Record<string, string> = { owner: 'owners', maintainer: 'owners and maintainers', reviewers: 'reviewers' }

/**
 * The next approval this person would give if their role allowed it, and why it does not: a member or maintainer on a
 * ticket whose pending gate needs a higher role. Shown as a disabled button with the reason instead of nothing.
 * Null when the person can approve it, or the gate is not up for approval.
 */
export function blockedApproval(t: TicketDocument, viewer: Viewer): { gate: 'requirements' | 'plan'; label: string; reason: string } | null {
  if (t.status === 'done' || !can(viewer.role, 'ticket.act')) return null
  const req = t.gates.requirements
  const plan = t.gates.plan
  const gate = req.state !== 'approved' && !!t.body.requirements?.trim() ? 'requirements' : plan.state !== 'approved' && req.state === 'approved' && t.tasks.length > 0 ? 'plan' : null
  if (!gate || canApproveGate(t, gate, viewer)) return null
  const g = t.gates[gate]
  // Only a role (or reviewer list) that excludes the person: not "you already approved" or "assignees cannot approve".
  const byRole = g.approvers === 'reviewers' ? !t.people.reviewers.includes(viewer.person) : !roleMeets(viewer.role, g.approvers)
  if (!byRole) return null
  const who = WHO[g.approvers] ?? 'a higher role'
  return { gate, label: `Approve ${gate} (${who} only)`, reason: `${GATE_LABEL[gate]} needs ${g.needed} ${g.needed === 1 ? 'approval' : 'approvals'} from ${g.approvers === 'reviewers' ? "the ticket's reviewers" : who}.` }
}

export function canAnswer(q: QuestionStatus, viewer: Viewer): boolean {
  if (q.state !== 'open') return false
  if (!can(viewer.role, 'ticket.act')) return false
  return q.to === viewer.person || can(viewer.role, 'question.answer.any')
}

export interface Available {
  approve: ('requirements' | 'plan' | 'code')[]
  /** R11 P09: requirements AND plan are both written, complete and open, and this person's one approval completes the requirements: one signature approves both. */
  approveBoth: boolean
  requestChanges: GateName[]
  verdict: boolean
  answer: QuestionStatus[]
}

/** B11: core says an approval still lacks something (e.g. no plan written yet): never offer Approve for it. */
export const lacks = (g: Pick<GateStatus, 'missing'>) => (g.missing?.length ?? 0) > 0

/** What the gate waits for when it lacks something: "Waiting for the agent to write the plan", else "Add … first". */
export function lackText(gate: 'requirements' | 'plan', missing: string[]): string {
  if (missing.includes(gate)) return `Waiting for the agent to write the ${gate}`
  return addFirst(missing)
}

/** The first of requirements, plan that is not approved and still lacks something (core's `missing`), in plain words. */
export type Gap = { gate: 'requirements' | 'plan'; missing: string[]; title: string; text: string }
export function gapOf(t: Pick<TicketDocument, 'gates'>): Gap | null {
  for (const gate of ['requirements', 'plan'] as const) {
    const g = t.gates[gate]
    if (g.state !== 'approved' && lacks(g)) {
      const missing = g.missing ?? []
      return { gate, missing, title: `The ${gate === 'plan' ? 'plan is' : 'requirements are'} not finished yet`, text: `Still missing: ${missingWords(missing)}.` }
    }
  }
  return null
}

/** Human actions the viewer can take now, only where the gate or the needs say so. */
export function availableActions(t: TicketDocument, viewer: Viewer): Available {
  const approve: Available['approve'] = []
  const requestChanges: GateName[] = []
  if (t.status !== 'done') {
    const req = t.gates.requirements
    if (req.state !== 'approved' && !!t.body.requirements?.trim() && !lacks(req) && canApproveGate(t, 'requirements', viewer)) {
      approve.push('requirements')
      requestChanges.push('requirements')
    }
    const plan = t.gates.plan
    // W06: like the combined card, never offer a plan approval while the plan itself is not written (the host's rule too)
    if (plan.state !== 'approved' && req.state === 'approved' && !!t.body.plan?.trim() && t.tasks.length > 0 && !lacks(plan) && canApproveGate(t, 'plan', viewer)) {
      approve.push('plan')
      requestChanges.push('plan')
    }
  }
  // One signature for both: only on a real host (the signer serves it), both gates written and complete, this person may
  // decide both, and their one approval completes the requirements (otherwise the plan cannot be approved in the same run).
  const planGate = t.gates.plan
  const approveBoth =
    isRealHost() &&
    !bothBlocked.has(t.key) &&
    t.status !== 'done' &&
    !!t.head?.seq &&
    !t.gates.requirements.single_only &&
    !t.gates.plan.single_only &&
    t.gates.requirements.state !== 'approved' && !!t.body.requirements?.trim() && !lacks(t.gates.requirements) && canApproveGate(t, 'requirements', viewer) &&
    t.gates.requirements.needed === 1 && t.gates.requirements.approvals.length === 0 &&
    planGate.state !== 'approved' && !!t.body.plan?.trim() && t.tasks.length > 0 && !lacks(planGate) && canApproveGate(t, 'plan', viewer)
  // A verdict the host marks stale (a fail followed by a resubmit, a new commit) does not count: a new one is due. A current verdict by someone else leaves the viewer's own to give (a verify gate with two people).
  const verdict = t.status === 'testing' && t.gates.verify.state !== 'approved' && !(t.verdict && t.verdict.current !== false && t.verdict.by === viewer.person) && canApproveGate(t, 'verify', viewer)
  if (verdict) requestChanges.push('verify')
  // The code review (opt-in): after a pass verdict, by a person the policy names, never an assignee.
  if (codeReviewWaits(t) && canApproveGate(t, 'code', viewer)) {
    approve.push('code')
    requestChanges.push('code')
  }
  return { approve, approveBoth, requestChanges, verdict, answer: t.questions_state.filter((q) => canAnswer(q, viewer)) }
}

/** The one thing the viewer should do next on this ticket, shown as the header's primary button (null: nothing). */
export type Primary = { kind: 'verdict' } | { kind: 'approve'; gate: 'requirements' | 'plan' | 'code' } | { kind: 'answer'; question: string } | { kind: 'start' }

/**
 * A blocking question > give the verdict > approve requirements, plan, code review > any other question > start the agent.
 * (A blocking question usually rewrites the requirements, so approving first is wasted work.) `start` is offered only when
 * the caller says an agent may be started now and nothing else is on offer.
 */
export function primaryAction(av: Available, opts: { start?: boolean } = {}): Primary | null {
  const blocking = av.answer.find((q) => q.blocking)
  if (blocking) return { kind: 'answer', question: blocking.id }
  if (av.verdict) return { kind: 'verdict' }
  if (av.approve.length) return { kind: 'approve', gate: av.approve[0] }
  if (av.answer.length) return { kind: 'answer', question: av.answer[0].id }
  if (opts.start) return { kind: 'start' }
  return null
}

export function primaryLabel(p: Primary, av?: Pick<Available, 'answer'>): string {
  if (p.kind === 'verdict') return 'Give verdict'
  if (p.kind === 'start') return 'Start agent'
  if (p.kind === 'answer') return av && av.answer.length > 1 ? `Answer questions (${av.answer.length})` : 'Answer question'
  return p.gate === 'requirements' ? 'Approve requirements' : p.gate === 'code' ? 'Approve code review' : 'Approve plan'
}

export const GATE_LABEL: Record<GateName, string> = { requirements: 'Requirements', plan: 'Plan', verify: 'Verify', code: 'Code review' }

/** The gate's policy in the same words as Settings: "Plan needs 1 approval from owners or maintainers." */
export function policyText(gate: GateName, g: Pick<GateStatus, 'approvers' | 'needed' | 'not'>): string {
  return policySentence(GATE_LABEL[gate], { approvers: g.approvers, count: g.needed, not: g.not })
}

/** Gate names inside action sentences; Verify is a proper name. */
export const gateActionName = (gate: GateName) => gate === 'verify' ? GATE_LABEL[gate] : GATE_LABEL[gate].toLowerCase()
