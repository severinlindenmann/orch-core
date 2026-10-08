// In-memory mock store seeded from fixtures. Mutations append events; state is re-derived from events.
// Appended events persist to localStorage (in try/catch; the viewer sandbox may block it).
import type {
  AddonActionResult,
  AddonDecision,
  AddonPackage,
  InstalledAddon,
  AddonOpRequest,
  Actor,
  AgentActivityItem,
  AgentSession,
  SavedView,
  BodySections,
  CoreLaunch,
  GateName,
  LaunchHarness,
  LaunchMode,
  LaunchWhere,
  Me,
  NewTicketRequest,
  NeedsYouItem,
  OrchEvent,
  Status,
  TicketDefinition,
  TicketDocument,
  TicketSummary,
  TodayDocument,
  GrantInfo,
  Workspace,
  WorkspaceEvent,
} from '@/api/types'
import { addonActive, pendingUpdate, manifestFor, sameSet, viewerActions } from '@/api/addons'
import { getAddon, openDecisions } from './addons'
import { deriveTicket, describeEvent, fnvHex, parseActor } from './derive'
import addonsFixture from './fixtures/addons.json'
import catalogFixture from './fixtures/catalog.json'
import demoFixture from './fixtures/demo.json'
import grantsFixture from './fixtures/grants.json'
import meFixture from './fixtures/me.json'
import otherFixture from './fixtures/other-workspaces.json'
import viewsFixture from './fixtures/views.json'
import workspacesFixture from './fixtures/workspaces.json'
import { SECTIONS_BY_TYPE, requiredAtCreation, sectionLabel, type SectionName } from '@/api/sections'
import { roleMeets } from '@/api/roles'
import { atLeast, can, canRevokeGrant, roleOf } from '@/api/permissions'
import { Simulator } from './sim'
import { activeGrantOf } from '@/api/grants'
import { isModelName, renderCommand, type LaunchSpec } from '@/api/launch'
import { HARNESSES, HARNESS_LABEL, MODES, MODE_LABEL, WHERES, WHERE_LABEL, launchSpec, foldSessions, sessionScript, type LaunchPlan, type LaunchRequest, type StartedSession } from './sessions'
import { clearPersisted, loadPersisted, savePersisted, type PersistedV2 } from './persist'
import { foldGrants, foldViews, foldWorkspace } from './workspace-log'
import { BUSY_SEED, generateBusy, type BusyData } from './busy/generate'
import { startLive } from './busy/live'
import { makeRng } from './busy/rng'

/** The mock "now" when the page loads: matches the fixtures (grant until 18:00 the same day). */
export const MOCK_EPOCH = '2026-10-09T11:30:00Z'

interface FixtureEvent {
  at: string
  type: string
  actor: string
  [k: string]: unknown
}
interface FixtureTicket {
  definition: Partial<TicketDefinition> & { key: string; uid: string; title: string }
  body: BodySections
  events: FixtureEvent[]
}

/** A failed store operation, mapped to an HTTP error by the router. */
export type StoreFailure = { ok: false; status: number; code: string; message: string; hint?: string }
export type GrantResult = { ok: true; grant: GrantInfo } | StoreFailure
/** Is `session` the root session itself or one of its subagents (`s_77c2.1` belongs to `s_77c2`; `s_77c21` does not)? */
export const sessionBelongsTo = (session: string, root: string) => session === root || session.startsWith(root + '.')
/** A persisted event the folds can read: an object with a string type, a string `at` and an actor {kind, id}. Anything else is dropped on load. */
const wellFormed = (e: unknown): boolean => {
  if (typeof e !== 'object' || e === null) return false
  const { type, at, actor } = e as { type?: unknown; at?: unknown; actor?: unknown }
  if (typeof type !== 'string' || typeof at !== 'string' || typeof actor !== 'object' || actor === null) return false
  const { kind, id } = actor as { kind?: unknown; id?: unknown }
  return typeof kind === 'string' && typeof id === 'string'
}
const refuse = (status: number, code: string, message: string, hint?: string): StoreFailure => ({ ok: false, status, code, message, hint })

export type Dataset = 'normal' | 'busy'

export interface StoreOptions {
  /** Persist appended events to localStorage. Default true; tests pass false. */
  persist?: boolean
  /** The demo dataset to start with. Default: the persisted one, else 'normal'. */
  dataset?: Dataset
  /** Play the Busy day's background script (one event every 4 to 8 seconds). The app turns it on; tests leave it off. */
  live?: boolean
}

function fillDefinition(d: FixtureTicket['definition']): TicketDefinition {
  return {
    schema: 'orch.ticket/2',
    type: 'feature',
    priority: 'medium',
    size: null,
    labels: [],
    parent: null,
    blocked_by: [],
    due: null,
    visibility: 'workspace',
    acceptance: [],
    tasks: [],
    questions: [],
    addons: {},
    ...d,
    links: { repos: [], branches: {}, prs: [], external: [], ...(d.links ?? {}) },
  } as TicketDefinition
}

export class MockStore {
  /** Workspaces with the workspace log folded in (derived; rebuilt by `refoldWorkspaces`). */
  workspaces: Workspace[] = []
  private seedWorkspaces: Workspace[] = []
  private wsEvents = new Map<string, WorkspaceEvent[]>()
  private created: PersistedV2['created'] = {}
  private addonStates: PersistedV2['addonState'] = {}
  /** Every known addon package (seeded and catalog). Global: per-workspace state lives in `workspaces[].addons`. */
  addons: AddonPackage[] = []
  private defs = new Map<string, TicketDefinition>()
  private bodies = new Map<string, BodySections>()
  private wsOfKey = new Map<string, string>() // key -> workspace id
  private events = new Map<string, OrchEvent[]>()
  private seeded = new Map<string, number>() // key -> number of seeded events
  private agentRegistry: (Omit<AgentSession, 'grant' | 'claims' | 'leases'> & { grant: string })[] = []
  private startedAt = Date.now()
  private clockBase = Date.parse(MOCK_EPOCH)
  viewer = meFixture.person
  private persist: boolean
  private cursors = new Map<string, number>()
  readonly sim = new Simulator(this)

  /** Which demo dataset is loaded: today's seed, or the seed plus a generated busy day (src/mocks/busy). */
  dataset: Dataset = 'normal'
  private busy: BusyData | null = null

  constructor(opts: StoreOptions = {}) {
    this.persist = opts.persist ?? true
    const saved = this.persist ? loadPersisted() : null
    this.dataset = opts.dataset ?? saved?.dataset ?? 'normal'
    this.live = opts.live ?? false
    this.seed()
    if (this.persist) this.load(saved)
    this.startLiveIfBusy()
  }

  private live = false
  private startLiveIfBusy() {
    if (this.live && this.dataset === 'busy') startLive(this)
  }

  // ------------------------------------------------------------ seeding & persistence

