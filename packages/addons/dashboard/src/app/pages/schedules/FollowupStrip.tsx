// The ticket page's Follow-up strip (#720, pick: a quiet card in the right column while the agent is waiting, a full-width
// banner only when a person is needed; on a narrow page a one-line link takes the card's place). It stays 7 days after the
// follow-up ends, so whoever arrives at the ticket sees what happened and why.
import { SafeMarkdown } from '@/addon-ui/SafeMarkdown'
import { chipTime, currentFor, headline, inText, kindOf, needsAPerson, reasonText, wakeLine, wakeText, type FollowupRow } from '@/api/followups'
import type { TicketDocument } from '@/api/types'
import { cn } from '@/lib/utils'
import { RowButtons } from './FollowupsPanel'
import { StateBadges, useFollowups, usePersonName } from './FollowupParts'
import { fmtExact } from '@/lib/time'

/** The follow-up this ticket shows now, with the workspace and what the host allows; null when there is none. */
function useCurrent(ticket: TicketDocument) {
  const f = useFollowups()
  const row = f.on && f.data ? currentFor(f.rows, ticket.key) : null
  return { ...f, row }
}

const whenIn = (r: FollowupRow) => (r.trigger === 'time' && r.due ? inText(r.due) : '')

const bannerKind = (r: FollowupRow) => needsAPerson(r) || kindOf(r) === 'woken'

/** Full-width, only when a person is needed (or the deadline woke the agent). */
export function FollowupBanner({ ticket }: { ticket: TicketDocument }) {
  const { row, ws, can } = useCurrent(ticket)
  const name = usePersonName()
  if (!row || !ws || !can || !bannerKind(row)) return null
  const bad = needsAPerson(row)
  return (
    <section
      aria-label="Follow-up"
      data-testid="followup-banner"
      className={cn('rounded-lg border p-3', bad ? 'border-warning/50 bg-warning-soft' : 'border-border bg-surface')}
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-[13px] font-semibold">Follow-up: {headline(row)}</h3>
        <StateBadges row={row} />
      </div>
      <p className="mt-1 text-[13px] text-text-muted">
        {reasonText(row)} Set by {name(row.person)}'s agent. {row.due ? `It was due ${wakeText(row.due)}.` : ''}
      </p>
      <div className="mt-2">
        <RowButtons row={row} ws={ws} can={can} primaryRun />
      </div>
    </section>
  )
}

/** On a narrow page the card is in the Panels sheet: one line at the top says there is a follow-up. */
export function FollowupLine({ ticket }: { ticket: TicketDocument }) {
  const { row } = useCurrent(ticket)
  if (!row || bannerKind(row)) return null
  const kind = kindOf(row)
  const text = kind === 'waiting' ? `Follow-up ${row.due ?? row.deadline ? chipTime((row.due ?? row.deadline)!) : ''}`.trim() : `Follow-up: ${headline(row)}`
  return (
    <p className="text-[12px] text-text-muted xl:hidden" data-testid="followup-line">
      <span aria-hidden>⏱ </span>
      {text}
    </p>
  )
}

/** The quiet card of the right column. */
export function TicketFollowup({ ticket }: { ticket: TicketDocument }) {
  const { row, ws, can } = useCurrent(ticket)
  const name = usePersonName()
  if (!row || !ws || !can || bannerKind(row)) return null
  const kind = kindOf(row)
  const waiting = kind === 'waiting'
  const sentence = reasonText(row)
  return (
    <section aria-label="Follow-up" className="space-y-2 rounded-lg border border-info/30 bg-surface p-3" data-testid="ticket-followup" data-kind={kind}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-text-faint">Follow-up</h3>
        <StateBadges row={row} />
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[13px]">
        {waiting && (
          <>
            <dt className="text-text-muted">Wakes</dt>
            <dd>{wakeLine(row).replace(/^Wakes /, '')}{whenIn(row) ? ` (${whenIn(row)})` : ''}.</dd>
          </>
        )}
        {kind === 'running' && row.fired_at && (
          <>
            <dt className="text-text-muted">Started</dt>
            <dd>{fmtExact(row.fired_at)}</dd>
          </>
        )}
        <dt className="text-text-muted">Set by</dt>
        <dd>
          {name(row.person)}'s agent{row.chain ? `. Follow-up ${row.chain.n} of ${row.chain.max}.` : ''}
        </dd>
        {(waiting || kind === 'running') && (
          <>
            <dt className="text-text-muted">Runs as</dt>
            <dd>{name(row.person)}'s rights, this ticket only, up to 8 h</dd>
          </>
        )}
      </dl>
      {sentence && <p className="rounded-md bg-surface-2 p-2 text-[12px] text-text-muted">{sentence}</p>}
      {waiting && row.brief && (
        <div className="space-y-1">
          <p className="text-[12px] text-text-muted">The agent's note to itself</p>
          <div className="rounded-md border border-border bg-bg p-2 text-[13px]">
            <SafeMarkdown text={row.brief} />
          </div>
        </div>
      )}
      <RowButtons row={row} ws={ws} can={can} />
    </section>
  )
}
