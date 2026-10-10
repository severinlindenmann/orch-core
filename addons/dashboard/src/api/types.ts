// Hand-written API types. They mirror docs/architecture/orch-v2-ticket-format.md:
//   §3 ticket.json (definitions), §5 events, §7 ticket document (definitions + state derived from events),
//   §10.4 error shape. Later these may be generated from the FastAPI OpenAPI schema.

import type { TicketNeeds } from './connections'

export type Status = 'backlog' | 'open' | 'in-progress' | 'waiting' | 'testing' | 'done'
export const STATUSES: Status[] = ['backlog', 'open', 'in-progress', 'waiting', 'testing', 'done']

export type TicketType = 'feature' | 'bug' | 'chore' | 'spike' | 'epic'
export type Priority = 'low' | 'medium' | 'high' | 'urgent'
export type Size = 'xs' | 's' | 'm' | 'l' | 'xl'
export type Role = 'owner' | 'maintainer' | 'member' | 'viewer'
/**
 * The gates, in order. `code` is the opt-in code review (owner decision 2026-10-10): off by default, turned on per
 * workspace or per ticket type; when on it follows the verdict and signs exactly the commit the verdict signed.
 */
export type GateName = 'requirements' | 'plan' | 'verify' | 'code'
/** Where the code review gate applies: nowhere (default), every ticket, or only tickets of these types. */
export type CodeReviewApplies = 'off' | 'all' | TicketType[]

// ---------------------------------------------------------------- workspace / people

export interface Member {
  person: string // p_sev
  name: string
  role: Role
  devices?: number
  last_seen?: string | null
}

/** Per-workspace state of an installed addon (folded from addon.* workspace events). */
export interface WorkspaceAddon {
  enabled: boolean
  status: AddonStatus
  installed: boolean
  /** The grant for the installed version; null until the owner signs one. */
  granted: AddonGrant | null
  version: string
  /** The installed package (from addon.installed / addon.updated); a grant must match it. */
  package_sha256: string
  capabilities: string[]
}

export interface Workspace {
  /** The workspace's folder on the host (the harness root that holds its repos); read-only, relative paths resolve against it. */
  root_folder?: string
  id: string
  prefix: string // DEMO
  name: string
  members: Member[]
  /** Gate policies. `code.applies` says where the code review gate is on (the other gates always apply). */
  gates: Record<GateName, { approvers: string; count: number; not?: string; applies?: CodeReviewApplies }>
  addons: Record<string, WorkspaceAddon>
  counts: Partial<Record<Status, number>>
  needs_you: number
  /** Whether the owner turned the (simulated) relay link on: folded from relay.connected / relay.stopped. */
  relay?: 'on' | 'off'
  /** Default agent grant length in hours (absent: 8); also the longest grant a member may sign for themselves. */
  grant_hours?: number
  /**
   * `config.json` → `settings.repos` (ticket format §2): repo name → working copy. Owner-signed (`settings.changed`,
   * §5.4.2); `links.repos` names must be in it. This is the one declared repo list (the Repos addon shows and edits it,
   * it keeps no second list).
   */
  repos?: Record<string, WorkspaceRepo>
}

/**
 * One `settings.repos` entry. `path` is the format's (absolute, or relative to the workspace folder). `remote` and
 * `default_branch` are a PROPOSED format amendment (docs/repos-addon-proposal.md): the source a missing repo is cloned
 * from; credential-free, never a secret.
 */
export interface WorkspaceRepo {
  path: string
  remote?: string
  default_branch?: string
}

/** A person this device knows (from any workspace or the identity registry): what the Add member combobox offers. */
export interface KnownPerson {
  person: string
  name: string
  email: string
}