  private seed() {
    this.defs.clear()
    this.bodies.clear()
    this.events.clear()
    this.seeded.clear()
    this.wsOfKey.clear()
    this.addons = structuredClone([...addonsFixture, ...catalogFixture]) as unknown as AddonPackage[]
    this.busy = this.dataset === 'busy' ? generateBusy() : null
    this.agentRegistry = [...meFixture.agents, ...(this.busy?.agents ?? [])] as unknown as typeof this.agentRegistry
    this.seedWorkspaces = (workspacesFixture as unknown as Workspace[]).map((w) => ({ ...structuredClone(w), counts: {}, needs_you: 0 }))
    this.wsEvents.clear()
    for (const w of this.seedWorkspaces) this.wsEvents.set(w.id, [])
    if (this.busy) this.installBusyAddons()
    this.created = {}
    this.addonStates = {}
    this.refoldWorkspaces()
    const byPrefix: Record<string, FixtureTicket[]> = {
      DEMO: demoFixture as unknown as FixtureTicket[],
      ...(otherFixture as unknown as Record<string, FixtureTicket[]>),
    }
    for (const ws of this.workspaces) {
      const extra = (this.busy?.tickets as Record<string, unknown[]> | undefined)?.[ws.prefix] ?? []
      for (const t of [...(byPrefix[ws.prefix] ?? []), ...(extra as FixtureTicket[])]) {
        const def = fillDefinition(structuredClone(t.definition))
        this.defs.set(def.key, def)
        this.bodies.set(def.key, t.body)
        this.wsOfKey.set(def.key, ws.id)
        const evs = t.events
          .map((e) => ({ e, i: 0 }))
          .sort((a, b) => a.e.at.localeCompare(b.e.at))
          .map(({ e }, idx) => this.expand(def, e, idx + 1))
        this.events.set(def.key, evs)
        this.seeded.set(def.key, evs.length)
      }
    }
  }

  /** Busy day: the catalog addons that fill the pages are installed, granted and on in DEMO. */
  private installBusyAddons() {
    const demo = this.seedWorkspaces.find((w) => w.prefix === 'DEMO')
    if (!demo) return
    for (const name of ['activity', 'records', 'worktrees', 'quick', 'models', 'schedules', 'factory']) {
      const pkg = this.addons.find((a) => a.name === name)
      if (!pkg || demo.addons[name]) continue
      demo.addons[name] = {
        enabled: true,
        status: 'active',
        installed: true,
        granted: { version: pkg.version, capabilities: pkg.capabilities, package_sha256: pkg.package_sha256, at: '2026-10-08T09:00:00Z', by: 'p_sev' },
        version: pkg.version,
        package_sha256: pkg.package_sha256,
        capabilities: pkg.capabilities,
      }
    }
  }

  private expand(def: TicketDefinition, e: FixtureEvent, seq: number): OrchEvent {
    const { actor, ...rest } = e
    return {
      v: 2,
      id: def.uid.slice(0, 14) + String(seq).padStart(12, '0'),
      seq,
      ...rest,
      actor: parseActor(actor),
    } as OrchEvent
  }

  private refoldWorkspaces() {
    this.workspaces = this.seedWorkspaces.map((w) => foldWorkspace(w, this.wsEvents.get(w.id) ?? []))
  }

  private load(p: PersistedV2 | null) {
    if (!p) return
    let latest = 0
    for (const [key, c] of Object.entries(p.created)) {
      if (this.defs.has(key) || !this.workspaces.some((w) => w.id === c.ws)) continue
      this.register(c.ws, c.def, c.body)
    }
    for (const [key, evs] of Object.entries(p.ticketEvents)) {
      const list = this.events.get(key)
      if (!list || !Array.isArray(evs)) continue
      for (const e of evs.filter(wellFormed)) {
        list.push(e)
        latest = Math.max(latest, Date.parse(e.at))
      }
    }
    for (const [id, evs] of Object.entries(p.wsEvents)) {
      const list = this.wsEvents.get(id)
      if (!list || !Array.isArray(evs)) continue
      for (const e of evs.filter(wellFormed)) {
        list.push(e)
        latest = Math.max(latest, Date.parse(e.at))
      }
    }
    this.created = p.created
    this.addonStates = p.addonState
    this.refoldWorkspaces()
    if (p.viewer) this.viewer = p.viewer
    if (latest) this.clockBase = Math.max(this.clockBase, latest + 1000)
  }

  private save() {
    if (!this.persist) return
    const ticketEvents: PersistedV2['ticketEvents'] = {}
    for (const [key, list] of this.events) {
      const extra = list.slice(this.seeded.get(key) ?? 0)
      if (extra.length) ticketEvents[key] = extra
    }
    const wsEvents: PersistedV2['wsEvents'] = {}
    for (const [id, list] of this.wsEvents) if (list.length) wsEvents[id] = list
    savePersisted({ v: 2, ticketEvents, created: this.created, wsEvents, addonState: this.addonStates, viewer: this.viewer, dataset: this.dataset })
  }

  /** Back to the seed. `dataset` switches the demo to that dataset; without it the current one is reloaded. */
  reset(dataset: Dataset = this.dataset) {
    this.sim.stopAll()
    this.dataset = dataset
    this.seed()
    this.viewer = meFixture.person
    this.startedAt = Date.now()
    this.clockBase = Date.parse(MOCK_EPOCH)
    clearPersisted()
    if (dataset !== 'normal') this.save() // the mode survives a reload
    this.startLiveIfBusy()
  }

  // ------------------------------------------------------------ clock & people

  now(): string {
    return new Date(this.clockBase + (Date.now() - this.startedAt)).toISOString().replace(/\.\d{3}Z$/, 'Z')
  }

  setViewer(person: string) {
    this.viewer = person
    this.save()
  }

  me(): Me {
    const member = this.workspaces.flatMap((w) => w.members).find((m) => m.person === this.viewer)
    return {
      person: this.viewer,
      name: member?.name ?? this.viewer,
      role: this.roleIn(this.workspaces[0].id, this.viewer) ?? 'viewer',
    }
  }

  roleIn(workspaceId: string, person: string) {
    return roleOf(this.workspaces.find((w) => w.id === workspaceId), person)
  }

  workspaceOf(key: string): Workspace | undefined {
    return this.workspaces.find((w) => w.id === this.wsOfKey.get(key))
  }

  // ------------------------------------------------------------ tickets

  hasTicket(key: string) {
    return this.defs.has(key)
  }

  isVisible(key: string, person = this.viewer): boolean {
    const def = this.defs.get(key)
    if (!def) return false
    return def.visibility === 'workspace' || def.visibility.restricted.includes(person)
  }

  /** Every ticket key of a workspace, whoever may see it (for workspace-wide jobs such as committing records). */
  ticketKeys(workspaceId: string): string[] {
    return [...this.wsOfKey].filter(([, w]) => w === workspaceId).map(([k]) => k)
  }

  eventsOf(key: string): OrchEvent[] {
    return this.events.get(key) ?? []
  }

  ticket(key: string): TicketDocument | undefined {
    const def = this.defs.get(key)
    const ws = this.workspaceOf(key)
    if (!def || !ws) return undefined
    const doc = deriveTicket(def, this.bodies.get(key) ?? {}, this.eventsOf(key), { gates: ws.gates, now: this.now() })
    if (def.type === 'epic') {
      doc.children = [...this.defs.values()].filter((d) => d.parent === key && this.isVisible(d.key)).map((d) => d.key)
    }
    return doc
  }

  summary(doc: TicketDocument): TicketSummary {
    const open = doc.questions_state.filter((q) => q.state === 'open')
    return {
      key: doc.key,
      uid: doc.uid,
      title: doc.title,
      type: doc.type,
      priority: doc.priority,
      size: doc.size,
      status: doc.status,
      labels: doc.labels,
      parent: doc.parent,
      owner: doc.people.owner,
      assignees: doc.people.assignees,
      claim: doc.claim ? { agent: doc.claim.agent, for: doc.claim.for, session: doc.claim.session } : null,
      turn: doc.turn,
      progress: {
        tasks_done: doc.tasks_state.filter((t) => t.state === 'done').length,
        tasks_total: doc.tasks_state.length,
        ac_proven: doc.acceptance_state.filter((a) => a.state === 'proven').length,
        ac_total: doc.acceptance_state.length,
      },
      open_questions: open.length,
      blocking_questions: open.filter((q) => q.blocking).length,
      restricted: doc.restricted,
      addons: doc.addons,
      updated_at: doc.updated_at,
    }
  }

