import { useQueries, useQuery } from '@tanstack/react-query'
import { countAttention } from '@/api/attention'
import { can } from '@/api/permissions'
import type { AgentSession, NeedsYouItem, TicketDocument } from '@/api/types'
import { useConnections } from './pages/settings/connectionUi'
import { useRole } from './useRole'
import { queries } from '@/api/queries'
import { useMe } from '@/app/useMe'
import { shapeApprovals } from './pages/today/queue'
import { lacks } from './pages/ticket/actions'

export interface Attention {
  /** Everything Today lists for the viewer: open questions, gates and verdicts, open addon decisions (both
   *  permission-filtered) and, for the owner, connections that need a new login (R-c, one count everywhere). */
  needsYou: { core: number; addon: number; connections: number; total: number }
  /** "Agent may need you: check its terminal" (a hint, never counted in `needsYou`): how many, and the first run to open. */
  terminalWaits: { count: number; run?: string }
  /** Open items the viewer cannot act on, and the people (ids) who can. */
  waitingOnOthers: { count: number; who: string[] }
  /** Agent sessions: root sessions only (subagents are counted apart). They add up: sessions = working + waitingOnYou + waitingOnOthers + idle + stopped. */
  agents: { sessions: number; working: number; waitingOnYou: number; stopped: number; waitingOnOthers: number; idle: number; subagents: number }
  /** What Today lists from the host's needs_you (R12 P03): requirements and plan as one approval, the requirements held back
   *  while the agent writes the plan. `needsYou` counts exactly these. */
  items: NeedsYouItem[]
  /** Requirements approvals held back because the agent is still writing the plan (listed as a quiet line, never counted). */
  writing: NeedsYouItem[]
  /** `needsYou.total` for the sidebar badge and the switcher pill: updated together with the header. */
  badge: number
  ready: boolean
}

export type SessionGroupId = 'waiting' | 'waiting-others' | 'idle' | 'working' | 'stopped'

/** Which group a root session belongs to for `viewer`: a session is waiting on you when it, or one of its subagents, asks you. */
export function groupOf(root: AgentSession, all: AgentSession[], viewer: string | undefined): SessionGroupId {
  if (root.state === 'stopped') return 'stopped'
  const asks = (s: AgentSession) => s.state === 'waiting' && s.for === viewer
  if (asks(root) || all.some((s) => s.parent === root.session && asks(s))) return 'waiting'
  if (root.state === 'waiting' || all.some(s => s.parent === root.session && s.state === 'waiting')) return 'waiting-others'
  return root.state === 'idle' ? 'idle' : 'working'
}

export function terminalWaitsOf(items: { kind: string; ref?: string }[]): { count: number; run?: string } {
  const waits = items.filter((i) => i.kind === 'terminal_wait')
  return { count: waits.length, run: waits[0]?.ref }
}

/** Is the ticket's agent inside `orch wait` (the host's own signal, run-10 J04), not just quiet at its prompt? */
export function agentInWait(items: { kind: string; ticket?: string; source?: string }[], ticketKey: string): boolean {
  return items.some((i) => i.kind === 'terminal_wait' && i.ticket === ticketKey && i.source === 'wait')
}

/** The run id of the terminal wait of one ticket's agent, if it waits. */
export function terminalWaitFor(items: { kind: string; ticket?: string; ref?: string }[], ticketKey: string): string | undefined {
  return items.find((i) => i.kind === 'terminal_wait' && i.ticket === ticketKey && i.ref)?.ref
}

/** The three nouns of the dashboard, defined once: what needs you, what waits on others, and the agent sessions. */
export function useAttention(ws: string | undefined): Attention {
  const me = useMe(ws)
  const today = useQuery({ ...queries.today(ws!), enabled: !!ws })
  const decisions = useQuery({ ...queries.addonDecisions(ws!), enabled: !!ws })
  const agents = useQuery({ ...queries.agents(ws!), enabled: !!ws })

  const viewer = me.data?.person
  const role = useRole(ws)
  const owner = can(role, 'settings')
  const connections = useConnections(owner ? ws : undefined)
  // The tickets behind requirements approvals decide how Today shows them (one card for both gates, or held back while
  // the agent writes the plan). Shared with Today's own reads; polled while the plan is still missing so a stopped agent
  // brings the card back. A ticket that cannot be read leaves its item as the host sent it.
  const needs = today.data?.needs_you ?? []
  const reqKeys = [...new Set(needs.filter((i) => i.kind === 'approval' && i.ref === 'requirements').map((i) => i.ticket))]
  const ticketQs = useQueries({
    queries: reqKeys.map((k) => ({
      ...queries.ticket(ws!, k),
      refetchInterval: (q: { state: { data?: TicketDocument } }) => (q.state.data && lacks(q.state.data.gates.plan) ? 5000 : false),
    })),
  })
  const tickets: Record<string, TicketDocument | undefined> = {}
  reqKeys.forEach((k, i) => (tickets[k] = ticketQs[i]?.data))
  const ticketsSettled = ticketQs.every((q) => !q.isPending)
  const shaped = shapeApprovals(needs, tickets, { person: viewer ?? '', role, members: [], name: (id) => id ?? 'nobody', ready: true })
  const counts = countAttention(shaped.items, decisions.data ?? [], owner ? connections.data ?? [] : [])
  const ready = !!today.data && !!decisions.data && !!agents.data && !!me.data && (!owner || !!connections.data) && ticketsSettled

  const roots = (agents.data ?? []).filter((s) => !s.parent)
  const groups = roots.map((s) => groupOf(s, agents.data ?? [], viewer))
  const waitingOnYou = groups.filter((g) => g === 'waiting').length
  const stopped = groups.filter((g) => g === 'stopped').length
  return {
    needsYou: counts,
    terminalWaits: terminalWaitsOf(today.data?.needs_you ?? []),
    waitingOnOthers: { count: today.data?.waiting_on_others.count ?? 0, who: today.data?.waiting_on_others.people ?? [] },
    agents: {
      sessions: roots.length,
      working: groups.filter(g => g === 'working').length,
      waitingOnOthers: groups.filter(g => g === 'waiting-others').length,
      idle: groups.filter(g => g === 'idle').length,
      waitingOnYou,
      stopped,
      subagents: (agents.data ?? []).length - roots.length,
    },
    items: shaped.items,
    writing: shaped.writing,
    badge: ready ? counts.total : 0,
    ready,
  }
}