export interface Me {
  person: string
  name: string
  role: Role // role in the current workspace is in workspaces[].members; this is the highest
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

/** POST /api/workspaces/:ws/tickets. Acceptance criteria are texts; the host assigns AC1.. */
export interface NewTicketRequest {
  type: TicketType
  title: string
  priority: Priority
  size: Size | null
  labels: string[]
  parent: string | null
  due: string | null
  visibility: Visibility
  people: { owner: string | null; assignees: string[]; reviewers: string[] }
  sections: BodySections
  acceptance: string[]
}

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

/** How an approval or answer came in. 'factory_charter': auto-approved by an agent under a signed factory charter (core's store.autoApprove). */
export type Via = 'cli' | 'dashboard' | 'phone' | 'factory_charter'
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
  /** `presence` is absent for a charter approval (no person was present; the charter was signed when the epic started). */
  approvals: { by: string; at: string; via?: Via; presence?: Presence; sig_ok?: boolean; source_sha?: string }[]
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
  /** Approvals an invalidation voided (for the record; they never count toward `needed`). */
  voided?: GateStatus['approvals']
  /** verify and code: the commit (branch head) the standing approval signed. */
  source_sha?: string
  /** code only: whether the code review gate applies to this ticket (workspace policy, by ticket type). */
  required?: boolean
}

/** The ticket's branch as core sees it (D53): its head is what a verdict and a code review sign. */
export interface TicketBranch {
  name: string
  /** The branch it is compared with (the landing target). */
  base: string
  /** The head commit now (short sha). */
  head: string
  commits: { sha: string; at: string; by: string; task?: string }[]
  /** Diffstat of the branch against `base`. */
  files: number
  additions: number
  deletions: number
}

/** GET /api/tickets/:key/changes: core's diff of the branch against its base (the Changes view). */
export interface TicketChanges {
  ticket: string
  branch: string
  base: string
  head: string
  additions: number
  deletions: number
  files: { path: string; additions: number; deletions: number; lines: string }[]
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
  /** `source_sha`: the commit the verdict signed. `via: 'factory_charter'`: auto-approved under a factory charter (no person reviewed it). */
  verdict: { result: 'pass' | 'fail'; by: string; at: string; text?: string; source_sha?: string; via?: Via; charter?: string; charter_signed_by?: string } | null
  turn: Turn
  body: BodySections
  head: { seq: number; hash: string }
  created_at: string
  updated_at: string
  restricted: boolean
  children?: string[] // epic: child keys
  /** Revisions per body section (oldest first), derived from section.edited events. */
  section_history?: Partial<Record<keyof BodySections, SectionRevision[]>>
  /** Skills, connections and env this ticket needs, with the connections' last check (core-computed; D55–D57). */
  needs?: TicketNeeds
  /** The ticket's branch: head commit, commits and diffstat against its base (core-computed). */
  branch: TicketBranch
  /**
   * Where the ticket is in landing (D53), read by core from the landing records in its own log (`land.*` events by the
   * landing addon), only while that addon is active. A done ticket that is queued, being checked or failed to land is
   * shown as "Landing" / "Landing failed", never as Done. Absent: not landing (never queued, landed or taken off).
   */
  landing?: TicketLanding
}

