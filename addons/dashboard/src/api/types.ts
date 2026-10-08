// Hand-written API types. They mirror docs/architecture/orch-v2-ticket-format.md:
//   §3 ticket.json (definitions), §5 events, §7 ticket document (definitions + state derived from events),
//   §10.4 error shape. Later these may be generated from the FastAPI OpenAPI schema.

export type Status = 'backlog' | 'open' | 'in-progress' | 'waiting' | 'testing' | 'done'
export const STATUSES: Status[] = ['backlog', 'open', 'in-progress', 'waiting', 'testing', 'done']

export type TicketType = 'feature' | 'bug' | 'chore' | 'spike' | 'epic'
export type Priority = 'low' | 'medium' | 'high' | 'urgent'
export type Size = 'xs' | 's' | 'm' | 'l' | 'xl'
export type Role = 'owner' | 'maintainer' | 'member' | 'viewer'
export type GateName = 'requirements' | 'plan' | 'verify'

// ---------------------------------------------------------------- workspace / people

export interface Member {
  person: string // p_sev
  name: string
  role: Role
}

export interface Workspace {
  id: string
  prefix: string // DEMO
  name: string
  members: Member[]
  gates: Record<GateName, { approvers: string; count: number; not?: string }>
  addons: Record<string, { enabled: boolean }>
  counts: Partial<Record<Status, number>>
  needs_you: number
}

export interface Me {
  person: string
  name: string
  role: Role // role in the current workspace is in workspaces[].members; this is the highest
  grant: { id: string; until: string; hours: number } | null
}

// ---------------------------------------------------------------- ticket.json (definitions)

export interface AcceptanceDef {
  id: string // AC1
  text: string
}

export interface TaskDef {
  id: string // T1
  text: string
  verify: { cmd: string } | null
  proves: string[]
  assignee?: string
}

export interface QuestionOption {
  key: string
  label: string
  cost?: string
}

export interface QuestionDef {
  id: string // Q1
  to: string // person or ticket role
  text: string
  why?: string
  options?: QuestionOption[]
  recommended?: string
  blocking: boolean
}

export interface TicketLinks {
  repos: string[]
  branches: Record<string, string>
  prs: { repo: string; url: string }[]
  external: { label: string; url: string }[]
}

export type Visibility = 'workspace' | { restricted: string[] }

/** ticket.json: definitions only, never state. */
export interface TicketDefinition {
  schema: 'orch.ticket/2'
  uid: string
  key: string
  title: string
  type: TicketType
  priority: Priority
  size: Size | null
  labels: string[]
  parent: string | null
  blocked_by: string[]
  due: string | null
  visibility: Visibility
  links: TicketLinks
  acceptance: AcceptanceDef[]
  tasks: TaskDef[]
  questions: QuestionDef[]
  /** Addon data lives only under addons.<name> (T11). */
  addons: Record<string, Record<string, unknown>>
}

/** body.md sections, keyed by section name (lowercase, underscores). */
export type BodySections = Partial<
  Record<
    'summary' | 'context' | 'requirements' | 'out_of_scope' | 'plan' | 'decisions' | 'verification' | 'current_state',
    string
  >
>

// ---------------------------------------------------------------- events (§5)

export type Actor =
  | { kind: 'person'; id: string; device?: string }
  | { kind: 'agent'; id: string; session: string; for: string; grant?: string }
  | { kind: 'host'; id: 'orch' }
  | { kind: 'addon'; id: string }

export interface OrchEvent {
  v: 2
  id: string
  seq: number
  at: string
  type: string
  actor: Actor
  /** Event-specific payload, flattened as in the spec (task, gate, name, kind, ...). */
  [field: string]: unknown
}

// ---------------------------------------------------------------- derived state (§7)

export type TaskState = 'todo' | 'doing' | 'done' | 'blocked' | 'skipped'

export interface TaskStatus extends TaskDef {
  state: TaskState
  lease: { session: string; agent: string; since: string } | null
  receipt: { exit: number; ms: number; commit?: string } | null
  done_at: string | null
}

export interface AcceptanceStatus extends AcceptanceDef {
  state: 'unproven' | 'proven'
  evidence: { kind: 'artifact' | 'task'; ref: string }[]
}

