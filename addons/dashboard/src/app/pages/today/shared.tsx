import { useEffect, useState } from 'react'
import { usePageWidth } from '../../pageWidth'
import type { AgentInfo, Workspace } from '@/api/types'

/** How long an item has waited ("14 min", "2 h", "3 days"): the one formatter, src/lib/time.ts. */
export { fmtAge as ago } from '@/lib/time'

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
  if (!dir.workspace) return '…' // members have not loaded yet: not the raw id
  return dir.workspace.members.find((m) => m.person === id)?.name ?? id
}

/** Today's two layouts: a side column from 1280 px, one column below. */
export const WIDE_QUERY = '(min-width: 1280px)'
/** Today's two columns need this page width (the shell's: the window minus a right-hand dock). */
export const TODAY_WIDE_MIN = 1280

/** Today (and its skeleton) lay out in two columns only when the page is wide: with the dock open, one column. */
export function useTodayWide(): boolean {
  return usePageWidth() >= TODAY_WIDE_MIN
}

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
