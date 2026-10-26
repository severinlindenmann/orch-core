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

/** What the host saw at a repo's folder just now (read-only). Only an owner or maintainer gets it. */
export interface HostRepoCheck {
  state: 'present' | 'missing' | 'not_git' | 'unreadable'
  branch: string | null
}

export interface HostRepo {
  name: string
  default_branch: string | null
  remote: string | null
  /** The declared folder: only when the host sent it (owners and maintainers). */
  path?: string | null
  check?: HostRepoCheck | null
  /** Keys of the tickets that link this repository (owners and maintainers; the host sends at most 12). */
  tickets?: string[] | null
}

/** The host's own spelling of a repo name, a canonical remote (https, no login) and what a path must look like first. */
export const REPO_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/
const REPO_REMOTE = /^https:\/\/[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?\/[^\s@?#]{0,1000}$/
const HIDDEN = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]/

export interface RepoDraft {
  name: string
  path: string
  remote: string
  branch: string
}

/** Why a repo form cannot be sent yet (the host checks the folder itself; this only stops what is plainly wrong). */
export function repoProblems(d: RepoDraft): Partial<Record<keyof RepoDraft, string>> {
  const out: Partial<Record<keyof RepoDraft, string>> = {}
  if (!REPO_ID.test(d.name)) out.name = 'Use lowercase letters, digits, dots and dashes (up to 64), starting with a letter or digit.'
  if (!d.path.startsWith('/') || d.path.length > 1024 || HIDDEN.test(d.path) || d.path.split('/').includes('..') || d.path.includes('//')) out.path = 'Write the full path of the folder, starting with / (no ".." in it).'
  if (d.remote !== '' && (!REPO_REMOTE.test(d.remote) || HIDDEN.test(d.remote))) out.remote = 'A remote is an https:// address without a login, or leave it empty.'
  if (d.branch !== '' && (d.branch.length > 255 || HIDDEN.test(d.branch) || /\s/.test(d.branch))) out.branch = 'A branch name is one word, or leave it empty.'
  return out
}

/** The host's look at a folder in the words a person uses. */
export function repoCheckWords(c: HostRepoCheck): string {
  switch (c.state) {
    case 'present':
      return c.branch ? `In place, on branch ${c.branch}` : 'In place, not on a branch'
    case 'missing':
      return 'Folder not found'
    case 'not_git':
      return 'The folder is not a git repository'
    default:
      return 'The folder cannot be read'
  }
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
  { key: 'grant_hours', label: 'Agent access length', unit: 'hours', min: 1, max: GRANT_MAX_HOURS, hint: 'The longest a new agent access lasts.' },
  { key: 'claim_ttl_min', label: 'Ticket reserved for', unit: 'minutes', min: 15, max: 1440, hint: 'When an agent takes a ticket and goes quiet, it is freed for others after this long.' },
  { key: 'lease_ttl_min', label: 'Single task held for', unit: 'minutes', min: 5, max: 1440, hint: 'When a helper working on one task goes quiet, the task is freed after this long.' },
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

/** The four ways to answer "who has to approve this step?" (1.4). Each maps to a `policy_set` (the host signs its own sentence). */
export type ApprovalPreset = 'owner' | 'owner_or_maintainer' | 'two' | 'independent'
export const APPROVAL_PRESETS: readonly ApprovalPreset[] = ['owner', 'owner_or_maintainer', 'two', 'independent']
export const PRESET_LABEL: Record<ApprovalPreset, string> = {
  owner: 'An owner',
  owner_or_maintainer: 'An owner or maintainer',
  two: 'Two people (owners or maintainers)',
  independent: 'Someone other than who did the work',
}
const PRESET_SHAPE: Record<ApprovalPreset, Pick<HostPolicy, 'approvers' | 'count' | 'not' | 'independent'>> = {
  owner: { approvers: ['owner'], count: 1, not: [], independent: false },
  owner_or_maintainer: { approvers: ['maintainer', 'owner'], count: 1, not: [], independent: false },
  two: { approvers: ['maintainer', 'owner'], count: 2, not: [], independent: false },
  independent: { approvers: ['maintainer', 'owner'], count: 1, not: ['assignees'], independent: true },
}

/** The policy a preset stands for. Where it applies (every ticket, some types) is kept as it is. */
export const presetPolicy = (preset: ApprovalPreset, current: HostPolicy): HostPolicy => canonicalPolicy({ ...PRESET_SHAPE[preset], applies: current.applies })

/** The preset a stored policy is exactly, or null (a custom rule). */
export const presetOf = (p: HostPolicy): ApprovalPreset | null => {
  const c = canonicalPolicy(p)
  return APPROVAL_PRESETS.find((k) => JSON.stringify({ ...canonicalPolicy({ ...PRESET_SHAPE[k], applies: c.applies }) }) === JSON.stringify(c)) ?? null
}

/** The people who could ever approve under a policy: members of the named roles, or any member for ticket roles, minus the roles it excludes. */
export function eligibleApprovers(p: HostPolicy, members: readonly { person: string; role: Role }[]): number {
  const roles: Role[] = ['owner', 'maintainer', 'member']
  const named = new Set(p.approvers.filter((a): a is Role => roles.includes(a as Role)))
  const ticketRoles = p.approvers.some((a) => !roles.includes(a as Role))
  const barred = new Set(p.not.filter((a): a is Role => roles.includes(a as Role)))
  return members.filter((m) => m.role !== 'viewer' && !barred.has(m.role) && (named.has(m.role) || ticketRoles)).length
}

/** Why a step could never be approved with the people there are now, or null. */
export function policyUnmet(p: HostPolicy, members: readonly { person: string; role: Role }[]): { needs: number; has: number } | null {
  if (p.applies === 'off') return null
  const has = eligibleApprovers(p, members)
  return p.count > has ? { needs: p.count, has } : null
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

/** The four device levels, in the words a person uses. */
export const SCOPE_HELP: Record<string, string> = {
  look: 'read tickets and the log',
  decide: 'approve, request changes and answer',
  operate: 'change members, settings and grants',
  type: 'type into terminals',
}
/** What a device may do, in the words a person uses (the host's four scopes). */
export const RIGHTS: Record<string, string> = { look: 'Can read', decide: 'Can approve', operate: 'Can change settings', type: 'Can use terminals' }

/** The union of what a person's valid devices may do, in a fixed order. */
export const rightsOf = (devices: readonly RosterDevice[]): string[] =>
  Object.entries(RIGHTS).filter(([scope]) => devices.some((d) => d.valid && d.scopes.includes(scope))).map(([, text]) => text)

/** The one line under a person's name: where they sign from. */
export const deviceLine = (devices: readonly RosterDevice[], mine: boolean): string =>
  devices.length === 0 ? 'No device' : mine ? (devices.length === 1 ? 'This computer' : `This computer and ${devices.length - 1} more`) : devices.length === 1 ? '1 device' : `${devices.length} devices`

/** A device below the level the workspace requires cannot read click-signed entries. The signing device is never flagged. */
export const deviceBelow = (devices: readonly RosterDevice[], required: number | null, signsHere: boolean): boolean =>
  !signsHere && required !== null && required >= 1 && devices.some((d) => d.valid && (d.reader ?? 0) < required)

/** A person or device id without its tail: `p_` and the first 12 hex digits. The full id stays in a tooltip. */
export const shortId = (id: string): string => (id.length > 16 ? `${id.slice(0, 14)}…` : id)
