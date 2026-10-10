// Mandates (docs/concept-mandates.md), PREVIEW ONLY. The owner approved Step 1 (the pilot) on 10 Oct 2026 and asked
// to see how it would look. Nothing here is part of the contract: the types, the endpoint
// (`/api/workspaces/:ws/preview/mandates`, served by the mock only) and every word may change once core specifies it.
// Nothing signs: the preview never posts to a signing or decision endpoint (postAction, grants, addon ops, settings,
// relay), and no request carries `confirm: 'sign'`.

/** The calm line every preview surface carries, word for word. */
export const PREVIEW_LINE = 'Preview of a proposed feature (concept: mandates, Step 1 pilot). Nothing here signs anything.'

/** The prerequisites the host checks before any mandate can be issued (concept §Summary, §2.12). */
export interface MandatePreflightCheck {
  id: 'p1' | 'p2' | 'p3' | 'p4'
  title: string
  detail: string
  /** `missing`: not available in this build; issuing stays blocked for real. */
  state: 'passed' | 'missing'
  note: string
}

/** The decision kinds the Step 1 pilot may decide (fixed; not editable in the dialog). */
export type MandateDecisionKind = 'requirements' | 'plan' | 'verdict'

export const DECISION_KIND_LABEL: Record<MandateDecisionKind, string> = {
  requirements: 'Requirements approved',
  plan: 'Plan approved',
  verdict: 'Verdict',
}

export interface MandateDecision {
  /** Workspace log position of the decision (`#1831`). */
  seq: number
  id: string
  ticket: string
  title?: string
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

export type MandateRefusalReason = 'protected_path' | 'veto' | 'limit'

export const REFUSAL_LABEL: Record<MandateRefusalReason, string> = {
  protected_path: 'Protected path',
  veto: 'Your veto',
  limit: 'Limit reached',
}

/** Something the mandate refused or skipped: it goes to a person, as a normal "needs you" item. */
export interface MandateRefusal {
  id: string
  ticket: string
  title?: string
  kind: MandateDecisionKind
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
  epic: { key: string; title: string }
  decides: MandateDecisionKind[]
  max_size: 'm'
  issued_at: string
  expires: string
  state: 'active' | 'stopping' | 'stopped' | 'revoked'
  stop?: { requested_at: string; stop_agents: boolean; boundary_seq?: number }
  revoked_at?: string
  limits: { decisions: MandateLimit; children: MandateLimit; rework: MandateLimit }
  decisions: MandateDecision[]
  refused: MandateRefusal[]
  revisions: { revision: number; at: string; what: string }[]
}

export interface MandatesPreviewState {
  preview: true
  /** The preview is turned on (Demo data "Preview: mandates", or "Show the pilot anyway"). Off: nothing shows. */
  on: boolean
  preflight: MandatePreflightCheck[]
  /** What the issue dialog offers (the workspace's agents and epics). */
  orchestrators: { name: string; session: string }[]
  epics: { key: string; title: string; children: number }[]
  /** The "never" list (concept §Summary), core text. */
  never: string[]
  /** The workspace's protected paths (concept §2.2): a diff touching any of them is human-only. */
  protected_paths: string[]
  mandate: PreviewMandate | null
}

export type MandatesPreviewRequest =
  | { op: 'enable'; seed?: boolean }
  | { op: 'disable' }
  | { op: 'issue'; orchestrator: string; epic: string }
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

/** "Verdict: via mandate md_3, for Severin — no person reviewed this (commit b7e1f02)". */
export function decisionLabel(d: MandateDecision, mandate: string, issuer: string): string {
  return `${DECISION_KIND_LABEL[d.kind]}: via mandate ${mandate}, for ${issuer} — no person reviewed this${d.commit ? ` (commit ${d.commit})` : ''}`
}