  listTickets(workspaceId: string): TicketDocument[] {
    const out: TicketDocument[] = []
    for (const [key, ws] of this.wsOfKey) {
      if (ws !== workspaceId || !this.isVisible(key)) continue
      const t = this.ticket(key)
      if (t) out.push(t)
    }
    return out.sort((a, b) => b.updated_at.localeCompare(a.updated_at))
  }

  /** Append an event (partial: type, actor string or object, payload). Returns the stored event. */
  append(key: string, input: { type: string; actor?: string | OrchEvent['actor']; [k: string]: unknown }): OrchEvent {
    const def = this.defs.get(key)
    if (!def) throw new Error(`unknown ticket ${key}`)
    const list = this.events.get(key)!
    const prev = list[list.length - 1]
    const seq = (prev?.seq ?? 0) + 1
    const { actor, ...rest } = input
    const event = {
      v: 2,
      id: def.uid.slice(0, 14) + String(seq).padStart(12, '0'),
      seq,
      at: this.now(),
      ...rest,
      actor: typeof actor === 'string' ? parseActor(actor) : (actor ?? parseActor(this.viewer)),
      prev: 'sha256:' + fnvHex(def.uid + (seq - 1), 12) + '…',
    } as OrchEvent
    list.push(event)
    this.bump(this.wsOfKey.get(key))
    this.save()
    return event
  }

  // ------------------------------------------------------------ creating tickets

  /** Registers a ticket with no events yet (the caller appends `ticket.created` first). */
  private register(wsId: string, def: TicketDefinition, body: BodySections) {
    this.defs.set(def.key, def)
    this.bodies.set(def.key, body)
    this.wsOfKey.set(def.key, wsId)
    this.events.set(def.key, [])
    this.seeded.set(def.key, 0)
  }

  /** The next free key of a workspace: max(number) + 1, zero-padded to 4. */
  nextKey(wsId: string): string {
    const prefix = this.workspaces.find((w) => w.id === wsId)!.prefix
    const nums = [...this.defs.keys()].filter((k) => k.startsWith(prefix + '-')).map((k) => Number(k.slice(prefix.length + 1)))
    return `${prefix}-${String(Math.max(0, ...nums) + 1).padStart(4, '0')}`
  }

  /** Creates a backlog ticket. `ticket.created` is its first event, then `people.set` when people were chosen. */
  createTicket(wsId: string, input: Omit<TicketDefinition, 'schema' | 'uid' | 'key' | 'links' | 'blocked_by' | 'tasks' | 'questions' | 'addons'>, body: BodySections, people: { owner: string | null; assignees: string[]; reviewers: string[] }, actor?: string): TicketDocument {
    const key = this.nextKey(wsId)
    const def = fillDefinition({ ...input, key, uid: '01J9ZN' + fnvHex(key + this.now(), 8).toUpperCase().padEnd(20, '0') })
    this.register(wsId, def, body)
    this.created[key] = { ws: wsId, def, body }
    this.append(key, { type: 'ticket.created', status: 'backlog', ...(actor ? { actor } : {}) })
    if (people.owner || people.assignees.length || people.reviewers.length) this.append(key, { type: 'people.set', ...people, watchers: [], ...(actor ? { actor } : {}) })
    return this.ticket(key)!
  }

  /** Mock of POST /api/workspaces/:ws/tickets: role check, validation and creation in one place (the router and addons share it). */
  createFromRequest(wsId: string, b: NewTicketRequest | null, opts: { actor?: string; person?: string } = {}): { ok: true; ticket: TicketDocument } | StoreFailure {
    // `person` is who the ticket is created for (role check); `actor` writes the events (an agent working for that person).
    const person = opts.person ?? this.viewer
    const ws = this.workspaces.find((w) => w.id === wsId)
    if (!ws) return refuse(404, 'not_found', 'No such workspace')
    if (!can(this.roleIn(wsId, person), 'ticket.create')) return refuse(403, 'forbidden', 'Viewers cannot create tickets.', 'Ask an owner or maintainer.')
    if (!b || typeof b !== 'object' || !Object.hasOwn(SECTIONS_BY_TYPE, b.type)) return refuse(400, 'validation', 'Body must be a new ticket.')
    const title = (b.title ?? '').trim()
    if (title.length < 3 || title.length > 120) return refuse(400, 'validation.title', 'The title needs 3 to 120 characters.')
    const needs = SECTIONS_BY_TYPE[b.type]
    const body: BodySections = {}
    for (const [name, text] of Object.entries(b.sections ?? {}) as [SectionName, string | undefined][]) {
      if (needs[name] === undefined || needs[name] === 'absent' || !text?.trim()) continue
      body[name] = text.trim()
    }
    for (const name of requiredAtCreation(b.type)) {
      if (!body[name]) return refuse(400, 'validation.section_missing', `${sectionLabel(b.type, name)} ${name === 'requirements' ? 'are' : 'is'} needed.`, 'Write it before creating the ticket.')
    }
    if (b.size !== null && !['xs', 's', 'm', 'l', 'xl'].includes(b.size as string)) return refuse(400, 'validation.size', `Unknown size ${String(b.size)}.`)
    // A restricted ticket always lists its creator: otherwise the creator would land on a ticket they cannot see.
    let visibility: NewTicketRequest['visibility'] = 'workspace'
    if (b.visibility && b.visibility !== 'workspace') {
      const list = (b.visibility as { restricted?: unknown }).restricted
      if (!Array.isArray(list) || !list.every((p) => typeof p === 'string')) return refuse(400, 'validation.visibility', 'Visibility is "workspace" or {restricted: [person, ...]}.')
      visibility = { restricted: list.includes(this.viewer) ? list : [...list, this.viewer] }
    }
    if (b.parent) {
      const parent = this.hasTicket(b.parent) && this.workspaceOf(b.parent)?.id === wsId ? this.ticket(b.parent) : undefined
      if (!parent || parent.type !== 'epic') return refuse(400, 'validation.parent', `${b.parent} is not an epic in this workspace.`)
    }
    const ticket = this.createTicket(
      wsId,
      {
        title,
        type: b.type,
        priority: b.priority ?? 'medium',
        size: b.size ?? null,
        labels: [...new Set((b.labels ?? []).map((l) => l.trim().toLowerCase()).filter(Boolean))],
        parent: b.parent ?? null,
        due: b.due ?? null,
        visibility,
        acceptance: (b.acceptance ?? []).map((t) => t.trim()).filter(Boolean).map((text, i) => ({ id: `AC${i + 1}`, text })),
      },
      body,
      b.people ?? { owner: null, assignees: [], reviewers: [] },
      opts.actor,
    )
    return { ok: true, ticket }
  }

  // ------------------------------------------------------------ live cursor

  /** Counter that increases on every ticket/workspace append (and addon action) in the workspace. */
  cursor(wsId: string): number {
    return this.cursors.get(wsId) ?? 0
  }

  private bump(wsId: string | undefined) {
    if (wsId) this.cursors.set(wsId, this.cursor(wsId) + 1)
  }

  // ------------------------------------------------------------ workspace log

  wsEventsOf(wsId: string): WorkspaceEvent[] {
    return this.wsEvents.get(wsId) ?? []
  }