export interface TicketLanding {
  state: 'queued' | 'failed'
  /** The addon that wrote the records. */
  addon: string
  attempt?: number
  reason?: 'conflict' | 'red_checks' | 'timeout'
  at: string
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
  /** The gate this ticket waits at right now (same rule as Today's approval items), or null. */
  awaiting_gate: GateName | null
  addons: Record<string, Record<string, unknown>>
  updated_at: string
  landing?: TicketLanding
  /** Set by list search (`q`) when a body section matched: the hit is wrapped in «». */
  match?: { section: keyof BodySections; snippet: string }
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

/** A session in the agents tree: top-level sessions and their subagents (`s_77c2.2` has parent `s_77c2`). */
export interface AgentSession extends AgentInfo {
  parent: string | null
  harness: 'claude-code' | 'codex' | 'ci'
  model?: string
  state: 'working' | 'waiting' | 'idle' | 'stopped'
  waiting_on?: { kind: 'question' | 'approval' | 'verdict'; ticket: string; ref?: string }
}
export interface AgentActivityItem {
  at: string
  ticket: string
  session: string
  agent: string
  for: string
  type: string // task.started, task.done, ask, claim.taken, refused, ...
  summary: string
  refusal?: { code: string; message: string; retryable: boolean; stop: boolean } // stop: the third same refusal
}

// ---------------------------------------------------------------- starting agents

export type LaunchMode = 'refine' | 'work' | 'fix' | 'continue'
export type LaunchHarness = 'claude-code' | 'codex'
export type LaunchWhere = 'terminals' | 'background'

/**
 * What core will start for one ticket, as the start-agent addon's state carries it (`previews[ticket]`): the labels
 * of the person's choice, the exact command and, when model routing is on, its one-line model summary. Core's
 * start dialog shows this; the start itself resolves it again with the same resolver.
 */
export interface LaunchPreview {
  ticket: string
  title: string
  mode: string
  harness: string
  where: string
  command: string
  /** "Model · work runs on standard: Standard (sonnet); subagents on haiku" (model routing on). */
  model?: string
  /** Why Start is blocked (a setting that is not a model name), as one sentence. */
  blocked?: string
  /** The choice as ids (what the addon asks core to start); core validates it and computes everything else itself. */
  request?: { mode: string; harness: string; where: string }
}

/** What core will start, computed by core from the request and the store (GET .../agents/launch). Core's start dialog shows only this as fact. */
export interface CoreLaunch {
  workspace: string
  ticket: string
  title: string
  mode: string
  harness: string
  where: string
  command: string
  model?: string
  tier?: string
  /** The subagent model passed in the environment (Claude Code only). */
  subagent_model?: string
  /** The launch addon's own one-line summary: its words, shown apart from core's facts (`line_by` names it). */
  line?: string
  line_by?: string
  /** A sentence from the launch addon (`blocked_by`) that blocks the start; shown as that addon's words. */
  blocked?: string
  blocked_by?: string
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
  /** Open items the viewer cannot act on: how many, and the people (ids) who can. */
  waiting_on_others: { count: number; people: string[] }
  working: TicketSummary[]
  recent: (Pick<OrchEvent, 'seq' | 'at' | 'type' | 'actor'> & { ticket: string; title: string; summary: string })[]
  counts: Partial<Record<Status, number>>
}

// ---------------------------------------------------------------- workspace settings

export interface WorkspaceIdentity {
  uuid: string
  prefix: string
  created_at: string
  key_fingerprint: string
  epoch: number
}

// ---------------------------------------------------------------- relay & devices (simulated; orch v2 P2/P3)

/** The link's state, as orch serve --remote names it (docs/remote.md): off, connecting, online, reconnecting, stopped, error. */
export type RelayLink = 'off' | 'connecting' | 'online' | 'reconnecting' | 'stopped'
/** Device scopes from the bridge protocol: Look, Decide, Operate, Type. */
export type DeviceScope = 'look' | 'decide' | 'operate' | 'type'
export interface RelayDevice {
  id: string // d_mac
  person: string
  label: string // "Severin's MacBook Pro"
  platform: 'mac' | 'iphone' | 'ipad' | 'linux'
  /** The device that holds the person key and signs device certificates. */
  primary: boolean
  /** The device this dashboard runs on (the workspace host). */
  this_device: boolean
  scopes: DeviceScope[]
  paired_at: string
  last_seen: string | null
  /** The newest epoch key sealed to this device; behind the workspace epoch until it fetches it from the relay. */
  epoch: number
}
export interface RelayPairing {
  id: string
  state: 'waiting' | 'confirm' | 'expired'
  started_at: string
  expires_at: string
  /** The 6-character code both screens show once the phone has joined (state 'confirm'). */
  fingerprint?: string
  /** What the joining device calls itself (state 'confirm'). */
  label?: string
  platform?: RelayDevice['platform']
}
export interface RelayQueueItem {
  id: string
  kind: 'seal_key' | 'push' | 'drop' | 'answer'
  label: string
  device?: string
  ticket?: string
  queued_at: string
  state: 'queued' | 'sent'
  sent_at?: string
}
/** GET /api/workspaces/:ws/relay. Every part is simulated in the mockup (`simulated: true`). */
export interface RelayState {
  simulated: true
  now: string
  relay_url: string
  link: RelayLink
  since: string | null
  epoch: number
  epoch_started: string
  next_rotation: string
  devices: RelayDevice[]
  pairing: RelayPairing | null
  queue: RelayQueueItem[]
}
/** POST /api/workspaces/:ws/relay. Owner only; connect, stop, confirm and remove are signed in the dashboard. */
export type RelayRequest =
  | { op: 'connect' | 'stop' | 'pair.start' | 'pair.cancel' }
  | { op: 'pair.confirm'; pairing: string; fingerprint: string }
  | { op: 'device.remove'; device: string }
/** POST /api/dev/relay/:ws (mock only): what the relay or a phone would do. */
export type RelaySimRequest = { op: 'drop' | 'scan' }

// ---------------------------------------------------------------- workspace artifacts

/** One artifact in the workspace browser (GET /api/workspaces/:ws/artifacts): no inline content. */
export interface ArtifactItem extends Omit<Artifact, 'preview'> {
  ticket: string
  ticket_title: string
  /** Who added it: a person, an agent (with the person it works for), an addon, or orch itself. */
  by: { kind: 'person' | 'agent' | 'addon' | 'host'; id: string; for?: string }
  has_preview: boolean
}
export interface ArtifactQuery {
  kind?: string
  ticket?: string
  /** 'people', 'agents', or one actor id (p_sev, claude-code, …). */
  by?: string
  /** '24h' | '7d' | '30d' (from the mock clock). */
  since?: string
  q?: string
  page?: number
  per?: number
}
export interface ArtifactPage {
  items: ArtifactItem[]
  total: number
  page: number
  pages: number
  per: number
  facets: {
    kinds: { kind: Artifact['kind']; count: number }[]
    tickets: { key: string; title: string; count: number }[]
    by: { id: string; kind: ArtifactItem['by']['kind']; count: number }[]
  }
}

/** POST /api/workspaces/:ws/settings. Owner only; every op except rename is signed in the UI. */
export type SettingsRequest =
  | { op: 'rename'; name: string }
  | { op: 'member.add'; person: string; name: string; role: Role }
  | { op: 'member.role'; person: string; role: Role }
  | { op: 'member.remove'; person: string }
  | { op: 'gate.policy'; gate: GateName; approvers: string; count: number; not?: 'assignees' | null; applies?: CodeReviewApplies }
  | { op: 'grant.hours'; hours: number }
  /** Format §5.4.2 `settings.changed` `set.repos`: repo name → entry, or null to remove it (signed in the UI). */
  | { op: 'repos'; set: Record<string, WorkspaceRepo | null> }
  | { op: 'archive'; prefix: string }

/** POST /api/workspaces/:ws/addons/:name. Owner only; grant and update are signed in the UI. */
export type AddonOpRequest =
  | { op: 'install' | 'enable' | 'disable' | 'uninstall' }
  /**
   * grant and update carry exactly what the person saw and signed; the host refuses (409 addon.changed) if it differs now.
   * `enable: true` on a grant makes turning the addon on part of the same signed act. An update is itself the grant of the
   * new version (one signature; the addon keeps its on/off state).
   */
  | { op: 'grant' | 'update'; version: string; package_sha256: string; capabilities: string[]; viewer_actions: string[]; enable?: boolean }
  /** Install from the catalog as one signed act: install, grant exactly these values and turn it on (409 addon.changed if the package differs now). */
  | { op: 'install'; version: string; package_sha256: string; capabilities: string[]; viewer_actions: string[]; enable?: boolean }

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
  /** Binding path (e.g. `addon.prByTicket.$ticket`): the contribution is skipped when it resolves to nothing. */
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
  /**
   * What answering it authorises, as plain values (at most 12, keys like arg keys): core shows each one as its own
   * line in the signing covers ("Words (key): value", in full), posts them with the answer, refuses the answer when they
   * no longer match (409 decision.closed) and records them in `addon.decided`. Invalid terms: the decision is not offered.
   */
  terms?: Record<string, string | number>
  /**
   * A delivery that waits out its hold window (factory full run, owner decision 2026-10-10 evening; provisional): it goes
   * out at `until` unless a person answers. The decision then has one option, `stop`. Core shows it on Today, in place
   * on the addon's page, and as a calm notice in the shell ("Delivering in 28 min · <deliver_means> · Stop…").
   */
  hold?: { until: string; deliver_means: string }
  /** Posted to POST /api/workspaces/:ws/addons/:addon/actions/:action with { option, ticket }. */
  action: string
}

