import { GRANT_MAX_HOURS } from './grants'
// Reduced workspace settings (M4, #306): the types the server serves (`server/src/orch_dashboard/adapters/settings.py`)
// and the pure rules the page shares. A field the host did not send is `null` (unknown), never a default of ours.
import type { Role } from './types'

export type PolicyGate = 'requirements' | 'plan' | 'verify' | 'code'
export const POLICY_GATES: readonly PolicyGate[] = ['requirements', 'plan', 'verify', 'code']
export const GATE_LABEL: Record<PolicyGate, string> = { requirements: 'Requirements', plan: 'Plan', verify: 'Verify', code: 'Code review' }

/** F1 5.7 approver tokens: the workspace roles and the ticket roles. A viewer never approves. */
export const APPROVER_TOKENS = ['owner', 'maintainer', 'member', 'ticket_owner', 'assignees', 'reviewers', 'watchers'] as const
export type ApproverToken = (typeof APPROVER_TOKENS)[number]
export const TOKEN_LABEL: Record<ApproverToken, string> = {
  owner: 'Owners',
  maintainer: 'Maintainers',
  member: 'Members',
  ticket_owner: 'The ticket owner',
  assignees: 'The assignees',
  reviewers: 'The reviewers',
  watchers: 'The watchers',
}
export const TICKET_TYPES = ['feature', 'bug', 'chore', 'spike', 'epic'] as const

export interface HostPolicy {
  approvers: string[]
  count: number
  not: string[]
  applies: 'all' | 'off' | string[]
  independent: boolean
}

export interface Stamp {
  seq: number
  at: string
  by: string
}

export interface HostRepo {
  name: string
  default_branch: string | null
  remote: string | null
}

/** `available: false` is a host without `settings/1`: everything else is then unknown. */
export interface HostSettings {
  sign_methods?: import('./signing').SignMethod[] | null
  available: boolean
  grant_hours: number | null
  claim_ttl_min: number | null
  lease_ttl_min: number | null
  /** Durations no `settings.changed` has set (the F1 defaults). */
  defaults: string[] | null
  gates: Record<PolicyGate, HostPolicy> | null
  repos: HostRepo[] | null
  repos_more: boolean
  settings_at: Stamp | null
  policy_at: Stamp | null
}

export const DURATIONS = [
  { key: 'grant_hours', label: 'Agent grant length', unit: 'hours', min: 1, max: GRANT_MAX_HOURS },
  { key: 'claim_ttl_min', label: 'Claim lasts without activity', unit: 'minutes', min: 15, max: 1440 },
  { key: 'lease_ttl_min', label: 'Task lease lasts without activity', unit: 'minutes', min: 5, max: 1440 },
] as const
export type DurationKey = (typeof DURATIONS)[number]['key']

export interface RosterDevice {
  id: string
  auth: string | null
  scopes: string[]
  valid: boolean
  /** v1.9 `read_ext/3`: the device's own name, untrusted text (null: none, or withheld when `label_hidden`). */
  label?: string | null
  /** The host's label had invisible or control characters: the server withholds it. */
  label_hidden?: boolean
  paired_at?: string | null
  relay?: boolean | null
  cert_expires_at?: string | null
  /** H12d-2: the level the device's reader record counts as now; null: no verified record (the gate counts it as 0). */
  reader?: number | null
}

/** `reader.list` (H12d-2, cap `reader/1`) as the server forwards it: no digests, no declarations. */
export interface HostReader {
  device: string
  person: string
  /** What the device declared (0 without a record that verifies). */
  reader: number
  /** The level the host counts now. */
  level: number
}
export interface HostReaders {
  /** False: the host does not serve `reader/1` (a host before H12d-2). */
  available: boolean
  readers: HostReader[]
  /** Every reader must be at this level: 1 while click-to-sign is on or the history has click-signed entries. */
  required_reader: number | null
}
/** `member.list`. The host serves no `last_seen`: there is no such field. */
export interface RosterMember {
  person: string
  /** The person id when the host's name had invisible characters (`name_hidden`): that name is never shown. */
  name: string
  role: Role
  devices: RosterDevice[]
  name_hidden?: boolean
}