  /** Append a workspace event (seq is per workspace; actor defaults to the viewer). */
  appendWs(wsId: string, input: { type: WorkspaceEvent['type']; actor?: string | WorkspaceEvent['actor']; [k: string]: unknown }): WorkspaceEvent {
    const list = this.wsEvents.get(wsId)
    if (!list) throw new Error(`unknown workspace ${wsId}`)
    const seq = (list[list.length - 1]?.seq ?? 0) + 1
    const { actor, ...rest } = input
    const event = {
      v: 2,
      id: wsId.slice(0, 8) + String(seq).padStart(8, '0'),
      seq,
      at: this.now(),
      ...rest,
      actor: typeof actor === 'string' ? parseActor(actor) : (actor ?? ({ kind: 'person', id: this.viewer } as const)),
    } as WorkspaceEvent
    list.push(event)
    this.bump(wsId)
    this.refoldWorkspaces()
    this.save()
    return event
  }

  /** Grants of a workspace: the seed folded with the log. */
  grants(wsId: string): GrantInfo[] {
    const seed = ((grantsFixture as unknown as Record<string, GrantInfo[]>)[wsId] ?? []).map((g) => ({ ...g, sessions: [...g.sessions, ...(this.busy?.grantSessions[g.id] ?? [])] }))
    return foldGrants(seed, this.wsEvents.get(wsId) ?? [])
  }

  /**
   * Issue a grant for `actor`. Human only: an agent actor is refused with `human_only`, whatever its role.
   * Owners and maintainers may issue (`grant.issue`); members and viewers may not.
   */
  issueGrant(wsId: string, req: { hours: number; scope: 'all' }, actor: Actor): GrantResult {
    if (actor.kind !== 'person') return refuse(403, 'human_only', 'Only a person can issue a grant.', 'Run orch grant yourself, or issue it from the dashboard.')
    const role = this.roleIn(wsId, actor.id)
    if (!can(role, 'grant.issue')) return refuse(403, 'forbidden', 'Only owners and maintainers issue grants.', 'Ask an owner or maintainer.')
    if (!Number.isInteger(req.hours) || req.hours < 1 || req.hours > 12) return refuse(400, 'validation', 'A grant lasts 1 to 12 hours.')
    if (req.scope !== 'all') return refuse(400, 'validation', 'Only the scope "all" can be issued here.')
    const id = `gr_01JA${String(this.grants(wsId).length).padStart(2, '0')}`
    const until = new Date(Date.parse(this.now()) + req.hours * 3600_000).toISOString().replace(/\.\d{3}Z$/, 'Z')
    this.appendWs(wsId, { type: 'grant.issued', actor, grant: id, person: actor.id, scope: req.scope, until, hours: req.hours, sessions: [], presence: 'touchid' })
    return { ok: true, grant: this.grants(wsId).find((g) => g.id === id)! }
  }

  /**
   * Revoke a grant: an owner, or a maintainer their own (`canRevokeGrant`), and only a person. Ends the claims and
   * leases of the sessions that use it (`reason: 'grant revoked'`); those sessions then derive as stopped.
   */
  revokeGrant(wsId: string, id: string, actor: Actor): GrantResult {
    if (actor.kind !== 'person') return refuse(403, 'human_only', 'Only a person can revoke a grant.')
    const g = this.grants(wsId).find((x) => x.id === id)
    if (!g) return refuse(404, 'not_found', `No grant ${id}`)
    const role = this.roleIn(wsId, actor.id)
    if (!canRevokeGrant(role, g.person, actor.id))
      return refuse(403, 'forbidden', can(role, 'grant.issue') ? `Only ${g.person} or an owner can revoke ${id}.` : 'Only owners and maintainers revoke grants.')
    if (g.revoked) return refuse(409, 'grant.revoked', `${id} was already revoked.`)
    this.appendWs(wsId, { type: 'grant.revoked', actor, grant: id, presence: 'touchid' })
    const roots = g.sessions
    // Runs started from the dashboard under this grant end now: their scripts stop and they are recorded as stopped.
    for (const r of roots) this.sim.stop(r)
    for (const st of this.startedSessions(wsId)) if (st.grant === id && !st.stopped) this.appendWs(wsId, { type: 'agent.stopped', actor, session: st.session, reason: 'grant revoked' })
    const uses = (session: string) => roots.some((r) => sessionBelongsTo(session, r))
    const reason = 'grant revoked'
    for (const [key, ws] of this.wsOfKey) {
      if (ws !== wsId) continue
      const doc = this.ticket(key)
      if (!doc) continue
      for (const t of doc.tasks_state)
        if (t.lease && uses(t.lease.session)) this.append(key, { type: 'lease.released', actor: `${t.lease.agent}:${t.lease.session}:${g.person}`, task: t.id, reason })
      if (doc.claim && uses(doc.claim.session))
        this.append(key, { type: 'claim.released', actor: `${doc.claim.agent}:${doc.claim.session}:${doc.claim.for}`, reason })
    }
    return { ok: true, grant: this.grants(wsId).find((x) => x.id === id)! }
  }

  // ------------------------------------------------------------ agent sessions started from the dashboard

  /** Sessions started from the dashboard in a workspace (folded from agent.started / agent.stopped). */
  startedSessions(wsId: string): StartedSession[] {
    return foldSessions(this.wsEvents.get(wsId) ?? [])
  }

  /** The person's active `all` grant in a workspace (not revoked, not expired), if any. */
  activeGrant(wsId: string, person: string): GrantInfo | undefined {
    return activeGrantOf(this.grants(wsId), person, Date.parse(this.now()))
  }

  /**
   * One resolver for the preview and the start: the plan of the active addon that holds a `launch` grant (model
   * routing), then the exact command. `commit` is the real start (one-shot choices such as "next start on Strong" are used up).
   */
  resolveLaunch(wsId: string, req: LaunchRequest, commit = false): { plan: LaunchPlan; spec: LaunchSpec; command: string } {
    const w = this.workspaces.find((x) => x.id === wsId)
    let plan: LaunchPlan = {}
    for (const pkg of this.addons) {
      const mod = getAddon(pkg.name)
      if (!mod?.launch || !addonActive(w, pkg.name) || !w?.addons[pkg.name]?.granted?.capabilities.includes('launch')) continue
      const lastTier = this.startedSessions(wsId).filter((x) => x.ticket === req.ticket).at(-1)?.tier
      plan = { ...mod.launch(this.addonState(wsId, pkg.name), req, { store: this, ws: wsId, viewer: this.viewer, commit, lastTier }), by: pkg.name }
      break
    }
    // Core checks the addon's plan itself: model names against the shared allowlist (a bad one blocks the start and
    // never reaches argv), the tier against the known tiers (anything else is dropped, never shown or recorded).
    if (plan.tier !== undefined && !['light', 'standard', 'strong'].includes(plan.tier)) plan = { ...plan, tier: undefined }
    const bad = [plan.model, plan.subagentModel].find((m) => m !== undefined && !isModelName(m))
    if (bad !== undefined) {
      const title = this.addons.find((a) => a.name === plan.by)?.title
      const from = plan.by ? `${title ?? plan.by} (${plan.by})` : 'an addon'
      plan = {
        error: plan.error ?? `The model "${String(bad)}" from ${from} is not a model name (no spaces, no leading "-"). Start is blocked until it is fixed in the ${title ?? 'addon\'s'} settings.`,
        by: plan.by,
      }
    }
    const spec = launchSpec(req, plan)
    return { plan, spec, command: renderCommand(spec) }
  }

