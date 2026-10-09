import { useCallback, useEffect, useState } from 'react'
import type { GroupBy } from '../board/grouping'

export interface TicketsGroup {
  group: GroupBy
  /** Sections the person folded (true) or unfolded (false) themselves; absent = the default for that epic. */
  sections: Record<string, boolean>
}
const DEFAULT: TicketsGroup = { group: 'epic', sections: {} }
/** Remembered per viewer. */
export const groupKey = (person: string) => `orch.tickets.group.${person}`

function load(person: string | undefined): TicketsGroup {
  if (!person) return DEFAULT
  try {
    const raw = JSON.parse(localStorage.getItem(groupKey(person)) ?? 'null') as Partial<TicketsGroup> | null
    if (!raw || typeof raw !== 'object') return DEFAULT
    return {
      group: raw.group === 'none' ? 'none' : 'epic',
      sections: raw.sections && typeof raw.sections === 'object' ? Object.fromEntries(Object.entries(raw.sections).filter(([, v]) => typeof v === 'boolean')) : {},
    }
  } catch {
    return DEFAULT
  }
}

/** The Tickets list's grouping, remembered per viewer. */
export function useTicketsGroup(person: string | undefined) {
  const [state, setState] = useState<{ person: string | undefined; value: TicketsGroup }>(() => ({ person, value: load(person) }))
  useEffect(() => {
    setState((s) => (s.person === person ? s : { person, value: load(person) }))
  }, [person])
  const value = state.person === person ? state.value : load(person)
  const update = useCallback(
    (patch: Partial<TicketsGroup>) => {
      setState((s) => {
        const next = { person, value: { ...(s.person === person ? s.value : load(person)), ...patch } }
        if (person) {
          try {
            localStorage.setItem(groupKey(person), JSON.stringify(next.value))
          } catch {
            /* storage unavailable: the choice lasts for this visit */
          }
        }
        return next
      })
    },
    [person],
  )
  return [value, update] as const
}
