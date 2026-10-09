import { grantCovers } from '@/api/addons'
import { findHarness, harnessCommand, harnessForAgent, isHarness } from '@/api/harnesses'
import type { ShellCtx, TerminalSessionView } from '@/api/terminals'
import { atLeast } from '@/api/permissions'
import { briefs } from '../busy/helpers'
import type { Rng } from '../busy/rng'
import type { MockStore } from '../store'
import { canSeeTicket, invalid, notFound, refusal, registerAddon, type AddonCtx } from './registry'

// terminals: a fake PTY per session (the shell itself is src/app/terminal/fakePty.ts and runs in the browser).
// This module owns the sessions and who may see and type in them.
//  - A person's shell is visible to its owner only. An agent's mirror is visible to every member and viewer, and is
//    always view-only. `interactive` is true only for the owner of a running person's shell who is a member or above.
//  - `pty` is never granted to agents: an agent session here is a mirror of work it does elsewhere, not a PTY.
//  - Which session is open is per viewer (`state.nav[viewer].current`); `open` is minRole 'viewer', new/close/open_ticket
//    are member-level (declared in the package manifest).
//  - A session runs a harness (src/api/harnesses.ts): a shell, Claude Code or Codex. `start` opens the viewer's own
//    session of a harness (with the ticket's context or a fresh window); `resume` starts a new one seeded with an ended
//    session's summary. Both are the person's own PTY: agents never get one, agent mirrors stay view-only.
// The shell context (grant, claim, cursor, ticket) is built in view() from the live store, so `orch status` is live.

interface Session {
  id: string
  kind: 'person' | 'agent'
  owner: string // person id or "agent:<id>"
  for?: string // agent mirrors: the person whose grant the agent works under
  ticket: string | null
  branch: string
  status: 'running' | 'stopped'
  started: string
  /** What an agent mirror shows it typed (default: a few commands). The busy day gives some a long one. */
  transcript?: string[]
  /** Default: a person's session is a shell, an agent mirror runs its agent's harness (an unknown agent: its own id). */
  harness?: string
  /** What an agent session is for ("Review"); default "Agent". */
  purpose?: string
  /** Started with the ticket's context (default: true when it has a ticket). */
  context?: boolean
  /** What an ended session left behind; a resume is seeded with it. */
  summary?: string
  /** The ended session this one resumed. */
  resumedFrom?: string
}

const SESSIONS: Session[] = [
  { id: 'shell1', kind: 'person', owner: 'p_sev', ticket: 'DEMO-0043', branch: 'feat/billing-join', status: 'running', started: '2026-10-09T09:12:00Z' },
  { id: 'agent1', kind: 'agent', owner: 'agent:claude-code', for: 'p_sev', ticket: 'DEMO-0043', branch: 'feat/billing-join', status: 'running', started: '2026-10-09T09:40:00Z' },
  { id: 'old1', kind: 'person', owner: 'p_sev', ticket: null, branch: 'main', status: 'stopped', started: '2026-10-08T15:05:00Z' },
  {
    id: 'codex0', kind: 'agent', owner: 'agent:codex', for: 'p_sev', ticket: 'DEMO-0043', branch: 'feat/billing-join', status: 'stopped', started: '2026-10-08T16:40:00Z',
    purpose: 'Review',
    transcript: ['git log --oneline -5', 'orch show DEMO-0043 --section current_state', 'git status'],
    summary: 'Reviewed the DEMO-0043 plan: tariff seeds before the billing join is the right order. Flagged VAT rounding on mixed tariffs (answered since). No code changes.',
  },
]
/** What the agent typed: its commands name the ticket it works on. */
/** When the session got DATABRICKS_TOKEN, its run includes a tool that prints it in debug output: the host's filter shows `•••• (DATABRICKS_TOKEN)`. */
const agentTranscript = (s: Session, ctx: ShellCtx) => [
  'orch status',
  'orch task next',
  ...(ctx.secrets?.includes('DATABRICKS_TOKEN') ? ['databricks current-user me --debug'] : []),
  ...(s.ticket ? [`orch approve ${s.ticket} plan`] : []),
]
const STOPPED_TRANSCRIPT = ['git status', 'exit']
const withTranscript = (s: Session, ctx: ShellCtx) => ({ ctx, transcript: s.kind === 'agent' ? (s.transcript ?? agentTranscript(s, ctx)) : s.status === 'stopped' ? STOPPED_TRANSCRIPT : [] })

const sessionsOf = (state: Record<string, unknown>) => state.sessions as Session[]
const navOf = (state: Record<string, unknown>) => (state.nav ??= {}) as Record<string, { current?: string }>
const hhmm = (iso: string) => iso.slice(11, 16)

