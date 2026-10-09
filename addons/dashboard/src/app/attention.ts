import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { AgentSession } from '@/api/types'

/** The badge changes at most once per this many milliseconds, so a live day does not flicker. */
export const BADGE_THROTTLE_MS = 5000

export interface Attention {
  /** Everything that needs the viewer: open questions, gates and verdicts, plus open addon decisions (both permission-filtered). */
  needsYou: { core: number; addon: number; total: number }
  /** Open items the viewer cannot act on, and the people (ids) who can. */
  waitingOnOthers: { count: number; who: string[] }
  /** Agent sessions: root sessions only (subagents are counted apart). They add up: sessions = waitingOnYou + working + stopped. */
  agents: { sessions: number; working: number; waitingOnYou: number; stopped: number; subagents: number }
  /** `needsYou.total` for the sidebar badge and the switcher pill: leading edge now, later changes at most every 5 s. */
  badge: number
  ready: boolean
}

export type SessionGroupId = 'waiting' | 'working' | 'stopped'

/** Which group a root session belongs to for `viewer`: a session is waiting on you when it, or one of its subagents, asks you. */
export function groupOf(root: AgentSession, all: AgentSession[], viewer: string | undefined): SessionGroupId {
  if (root.state === 'stopped') return 'stopped'
  const asks = (s: AgentSession) => s.state === 'waiting' && s.for === viewer
  return asks(root) || all.some((s) => s.parent === root.session && asks(s)) ? 'waiting' : 'working'
}

function useThrottled(value: number | undefined, resetKey: string): number | undefined {
  const [shown, setShown] = useState(value)
  const last = useRef({ at: 0, key: resetKey })
  useEffect(() => {
    if (last.current.key !== resetKey) last.current = { at: 0, key: resetKey }
    if (value === undefined || value === shown) return
    const wait = last.current.at === 0 ? 0 : Math.max(0, last.current.at + BADGE_THROTTLE_MS - Date.now())
    const apply = () => {
      last.current.at = Date.now()
      setShown(value)
    }
    if (wait === 0) {
      apply()
      return
    }
    const t = setTimeout(apply, wait)
    return () => clearTimeout(t)
  }, [value, shown, resetKey])
  return shown
}

/** The three nouns of the dashboard, defined once: what needs you, what waits on others, and the agent sessions. */
export function useAttention(ws: string | undefined): Attention {
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const decisions = useQuery({ queryKey: ['addon-decisions', ws], queryFn: () => api.getAddonDecisions(ws!), enabled: !!ws })
  const agents = useQuery({ queryKey: ['agents', ws], queryFn: () => api.getAgents(ws!), enabled: !!ws })

  const viewer = me.data?.person
  const core = today.data?.needs_you.length ?? 0
  const addon = decisions.data?.length ?? 0
  const total = core + addon
  const ready = !!today.data && !!decisions.data && !!agents.data && !!me.data
  const badge = useThrottled(ready ? total : undefined, `${ws}:${viewer}`)

  const roots = (agents.data ?? []).filter((s) => !s.parent)
  const groups = roots.map((s) => groupOf(s, agents.data ?? [], viewer))
  const waitingOnYou = groups.filter((g) => g === 'waiting').length
  const stopped = groups.filter((g) => g === 'stopped').length
  return {
    needsYou: { core, addon, total },
    waitingOnOthers: { count: today.data?.waiting_on_others.count ?? 0, who: today.data?.waiting_on_others.people ?? [] },
    agents: {
      sessions: roots.length,
      working: roots.length - waitingOnYou - stopped,
      waitingOnYou,
      stopped,
      subagents: (agents.data ?? []).length - roots.length,
    },
    badge: badge ?? 0,
    ready,
  }
}
