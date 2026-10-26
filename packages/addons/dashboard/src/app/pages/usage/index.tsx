// Usage on a real host (add-on): how long agents worked on each ticket in the last 7 and 30 days. Read only. The times are
// the host's own records of when each run started and ended; tokens and cost are not reported in a form that can be
// trusted, so they say so. Totals are the runs you started (the host lists no one else's).
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { hasFeature } from '@/api/features'
import { NO_TOKENS_NOTE, runsText, spanText, usageQueries, WINDOW_LABEL, type UsageTicketRow } from '@/api/usage'
import { LoadFailed } from '@/components/LoadFailed'
import { Inline } from '@/components/sign/visible'
import { cn } from '@/lib/utils'
import { usePageHeader } from '@/app/shell/ShellUi'
import { useWorkspace } from '@/app/workspace'
import { Tiles } from './Windows'

function Row({ r }: { r: UsageTicketRow }) {
  return (
    <li className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 py-2.5 text-[13px] max-sm:flex-col max-sm:items-start" data-testid="usage-row">
      {r.ticket ? (
        <Link to="/ticket/$key" params={{ key: r.ticket }} className="font-mono text-[12px] text-brand hover:underline">
          {r.ticket}
        </Link>
      ) : null}
      <span className="min-w-0 max-w-full flex-1 basis-48 break-words max-sm:flex-none max-sm:basis-auto">
        {r.ticket ? r.title ? <Inline>{r.title}</Inline> : <span className="text-text-faint">title not shown</span> : r.other ? 'Other tickets' : 'Not on a ticket'}
      </span>
      <span className="tabular-nums">{spanText(r)}</span>
      <span className="ml-auto text-right text-text-muted max-sm:ml-0 max-sm:text-left">{runsText(r)}</span>
    </li>
  )
}

export function UsagePage() {
  usePageHeader('Usage')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const [win, setWin] = useState<'week' | 'month'>('week')
  const served = !!ws && hasFeature('usage', ws)
  const data = useQuery({ ...usageQueries.workspace(ws ?? ''), enabled: served })
  if (!workspace || !ws) return null
  if (!served || (data.data && !data.data.available)) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Usage</h1>
        <p data-testid="usage-unavailable" className="text-[13px] text-text-muted">
          Usage is off for this workspace, or this host cannot say how long agents ran. Owners can turn it on in Settings, under Add-ons.
        </p>
      </div>
    )
  }
  if (data.isError) return <LoadFailed what="usage" onRetry={() => void data.refetch()} />
  const d = data.data
  return (
    <div className="w-full min-w-0 max-w-4xl space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Usage</h1>
      <p className="text-[13px] text-text-muted">
        How long the agents you started worked, per ticket. The times come from orch’s own record of when each run started and ended. {NO_TOKENS_NOTE}
      </p>
      {!d || !d.available ? (
        <p role="status" className="text-[13px] text-text-muted">
          Reading the usage…
        </p>
      ) : (
        <>
          <Tiles windows={d.windows} />
          <div className="flex gap-1" role="group" aria-label="Period">
            {(['week', 'month'] as const).map((k) => (
              <button
                key={k}
                type="button"
                aria-pressed={win === k}
                onClick={() => setWin(k)}
                className={cn('rounded-md border px-2.5 py-1 text-[12px]', win === k ? 'border-brand bg-brand-soft text-brand' : 'border-border text-text-muted hover:text-text')}
              >
                {WINDOW_LABEL[k]}
              </button>
            ))}
          </div>
          <h2 className="text-[15px] font-semibold" data-testid="usage-tickets-heading">
            Time by ticket — {WINDOW_LABEL[win]}
          </h2>
          {d.windows[win].tickets.length === 0 ? (
            <p data-testid="usage-empty" className="text-[13px] text-text-muted">
              No agent ran in the {WINDOW_LABEL[win].toLowerCase()} yet. Start an agent from a ticket on the{' '}
              <Link to="/board" className="text-brand hover:underline">
                Board
              </Link>{' '}
              and its running time shows up here.
            </p>
          ) : (
            <ul aria-label="Time per ticket" className="divide-y divide-border rounded-lg border border-border bg-surface px-3">
              {d.windows[win].tickets.map((r) => (
                <Row key={r.ticket ?? (r.other ? '~other' : '~none')} r={r} />
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}