const harnessOfSession = (s: Session): string => s.harness ?? (s.kind === 'agent' ? harnessForAgent(s.owner.slice('agent:'.length)) : 'shell')
const contextOf = (s: Session) => !!s.ticket && (s.context ?? true)

const nameOf = (ctx: Pick<AddonCtx, 'store' | 'ws'>, person: string) => ctx.store.workspaces.find((w) => w.id === ctx.ws)?.members.find((m) => m.person === person)?.name ?? person
/** A person's own shell is theirs alone; an agent mirror is visible to the workspace. */
/** ...and only while its ticket (if any) is one the viewer can see: label, branch and ticket key would name it. */
const visibleTo = (c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>, s: Session) => (s.kind === 'agent' || s.owner === c.viewer) && (!s.ticket || canSeeTicket(c, s.ticket))

function shellCtx(c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>, s: Session): ShellCtx {
  const { store, ws } = c
  // A ticket reaches the shell only if it belongs to THIS workspace and the viewer may see it.
  const doc = s.ticket && canSeeTicket(c, s.ticket) ? store.ticket(s.ticket) : undefined
  const forPerson = s.kind === 'agent' ? (s.for ?? '') : s.owner
  const g = store.grants(ws).find((x) => x.person === forPerson && !x.revoked && x.until > store.now())
  const next = doc?.tasks_state.find((t) => t.state === 'doing' || t.state === 'todo')
  return {
    user: s.kind === 'agent' ? 'claude' : nameOf(c, s.owner).toLowerCase(),
    cwd: '~/energy',
    branch: s.branch,
    owner: s.kind,
    now: store.now(),
    cursor: store.cursor(ws),
    grant: g ? { id: g.id, scope: g.scope, until: g.until } : null,
    claim: doc?.claim ? { agent: doc.claim.agent, session: doc.claim.session, for: doc.claim.for, expires: doc.claim.expires } : null,
    // Agent sessions get the env names their ticket's skills declare (core-computed needs); names only.
    ...(s.kind === 'agent' && doc?.needs?.env.length ? { secrets: doc.needs.env } : {}),
    ticket: doc
      ? {
          key: doc.key,
          title: doc.title,
          status: doc.status,
          current_state: doc.body.current_state ?? '',
          next_task: next ? { id: next.id, text: next.text } : null,
          move: { who: doc.turn.who, why: doc.turn.why },
          gates: (['requirements', 'plan', 'verify'] as const).map((name) => ({ name, state: doc.gates[name].state })),
          questions: { open: doc.questions_state.filter((q) => q.state === 'open').length, total: doc.questions_state.length },
          tasks: { done: doc.tasks_state.filter((t) => t.state === 'done').length, total: doc.tasks_state.length, doing: doc.tasks_state.find((t) => t.state === 'doing')?.id ?? null },
        }
      : null,
  }
}

const SEED_BASE = (ws: string, store: MockStore): Record<string, unknown> => ({
  settings: { shell: '/bin/zsh', font_size: 13 },
  sessions: store.workspaces.find((w) => w.id === ws)?.prefix === 'DEMO' ? structuredClone(SESSIONS) : [],
  nav: {},
  seq: 1,
})

/** A long, plausible run of commands in a ticket's worktree. */
function longTranscript(rng: Rng, ticket: string, lines: number): string[] {
  const pool = ['orch status', 'orch task next', `orch show ${ticket} --section plan`, `orch show ${ticket} --section requirements`, 'git status', 'git log --oneline -5', 'ls', 'pwd']
  return Array.from({ length: lines }, () => rng.pick(pool))
}

/** Busy day: the DEMO sessions plus three more agent mirrors (7 in all: 2 person shells, 5 mirrors of which one ended), two of them with a few hundred lines. */
function seedBusy(ws: string, store: MockStore, rng: Rng) {
  const state = SEED_BASE(ws, store) as { sessions: Session[]; seq: number }
  if (state.sessions.length === 0) return state
  const working = briefs(store, ws).filter((t) => t.claimed && !t.restricted)
  const asked = rng.shuffle(working).slice(0, 3)
  asked.forEach((t, i) => {
    state.sessions.push({ id: `agent${i + 2}`, kind: 'agent', owner: `agent:${i % 2 ? 'codex' : 'claude-code'}`, for: i % 2 ? 'p_mara' : 'p_sev', ticket: t.key, branch: t.branch, status: 'running', started: `2026-10-09T${String(8 + i).padStart(2, '0')}:${10 + i * 7}:00Z`, transcript: longTranscript(rng, t.key, 160 + i * 120) })
  })
  // The seeded mirror also gets a long run.
  const first = state.sessions.find((s) => s.id === 'agent1')
  if (first?.ticket) first.transcript = longTranscript(rng, first.ticket, 220)
  return state
}

