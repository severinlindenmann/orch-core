import { useEffect, useState } from 'react'
import type { AgentInfo, Workspace } from '@/api/types'

/** Elapsed time between two ISO instants, as "14m", "2h 05m" or "3d". */
export function ago(from: string, now: string) {
  const mins = Math.max(0, Math.round((Date.parse(now) - Date.parse(from)) / 60_000))
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m`
  if (mins < 60 * 24) return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`
  return `${Math.floor(mins / 1440)}d`
}

export interface Directory {
  workspace?: Workspace
  agents: AgentInfo[]
}

/** person id (p_sev) or "agent:claude-code" -> display name. */
export function displayName(dir: Directory, id: string): string {
  if (id.startsWith('agent:')) {
    const a = dir.agents.find((x) => x.id === id.slice(6))
    return a ? `${a.name} for ${displayName(dir, a.for)}` : id.slice(6)
  }
  return dir.workspace?.members.find((m) => m.person === id)?.name ?? id
}

/** Today's two layouts: a side column from 1280 px, one column below. */
export const WIDE_QUERY = '(min-width: 1280px)'

export function useMediaQuery(query: string): boolean {
  const get = () => (typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(query).matches : false)
  const [matches, setMatches] = useState(get)
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const m = window.matchMedia(query)
    const on = () => setMatches(m.matches)
    on()
    m.addEventListener?.('change', on)
    return () => m.removeEventListener?.('change', on)
  }, [query])
  return matches
}

/** A value kept for this browser session (try/catch: storage can be blocked); falls back to memory. */
export function useSessionState<T>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = sessionStorage.getItem(key)
      return raw ? (JSON.parse(raw) as T) : initial
    } catch {
      return initial
    }
  })
  const set = (v: T) => {
    setValue(v)
    try {
      sessionStorage.setItem(key, JSON.stringify(v))
    } catch {
      /* storage blocked: keep it in memory */
    }
  }
  return [value, set]
}
