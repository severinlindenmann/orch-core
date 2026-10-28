// Tickets with a recurring schedule are not open work: they wait for their next run. The board keeps them out of the
// Open column and shows them in one quiet "Scheduled" group (folded, count shown); a small clock marks their card.
import { useMemo } from 'react'
import { Clock } from 'lucide-react'
import { followupsOn } from '@/api/followups'
import { useSchedules } from '@/api/schedules'
import type { TicketSummary } from '@/api/types'
import { useWorkspace } from '@/app/workspace'
import { ListView } from './ListView'
import type { BoardPeople } from './TicketCard'

/** The keys of tickets that have a live repeating schedule (a one-time run is just a wake-up, not a routine). */
export function useScheduledKeys(): ReadonlySet<string> {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const { data } = useSchedules(ws ?? '', !!ws && followupsOn(workspace))
  return useMemo(() => new Set((data?.rows ?? []).filter((r) => r.state === 'live' && !!r.rule.every && !!r.ticket).map((r) => r.ticket)), [data])
}

export function ScheduledMark({ ticketKey }: { ticketKey: string }) {
  const keys = useScheduledKeys()
  if (!keys.has(ticketKey)) return null
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
