// The board card's follow-up chip (#720, pick A: a clock chip). A ticket waiting for its agent says when it resumes; one
// where the follow-up went wrong says so for as long as it is the ticket's latest, so a stalled ticket shows without opening it.
// The words of the chip are the words of the ticket page (the tooltip is the same sentence).
import { useQuery } from '@tanstack/react-query'
import { Clock } from 'lucide-react'
import { chipTime, currentFor, followupQueries, followupsOn, kindOf, reasonText } from '@/api/followups'
import { useWorkspace } from '@/app/workspace'
import { cn } from '@/lib/utils'

export function FollowupChip({ ticketKey, claimed = false }: { ticketKey: string; claimed?: boolean }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const on = followupsOn(workspace)
  const { data } = useQuery({ ...followupQueries.list(ws ?? ''), enabled: !!ws && on })
  const row = data?.available ? currentFor(data.rows, ticketKey) : null
  if (!row) return null
  const kind = kindOf(row)
  const at = row.due ?? row.deadline
  let text: string | null = null
  let tone = ''
  if (kind === 'waiting' && !claimed && at) {
    text = `Resumes ${chipTime(at)}`
    tone = 'border-info/40 bg-info-soft text-info'
  } else if (kind === 'needs_person') {
    text = 'Follow-up needs a person'
    tone = 'border-warning/50 bg-warning-soft text-warning'
  } else if (kind === 'ran_late') {
    text = 'Follow-up late'
    tone = 'border-warning/50 bg-warning-soft text-warning'
  } else if (kind === 'failed') {
    text = 'Follow-up failed'
    tone = 'border-danger/50 bg-danger-soft text-danger'
  } else if (kind === 'did_not_run') {
    text = 'Follow-up did not run'
    tone = 'border-danger/50 bg-danger-soft text-danger'
  }
  if (!text) return null
  const tip = reasonText(row)
  return (
    <span title={tip || text} data-testid="followup-chip" data-kind={kind} className={cn('inline-flex max-w-full items-center gap-1 self-start rounded-full border px-1.5 py-px text-[11px] leading-4', tone)}>
      {kind === 'waiting' && <Clock aria-hidden className="size-3 shrink-0" />}
      <span className="truncate">{text}</span>
    </span>
  )
}