export interface QuestionStatus extends QuestionDef {
  state: 'open' | 'answered'
  asked_at: string
  asked_by: string
  answer: { option?: string; text?: string; by: string; at: string; via?: Via; presence?: Presence } | null
  /** Hash an answer signs (the question text, options and recommendation). */
  hash?: string
}

export type Via = 'cli' | 'dashboard' | 'phone'
export type Presence = 'touchid' | 'passkey' | 'password'

export interface Artifact {
  name: string
  kind:
    | 'screenshot'
    | 'log'
    | 'report'
    | 'link'
    | 'dataset'
    | 'build'
    | 'diagram'
    | 'receipt'
    | 'feedback'
    | 'other'
  bytes: number
  sha256: string
  task?: string
  ac?: string
  label?: string
  added_by: string
  at: string
  /** Inline content for logs, receipts, reports and datasets (mock only; the real host serves the file). */
  preview?: string
  /** link artifacts. */
  url?: string
  /** Addon artifacts have an addon + ref instead of a file. */
  addon?: string
  ref?: string
}

export interface Claim {
  agent: string // claude-code
  session: string // s_77c2
  for: string // p_sev
  grant: string
  since: string
  expires: string
}

export interface GateStatus {
  state: 'pending' | 'approved' | 'invalidated' | 'changes_requested'
  approvals: { by: string; at: string; via?: Via; presence?: Presence; sig_ok?: boolean }[]
  needed: number
  approvers: string
  /** Excluded group, e.g. "assignees". */
  not?: string
  note?: string
  /** Hash of the gated content as it is now. */
  hash?: string
  /** What an approval covers, in words. */
  covers?: string[]
  /** Why an approval was invalidated (state === 'invalidated'). */
  reason?: string
}

export interface SectionRevision {
  rev: number
  at: string
  by: string
  text: string
}

export interface People {
  owner: string | null
  assignees: string[]
  reviewers: string[]
  watchers: string[]
}

export interface Turn {
  who: string // person id, "agent:<id>" or "nobody"
  why: string
}

export interface TicketDocument extends TicketDefinition {
  status: Status
  people: People
  claim: Claim | null
  tasks_state: TaskStatus[]
  acceptance_state: AcceptanceStatus[]
  questions_state: QuestionStatus[]
  gates: Record<GateName, GateStatus>
  artifacts: Artifact[]
  verdict: { result: 'pass' | 'fail'; by: string; at: string; text?: string } | null
  turn: Turn
  body: BodySections
  head: { seq: number; hash: string }
  created_at: string
  updated_at: string
  restricted: boolean
  children?: string[] // epic: child keys
  /** Revisions per body section (oldest first), derived from section.edited events. */
  section_history?: Partial<Record<keyof BodySections, SectionRevision[]>>
}

export interface TicketSummary {
  key: string
  uid: string
  title: string
  type: TicketType
  priority: Priority
  size: Size | null
  status: Status
  labels: string[]
  parent: string | null
  owner: string | null
  assignees: string[]
  claim: Pick<Claim, 'agent' | 'for' | 'session'> | null
  turn: Turn
  progress: { tasks_done: number; tasks_total: number; ac_proven: number; ac_total: number }
  open_questions: number
  blocking_questions: number
  restricted: boolean
  addons: Record<string, Record<string, unknown>>
  updated_at: string
}

// ---------------------------------------------------------------- agents

export interface AgentLease {
  ticket: string
  task: string
  session: string
}

export interface AgentInfo {
  id: string // claude-code
  name: string
  for: string // person
  session: string
  grant: { id: string; until: string } | null
  claims: { ticket: string; since: string; expires: string }[]
  leases: AgentLease[]
  last_seen: string
}

// ---------------------------------------------------------------- today

export type NeedsYouKind = 'question' | 'verdict' | 'approval' | 'handoff' | 'expiring'

export interface NeedsYouItem {
  kind: NeedsYouKind
  ticket: string
  title: string
  text: string
  since: string
  ref?: string // Q2, gate name, ...
  /** Gate hash an approval item covers (approval items only). */
  hash?: string
  blocking?: boolean
}