  /**
   * What core would start (GET .../agents/launch): validated choice, the ticket as the viewer sees it, labels, the
   * exact command and model line from the one resolver. Nothing in it comes from an addon.
   */
  launchPreview(wsId: string, req: { ticket?: unknown; mode?: unknown; harness?: unknown; where?: unknown }): { ok: true; launch: CoreLaunch } | StoreFailure {
    const w = this.workspaces.find((x) => x.id === wsId)
    if (!w) return refuse(404, 'not_found', 'No such workspace')
    const { ticket, mode, harness, where } = req
    if (!MODES.includes(mode as LaunchMode) || !HARNESSES.includes(harness as LaunchHarness) || !WHERES.includes(where as LaunchWhere)) return refuse(400, 'validation', 'Pick a mode, a harness and where it runs.')
    if (typeof ticket !== 'string' || !this.hasTicket(ticket) || this.wsOfKey.get(ticket) !== wsId || !this.isVisible(ticket)) return refuse(404, 'not_found', `No ticket ${String(ticket)}`)
    const r: LaunchRequest = { ticket, mode: mode as LaunchMode, harness: harness as LaunchHarness, where: where as LaunchWhere }
    const { plan, command } = this.resolveLaunch(wsId, r)
    return {
      ok: true,
      launch: {
        workspace: w.name,
        ticket,
        title: this.ticket(ticket)!.title,
        mode: MODE_LABEL[r.mode],
        harness: HARNESS_LABEL[r.harness],
        where: WHERE_LABEL[r.where],
        command,
        ...(plan.model ? { model: plan.model } : {}),
        ...(plan.tier ? { tier: plan.tier } : {}),
        ...(plan.subagentModel && r.harness === 'claude-code' ? { subagent_model: plan.subagentModel } : {}),
        ...(plan.line ? { line: plan.line, line_by: plan.by } : {}),
        ...(plan.error ? { blocked: plan.error, blocked_by: plan.by } : {}),
      },
    }
  }

  /**
   * Start an agent session on a ticket for `actor` (core; an addon with a `spawn_agent` grant asks for it).
   * A person only: agents never start agents. Member or above, a ticket of this workspace the person can see and
   * nobody claims, and an active grant of their own; the session is registered on that grant and the simulated run plays.
   */
  startSession(wsId: string, req: LaunchRequest & { addon: string }, actor: Actor): { ok: true; session: StartedSession } | StoreFailure {
    if (actor.kind !== 'person') return refuse(403, 'human_only', 'Only a person can start an agent.', 'Agents never start agents.')
    const w = this.workspaces.find((x) => x.id === wsId)
    if (!w) return refuse(404, 'not_found', 'No such workspace')
    if (!can(this.roleIn(wsId, actor.id), 'ticket.act')) return refuse(403, 'forbidden', 'Viewers cannot start agents.', 'Ask an owner or maintainer.')
    if (!addonActive(w, req.addon) || !w.addons[req.addon]?.granted?.capabilities.includes('spawn_agent'))
      return refuse(403, 'capability.missing', `${req.addon} may not start agents in this workspace.`, 'An owner grants spawn_agent in Settings > Addons.')
    if (!MODES.includes(req.mode) || !HARNESSES.includes(req.harness) || !WHERES.includes(req.where)) return refuse(400, 'validation', 'Pick a mode, a harness and where it runs.')
    if (!this.hasTicket(req.ticket) || this.wsOfKey.get(req.ticket) !== wsId || !this.isVisible(req.ticket, actor.id)) return refuse(404, 'not_found', `No ticket ${req.ticket}`)
    const doc = this.ticket(req.ticket)!
    if (doc.status === 'done') return refuse(409, 'ticket.done', `${req.ticket} is done.`)
    const pending = this.startedSessions(wsId).find((x) => x.ticket === req.ticket && !x.stopped && this.sim.running().includes(x.session))
    if (doc.claim || pending)
      return refuse(409, 'claim.held', `${req.ticket} is claimed by ${doc.claim ? `${doc.claim.agent} (${doc.claim.session})` : `${pending!.name} (${pending!.session})`}.`, 'Stop that session first.')
    const grant = this.activeGrant(wsId, actor.id)
    if (!grant) return refuse(409, 'grant.none', 'You have no active grant in this workspace.', 'Sign one in the start dialog, or ask an owner or maintainer to issue one.')
    const blocked = this.resolveLaunch(wsId, req).plan.error
    if (blocked) return refuse(409, 'launch.invalid_model', blocked, 'Fix the model names in the launch addon\'s settings.')
    // The committing resolve is the one that counts: refuse if it blocks too (nothing is recorded or started then).
    const { plan, spec, command } = this.resolveLaunch(wsId, req, true)
    if (plan.error) return refuse(409, 'launch.invalid_model', plan.error, 'Fix the model names in the launch addon\'s settings.')
    const session = 's_' + fnvHex(`${req.ticket}|${this.now()}|${this.wsEventsOf(wsId).length}|${this.cursor(wsId)}`, 8)
    this.appendWs(wsId, {
      type: 'agent.started',
      actor,
      session,
      agent: req.harness,
      for: actor.id,
      grant: grant.id,
      ticket: req.ticket,
      mode: req.mode,
      where: req.where,
      ...(plan.model ? { model: plan.model } : {}),
      ...(plan.tier ? { tier: plan.tier } : {}),
      argv: spec.argv,
      env: spec.env,
      command,
      addon: req.addon,
    })
    const started = this.startedSessions(wsId).find((x) => x.session === session)!
    this.sim.play(session, sessionScript(started, wsId, grant.until))
    return { ok: true, session: started }
  }

  /** Stop a session started from the dashboard: its script ends, its claim and leases are released. The person it works for, or an owner. */
  stopSession(wsId: string, session: string, actor: Actor): { ok: true; session: StartedSession } | StoreFailure {
    if (actor.kind !== 'person') return refuse(403, 'human_only', 'Only a person can stop an agent from here.')
    const role = this.roleIn(wsId, actor.id)
    if (!can(role, 'ticket.act')) return refuse(403, 'forbidden', 'Viewers cannot stop agents.', 'Ask an owner or maintainer.')
    const s = this.startedSessions(wsId).find((x) => x.session === session)
    if (!s || !this.isVisible(s.ticket, actor.id)) return refuse(404, 'not_found', `No session ${session}`)
    if (s.for !== actor.id && !can(role, 'grant.revoke.any')) return refuse(403, 'forbidden', `Only the person it works for or an owner can stop ${session}.`)
    if (s.stopped) return refuse(409, 'session.stopped', `${session} has already stopped.`)
    this.endRun(wsId, session, 'stopped', actor)
    return { ok: true, session: this.startedSessions(wsId).find((x) => x.session === session)! }
  }

  /** End a running session (stop, grant gone, ticket taken): the script stops, its claim and leases are released, `agent.stopped` is recorded once. */
  endRun(wsId: string, session: string, reason: string, actor: Actor = { kind: 'host', id: 'orch' }): void {
    this.sim.stop(session)
    const s = this.startedSessions(wsId).find((x) => x.session === session)
    if (!s || s.stopped) return
    const doc = this.ticket(s.ticket)
    const agent = `${s.agent}:${s.session}:${s.for}`
    for (const t of doc?.tasks_state ?? []) if (t.lease && sessionBelongsTo(t.lease.session, session)) this.append(s.ticket, { type: 'lease.released', actor: agent, task: t.id, reason })
    if (doc?.claim && doc.claim.session === session) this.append(s.ticket, { type: 'claim.released', actor: agent, reason })
    this.appendWs(wsId, { type: 'agent.stopped', actor, session, reason })
  }

  /** Saved views the viewer can see: their own and the shared ones. */
  views(wsId: string): SavedView[] {
    const seed = (viewsFixture as unknown as Record<string, SavedView[]>)[wsId] ?? []
    return foldViews(seed, this.wsEvents.get(wsId) ?? []).filter((v) => v.shared || v.owner === this.viewer)
  }

  // ------------------------------------------------------------ addon actions (mock)

  setAddonData(key: string, addon: string, fn: (data: Record<string, unknown>) => void) {
    const def = this.defs.get(key)
    if (!def) return
    const data = (def.addons[addon] ??= {})
    fn(data)
  }

