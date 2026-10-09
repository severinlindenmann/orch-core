import { useQuery } from '@tanstack/react-query'
import { countAttention } from '@/api/attention'
import { api } from '@/api/client'
import { can } from '@/api/permissions'
import type { AgentSession } from '@/api/types'
import { useConnections } from './pages/settings/connectionUi'
import { useRole } from './useRole'

export interface Attention {
  /** Everything Today lists for the viewer: open questions, gates and verdicts, open addon decisions (both
   *  permission-filtered) and, for the owner, connections that need a new login (R-c, one count everywhere). */
  needsYou: { core: number; addon: number; connections: number; total: number }
  /** Open items the viewer cannot act on, and the people (ids) who can. */
  waitingOnOthers: { count: number; who: string[] }
  /** Agent sessions: root sessions only (subagents are counted apart). They add up: sessions = working + waitingOnYou + waitingOnOthers + idle + stopped. */
  agents: { sessions: number; working: number; waitingOnYou: number; stopped: number; waitingOnOthers: number; idle: number; subagents: number }
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

/** The three nouns of the dashboard, defined once: what needs you, what waits on others, and the agent sessions. */
export function useAttention(ws: string | undefined): Attention {
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const decisions = useQuery({ queryKey: ['addon-decisions', ws], queryFn: () => api.getAddonDecisions(ws!), enabled: !!ws })
  const agents = useQuery({ queryKey: ['agents', ws], queryFn: () => api.getAgents(ws!), enabled: !!ws })

  const viewer = me.data?.person
  const owner = can(useRole(ws), 'settings')
  const connections = useConnections(owner ? ws : undefined)
  const counts = countAttention(today.data?.needs_you ?? [], decisions.data ?? [], owner ? connections.data ?? [] : [])
  const ready = !!today.data && !!decisions.data && !!agents.data && !!me.data && (!owner || !!connections.data)

  const roots = (agents.data ?? []).filter((s) => !s.parent)
  const groups = roots.map((s) => groupOf(s, agents.data ?? [], viewer))
  const waitingOnYou = groups.filter((g) => g === 'waiting').length
  const stopped = groups.filter((g) => g === 'stopped').length
  return {
    needsYou: counts,
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
    badge: ready ? counts.total : 0,
    ready,
  }
}
