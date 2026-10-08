import type { ShellCtx, TerminalSessionView } from '@/api/terminals'
import { atLeast } from '@/api/permissions'
import { registerAddon, type AddonCtx } from './registry'

// terminals: a fake PTY per session (the shell itself is src/app/terminal/fakePty.ts and runs in the browser).
// This module owns the sessions and who may see and type in them.
//  - A person's shell is visible to its owner only. An agent's mirror is visible to every member and viewer, and is
//    always view-only. `interactive` is true only for the owner of a running person's shell who is a member or above.
//  - `pty` is never granted to agents: an agent session here is a mirror of work it does elsewhere, not a PTY.
//  - Which session is open is per viewer (`state.nav[viewer].current`); `open` is minRole 'viewer', new/close/open_ticket
//    are member-level (declared in the package manifest).
// The shell context (grant, claim, cursor, ticket) is built in view() from the live store, so `orch status` is live.

interface Session {
  id: string
  label: string
  kind: 'person' | 'agent'
  owner: string // person id or "agent:<id>"
  for?: string // agent mirrors: the person whose grant the agent works under
  ticket: string | null
  branch: string
  status: 'running' | 'stopped'
  started: string
}

const SESSIONS: Session[] = [
  { id: 'shell1', label: 'Severin · DEMO-0043 worktree', kind: 'person', owner: 'p_sev', ticket: 'DEMO-0043', branch: 'feat/billing-join', status: 'running', started: '2026-10-09T09:12:00Z' },
  { id: 'agent1', label: 'agent: claude-code (read only, no typing)', kind: 'agent', owner: 'agent:claude-code', for: 'p_sev', ticket: 'DEMO-0043', branch: 'feat/billing-join', status: 'running', started: '2026-10-09T09:40:00Z' },
  { id: 'old1', label: 'Scratch', kind: 'person', owner: 'p_sev', ticket: null, branch: 'main', status: 'stopped', started: '2026-10-08T15:05:00Z' },
]
const AGENT_TRANSCRIPT = ['orch status', 'orch task next', 'orch approve DEMO-0043 plan']
const STOPPED_TRANSCRIPT = ['git status', 'exit']

const sessionsOf = (state: Record<string, unknown>) => state.sessions as Session[]
const navOf = (state: Record<string, unknown>) => (state.nav ??= {}) as Record<string, { current?: string }>
const hhmm = (iso: string) => iso.slice(11, 16)

const nameOf = (ctx: Pick<AddonCtx, 'store' | 'ws'>, person: string) => ctx.store.workspaces.find((w) => w.id === ctx.ws)?.members.find((m) => m.person === person)?.name ?? person
/** A person's own shell is theirs alone; an agent mirror is visible to the workspace. */
const visibleTo = (s: Session, viewer: string) => s.kind === 'agent' || s.owner === viewer

function shellCtx(c: Pick<AddonCtx, 'store' | 'ws'>, s: Session): ShellCtx {
  const { store, ws } = c
  const doc = s.ticket && store.hasTicket(s.ticket) ? store.ticket(s.ticket) : undefined
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
    ticket: doc ? { key: doc.key, title: doc.title, status: doc.status, current_state: doc.body.current_state ?? '', next_task: next ? { id: next.id, text: next.text } : null } : null,
  }
}

