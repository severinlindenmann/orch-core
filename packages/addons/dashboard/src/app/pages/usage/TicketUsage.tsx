// The ticket page's Usage box (add-on on): the agent time on this ticket, from the host's own run records. Tokens and
// cost say "Not reported". Hidden when the add-on is off or the host cannot say.
import { useQuery } from '@tanstack/react-query'
import { hasFeature, isRealHost } from '@/api/features'
import { runsText, spanText, usageQueries, NOT_REPORTED, WINDOW_LABEL } from '@/api/usage'
import { findHarness } from '@/api/harnesses'
import { fmtDateTime, fmtSpan } from '@/lib/time'
import type { TicketDocument } from '@/api/types'
import { useWorkspace } from '@/app/workspace'

export function TicketUsage({ ticket }: { ticket: TicketDocument }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const served = isRealHost() && !!ws && hasFeature('usage', ws)
  const data = useQuery({ ...usageQueries.ticket(ws ?? '', ticket.key), enabled: served })
  const d = data.data
  if (!served || !d || !d.available) return null
  const month = d.windows.month
  return (
    <section aria-label="Usage" className="space-y-2 rounded-lg border border-border bg-surface p-3" data-testid="ticket-usage">
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-text-faint">Usage</h3>
      {month.runs === 0 ? (
        <p className="text-[13px] text-text-muted">No agent has run on this ticket in the last 30 days.</p>
      ) : (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[13px]">
          {(['week', 'month'] as const).map((k) => (
            <div key={k} className="contents">
              <dt className="text-text-muted">{WINDOW_LABEL[k]}</dt>
              <dd>
                {spanText(d.windows[k])} · {runsText(d.windows[k])}
              </dd>
            </div>
          ))}
          <dt className="text-text-muted">Tokens</dt>
          <dd>{NOT_REPORTED}</dd>
          <dt className="text-text-muted">Cost</dt>
          <dd>{NOT_REPORTED}</dd>
        </dl>
      )}
      {d.recent.length > 0 && (
        <details className="text-[12px] text-text-muted">
          <summary className="cursor-pointer select-none">Recent runs</summary>
          <ul className="mt-1 space-y-0.5">
            {d.recent.map((r) => (
              <li key={r.started_at + (r.harness ?? '')}>
                {(r.harness && findHarness(r.harness)?.label) || 'An agent'} · {fmtDateTime(r.started_at)} ·{' '}
                {r.ms === null ? 'time not recorded' : r.running ? `${fmtSpan(r.ms)} so far` : fmtSpan(r.ms)}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}