export type AddonStatus = 'active' | 'disabled' | 'needs_grant'

/** The owner's signed addon.granted: what an addon may do at one version, bound to its package hash. */
export interface AddonGrant {
  version: string
  capabilities: string[]
  package_sha256: string
  at: string
  by: string
}

/** An action's manifest entry: its minimum role and, for viewer-level actions, the name shown when the owner signs a grant. */
export interface ActionMeta {
  minRole: Role
  label?: string
  /**
   * Core confirms this action in its own dialog before it is posted (the addon's node cannot skip it).
   * 'spawn_agent': the start-agent dialog (signs a grant first when the person has none). The host refuses the
   * action without core's `confirmed` flag (409 confirm.required).
   * 'sign': core's own signing prompt (what is covered, then Touch ID). Title, covers and button are core's words
   * (the action id, the addon's "Title (id)", every arg); `label` is shown in the addon's labelled region. The host
   * refuses the action without core's `confirmed` flag. Use it for switches only a human may flip (arm, pause).
   */
  confirm?: 'spawn_agent' | 'sign' | 'destructive' | 'options'
  /**
   * 'destructive': core's own confirm dialog (not a signature). Title, the args it sends, the consequence line and
   * the button ("Confirm: Revoke (revoke)") are core's; `confirmLabel` and `confirmText` are the package's words, shown
   * in full in its labelled region. The host refuses the action without core's `confirmed` flag (409 confirm.required).
   */
  confirmLabel?: string
  confirmText?: string
  /**
   * 'options': core's small dialog asks for these choices first (a select per field), then posts the action with
   * `{ [field.key]: chosen value }` merged into its args and core's `confirmed` flag (the host refuses it without).
   * Not a signature. Title, the args it sends and the button ("Continue: Share once (share_once)") are core's; the
   * label, note, field and choice labels are the package's words, shown in its labelled regions.
   */
  options?: { fields: { key: string; label: string; choices: { value: string | number; label: string }[]; default: string | number }[]; note?: string }
  /**
   * The action that reverses this one (e.g. stop -> start). Core shows "Undo" on the success toast only when the
   * response's `undo.action` is exactly this, and the target is a plain action (no confirm, no decision, not navigation)
   * the viewer's role may run. It then runs through the normal path. The pair is read from the manifest, never the response.
   */
  undo?: string
  /**
   * 'navigation': the action only changes what this viewer is looking at (`state.nav[viewer]`: current item, filters,
   * search, page size) or opens something. Core shows no success toast, refetches only this addon's state, and the
   * host does not move the workspace cursor (other clients do not refetch).
   */
  kind?: 'navigation'
  /**
   * The action answers an addon decision (`AddonDecision.action`). Core decides who may answer (`addon.decide`),
   * checks the decision is open for the caller and the option is one of its options (400 validation.option), and
   * records `addon.decided` in the workspace log. Today asks for presence (core's signing prompt) first.
   */
  decision?: boolean
}

