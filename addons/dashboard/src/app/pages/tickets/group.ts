import { useCallback, useState } from 'react'
import type { GroupBy } from '../board/grouping'

export interface TicketsGroup {
  group: GroupBy
  /** Sections the person folded (true) or unfolded (false) themselves; absent = the default for that epic. */
  sections: Record<string, boolean>
}
const DEFAULT: TicketsGroup = { group: 'epic', sections: {} }
const KEY = 'orch.tickets.group'

function load(): TicketsGroup {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? 'null') as Partial<TicketsGroup> | null
    if (!raw || typeof raw !== 'object') return DEFAULT
    return {
      group: raw.group === 'none' ? 'none' : 'epic',
      sections: raw.sections && typeof raw.sections === 'object' ? Object.fromEntries(Object.entries(raw.sections).filter(([, v]) => typeof v === 'boolean')) : {},
    }
  } catch {
    return DEFAULT
  }
}

/** The Tickets list's grouping, remembered per browser. */
export function useTicketsGroup() {
  const [state, setState] = useState<TicketsGroup>(load)
  const update = useCallback((patch: Partial<TicketsGroup>) => {
    setState((s) => {
      const next = { ...s, ...patch }
      try {
        localStorage.setItem(KEY, JSON.stringify(next))
      } catch {
        /* storage unavailable: the choice lasts for this visit */
      }
      return next
    })
  }, [])
  return [state, update] as const
}
