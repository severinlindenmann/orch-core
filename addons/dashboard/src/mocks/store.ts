// In-memory mock store seeded from fixtures. Mutations append events; state is re-derived from events.
// Appended events persist to localStorage (in try/catch; the viewer sandbox may block it).
import type {
  AddonActionResult,
  AddonManifest,
  AgentInfo,
  BodySections,
  GateName,
  Me,
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
import { deriveTicket, describeEvent, fnvHex, parseActor } from './derive'
import addonsFixture from './fixtures/addons.json'
import demoFixture from './fixtures/demo.json'
import meFixture from './fixtures/me.json'
import otherFixture from './fixtures/other-workspaces.json'
import workspacesFixture from './fixtures/workspaces.json'
import { clearPersisted, loadPersisted, savePersisted, type PersistedV2 } from './persist'
import { foldGrants, foldWorkspace } from './workspace-log'

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

export interface StoreOptions {
  /** Persist appended events to localStorage. Default true; tests pass false. */
  persist?: boolean
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
  private addonState: PersistedV2['addonState'] = {}
  addons: AddonManifest[] = []
  private defs = new Map<string, TicketDefinition>()
  private bodies = new Map<string, BodySections>()
  private wsOfKey = new Map<string, string>() // key -> workspace id
  private events = new Map<string, OrchEvent[]>()
  private seeded = new Map<string, number>() // key -> number of seeded events
  private agentRegistry = meFixture.agents
  private startedAt = Date.now()
  private clockBase = Date.parse(MOCK_EPOCH)
  viewer = meFixture.person
  private persist: boolean

  constructor(opts: StoreOptions = {}) {
    this.persist = opts.persist ?? true
    this.seed()
    if (this.persist) this.load()
  }

  // ------------------------------------------------------------ seeding & persistence

  private seed() {
    this.defs.clear()
    this.bodies.clear()
    this.events.clear()
    this.seeded.clear()
    this.wsOfKey.clear()
    this.addons = structuredClone(addonsFixture) as unknown as AddonManifest[]
    this.seedWorkspaces = (workspacesFixture as unknown as Workspace[]).map((w) => ({ ...structuredClone(w), counts: {}, needs_you: 0 }))
    this.wsEvents.clear()
    for (const w of this.seedWorkspaces) this.wsEvents.set(w.id, [])
    this.created = {}
    this.addonState = {}
    this.refoldWorkspaces()
    const byPrefix: Record<string, FixtureTicket[]> = {
      DEMO: demoFixture as unknown as FixtureTicket[],
      ...(otherFixture as unknown as Record<string, FixtureTicket[]>),
    }
    for (const ws of this.workspaces) {
      for (const t of byPrefix[ws.prefix] ?? []) {
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

  private load() {
    const p = loadPersisted()
    if (!p) return
    let latest = 0
    for (const [key, evs] of Object.entries(p.ticketEvents)) {
      const list = this.events.get(key)
      if (!list) continue
      for (const e of evs) {
        list.push(e)
        latest = Math.max(latest, Date.parse(e.at))
      }
    }
    for (const [id, evs] of Object.entries(p.wsEvents)) {
      const list = this.wsEvents.get(id)
      if (!list) continue
      for (const e of evs) {
        list.push(e)
        latest = Math.max(latest, Date.parse(e.at))
      }
    }
    this.created = p.created
    this.addonState = p.addonState
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
    savePersisted({ v: 2, ticketEvents, created: this.created, wsEvents, addonState: this.addonState, viewer: this.viewer })
  }

  reset() {
    this.seed()
    this.viewer = meFixture.person
    this.startedAt = Date.now()
    this.clockBase = Date.parse(MOCK_EPOCH)
    clearPersisted()
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
    const isSev = this.viewer === meFixture.person
    return {
      person: this.viewer,
      name: member?.name ?? this.viewer,
      role: this.roleIn(this.workspaces[0].id, this.viewer) ?? 'viewer',
      grant: isSev ? meFixture.grant : null,
    }
  }

  roleIn(workspaceId: string, person: string) {
    return this.workspaces.find((w) => w.id === workspaceId)?.members.find((m) => m.person === person)?.role
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

  eventsOf(key: string): OrchEvent[] {
    return this.events.get(key) ?? []
  }

  ticket(key: string): TicketDocument | undefined {
    const def = this.defs.get(key)
    const ws = this.workspaceOf(key)
    if (!def || !ws) return undefined
    const doc = deriveTicket(def, this.bodies.get(key) ?? {}, this.eventsOf(key), { gates: ws.gates })
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
    this.save()
    return event
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
    this.refoldWorkspaces()
    this.save()
    return event
  }

  /** Grants of a workspace: the seed (the demo grant lives in the first workspace) folded with the log. */
  grants(wsId: string): GrantInfo[] {
    const seed: GrantInfo[] =
      wsId === this.seedWorkspaces[0]?.id
        ? [
            {
              id: meFixture.grant.id,
              person: meFixture.person,
              scope: 'all',
              issued_at: new Date(Date.parse(meFixture.grant.until) - meFixture.grant.hours * 3600_000).toISOString().replace(/\.\d{3}Z$/, 'Z'),
              until: meFixture.grant.until,
              revoked: null,
              sessions: this.agentRegistry.filter((a) => a.grant === meFixture.grant.id).map((a) => a.session),
            },
          ]
        : []
    return foldGrants(seed, this.wsEvents.get(wsId) ?? [])
  }

  // ------------------------------------------------------------ addon actions (mock)

  private setAddonData(key: string, addon: string, fn: (data: Record<string, unknown>) => void) {
    const def = this.defs.get(key)
    if (!def) return
    const data = (def.addons[addon] ??= {})
    fn(data)
  }

  /** github/import: a lane issue becomes a new backlog ticket in the first workspace. */
  private importGithubIssue(item: { title?: string; subtitle?: string; badge?: string }): AddonActionResult {
    const ws = this.workspaces[0]
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
    const lane = this.addons.find((a) => a.name === 'github')?.contributions.find((c) => c.slot === 'board.lane')
    const node = lane?.node as { items?: { subtitle?: string }[] } | undefined
    if (node?.items) node.items = node.items.filter((i) => i.subtitle !== item.subtitle)
    return { ok: true, message: `Imported ${external} as ${key}`, changed: true }
  }

  /** Mock of POST /api/addons/:name/actions/:id. Returns null for an unknown action. */
  addonAction(addon: string, id: string, body: Record<string, unknown>): AddonActionResult | null {
    const ticket = typeof body.ticket === 'string' ? body.ticket : undefined
    switch (`${addon}/${id}`) {
      case 'publish/share': {
        if (!ticket || !this.defs.has(ticket)) return { ok: true, message: 'Pick a ticket first.' }
        this.setAddonData(ticket, 'publish', (d) => {
          const shares = (d.shares as unknown[] | undefined) ?? []
          shares.unshift({ title: `share/${ticket.toLowerCase()}-${shares.length + 1}`, subtitle: 'expires in 7 days · 0 views', badge: 'secret link' })
          d.shares = shares
        })
        this.append(ticket, { type: 'publish.shared', actor: { kind: 'addon', id: 'publish' } })
        return { ok: true, message: `Shared ${ticket} as a secret link for 7 days.`, changed: true }
      }
      case 'publish/decide': {
        const option = String(body.option ?? '')
        const decisions = this.addons.find((a) => a.name === 'publish')?.decisions
        if (decisions) {
          const i = decisions.findIndex((d) => d.id === body.id)
          if (i >= 0) {
            const [done] = decisions.splice(i, 1)
            if (done.ticket && this.defs.has(done.ticket)) this.append(done.ticket, { type: 'publish.decided', actor: { kind: 'addon', id: 'publish' }, option })
          }
        }
        return { ok: true, message: option === 'yes' ? 'Published as a secret link for 7 days.' : 'Not published.', changed: true }
      }
      case 'estimate/set': {
        const points = Number((body.formData as { points?: unknown } | undefined)?.points)
        if (!ticket || !Number.isFinite(points)) return { ok: true, message: 'Nothing to save.' }
        this.setAddonData(ticket, 'estimate', (d) => (d.points = points))
        this.append(ticket, { type: 'estimate.set', actor: { kind: 'addon', id: 'estimate' }, points })
        return { ok: true, message: `${ticket} estimated at ${points} points.`, changed: true }
      }
      case 'estimate/save_settings':
      case 'publish/save_settings':
      case 'usage/save_settings':
      case 'terminals/save_settings':
        return { ok: true, message: 'Settings saved (mock).' }
      case 'terminals/open':
        return { ok: true, message: 'Terminals arrive in a later iteration.' }
      case 'github/import':
        return this.importGithubIssue((body.item ?? {}) as { title?: string; subtitle?: string; badge?: string })
      case 'github/refresh':
        return { ok: true, message: 'Checked GitHub: 2 pull requests updated.' }
      case 'wiki/open':
        return { ok: true, message: 'Wiki editor arrives in a later iteration.' }
      default:
        return null
    }
  }

  // ------------------------------------------------------------ workspace views

  /** Gate policy: why `person` may not approve `gate` on `t` (null when eligible). */
  canApprove(t: TicketDocument, gate: GateName, person: string): string | null {
    const ws = this.workspaceOf(t.key)!
    const policy = ws.gates[gate]
    const role = this.roleIn(ws.id, person)
    if (!role || role === 'viewer') return 'Viewers cannot approve.'
    if (policy.approvers === 'reviewers' ? !t.people.reviewers.includes(person) : role !== policy.approvers)
      return policy.approvers === 'reviewers' ? 'Only a reviewer of this ticket can approve this gate.' : `Only the ${policy.approvers} can approve this gate.`
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
    const role = this.roleIn(workspaceId, person)
    if (!role || role === 'viewer') return []
    return this.openItems(workspaceId, person)
  }

  /** Everything open in the workspace, for the read-only view (viewers only). */
  readOnlyOpen(workspaceId: string, person = this.viewer): NeedsYouItem[] {
    const role = this.roleIn(workspaceId, person)
    return !role || role === 'viewer' ? this.openItems(workspaceId) : []
  }

  /** May the current viewer decide addon decisions? */
  canDecide(): boolean {
    const role = this.roleIn(this.workspaces[0].id, this.viewer)
    return role === 'owner' || role === 'maintainer'
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

  agents(workspaceId: string): AgentInfo[] {
    const tickets = this.listTickets(workspaceId)
    return this.agentRegistry.map((a) => {
      const claims = tickets
        .filter((t) => t.claim?.agent === a.id && t.claim.for === a.for)
        .map((t) => ({ ticket: t.key, since: t.claim!.since, expires: t.claim!.expires }))
      const leases = tickets.flatMap((t) =>
        t.tasks_state
          .filter((x) => x.lease && x.lease.agent === a.id && x.lease.session.startsWith(a.session))
          .map((x) => ({ ticket: t.key, task: x.id, session: x.lease!.session })),
      )
      const grantUntil = a.for === meFixture.person ? meFixture.grant.until : '2026-10-09T17:00:00Z'
      return { ...a, grant: { id: a.grant, until: grantUntil }, claims, leases }
    })
  }
}

export function createMockStore(opts?: StoreOptions): MockStore {
  return new MockStore(opts)
}
