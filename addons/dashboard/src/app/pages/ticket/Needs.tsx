// What a ticket needs (D55–D57), from the ticket document (core-computed): the rail's "Needs" line and the header's
// "Blocked: <connection> <status>" line.
import { Link } from '@tanstack/react-router'
import { KeyRound } from 'lucide-react'
import { blockedText, CHECK_LABEL, type TicketNeeds } from '@/api/connections'
import type { TicketDocument } from '@/api/types'
import { cn } from '@/lib/utils'

/** "skills orch, dbt-seeds · connections gh (ok), databricks-ci (ok)". */
export function NeedsValue({ needs }: { needs: TicketNeeds }) {
  return (
    <span className="block min-w-0 space-y-0.5" data-testid="ticket-needs">
      <span className="block">
        <span className="text-text-muted">skills </span>
        {needs.skills.map((s, i) => (
          <span key={s.name}>
            {i > 0 && ', '}
            <Link to="/settings/$tab" params={{ tab: 'skills' }} className="font-mono text-[12px] hover:underline">
              {s.name}
            </Link>
            {s.needs !== 'declared' && <span className="text-warning"> (needs unknown)</span>}
          </span>
        ))}
      </span>
      {needs.connections.length > 0 && (
        <span className="block">
          <span className="text-text-muted">connections </span>
          {needs.connections.map((c, i) => (
            <span key={c.name}>
              {i > 0 && ', '}
              <Link to="/settings/$tab" params={{ tab: 'connections' }} className="font-mono text-[12px] hover:underline">
                {c.name}
              </Link>{' '}
              <span className={cn(c.status === 'ok' ? 'text-success' : c.status === 'unknown' ? 'text-text-muted' : c.status === 'service_down' ? 'text-warning' : 'text-danger')}>({CHECK_LABEL[c.status]})</span>
            </span>
          ))}
        </span>
      )}
      {needs.env.length > 0 && (
        <span className="block text-[12px]">
          <span className="text-text-muted">env </span>
          <span className="font-mono">{needs.env.join(', ')}</span>
        </span>
      )}
    </span>
  )
}

/** The header line when a needed connection fails auth or identity; claim and Start agent refuse with the same words. */
export function BlockedByConnection({ ticket }: { ticket: TicketDocument }) {
  const b = ticket.needs?.blocked
  if (!b) return null
  return (
    <p role="status" data-testid="connection-blocked" className="flex items-center gap-2 rounded-md border border-danger/40 bg-danger-soft px-2.5 py-1.5 text-[13px] text-text">
      <KeyRound className="size-3.5 shrink-0 text-danger" aria-hidden />
      <span className="min-w-0">
        <span className="font-medium">{blockedText(b)}</span>
        <span className="text-text-muted"> · agents cannot claim or start until an owner logs in again. </span>
        <Link to="/settings/$tab" params={{ tab: 'connections' }} className="text-brand hover:underline">
          Connections
        </Link>
      </span>
    </p>
  )
}