  /** github/import: a lane issue becomes a new backlog ticket in workspace `wsId` (the github module removes the issue from its state). */
  importGithubIssue(wsId: string, item: { title?: string; subtitle?: string; badge?: string }): AddonActionResult {
    const ws = this.workspaces.find((w) => w.id === wsId)!
    const title = item.title?.trim()
    const m = /^(.*)#(\d+)$/.exec(item.subtitle ?? '')
    if (!title || !m) return { ok: true, message: 'Nothing to import.' }
    const [, repo, number] = m
    const external = `GH-${number}`
    const nums = [...this.defs.keys()].filter((k) => k.startsWith(ws.prefix + '-')).map((k) => Number(k.slice(ws.prefix.length + 1)))
    const key = `${ws.prefix}-${String(Math.max(0, ...nums) + 1).padStart(4, '0')}`
    const type = item.badge === 'bug' ? 'bug' : item.badge === 'feature' ? 'feature' : 'chore'
    const uid = '01J9ZN' + fnvHex(key, 8).toUpperCase().padEnd(20, '0')
    const def = fillDefinition({
      key,
      uid,
      title,
      type,
      links: { repos: [], branches: {}, prs: [], external: [{ label: external, url: `https://github.com/${repo}/issues/${number}` }] },
    } as FixtureTicket['definition'])
    this.defs.set(key, def)
    this.bodies.set(key, { summary: `Imported from GitHub ${item.subtitle}.` })
    this.wsOfKey.set(key, ws.id)
    this.events.set(key, [])
    this.seeded.set(key, 0)
    const actor = { kind: 'addon', id: 'github' } as const
    this.append(key, { type: 'ticket.created', actor, status: 'backlog' })
    this.append(key, { type: 'people.set', owner: this.viewer, assignees: [], reviewers: [], watchers: [] })
    this.append(key, { type: 'github.imported', actor, external })
    return { ok: true, message: `Imported ${external} as ${key}`, changed: true }
  }

  /** Per-workspace state of an addon (lazily seeded, persisted). */
  addonState(ws: string, name: string): Record<string, unknown> {
    const key = `${ws}/${name}`
    if (this.addonStates[key]) return this.addonStates[key]
    const mod = getAddon(name)
    // The busy dataset seeds an addon with its own, bigger state when the module has one.
    const seeded = this.dataset === 'busy' && mod?.seedBusy ? mod.seedBusy(ws, this, makeRng(BUSY_SEED).fork(key)) : (mod?.seed(ws, this) ?? {})
    return (this.addonStates[key] = seeded)
  }

  // ------------------------------------------------------------ addon manager

  /** Addons installed in `wsId`: the package plus that workspace's version, grant and status under `ws`. */
  workspaceAddons(wsId: string): InstalledAddon[] {
    const w = this.workspaces.find((x) => x.id === wsId)
    if (!w) return []
    return this.addons.filter((p) => w.addons[p.name]).map((p) => ({ ...p, ws: w.addons[p.name] }))
  }

  /** Every known package that is not installed in `wsId` (an uninstalled seeded addon comes back here). */
  workspaceCatalog(wsId: string): AddonPackage[] {
    const w = this.workspaces.find((x) => x.id === wsId)
    return this.addons.filter((p) => !w?.addons[p.name])
  }

  /**
   * Owner-only addon lifecycle. A person only: an agent never installs, grants or enables (`human_only`).
   * `grant` and `update` are signed (presence 'touchid'). Enabling is refused until the installed version has a grant.
   */
  addonOp(wsId: string, name: string, req: AddonOpRequest, actor: Actor): { ok: true; addon: InstalledAddon } | StoreFailure {
    if (actor.kind !== 'person') return refuse(403, 'human_only', 'Only a person can change addons.', 'Agents never install, grant or enable addons.')
    const w = this.workspaces.find((x) => x.id === wsId)
    if (!w) return refuse(404, 'not_found', 'No such workspace')
    if (!can(this.roleIn(wsId, actor.id), 'addon.manage')) return refuse(403, 'forbidden', 'Only owners change settings.', 'Ask an owner.')
    const st = w.addons[name]
    const pkg = this.addons.find((a) => a.name === name)
    const done = () => ({ ok: true as const, addon: this.workspaceAddons(wsId).find((a) => a.name === name)! })
    if (req.op === 'install') {
      if (st) return refuse(409, 'addon.installed', `${name} is already installed.`)
      const c = pkg
      if (!c) return refuse(404, 'not_found', `No addon ${name} in the catalog`)
      this.appendWs(wsId, { type: 'addon.installed', actor, name, version: c.version, package_sha256: c.package_sha256, capabilities: c.capabilities })
      return done()
    }
    if (!st || !pkg) return refuse(404, 'not_found', `${name} is not installed in this workspace.`)
    const v: InstalledAddon = { ...pkg, ws: st }
    const update = pendingUpdate(v)
    switch (req.op) {
      case 'grant':
        if (req.op !== 'grant') break
        if (req.version !== st.version) return refuse(409, 'addon.version_mismatch', `The installed version is ${st.version}; a grant for ${String(req.version)} was refused.`, 'Review the installed version and grant again.')
        if (req.package_sha256 !== st.package_sha256 || !Array.isArray(req.capabilities) || !sameSet(req.capabilities, st.capabilities) || !Array.isArray(req.viewer_actions) || !sameSet(req.viewer_actions, viewerActions(manifestFor(pkg, st.version)).map((a) => a.id)))
          return refuse(409, 'addon.changed', `${v.title} changed since you reviewed it; nothing was signed.`, 'Open the grant again and review the current package.')
        this.appendWs(wsId, { type: 'addon.granted', actor, name, version: st.version, package_sha256: st.package_sha256, capabilities: st.capabilities, viewer_actions: req.viewer_actions, presence: 'touchid' })
        break
      case 'enable':
        if (st.status === 'needs_grant') return refuse(409, 'addon.needs_grant', `${v.title} ${st.version} has no grant yet.`, 'Review its capabilities and grant them first.')
        this.appendWs(wsId, { type: 'addon.enabled', actor, name })
        break
      case 'disable':
        this.appendWs(wsId, { type: 'addon.disabled', actor, name })
        this.sim.stopPrefix(`${name}:${wsId}`) // the addon's simulator scripts stop writing
        break
      case 'update':
        if (req.op !== 'update') break
        if (!update) return refuse(409, 'addon.no_update', `${v.title} is up to date.`)
        if (req.version !== update.version || req.package_sha256 !== update.package_sha256 || !Array.isArray(req.capabilities) || !sameSet(req.capabilities, update.capabilities) || !Array.isArray(req.viewer_actions) || !sameSet(req.viewer_actions, viewerActions({ actions: update.actions ?? pkg.actions }).map((a) => a.id)))
          return refuse(409, 'addon.changed', `The update to ${v.title} changed since you reviewed it; nothing was signed.`, 'Open the update again and review it.')
        this.appendWs(wsId, { type: 'addon.updated', actor, name, version: update.version, from: st.version, package_sha256: update.package_sha256, capabilities: update.capabilities, viewer_actions: req.viewer_actions, presence: 'touchid' })
        break
      case 'uninstall': // ticket data under addons.<name> stays; the UI shows it inactive
        this.appendWs(wsId, { type: 'addon.uninstalled', actor, name })
        this.sim.stopPrefix(`${name}:${wsId}`)
        return { ok: true, addon: v }
      default:
        return refuse(400, 'validation', `Unknown op ${(req as { op: string }).op}`)
    }
    return done()
  }