registerAddon({
  name: 'terminals',
  // The demo sessions belong to the DEMO workspace; every other workspace starts with none.
  seed: SEED_BASE,
  seedBusy,
  view(state, c) {
    const { viewer } = c
    const role = c.store.roleIn(c.ws, viewer)
    const mine = (s: Session) => s.kind === 'person' && s.owner === viewer
    const shown = sessionsOf(state).filter((s) => visibleTo(c, s))
    const sessions: TerminalSessionView[] = shown.map((s) => ({
      id: s.id,
      label: sessionTitle(s),
      started: s.started,
      kind: s.kind,
      owner: s.owner,
      ticket: s.ticket,
      status: s.status,
      // Typing needs a harness that takes input (an unsupported harness is a read-only transcript).
      interactive: mine(s) && s.status === 'running' && !!role && atLeast(role, 'member') && !!findHarness(harnessOfSession(s))?.capabilities.interactive,
      ...withTranscript(s, shellCtx(c, s)),
      harness: harnessOfSession(s),
      purpose: s.kind === 'agent' ? (s.purpose ?? null) : null,
      command: harnessCommand(harnessOfSession(s), { ticket: s.ticket, context: contextOf(s) }),
      context: contextOf(s),
      summary: s.status === 'stopped' ? (s.summary ?? null) : null,
      resumedFrom: resumedFromView(c, state, s),
    }))
    const myNav = ((state.nav ?? {}) as ReturnType<typeof navOf>)[viewer] // read-only: view() never creates state.nav
    const cur = shown.find((s) => s.id === myNav?.current) ?? shown.find((s) => mine(s) && s.status === 'running') ?? shown[0]
    const sessionByTicket: Record<string, string> = {}
    for (const s of shown) if (mine(s) && s.status === 'running' && s.ticket && !sessionByTicket[s.ticket]) sessionByTicket[s.ticket] = s.id
    return {
      sessions,
      current: { id: cur?.id ?? 'none' },
      sessionByTicket,
      items: shown.map((s) => ({
        title: sessionTitle(s),
        subtitle: `${findHarness(harnessOfSession(s))?.short ?? harnessOfSession(s)} · ${s.branch}${s.ticket ? ` · ${s.ticket}` : ''} · started ${hhmm(s.started)} UTC`,
        badge: s.kind === 'agent' ? 'read only' : s.status,
        actions: [{ action: 'open', label: 'Open', args: { session: s.id } }, ...(mine(s) && s.status === 'running' ? [{ action: 'close', label: 'Close', args: { session: s.id } }] : [])],
      })),
    }
  },
  actions: {
    open(ctx) {
      const { state, body, viewer } = ctx
      // Without a session (the "Open terminal" command) go back to the default: your running shell, else the first visible.
      if (body.session === undefined) {
        delete navOf(state)[viewer]
        return { ok: true, message: 'Your terminal is under Terminals in the sidebar.', changed: true }
      }
      const s = sessionsOf(state).find((x) => x.id === body.session && visibleTo(ctx, x))
      if (!s) return notFound('No such terminal session.')
      navOf(state)[viewer] = { current: s.id }
      return { ok: true, message: `Opened ${sessionTitle(s)}.`, changed: true }
    },
    new({ state, store, viewer }) {
      const s = newShell(state, store, viewer, null)
      navOf(state)[viewer] = { current: s.id }
      return { ok: true, message: `Started ${sessionTitle(s)}.`, changed: true }
    },
    close(ctx) {
      const { state, body, viewer } = ctx
      const s = sessionsOf(state).find((x) => x.id === body.session && visibleTo(ctx, x))
      if (!s) return notFound('No such terminal session.')
      if (s.kind !== 'person' || s.owner !== viewer) return refusal(403, 'forbidden', 'Only the owner can close a terminal; agent sessions are mirrors.')
      s.status = 'stopped'
      return { ok: true, message: `Closed ${sessionTitle(s)}.`, changed: true }
    },
    open_ticket(ctx) {
      const { state, store, viewer, ticket } = ctx
      if (!ticket || !canSeeTicket(ctx, ticket)) return invalid('Pick a ticket first.')
      const s = sessionsOf(state).find((x) => x.kind === 'person' && x.owner === viewer && x.status === 'running' && x.ticket === ticket) ?? newShell(state, store, viewer, ticket)
      navOf(state)[viewer] = { current: s.id }
      return { ok: true, message: `Terminal open in the ${ticket} worktree.`, changed: true }
    },
    /**
     * Start the viewer's own session of a harness: in the ticket's worktree when `ticket` is given, else in the
     * workspace; `context: true` also loads the ticket's current-state summary (harnesses with contextInjection only).
     */
    start(ctx) {
      const { state, store, viewer, ticket, body } = ctx
      if (!ptyGranted(ctx)) return noPty()
      const h = isHarness(body.harness) ? findHarness(body.harness) : undefined
      if (!h || !h.capabilities.interactive) return invalid('Pick Shell, Claude Code or Codex.')
      if (ticket && !canSeeTicket(ctx, ticket)) return notFound('No such ticket.')
      const s = newShell(state, store, viewer, ticket ?? null)
      s.harness = h.id
      s.context = !!ticket && body.context === true && h.capabilities.contextInjection
      navOf(state)[viewer] = { current: s.id }
      return { ok: true, message: `Started ${h.label}${ticket ? ` in the ${ticket} worktree` : ' in the workspace'}${s.context ? ' with the ticket summary' : ''}.`, changed: true }
    },
    /** Continue from an ended session's summary: a new session of the same harness, seeded with that summary. */
    resume(ctx) {
      const { state, store, viewer, body } = ctx
      if (!ptyGranted(ctx)) return noPty()
      const old = sessionsOf(state).find((x) => x.id === body.session && visibleTo(ctx, x))
      if (!old) return notFound('No such terminal session.')
      if (old.status !== 'stopped') return refusal(409, 'terminals.running', 'That session is still running: join it instead.')
      const h = findHarness(harnessOfSession(old))
      if (!h || !h.capabilities.interactive) return refusal(409, 'terminals.unsupported', `This dashboard cannot start ${harnessOfSession(old)} sessions.`)
      const s = newShell(state, store, viewer, old.ticket)
      s.harness = h.id
      s.context = !!old.ticket && h.capabilities.contextInjection
      s.resumedFrom = old.id
      if (old.ticket) s.branch = old.branch
      navOf(state)[viewer] = { current: s.id }
      return { ok: true, message: `Continued from ${sessionTitle(old)} in a new ${h.label} session.`, changed: true }
    },
    save_settings: ({ state, body }) => {
      const d = (body.formData ?? {}) as { shell?: unknown; font_size?: unknown }
      const size = typeof d.font_size === 'number' && Number.isFinite(d.font_size) ? Math.min(20, Math.max(10, Math.round(d.font_size))) : 13
      state.settings = { shell: typeof d.shell === 'string' && d.shell ? d.shell : '/bin/zsh', font_size: size }
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})

/** Core lets terminals run only while its grant covers `pty` here; start/continue check it again (defence in depth). */
function ptyGranted(c: Pick<AddonCtx, 'store' | 'ws'>): boolean {
  const a = c.store.workspaces.find((w) => w.id === c.ws)?.addons.terminals
  return !!a && a.enabled && a.capabilities.includes('pty') && grantCovers(a, a.granted) && a.granted.capabilities.includes('pty')
}
const noPty = () => refusal(409, 'terminals.no_pty', 'Terminals is not granted pty in this workspace.', 'An owner grants it in Settings → Addons.')

/** The session this one resumed, as the viewer may see it (a source the viewer cannot see is left out). */
function resumedFromView(c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>, state: Record<string, unknown>, s: Session): TerminalSessionView['resumedFrom'] {
  const old = s.resumedFrom ? sessionsOf(state).find((x) => x.id === s.resumedFrom && visibleTo(c, x)) : undefined
  return old ? { id: old.id, label: sessionTitle(old), summary: old.summary ?? null } : null
}

/** What the person sees: ticket first ("DEMO-0043 · Claude Code", "DEMO-0043 · Your shell", "Scratch shell"). */
function sessionTitle(s: Pick<Session, 'kind' | 'owner' | 'ticket' | 'harness'>): string {
  if (s.kind === 'person') {
    const h = s.harness && s.harness !== 'shell' ? (findHarness(s.harness)?.label ?? s.harness) : null
    return h ? (s.ticket ? `${s.ticket} · Your ${h}` : `Your ${h}`) : s.ticket ? `${s.ticket} · Your shell` : 'Scratch shell'
  }
  const agent = s.owner.slice('agent:'.length).split('-').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
  return s.ticket ? `${s.ticket} · ${agent}` : agent
}

function newShell(state: Record<string, unknown>, store: AddonCtx['store'], viewer: string, ticket: string | null): Session {
  const n = ((state.seq as number) ?? 1) + 1
  state.seq = n
  const s: Session = {
    id: `sh${n}`,
    kind: 'person',
    owner: viewer,
    ticket,
    branch: ticket ? `feat/${ticket.toLowerCase()}` : 'main',
    status: 'running',
    started: store.now(),
  }
  sessionsOf(state).push(s)
  return s
}
