// The Follow-ups tab of Schedules (#720, pick B: grouped). Needs a person first, then what is waiting, then the last 7
// days of what finished. A follow-up is never created here: only an agent sets one; a person can cancel, run now or move one.
import { Link } from '@tanstack/react-router'
import { SafeMarkdown } from '@/addon-ui/SafeMarkdown'
import { groups, kindOf, reasonText, wakeLine, inText, type FollowupRow } from '@/api/followups'
import { LoadFailed } from '@/components/LoadFailed'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { plural } from '@/lib/time'
import { CancelButton, RescheduleButton, RunNowButton, StateBadges, usePersonName, useFollowups } from './FollowupParts'

/** The buttons of one row, by what it is. The viewer who cannot act sees none (a viewer has no buttons, never disabled ones that explain nothing). */
export function RowButtons({ row, ws, can, primaryRun = false }: { row: FollowupRow; ws: string; can: { cancel: boolean; reschedule: boolean; undo: boolean }; primaryRun?: boolean }) {
  const k = kindOf(row)
  const live = k === 'waiting' || k === 'needs_person'
  const stuck = k === 'failed' || k === 'did_not_run'
  if (!live && !stuck) return null
  if (!can.cancel) {
    // A viewer sees what the buttons would be, switched off, and why.
    return (
      <div className="flex flex-wrap items-center gap-2" data-testid="followup-buttons-off" title="Viewers cannot change follow-ups.">
        {['Run now as me', 'Reschedule', 'Cancel follow-up'].map((t) => (
          <Button key={t} type="button" size="sm" variant="outline" disabled>
            {t}
          </Button>
        ))}
      </div>
    )
  }
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="followup-buttons">
      <RunNowButton row={row} ws={ws} primary={primaryRun || stuck || k === 'needs_person'} />
      <RescheduleButton row={row} ws={ws} canMove={can.reschedule} />
      <CancelButton row={row} ws={ws} canUndo={can.undo} />
    </div>
  )
}

function Row({ row, ws, can }: { row: FollowupRow; ws: string; can: { cancel: boolean; reschedule: boolean; undo: boolean } }) {
  const name = usePersonName()
  const kind = kindOf(row)
  const sentence = reasonText(row)
  const wakes = kind === 'waiting' ? wakeLine(row) : ''
  const wait = kind === 'waiting' && (row.due ?? row.deadline) ? inText((row.due ?? row.deadline)!) : ''
  return (
    <li className="space-y-2 px-4 py-3" data-testid="followup-row" data-followup={row.id} data-ticket={row.ticket ?? ''}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {row.ticket && (
          <Link to="/ticket/$key" params={{ key: row.ticket }} className="font-mono text-[13px] font-semibold text-brand hover:underline">
            {row.ticket}
          </Link>
        )}
        <span className="min-w-0 truncate text-[13px] font-medium">{row.title ?? 'A ticket you cannot see here'}</span>
        <StateBadges row={row} />
      </div>
      <p className="text-[13px] text-text-muted">
        {wakes && <span>{wakes} </span>}
        {wait && <span>({wait}) </span>}
        {sentence && <span>{sentence} </span>}
        <span>
          {row.chain && kind !== 'cancelled' ? `Follow-up ${row.chain.n} of ${row.chain.max}. ` : ''}Set by {name(row.person)}'s agent.
        </span>
      </p>
      {kind === 'waiting' && row.brief && (
        <details className="text-[13px]">
          <summary className="cursor-pointer select-none text-text-muted">The agent's note to itself</summary>
          <div className="mt-1 rounded-md border border-border bg-bg p-2">
            <SafeMarkdown text={row.brief} />
          </div>
        </details>
      )}
      <RowButtons row={row} ws={ws} can={can} />
    </li>
  )
}

function Group({ title, rows, ws, can, id }: { title: string; rows: FollowupRow[]; ws: string; can: { cancel: boolean; reschedule: boolean; undo: boolean }; id: string }) {
  if (rows.length === 0) return null
  return (
    <section aria-labelledby={id} data-testid={`followups-${id}`}>
      <h3 id={id} className="border-b border-border bg-surface-2 px-4 py-1.5 text-[11px] font-medium uppercase tracking-wide text-text-muted">
        {title} · {rows.length}
      </h3>
      <ul className="divide-y divide-border">
        {rows.map((r) => (
          <Row key={r.id} row={r} ws={ws} can={can} />
        ))}
      </ul>
    </section>
  )
}

export default function FollowupsPanel() {
  const { ws, on, query, data, rows, can } = useFollowups()
  if (!on || !ws) return <p className="text-[13px] text-text-muted">Follow-ups are off. Turn Schedules on in Add-ons.</p>
  if (query.isError) return <LoadFailed what="follow-ups" onRetry={() => void query.refetch()} />
  if (!data || !can) return <Skeleton className="h-24 w-full" />
  const g = groups(rows)
  if (g.needs.length + g.waiting.length + g.finished.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-surface p-6 text-center" data-testid="followups-empty">
        <p className="text-[14px] font-medium">No follow-ups waiting</p>
        <p className="mx-auto mt-1 max-w-md text-[13px] text-text-muted">
          When an agent has to wait more than an hour (a long job, a reply from someone), it can schedule its own next session on the ticket. It shows here, and on the ticket.
        </p>
      </div>
    )
  }
  return (
    <div className="space-y-3" data-testid="followups-panel">
      <p className="text-[13px] text-text-muted">
        {plural(g.waiting.length, 'follow-up')} waiting{g.needs.length ? `, ${g.needs.length} ${g.needs.length === 1 ? 'needs' : 'need'} a person` : ''}. Only an agent sets a follow-up; you can run it now, move it or cancel it.
      </p>
      <div className="overflow-hidden rounded-lg border border-border bg-surface">
        <Group id="needs" title="Needs a person" rows={g.needs} ws={ws} can={can} />
        <Group id="waiting" title="Waiting" rows={g.waiting} ws={ws} can={can} />
        <Group id="finished" title="Finished in the last 7 days" rows={g.finished} ws={ws} can={can} />
      </div>
    </div>
  )
}