  /** GET .../addons/:name/state: state with the addon's derived view merged over it. Null when unknown or disabled. */
  addonStateView(ws: string, name: string): Record<string, unknown> | null {
    const addon = getAddon(name)
    const w = this.workspaces.find((x) => x.id === ws)
    if (!addon || !addonActive(w, name)) return null
    const state = this.addonState(ws, name)
    const { nav: _perViewer, ...shared } = { ...state, ...(addon.view?.(state, { store: this, ws, viewer: this.viewer }) ?? {}) } // `nav` is the per-viewer map (convention): never sent raw
    return shared
  }

  /** Mock of POST /api/workspaces/:ws/addons/:name/actions/:id. A `ticket` in the body must belong to `ws`. Null for an unknown action. */
  runAddon(ws: string, name: string, id: string, body: Record<string, unknown>): AddonActionResult | StoreFailure | null {
    const addon = getAddon(name)
    const action = addon && Object.hasOwn(addon.actions, id) ? addon.actions[id] : undefined
    if (!addon || !action) return null
    const w = this.workspaces.find((x) => x.id === ws)
    if (!w) return refuse(404, 'not_found', 'No such workspace')
    const ticket = typeof body.ticket === 'string' ? body.ticket : undefined
    if (ticket !== undefined) {
      if (!this.hasTicket(ticket) || !this.isVisible(ticket)) return refuse(404, 'not_found', `No ticket ${ticket}`)
      if (this.wsOfKey.get(ticket) !== ws) return refuse(409, 'ticket.other_workspace', `${ticket} is not in ${w.prefix}.`, 'Run the action in the ticket\'s own workspace.')
    }
    // A disabled addon, or one whose installed version has no grant, runs nothing.
    if (!addonActive(w, name)) return refuse(409, 'addon.inactive', `${name} is not active in this workspace.`, 'Enable it, or grant its capabilities, in Settings > Addons.')
    // Who may run an action is declared once, in the package manifest (`actions[id].minRole`, default member).
    // Non-members run nothing; a viewer runs only the actions the package marks 'viewer' (e.g. navigation).
    const role = this.roleIn(ws, this.viewer)
    const pkg = this.addons.find((a) => a.name === name)
    const installed = w.addons[name]
    const meta = (pkg && installed ? manifestFor(pkg, installed.version).actions : pkg?.actions)?.[id]
    const min = meta?.minRole ?? 'member'
    if (!role) return refuse(403, 'forbidden', 'You are not a member of this workspace.', 'Ask an owner.')
    if (!atLeast(role, min)) {
      if (min === 'member') return refuse(403, 'forbidden', 'Viewers cannot do this.', 'Ask an owner or maintainer.')
      return refuse(403, 'forbidden', `Only ${min === 'owner' ? 'owners' : 'owners and maintainers'} can do this.`, min === 'owner' ? 'Ask an owner.' : 'Ask an owner or maintainer.')
    }
    // Starting an agent goes through core's own dialog first; only core sets `confirmed` (addon nodes cannot, see actionRuntime).
    if (meta?.confirm === 'spawn_agent' && (body.confirmed !== true || typeof body.launch !== 'object' || body.launch === null)) return refuse(409, 'confirm.required', 'Starting an agent needs your confirmation in orch\'s own dialog.', 'Press Start and confirm in the dialog.')
    if (meta?.confirm === 'sign' && body.confirmed !== true) return refuse(409, 'confirm.required', 'This needs your signature in orch\'s own dialog.', 'Press the button and sign in the dialog.')
    // A decision that is no longer open (already decided, or its condition went away), or is about a ticket the caller cannot see, is closed for every addon.
    const decision = typeof body.id === 'string' ? pkg?.decisions?.find((d) => d.id === body.id && d.action === id) : undefined
    if (decision && !openDecisions(addon, this.addonState(ws, name), pkg?.decisions ?? [], { store: this, ws, viewer: this.viewer }).some((d) => d.id === decision.id && (!d.ticket || this.isVisible(d.ticket)))) return { ok: true, message: 'That decision is closed.' }
    const res = action({ store: this, ws, viewer: this.viewer, ticket, body, state: this.addonState(ws, name) })
    // Core's own record of a signed action (the addon cannot write or hide it): who signed which action, with scalar args only, whether or not the addon says it changed anything.
    if (meta?.confirm === 'sign' && res.ok) {
      const args: Record<string, string | number | boolean> = {}
      for (const [k, v] of Object.entries(body).slice(0, 12)) {
        if (k === 'confirmed') continue
        if (typeof v === 'string') args[k.slice(0, 40)] = v.slice(0, 120)
        else if (typeof v === 'number' || typeof v === 'boolean') args[k.slice(0, 40)] = v
      }
      this.appendWs(ws, { type: 'addon.action_signed', name, action: id, args, changed: !!res.changed, presence: 'touchid' })
    }
    this.bump(ws) // addon actions change state without events; let live pages refresh
    this.save()
    return res
  }

  // ------------------------------------------------------------ workspace views

  /** Gate policy: why `person` may not approve `gate` on `t` (null when eligible). */
  canApprove(t: TicketDocument, gate: GateName, person: string): string | null {
    const ws = this.workspaceOf(t.key)!
    const policy = ws.gates[gate]
    const role = this.roleIn(ws.id, person)
    if (!can(role, 'ticket.act')) return 'Viewers cannot approve.'
    if (policy.approvers === 'reviewers' ? !t.people.reviewers.includes(person) : !roleMeets(role, policy.approvers))
      return policy.approvers === 'reviewers'
        ? 'Only a reviewer of this ticket can approve this gate.'
        : policy.approvers === 'maintainer'
          ? 'Only an owner or a maintainer can approve this gate.'
          : `Only the ${policy.approvers} can approve this gate.`
    if (policy.not === 'assignees' && t.people.assignees.includes(person)) return 'Assignees cannot approve their own work.'
    if (t.gates[gate].approvals.some((a) => a.by === person)) return 'You already approved this gate.'
    return null
  }

  /** Is the question addressed to `person`, by person id or by ticket role? */
  private addressedTo(t: TicketDocument, to: string, person: string): boolean {
    if (to === person) return true
    const p = t.people
    switch (to) {
      case 'owner':
        return p.owner === person
      case 'assignee':
      case 'assignees':
        return p.assignees.includes(person)
      case 'reviewer':
      case 'reviewers':
        return p.reviewers.includes(person)
      default:
        return false
    }
  }

  /** Open questions, pending gates and verdicts on the workspace's tickets; `eligible` filters to what that person can act on. */
  private openItems(workspaceId: string, eligible?: string): NeedsYouItem[] {
    const items: NeedsYouItem[] = []
    for (const t of this.listTickets(workspaceId)) {
      if (t.status === 'done') continue
      const can = (gate: GateName) => !eligible || !this.canApprove(t, gate, eligible)
      for (const q of t.questions_state) {
        if (q.state === 'open' && (!eligible || this.addressedTo(t, q.to, eligible)))
          items.push({ kind: 'question', ticket: t.key, title: t.title, text: q.text, since: q.asked_at, ref: q.id, blocking: q.blocking })
      }
      if (t.status === 'testing' && !t.verdict && can('verify'))
        items.push({ kind: 'verdict', ticket: t.key, title: t.title, text: 'Verdict needed: all evidence is attached.', since: t.updated_at, ref: 'verify' })
      const req = t.body.requirements
      if (t.status === 'backlog' && t.gates.requirements.state === 'pending' && req && !/not refined/i.test(req) && can('requirements'))
        items.push({ kind: 'approval', ticket: t.key, title: t.title, text: 'Approve the requirements.', since: t.created_at, ref: 'requirements', hash: t.gates.requirements.hash })
      if (t.status === 'open' && t.gates.plan.state === 'pending' && t.tasks.length > 0 && can('plan'))
        items.push({ kind: 'approval', ticket: t.key, title: t.title, text: 'Approve the plan.', since: t.updated_at, ref: 'plan', hash: t.gates.plan.hash })
    }
    return items.sort((a, b) => b.since.localeCompare(a.since))
  }