export interface AddonUpdate {
  version: string
  capabilities: string[]
  package_sha256: string
  changelog: string
  /** The update's action manifest, when it differs from the installed package's (viewer-level actions are part of the grant). */
  actions?: Record<string, ActionMeta>
}

/** A published addon package: global, the same in every workspace. Per-workspace state is `WorkspaceAddon`. */
export interface AddonPackage {
  name: string
  title: string
  /** The published version (what installing gets). */
  version: string
  description: string
  capabilities: string[]
  first_party: boolean
  package_sha256: string
  /** A newer published version, if any. */
  update: AddonUpdate | null
  /** A preview: core draws a "Preview" chip wherever the title appears (nav, page title, addon manager). Never addon-authored markup. */
  preview?: boolean
  contributions: AddonContribution[]
  decisions?: AddonDecision[]
  /** Command palette entries; each runs POST /api/workspaces/:ws/addons/:name/actions/:action. */
  commands?: { id: string; title: string; action: string }[]
  /** Who may run an action: its minimum role (default 'member'; 'viewer' for read-only navigation). The one source of truth. */
  actions?: Record<string, ActionMeta>
}

/** An addon installed in one workspace: the package plus that workspace's state (installed version, grant, status). */
export type InstalledAddon = AddonPackage & { ws: WorkspaceAddon }

