// The ticket page's Shares box (real host, Publish add-on on): Share files, the copies waiting for a signature
// (an agent may have prepared one), and this ticket's shares.
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { Share2 } from 'lucide-react'
import { useState } from 'react'
import { hasFeature, isRealHost } from '@/api/features'
import { atLeast } from '@/api/permissions'
import { effectiveState, kindText, shareQueries, STATE_TEXT, type Share, type Staged } from '@/api/shares'
import type { TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { fmtDateTime } from '@/lib/time'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'
import { ShareFlow } from './ShareFlow'
import { RevokeShare } from './Revoke'

export function TicketShares({ ticket }: { ticket: TicketDocument }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const [sharing, setSharing] = useState(false)
  const [resume, setResume] = useState<Staged | null>(null)
  const [revoking, setRevoking] = useState<Share | null>(null)
  const served = isRealHost() && !!ws && hasFeature('publish', ws)
  const list = useQuery({ ...shareQueries.ofTicket(ws ?? '', ticket.key), enabled: served })
  if (!served || !ws || !list.data?.available) return null
  const data = list.data
  const mayChange = atLeast(role, 'member') && ticket.status !== 'done'
  const live = data.shares.filter((s) => ['live', 'sending'].includes(effectiveState(s)))
  return (
    <section aria-label="Shares" className="space-y-2 rounded-lg border border-border bg-surface p-3" data-testid="ticket-shares">
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-text-faint">Shares</h3>
      {data.staged.map((s) => (
        <p key={s.staged_id} className="text-[13px]" data-staged={s.staged_id}>
          <span className="block text-text-muted">
            Waiting for your signature: {s.files.length} file{s.files.length === 1 ? '' : 's'}
            {s.origin === 'agent' ? ', prepared by an agent' : ''}.
          </span>
          {mayChange && (
            <Button size="sm" variant="secondary" className="mt-1" onClick={() => setResume(s)}>
              Review and sign
            </Button>
          )}
        </p>
      ))}
      {live.length === 0 && data.staged.length === 0 && <p className="text-[12px] text-text-muted">Nothing is shared from this ticket.</p>}
      {live.length > 0 && (
        <ul className="space-y-1.5 text-[13px]">
          {live.map((s) => (
            <li key={s.id} className="flex items-start gap-2">
              <Share2 className="mt-0.5 size-3.5 shrink-0 text-text-muted" aria-hidden />
              <span className="min-w-0 flex-1">
                {kindText(s.kind, s.view_limit)}
                <span className="block text-[12px] text-text-muted">
                  {s.expires_at ? `until ${fmtDateTime(s.expires_at)}` : ''}
                  {s.views !== null ? ` · opened ${s.views} time${s.views === 1 ? '' : 's'}` : ''} · {STATE_TEXT[effectiveState(s)]}
                </span>
              </span>
              {mayChange && (
                <Button size="sm" variant="ghost" className="h-7 text-[12px]" aria-label={`Revoke the ${kindText(s.kind, s.view_limit)}`} onClick={() => setRevoking(s)}>
                  Revoke
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {mayChange && (
        <Button size="sm" variant="secondary" onClick={() => setSharing(true)}>
          Share files
        </Button>
      )}
      <Link to="/shares" className="block text-[12px] text-brand hover:underline">
        All shares
      </Link>
      {sharing && <ShareFlow ws={ws} ticket={ticket} onClose={() => setSharing(false)} />}
      {resume && <ShareFlow ws={ws} ticket={ticket} resume={resume} onClose={() => setResume(null)} />}
      {revoking && <RevokeShare ws={ws} share={revoking} onClose={() => setRevoking(null)} />}
    </section>
  )
}
