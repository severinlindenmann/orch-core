// In-memory mock store seeded from fixtures. Mutations append events; state is re-derived from events.
// Appended events persist to localStorage (in try/catch; the viewer sandbox may block it).
import type {
  AddonActionResult,
  AddonManifest,
  AgentInfo,
  BodySections,
  Me,
  NeedsYouItem,
  OrchEvent,
  Status,
  TicketDefinition,
  TicketDocument,
  TicketSummary,
  TodayDocument,
  Workspace,
} from '@/api/types'
import { deriveTicket, describeEvent, fnvHex, parseActor } from './derive'
import addonsFixture from './fixtures/addons.json'
import demoFixture from './fixtures/demo.json'
import meFixture from './fixtures/me.json'
import otherFixture from './fixtures/other-workspaces.json'
import workspacesFixture from './fixtures/workspaces.json'

const STORAGE_KEY = 'orch.dashboard.mock.v1'
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

interface Persisted {
  events: Record<string, OrchEvent[]>
  viewer?: string
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
  workspaces: Workspace[] = []
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
    this.workspaces = (workspacesFixture as unknown as Workspace[]).map((w) => ({ ...w, counts: {}, needs_you: 0 }))
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

  private load() {
    try {
      const raw = globalThis.localStorage?.getItem(STORAGE_KEY)
      if (!raw) return
      const p = JSON.parse(raw) as Persisted
      let latest = 0
      for (const [key, evs] of Object.entries(p.events ?? {})) {
        const list = this.events.get(key)
        if (!list) continue
        for (const e of evs) {
          list.push(e)
          latest = Math.max(latest, Date.parse(e.at))
        }
      }
      if (p.viewer) this.viewer = p.viewer
      if (latest) this.clockBase = Math.max(this.clockBase, latest + 1000)
    } catch {
      /* storage unavailable or corrupt: start from the seed */
    }
  }

  private save() {
    if (!this.persist) return
    try {
      const events: Record<string, OrchEvent[]> = {}
      for (const [key, list] of this.events) {
        const extra = list.slice(this.seeded.get(key) ?? 0)
        if (extra.length) events[key] = extra
      }
      globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify({ events, viewer: this.viewer } satisfies Persisted))
    } catch {
      /* ignore */
    }
  }

  reset() {
    this.seed()
    this.viewer = meFixture.person
    this.startedAt = Date.now()
    this.clockBase = Date.parse(MOCK_EPOCH)
    try {
      globalThis.localStorage?.removeItem(STORAGE_KEY)
    } catch {
      /* ignore */
    }
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

  // ------------------------------------------------------------ addon actions (mock)

  private setAddonData(key: string, addon: string, fn: (data: Record<string, unknown>) => void) {
    const def = this.defs.get(key)
    if (!def) return
    const data = (def.addons[addon] ??= {})
    fn(data)
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
        return { ok: true, message: `Shared ${ticket} as a secret link for 7 days.`, changed: true }
      }
      case 'publish/decide': {
        const option = String(body.option ?? '')
        const decisions = this.addons.find((a) => a.name === 'publish')?.decisions
        if (decisions) {
          const i = decisions.findIndex((d) => d.id === body.id)
          if (i >= 0) decisions.splice(i, 1)
        }
        return { ok: true, message: option === 'yes' ? 'Published as a secret link for 7 days.' : 'Not published.', changed: true }
      }
      case 'estimate/set': {
        const points = Number((body.formData as { points?: unknown } | undefined)?.points)
        if (!ticket || !Number.isFinite(points)) return { ok: true, message: 'Nothing to save.' }
        this.setAddonData(ticket, 'estimate', (d) => (d.points = points))
        return { ok: true, message: `${ticket} estimated at ${points} points.`, changed: true }
      }
      case 'estimate/save_settings':
      case 'publish/save_settings':
      case 'usage/save_settings':
      case 'terminals/save_settings':
        return { ok: true, message: 'Settings saved (mock).' }
      case 'terminals/open':
        return { ok: true, message: 'Terminals arrive in a later iteration.' }
      case 'github/refresh':
        return { ok: true, message: 'Checked GitHub: 2 pull requests updated.' }
      case 'wiki/open':
        return { ok: true, message: 'Wiki editor arrives in a later iteration.' }
      default:
        return null
    }
  }

  // ------------------------------------------------------------ workspace views

  needsYou(workspaceId: string, person = this.viewer): NeedsYouItem[] {
    const items: NeedsYouItem[] = []
    const role = this.roleIn(workspaceId, person)
    for (const t of this.listTickets(workspaceId)) {
      if (t.status === 'done') continue
      for (const q of t.questions_state) {
        if (q.state === 'open' && q.to === person)
          items.push({ kind: 'question', ticket: t.key, title: t.title, text: q.text, since: q.asked_at, ref: q.id, blocking: q.blocking })
      }
      if (t.status === 'testing' && !t.verdict && t.people.reviewers.includes(person))
        items.push({ kind: 'verdict', ticket: t.key, title: t.title, text: 'Verdict needed: all evidence is attached.', since: t.updated_at, ref: 'verify' })
      if (role === 'owner') {
        const req = t.body.requirements
        if (t.status === 'backlog' && t.gates.requirements.state === 'pending' && req && !/not refined/i.test(req))
          items.push({ kind: 'approval', ticket: t.key, title: t.title, text: 'Approve the requirements.', since: t.created_at, ref: 'requirements' })
        if (t.status === 'open' && t.gates.plan.state === 'pending' && t.tasks.length > 0)
          items.push({ kind: 'approval', ticket: t.key, title: t.title, text: 'Approve the plan.', since: t.updated_at, ref: 'plan' })
      }
    }
    return items.sort((a, b) => b.since.localeCompare(a.since))
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