export interface TodayDocument {
  now: string
  workspace: string
  needs_you: NeedsYouItem[]
  /** Viewers get an empty needs_you; this lists what is open in the workspace, read only. */
  read_only_open: NeedsYouItem[]
  working: TicketSummary[]
  recent: (Pick<OrchEvent, 'seq' | 'at' | 'type' | 'actor'> & { ticket: string; title: string; summary: string })[]
  counts: Partial<Record<Status, number>>
}

// ---------------------------------------------------------------- addons.json

export type AddonSlot = 'nav' | 'today.card' | 'ticket.panel' | 'board.lane' | 'board.card_field' | 'settings'
export const ADDON_SLOTS: AddonSlot[] = ['nav', 'today.card', 'ticket.panel', 'board.lane', 'board.card_field', 'settings']

export interface AddonContribution {
  slot: AddonSlot
  id: string
  title: string
  icon?: string // lucide icon name (fixed set, see app/icons.tsx)
  /**
   * Declarative node tree (see addon-ui/nodes.ts). Validated at render time, never trusted.
   * String values may use `${path}` and `{"$ref": "path"}` bindings against the slot context
   * (`ticket`, `workspace`); the addon never ships code into the page.
   * nav: the page body. board.lane: a `list` node (items become cards). board.card_field: a small `stat`/`kv`.
   */
  node: unknown
  /** Binding path (e.g. `ticket.addons.github.pr`): the contribution is skipped when it resolves to nothing. */
  when?: string
}

/** Data an addon hands to core when it needs a human decision. Core renders it (never an addon node). */
export interface AddonDecision {
  kind: 'decision'
  id: string
  addon: string
  ticket?: string
  title: string
  question: string
  detail?: string
  options: { key: string; label: string; primary?: boolean }[]
  /** Posted to POST /api/addons/:addon/actions/:action with { option, ticket }. */
  action: string
}

export interface AddonManifest {
  name: string
  title: string
  version: string
  description: string
  capabilities: string[]
  enabled: boolean
  first_party: boolean
  contributions: AddonContribution[]
  decisions?: AddonDecision[]
  /** Command palette entries; each runs POST /api/addons/:name/actions/:action. */
  commands?: { id: string; title: string; action: string }[]
}

export interface AddonActionResult {
  ok: true
  message: string
  /** True when the action changed addon data: the client refetches. */
  changed?: boolean
}

// ---------------------------------------------------------------- actions & errors

export type ActionRequest =
  | { action: 'answer'; question: string; option?: string; text?: string }
  | { action: 'approve'; gate: GateName }
  | { action: 'request_changes'; gate: GateName; text: string }
  | { action: 'verdict'; result: 'pass' | 'fail'; text?: string }
  | { action: 'comment'; text: string }
  | { action: 'ask'; to: string; text: string; options?: QuestionOption[]; blocking?: boolean }
  | { action: 'claim' }
  | { action: 'release' }
  | { action: 'set_status'; status: Status }

export interface ActionResult {
  ok: true
  event: OrchEvent
  ticket: TicketDocument
}

export interface ApiErrorBody {
  ok: false
  error: { code: string; message: string; hint?: string; retryable: boolean }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly hint?: string
  constructor(status: number, body: ApiErrorBody['error']) {
    super(body.message)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code
    this.hint = body.hint
  }
}

// ---------------------------------------------------------------- workspace event log

export type WorkspaceEventType =
  | 'member.added' | 'member.role_changed' | 'member.removed'
  | 'gate.policy_set'
  | 'addon.installed' | 'addon.granted' | 'addon.enabled' | 'addon.disabled' | 'addon.updated' | 'addon.uninstalled'
  | 'addon.settings_saved'
  | 'grant.issued' | 'grant.revoked'
  | 'view.saved' | 'view.deleted'
  | 'workspace.renamed'
export interface WorkspaceEvent {
  v: 2
  id: string
  seq: number
  at: string
  type: WorkspaceEventType
  actor: Actor
  [k: string]: unknown
}
export interface GrantInfo {
  id: string
  person: string
  scope: 'all' | 'ci'
  issued_at: string
  until: string
  revoked: { at: string; by: string } | null
  sessions: string[] // agent sessions currently using it
}