registerAddon({
  name: 'terminals',
  seed: () => ({ settings: { shell: '/bin/zsh', font_size: 13 }, sessions: structuredClone(SESSIONS), nav: {}, seq: 1 }),
  view(state, c) {
    const { viewer } = c
    const role = c.store.roleIn(c.ws, viewer)
    const mine = (s: Session) => s.kind === 'person' && s.owner === viewer
    const shown = sessionsOf(state).filter((s) => visibleTo(s, viewer))
    const sessions: TerminalSessionView[] = shown.map((s) => ({
      id: s.id,
      label: s.label,
      kind: s.kind,
      owner: s.owner,
      ticket: s.ticket,
      status: s.status,
      interactive: mine(s) && s.status === 'running' && !!role && atLeast(role, 'member'),
      ctx: shellCtx(c, s),
      transcript: s.kind === 'agent' ? AGENT_TRANSCRIPT : s.status === 'stopped' ? STOPPED_TRANSCRIPT : [],
    }))
    const cur = shown.find((s) => s.id === navOf(state)[viewer]?.current) ?? shown.find((s) => mine(s) && s.status === 'running') ?? shown[0]
    const sessionByTicket: Record<string, string> = {}
    for (const s of shown) if (mine(s) && s.status === 'running' && s.ticket && !sessionByTicket[s.ticket]) sessionByTicket[s.ticket] = s.id
    return {
      sessions,
      current: { id: cur?.id ?? 'none' },
      sessionByTicket,
      items: shown.map((s) => ({
        title: s.label,
        subtitle: `zsh · ${s.branch}${s.ticket ? ` · ${s.ticket}` : ''} · started ${hhmm(s.started)} UTC`,
        badge: s.kind === 'agent' ? 'read only' : s.status,
        actions: [{ action: 'open', label: 'Open', args: { session: s.id } }, ...(mine(s) && s.status === 'running' ? [{ action: 'close', label: 'Close', args: { session: s.id } }] : [])],
      })),
    }
  },
  actions: {
    open({ state, body, viewer }) {
      // Without a session (the "Open terminal" command) go back to the default: your running shell, else the first visible.
      if (body.session === undefined) {
        delete navOf(state)[viewer]
        return { ok: true, message: 'Opened your terminal.', changed: true }
      }
      const s = sessionsOf(state).find((x) => x.id === body.session && visibleTo(x, viewer))
      if (!s) return { ok: true, message: 'No such terminal session.' }
      navOf(state)[viewer] = { current: s.id }
      return { ok: true, message: `Opened ${s.label}.`, changed: true }
    },
    new({ state, store, ws, viewer }) {
      const s = newShell(state, store, ws, viewer, null)
      navOf(state)[viewer] = { current: s.id }
      return { ok: true, message: `Started ${s.label}.`, changed: true }
    },
    close({ state, body, viewer }) {
      const s = sessionsOf(state).find((x) => x.id === body.session && visibleTo(x, viewer))
      if (!s) return { ok: true, message: 'No such terminal session.' }
      if (s.kind !== 'person' || s.owner !== viewer) return { ok: true, message: 'Only the owner can close a terminal; agent sessions are mirrors.' }
      s.status = 'stopped'
      return { ok: true, message: `Closed ${s.label}.`, changed: true }
    },
    open_ticket({ state, store, ws, viewer, ticket }) {
      if (!ticket || !store.hasTicket(ticket)) return { ok: true, message: 'Pick a ticket first.' }
      const s = sessionsOf(state).find((x) => x.kind === 'person' && x.owner === viewer && x.status === 'running' && x.ticket === ticket) ?? newShell(state, store, ws, viewer, ticket)
      navOf(state)[viewer] = { current: s.id }
      return { ok: true, message: `Terminal open in the ${ticket} worktree.`, changed: true }
    },
    save_settings: ({ state, body }) => {
      const d = (body.formData ?? {}) as { shell?: unknown; font_size?: unknown }
      const size = typeof d.font_size === 'number' && Number.isFinite(d.font_size) ? Math.min(20, Math.max(10, Math.round(d.font_size))) : 13
      state.settings = { shell: typeof d.shell === 'string' && d.shell ? d.shell : '/bin/zsh', font_size: size }
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})

function newShell(state: Record<string, unknown>, store: AddonCtx['store'], ws: string, viewer: string, ticket: string | null): Session {
  const n = ((state.seq as number) ?? 1) + 1
  state.seq = n
  const s: Session = {
    id: `sh${n}`,
    label: ticket ? `${nameOf({ store, ws }, viewer)} · ${ticket} worktree` : `${nameOf({ store, ws }, viewer)} · shell ${n}`,
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