  /** What needs `person`: viewers (and non-members) get an empty list. */
  needsYou(workspaceId: string, person = this.viewer): NeedsYouItem[] {
    if (!can(this.roleIn(workspaceId, person), 'ticket.act')) return []
    return this.openItems(workspaceId, person)
  }

  /** Everything open in the workspace, for the read-only view (viewers only). */
  readOnlyOpen(workspaceId: string, person = this.viewer): NeedsYouItem[] {
    return can(this.roleIn(workspaceId, person), 'ticket.act') ? [] : this.openItems(workspaceId)
  }

  /** May the current viewer decide addon decisions in `wsId`? */
  canDecide(wsId: string): boolean {
    return can(this.roleIn(wsId, this.viewer), 'addon.decide')
  }

  /** Open decisions of the addons that are active in `wsId`; none for a viewer. */
  addonDecisions(wsId: string): AddonDecision[] {
    const w = this.workspaces.find((x) => x.id === wsId)
    if (!w || !this.canDecide(wsId)) return []
    // A decision about a ticket is shown only to people who can see that ticket.
    return this.addons
      .filter((a) => addonActive(w, a.name))
      .flatMap((a) => openDecisions(getAddon(a.name), this.addonState(wsId, a.name), a.decisions ?? [], { store: this, ws: wsId, viewer: this.viewer }))
      .filter((d) => !d.ticket || (this.wsOfKey.get(d.ticket) === wsId && this.isVisible(d.ticket)))
  }

  today(workspaceId: string): TodayDocument {
    const tickets = this.listTickets(workspaceId)
    const counts: Partial<Record<Status, number>> = {}
    for (const t of tickets) counts[t.status] = (counts[t.status] ?? 0) + 1
    const recent: TodayDocument['recent'] = []
    for (const t of tickets) {
      for (const e of this.eventsOf(t.key)) {
        if (e.type === 'people.set' || e.type === 'ticket.created') continue
        recent.push({ seq: e.seq, at: e.at, type: e.type, actor: e.actor, ticket: t.key, title: t.title, summary: describeEvent(e) })
      }
    }
    recent.sort((a, b) => b.at.localeCompare(a.at))
    return {
      now: this.now(),
      workspace: workspaceId,
      needs_you: this.needsYou(workspaceId),
      read_only_open: this.readOnlyOpen(workspaceId),
      working: tickets.filter((t) => t.claim).map((t) => this.summary(t)),
      recent: recent.slice(0, 15),
      counts,
    }
  }

  workspaceList(): Workspace[] {
    return this.workspaces.map((w) => {
      const counts: Partial<Record<Status, number>> = {}
      for (const t of this.listTickets(w.id)) counts[t.status] = (counts[t.status] ?? 0) + 1
      return { ...w, counts, needs_you: this.needsYou(w.id).length }
    })
  }

  /** Sessions (and subagents) whose grant belongs to the workspace. A revoked or expired grant stops them. */
  agents(workspaceId: string): AgentSession[] {
    const tickets = this.listTickets(workspaceId)
    const grants = this.grants(workspaceId)
    const now = this.now()
    const seeded: AgentSession[] = this.agentRegistry
      .filter((a) => grants.some((g) => g.id === a.grant))
      .map((a) => {
        const g = grants.find((x) => x.id === a.grant)!
        const claims = tickets
          .filter((t) => t.claim?.agent === a.id && t.claim.for === a.for && t.claim.session === a.session)
          .map((t) => ({ ticket: t.key, since: t.claim!.since, expires: t.claim!.expires }))
        const leases = tickets.flatMap((t) =>
          t.tasks_state
            .filter((x) => x.lease && x.lease.agent === a.id && sessionBelongsTo(x.lease.session, a.session))
            .map((x) => ({ ticket: t.key, task: x.id, session: x.lease!.session })),
        )
        const stopped = !!g.revoked || g.until <= now || a.state === 'stopped'
        const { waiting_on, ...rest } = a
        return { ...rest, grant: { id: g.id, until: g.until }, claims, leases, state: stopped ? 'stopped' : a.state, ...(!stopped && waiting_on ? { waiting_on } : {}) } satisfies AgentSession
      })
    return [...seeded, ...this.startedSessions(workspaceId).map((s) => this.startedAgent(s, tickets, grants, now))]
  }

  /** A session started from the dashboard, as the Agents page and Today see it. Ticket details only for tickets the viewer sees (`tickets`). */
  private startedAgent(s: StartedSession, tickets: TicketDocument[], grants: GrantInfo[], now: string): AgentSession {
    const g = grants.find((x) => x.id === s.grant)
    const doc = tickets.find((t) => t.key === s.ticket)
    const claims = doc?.claim?.session === s.session ? [{ ticket: doc.key, since: doc.claim.since, expires: doc.claim.expires }] : []
    const leases = (doc?.tasks_state ?? []).filter((t) => t.lease && sessionBelongsTo(t.lease.session, s.session)).map((t) => ({ ticket: doc!.key, task: t.id, session: t.lease!.session }))
    const stopped = !!s.stopped || !g || !!g.revoked || g.until <= now
    const mine = doc ? this.eventsOf(doc.key).filter((e) => e.actor.kind === 'agent' && e.actor.session === s.session) : []
    const asked = mine.filter((e) => e.type === 'question.asked').map((e) => doc!.questions_state.find((q) => q.id === e.question)).find((q) => q?.state === 'open' && q.blocking)
    const waiting = !stopped && !!asked
    return {
      id: s.agent,
      name: s.name,
      for: s.for,
      session: s.session,
      grant: g ? { id: g.id, until: g.until } : null,
      claims,
      leases,
      last_seen: s.stopped?.at ?? mine.at(-1)?.at ?? s.started_at,
      parent: null,
      harness: s.agent,
      ...(s.model ? { model: s.model } : {}),
      state: stopped ? 'stopped' : waiting ? 'waiting' : 'working',
      ...(waiting ? { waiting_on: { kind: 'question' as const, ticket: doc!.key, ref: asked!.id } } : {}),
    }
  }

  /** Agent-attributed ticket events of the workspace, newest first (50). The third same refusal by a session is the stop. */
  agentActivity(workspaceId: string): AgentActivityItem[] {
    const items: AgentActivityItem[] = []
    for (const [key, ws] of this.wsOfKey) {
      if (ws !== workspaceId || !this.isVisible(key)) continue
      for (const e of this.eventsOf(key)) {
        if (e.actor.kind !== 'agent') continue
        const refused = e.type === 'agent.refused'
        items.push({
          at: e.at,
          ticket: key,
          session: e.actor.session,
          agent: e.actor.id,
          for: e.actor.for,
          type: refused ? 'refused' : e.type,
          summary: describeEvent(e),
          ...(refused ? { refusal: { code: String(e.code), message: String(e.message ?? ''), retryable: e.retryable === true, stop: false } } : {}),
        })
      }
    }
    const seen = new Map<string, number>()
    for (const it of [...items].sort((a, b) => a.at.localeCompare(b.at))) {
      if (!it.refusal) continue
      const k = `${it.session}|${it.refusal.code}`
      const n = (seen.get(k) ?? 0) + 1
      seen.set(k, n)
      it.refusal.stop = n === 3
    }
    return items.sort((a, b) => b.at.localeCompare(a.at)).slice(0, 50)
  }
}

export function createMockStore(opts?: StoreOptions): MockStore {
  return new MockStore(opts)
}