export interface AddonActionResult {
  ok: true
  message: string
  /** True when the action changed addon data: the client refetches. */
  changed?: boolean
  /** An https address the client opens in a new tab (e.g. github's Open). */
  url?: string
  /** A ticket this action created (e.g. an imported issue): the success toast offers "Open". */
  ticket?: string
  /** A terminals session this action opened (e.g. Worktrees' "Open terminal here"): core opens the dock on it. */
  terminal?: string
  /** Reversible: the toast carries "Undo", which posts `undo.action` with `undo.args` (same addon). */
  undo?: { action: string; args?: Record<string, string | number | boolean> }
  /**
   * A value shown once (a share link). Core shows it in a modal with Copy and "I saved it", never in a toast,
   * and the message must not contain it.
   */
  secret?: { label: string; value: string; note?: string }
}

// ---------------------------------------------------------------- actions & errors

export type ActionRequest =
  /** `hash`: the question's hash as the dialog showed it; refused when the question changed since (`question.stale`). */
  | { action: 'answer'; question: string; option?: string; text?: string; hash: string }
  /**
   * `source_sha` (code gate): the commit the person reviewed; refused when the branch moved since. `hash` (requirements
   * and plan): the gate's content hash as the dialog showed it; required, refused when the content changed (`gate.stale`).
   */
  | { action: 'approve'; gate: GateName; source_sha?: string; hash?: string }
  | { action: 'request_changes'; gate: GateName; text: string }
  /** `source_sha`: the branch head the dialog showed; the host refuses a verdict on another commit (`verdict.stale`). */
  | { action: 'verdict'; result: 'pass' | 'fail'; text?: string; source_sha: string }
  | { action: 'comment'; text: string }
  | { action: 'ask'; to: string; text: string; options?: QuestionOption[]; blocking?: boolean }
  | { action: 'claim' }
  | { action: 'release' }
  | { action: 'set_status'; status: Status }
  | { action: 'add_label'; label: string }

export interface ActionResult {
  ok: true
  /** The event the action appended; null for a no-op (e.g. adding a label the ticket already has). */
  event: OrchEvent | null
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
  | 'repos.checked' | 'repos.clone_queued' | 'repos.cloned' | 'repos.clone_failed' | 'repos.clone_cancelled' | 'repos.fetched' | 'repos.terminal_opened'
  | 'settings.changed'
  | 'member.added' | 'member.role_changed' | 'member.removed'
  | 'gate.policy_set'
  | 'addon.installed' | 'addon.granted' | 'addon.enabled' | 'addon.disabled' | 'addon.updated' | 'addon.uninstalled'
  | 'addon.settings_saved' | 'addon.action_signed' | 'addon.decided'
  | 'grant.issued' | 'grant.revoked'
  | 'agent.started' | 'agent.stopped'
  | 'view.saved' | 'view.deleted'
  | 'ticket.discarded'
  | 'workspace.renamed' | 'workspace.grant_hours_set'
  | 'terminal.shell_opened'
  | 'relay.connected' | 'relay.stopped' | 'device.paired' | 'device.removed' | 'epoch.rotated'
  | 'skill.credentials_granted' | 'connection.checked'
  | 'records.committed' | 'records.pushed' | 'records.pulled'
  | 'links.pairing_started' | 'links.pairing_cancelled' | 'links.terms_set' | 'links.pairing_denied' | 'links.paired' | 'links.revoked'
  | 'links.request_received' | 'links.request_accepted' | 'links.request_denied' | 'links.answered' | 'links.scope_changed' | 'links.sent' | 'links.handoff_returned'
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
  /** `all`: every ticket in the workspace (owners, maintainers); `workable`: the tickets the person may work on (a member's self-grant); `ci`: CI only. */
  scope: 'all' | 'workable' | 'ci'
  issued_at: string
  until: string
  revoked: { at: string; by: string } | null
  sessions: string[] // agent sessions currently using it
}

/** The filters of the tickets list (same names as its router search params). */
export interface ViewParams {
  q?: string
  status?: Status[]
  type?: string
  priority?: Priority[]
  person?: string
  needs?: 'me' | 'agent' | 'nobody'
  label?: string
  sort?: 'updated' | 'priority' | 'key' | 'status'
}
export interface SavedView {
  id: string
  name: string
  owner: string
  shared: boolean
  params: ViewParams
}
