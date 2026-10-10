// Mandates (docs/concept-mandates.md), PREVIEW ONLY. Owner decision 10 Oct 2026 (evening): the wide mandate (it steers
// the whole workspace in the owner's name except the always-human areas and protected paths; time-boxed up to 30 days,
// renewable; never another mandate). Shown so the owner can see how it would look. Nothing here is part of the contract: the types, the endpoint
// (`/api/workspaces/:ws/preview/mandates`, served by the mock only) and every word may change once core specifies it.
// Nothing signs: the preview never posts to a signing or decision endpoint (postAction, grants, addon ops, settings,
// relay), and no request carries `confirm: 'sign'`.

/** The calm line every preview surface carries, word for word. */
export const PREVIEW_LINE = 'Preview of a proposed feature (concept: mandates, owner decision 10 Oct). Nothing here signs anything.'

/** The prerequisites the host checks before any mandate can be issued (concept §Summary, §2.12). */
export interface MandatePreflightCheck {
  id: 'p1' | 'p2' | 'p3' | 'p4'
  title: string
  detail: string
  /** `missing`: not available in this build; issuing stays blocked for real. */
  state: 'passed' | 'missing'
  note: string
}

/** What a mandate decided for the owner (the seeded log shows a mix). */
export type MandateDecisionKind = 'requirements' | 'plan' | 'verdict' | 'code_review' | 'unblock' | 'permit' | 'factory_enabled' | 'factory_run' | 'grant'

export const DECISION_KIND_LABEL: Record<MandateDecisionKind, string> = {
  requirements: 'Requirements approved',
  plan: 'Plan approved',
  verdict: 'Verdict',
  code_review: 'Code review approved',
  unblock: 'Ticket unblocked',
  permit: 'Factory permit granted',
  factory_enabled: 'Factory enabled',
  factory_run: 'Factory run started',
  grant: 'Grant issued (agent started)',
}

/** Durations the issue and renew dialogs offer (owner decision: the owner picks, up to 30 days). */
export const MANDATE_DAYS = [1, 3, 7, 14, 30] as const
export const MAX_MANDATE_DAYS = 30

export interface MandateDecision {
  /** Workspace log position of the decision (`#1831`). */
  seq: number
  id: string
  /** The ticket the decision is on; none for a workspace-level decision (a factory enabled, a grant). */
  ticket?: string
  title?: string
  /** What a workspace-level decision is about, in core words ("AI Factory in DEMO", "grant gr_7Q2 for 8 h"). */
  target?: string
  kind: MandateDecisionKind
  /** The commit a verdict signs (`source_sha`), short. */
  commit?: string
  at: string
  checker: { identity: string; result: 'passed' }
  /** Work the decision covers already landed (by a person): listed for review on Revoke and void, never voided. */
  landed: boolean
  /** The owner's look in the digest: unseen until then. */
  review?: 'looks_right' | 'veto'
  /** Voided by Revoke and void (`gate.invalidated {cause: mandate_revoked}`). */
  voided?: boolean
}

export type MandateRefusalReason = 'protected_path' | 'veto' | 'limit' | 'always_human' | 'chain'

export const REFUSAL_LABEL: Record<MandateRefusalReason, string> = {
  protected_path: 'Protected path',
  veto: 'Your veto',
  limit: 'Limit reached',
  always_human: 'Always yours',
  chain: 'No mandate chains',
}

/** Something the mandate refused or skipped: it goes to a person, as a normal "needs you" item. */
export interface MandateRefusal {
  id: string
  ticket?: string
  title?: string
  /** What was asked, in core words, when it is not a decision on a ticket ("Install the addon drop"). */
  asked?: string
  kind?: MandateDecisionKind
  reason: MandateRefusalReason
  /** Core's sentence: what was refused and why. */
  detail: string
  at: string
}

export interface MandateLimit {
  used: number
  max: number
}

export interface PreviewMandate {
  id: string
  revision: number
  issuer: string
  orchestrator: { name: string; identity: string }
  checker: { identity: string }
  /** The wide mandate steers the whole workspace (minus the always-human areas and protected paths). */
  scope: 'workspace'
  /** The length the owner picked (days, at most 30); a renewal picks again. */
  days: number
  issued_at: string
  expires: string
  state: 'active' | 'stopping' | 'stopped' | 'revoked'
  stop?: { requested_at: string; stop_agents: boolean; boundary_seq?: number }
  revoked_at?: string
  limits: { decisions: MandateLimit; grants: MandateLimit }
  decisions: MandateDecision[]
  refused: MandateRefusal[]
  revisions: { revision: number; at: string; what: string }[]
}

export interface MandatesPreviewState {
  preview: true
  /** The preview is turned on (Demo data "Preview: mandates", or "Show the mandate anyway"). Off: nothing shows. */
  on: boolean
  preflight: MandatePreflightCheck[]
  /** What the issue dialog offers (the workspace's agents). */
  orchestrators: { name: string; session: string }[]
  /** What the mandate may do in your name (owner decision 10 Oct evening), core text. */
  may: string[]
  /** What stays human, always (concept §Summary), core text. */
  always_human: string[]
  /** The workspace's protected paths (concept §2.2): a diff touching any of them is human-only. */
  protected_paths: string[]
  mandate: PreviewMandate | null
}

export type MandatesPreviewRequest =
  | { op: 'enable'; seed?: boolean }
  | { op: 'disable' }
  | { op: 'issue'; orchestrator: string; days: number }
  | { op: 'renew'; days: number }
  | { op: 'stop'; stop_agents: boolean }
  | { op: 'review'; decision: string; review: 'looks_right' | 'veto' }
  | { op: 'revoke' }

/** Decisions Revoke and void would void (work not landed, not voided yet), and landed work listed for review. */
export function revokeSplit(m: PreviewMandate): { voids: MandateDecision[]; landed: MandateDecision[] } {
  const live = m.decisions.filter((d) => !d.voided)
  return { voids: live.filter((d) => !d.landed), landed: live.filter((d) => d.landed) }
}

/** Decisions the owner has not looked at yet (the digest on Today). */
export const unseen = (m: PreviewMandate) => m.decisions.filter((d) => !d.review && !d.voided)

/** "Verdict: via mandate md_3, for Severin — no person reviewed this (commit b7e1f02)". Used by tests and docs. */
export function decisionLabel(d: MandateDecision, mandate: string, issuer: string): string {
  return `${DECISION_KIND_LABEL[d.kind]}: via mandate ${mandate}, for ${issuer} — no person reviewed this${d.commit ? ` (commit ${d.commit})` : ''}`
}