export interface HostGrant {
  id: string
  person: string
  scope: string
  verbs: string | string[]
  issued_at: string
  expires_at: string
  revoked: { at: string; by: string | null; seq: number | null } | null
  sessions: number
  /** H11c-2: the grant.issued label (`runner` marks the runner grant); absent or null = the host does not say. */
  label?: string | null
  /** `read_ext/2`; null = the host does not say. Capped at 100 by the host. */
  session_ids: string[] | null
  sessions_more: boolean | null
}

/** Is this value a whole number in the closed range? */
export const inRange = (v: number, min: number, max: number) => Number.isInteger(v) && v >= min && v <= max

/**
 * Who may sign what (host contract matrix, F1 5.4.2 and 10.1): the page hides or disables what the signed-in person may
 * not sign. The host stays the authority and refuses the rest.
 */
export const canChangeSettings = (role: Role | undefined) => role === 'owner'
export const canChangeRoles = (role: Role | undefined) => role === 'owner'
/** Owners add anyone; maintainers add members and viewers. */
export const canAddRole = (me: Role | undefined, role: Role) => me === 'owner' || (me === 'maintainer' && (role === 'member' || role === 'viewer'))
/** Owners remove anyone; maintainers remove members and viewers. */
export const canRemoveMember = (me: Role | undefined, target: Role) => canAddRole(me, target)
/** Owners revoke any grant, everyone else their own (F1 10.1 A3). */
export const canRevokeHostGrant = (me: Role | undefined, grantPerson: string, person: string | undefined) => me === 'owner' || (!!me && me !== 'viewer' && grantPerson === person)

/** A policy as the host stores it (canonical: lists sorted, no repeats), for a form's comparison. */
export function canonicalPolicy(p: HostPolicy): HostPolicy {
  const sorted = (xs: string[]) => [...new Set(xs)].sort()
  return { approvers: sorted(p.approvers), count: p.count, not: sorted(p.not), applies: typeof p.applies === 'string' ? p.applies : sorted(p.applies), independent: p.independent }
}

export const samePolicy = (a: HostPolicy, b: HostPolicy) => JSON.stringify(canonicalPolicy(a)) === JSON.stringify(canonicalPolicy(b))

/** Why this policy cannot be saved, or null. The code review is never approved by the assignees and is always independent (D59). */
export function policyProblem(gate: PolicyGate, p: HostPolicy): string | null {
  if (p.approvers.length === 0) return 'Pick at least one kind of approver.'
  if (p.approvers.length > 8 || p.not.length > 8) return 'A policy names at most 8 kinds of approver.'
  if (!inRange(p.count, 1, 9)) return 'Approvals needed is 1 to 9.'
  if (Array.isArray(p.applies) && p.applies.length === 0) return 'Pick at least one ticket type, or turn the code review off.'
  if (gate === 'code' && (!p.not.includes('assignees') || !p.independent)) return 'The code review is never approved by the assignees and is always independent.'
  return null
}

/** The base64url alphabet only, 65 bytes: 87 characters, no padding. */
export const PUBLIC_KEY = /^[A-Za-z0-9_-]{87}$/
export const MAX_CERT_BYTES = 16384
export const MAX_KEY_FILE_BYTES = 256

/** The text of an uploaded public-key file: trimmed once at its ends (a trailing newline), nothing else repaired. */
export const keyText = (raw: string) => raw.replace(/^\s+|\s+$/g, '')

/** What can be said about an uploaded certificate file before the host looks at it; the host's `derived` is what is shown as fact. */
export function certProblem(raw: string): string | null {
  if (new TextEncoder().encode(raw).length > MAX_CERT_BYTES) return 'The certificate file is larger than 16 KiB, so it is not a device certificate.'
  try {
    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== 'object' || Array.isArray(value)) return 'A device certificate is a JSON object.'
  } catch {
    return 'The certificate file is not JSON.'
  }
  return null
}
