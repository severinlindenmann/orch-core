// Tickets with a recurring schedule are not open work: they wait for their next run. The board keeps them out of the
// Open column and shows them in one quiet "Scheduled" group (folded, count shown); a small clock marks their card.
import { createContext, useContext, useMemo } from 'react'
import { Clock } from 'lucide-react'
import { followupsOn } from '@/api/followups'
import { useSchedules, type ScheduleRow } from '@/api/schedules'
import type { TicketSummary } from '@/api/types'
import { useWorkspace } from '@/app/workspace'
import { ListView } from './ListView'
import type { BoardPeople } from './TicketCard'

/** The keys of tickets with a live repeating schedule (a one-time run is a wake-up, not a routine); none when the
 * schedules add-on is off. */
export function scheduledKeysOf(rows: readonly ScheduleRow[] | undefined, on: boolean): ReadonlySet<string> {
  if (!on) return new Set()
  return new Set((rows ?? []).filter((r) => r.state === 'live' && !!r.rule.every && !!r.ticket).map((r) => r.ticket))
}

/** Open tickets that have such a schedule leave the board for the Scheduled group. A ticket that is in progress (or
 * anything but Open) stays where it is, and so does an epic while epics are lanes (it is the lane header). */
export function splitScheduled(tickets: TicketSummary[], keys: ReadonlySet<string>, epicsAreLanes: boolean) {
  if (keys.size === 0) return { scheduled: [] as TicketSummary[], rest: tickets }
  const isScheduled = (t: TicketSummary) => t.status === 'open' && keys.has(t.key) && !(epicsAreLanes && t.type === 'epic')
  return { scheduled: tickets.filter(isScheduled), rest: tickets.filter((t) => !isScheduled(t)) }
}

const KeysContext = createContext<ReadonlySet<string>>(new Set())
export const ScheduledKeysProvider = KeysContext.Provider

/** Read once by the board (one query), handed to every card through the provider. */
export function useScheduledKeys(): ReadonlySet<string> {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const on = !!ws && followupsOn(workspace)
  const { data } = useSchedules(ws ?? '', on)
  return useMemo(() => scheduledKeysOf(data?.rows, on), [data, on])
}

export function ScheduledMark({ ticketKey }: { ticketKey: string }) {
  if (!useContext(KeysContext).has(ticketKey)) return null
  return <Clock role="img" aria-label="Has a schedule" className="size-3 shrink-0 text-text-faint" data-testid="scheduled-mark" />
}

/** Folded by default; only drawn when there is something in it. */
export function ScheduledGroup({ tickets, people, onOpen }: { tickets: TicketSummary[]; people: BoardPeople; onOpen: (key: string) => void }) {
  if (tickets.length === 0) return null
  return (
    <details data-testid="board-scheduled" className="shrink-0 rounded-lg border border-border bg-surface text-[13px]">
      <summary className="flex cursor-pointer items-center gap-1.5 px-3 py-2 font-medium text-text-muted">
        <Clock aria-hidden className="size-3.5" />
        Scheduled
        <span className="font-mono text-[11px] text-text-faint">{tickets.length}</span>
      </summary>
      <div className="max-h-64 overflow-auto border-t border-border">
        <ListView tickets={tickets} people={people} onOpen={onOpen} />
      </div>
    </details>
  )
}
