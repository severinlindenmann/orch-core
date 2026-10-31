// Shares of a real host (the Publish add-on): every link made from a ticket's files, with its kind, end time, opens
// and state, and Stop sharing. A link itself is never listed: orch keeps none, it is shown once when it is made.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { queries } from '@/api/queries'
import { hasFeature } from '@/api/features'
import { atLeast } from '@/api/permissions'
import { effectiveState, kindText, newIdem, shareError, shareQueries, sharesApi, STATE_TEXT, stopName, viewsText, type Share, type Staged } from '@/api/shares'
import { LoadFailed } from '@/components/LoadFailed'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { fmtDateTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { usePageHeader } from '@/app/shell/ShellUi'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'
import { refreshShares, ShareFlow } from './ShareFlow'
import { RevokeShare } from './Revoke'

// Wide: a table cell. Phone: a labelled line of a stacked card.
const CELL = 'px-3 py-2 max-sm:flex max-sm:justify-between max-sm:gap-3 max-sm:px-0 max-sm:py-0 max-sm:before:text-text-faint max-sm:before:content-[attr(data-label)]'

function StagedRow({ s, ws, mayChange, onSign }: { s: Staged; ws: string; mayChange: boolean; onSign: (s: Staged) => void }) {
  const qc = useQueryClient()
  const [error, setError] = useState('')
  const discard = async () => {
    try {
      await sharesApi.discard(ws, s.staged_id, newIdem())
      await refreshShares(qc, ws)
    } catch (e) {
      setError(shareError(e))
    }
  }
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-[13px]" data-staged={s.staged_id}>
      {s.ticket && (
        <Link to="/ticket/$key" params={{ key: s.ticket }} className="font-mono text-[12px] text-brand hover:underline">
          {s.ticket}
        </Link>
      )}
      <span className="min-w-0 flex-1 basis-48">
        {s.files.length} file{s.files.length === 1 ? '' : 's'} · {kindText(s.kind, s.views)}
        {s.origin === 'agent' ? ' · prepared by an agent' : ''}
      </span>
      <span className="text-text-muted">Waiting for your signature</span>
      {mayChange && (
        <>
          <Button size="sm" className="h-7 text-[12px]" onClick={() => onSign(s)}>
            Review and sign
          </Button>
          <Button size="sm" variant="ghost" className="h-7 text-[12px]" onClick={() => void discard()}>
            Discard
          </Button>
        </>
      )}
      {error && (
        <p role="alert" className="basis-full text-danger">
          {error}
        </p>
      )}
    </li>
  )
}

function SharesBody({ onRevoke, onSign, onShareAgain }: { onShareAgain: (key: string) => void; onRevoke: (s: Share) => void; onSign: (s: Staged) => void }) {
  usePageHeader('Shares')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const served = !!ws && hasFeature('publish', ws)
  const list = useQuery({ ...shareQueries.list(ws ?? ''), enabled: served })
  if (!workspace || !ws) return null
  if (!served || (list.data && !list.data.available)) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Shares</h1>
        <p data-testid="shares-unavailable" className="text-[13px] text-text-muted">
          No file links yet.
        </p>
        <Link to="/tickets" className="text-[13px] text-brand hover:underline">Open a ticket to share its files</Link>
      </div>
    )
  }
  if (list.isError) return <LoadFailed what="shares" detail={shareError(list.error)} onRetry={() => void list.refetch()} />
  const data = list.data && list.data.available ? list.data : null
  const mayChange = atLeast(role, 'member')
  const shares = data?.shares ?? []
  const staged = data?.staged ?? []
  return (
    <div className="w-full max-w-4xl space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Shares</h1>
      <p className="text-[13px] text-text-muted">
        Manage links to files you share.
      </p>
      <Link to="/tickets" className="inline-block text-[13px] text-brand hover:underline">Open a ticket to share its files</Link>
      {data && !data.configured && (
        <p data-testid="shares-not-configured" role="status" className="rounded-lg border border-warning/40 px-3 py-2 text-[13px]">
          Publish needs a publish server. <Link to="/addons" search={{ tab: 'installed' }} className="text-brand hover:underline">Set it in Add-ons, Publish</Link>.
        </p>
      )}
      {data && data.configured && !data.live && data.why === 'not_allowed' && (
        <p role="status" data-testid="shares-not-allowed" className="flex flex-wrap items-center gap-2 rounded-lg border border-warning/40 px-3 py-2 text-[12px]">
          <span>Your hub has not switched on publishing for this workspace yet. Nothing was sent.</span>
          <Button size="sm" variant="secondary" onClick={() => void list.refetch()}>
            Try again
          </Button>
        </p>
      )}
      {data && data.configured && !data.live && (data.why === undefined || data.why === 'unreachable') && (
        <p role="status" data-testid="shares-unreachable" className="flex flex-wrap items-center gap-2 rounded-lg border border-warning/40 px-3 py-2 text-[12px]">
          <span>Sharing is paused: your hub cannot be reached. Nothing was sent.</span>
          <Button size="sm" variant="secondary" onClick={() => void list.refetch()}>
            Try again
          </Button>
        </p>
      )}
      {list.isPending ? (
        <p role="status" className="text-[13px] text-text-muted">
          Reading the shares…
        </p>
      ) : (
        <>
          {staged.length > 0 && (
            <section aria-label="Waiting for your signature" className="space-y-1">
              <h2 className="text-[11px] font-medium uppercase tracking-wide text-text-faint">Waiting for your signature</h2>
              <ul className="divide-y divide-border rounded-lg border border-border bg-surface px-3">
                {staged.map((s) => (
                  <StagedRow key={s.staged_id} s={s} ws={ws} mayChange={mayChange} onSign={onSign} />
                ))}
              </ul>
            </section>
          )}
          {shares.length === 0 && ((data && !data.configured) || staged.length > 0) ? null : shares.length === 0 ? (
            <p data-testid="shares-empty" className="text-[13px] text-text-muted">
              No file links yet.
            </p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-border bg-surface">
              <table className="w-full text-left text-[13px] max-sm:block" aria-label="Shares">
                <thead className="text-[11px] uppercase tracking-wide text-text-faint max-sm:sr-only">
                  <tr>
                    <th className="px-3 py-2 font-medium">Ticket</th>
                    <th className="px-3 py-2 font-medium">Kind</th>
                    <th className="px-3 py-2 font-medium">Expires</th>
                    <th className="px-3 py-2 font-medium">Opens</th>
                    <th className="px-3 py-2 font-medium">State</th>
                    <th className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-border max-sm:block">
                  {shares.map((s) => {
                    const state = effectiveState(s)
                    return (
                      <tr key={s.id} data-share={s.id} className="max-sm:block max-sm:space-y-1 max-sm:px-3 max-sm:py-3">
                        <td className="px-3 py-2 max-sm:block max-sm:px-0 max-sm:py-0">
                          {s.ticket ? (
                            <Link to="/ticket/$key" params={{ key: s.ticket }} className="font-mono text-[12px] text-brand hover:underline">
                              {s.ticket}
                            </Link>
                          ) : (
                            'not shown'
                          )}
                          {s.title && (
                            <span className="block text-text-muted">
                              <Inline>{s.title}</Inline>
                            </span>
                          )}
                          {s.files.length > 0 && (
                            <span className="block text-[12px] text-text-faint">
                              {s.files.length} file{s.files.length === 1 ? '' : 's'}: <Inline>{s.files.slice(0, 3).join(', ')}</Inline>
                              {s.files.length > 3 ? ' …' : ''}
                            </span>
                          )}
                        </td>
                        <td className={CELL} data-label="Kind">{kindText(s.kind, s.view_limit)}</td>
                        <td className={CELL} data-label="Expires">{state === 'revoked' ? null : s.expires_at ? fmtDateTime(s.expires_at) : 'not known'}</td>
                        <td className={CELL} data-label="Opens">{state === 'revoked' ? null : viewsText(s)}</td>
                        <td className={CELL} data-label="State">
                          <span className={cn('rounded-full border px-2 py-px text-[11px]', state === 'live' ? 'border-border text-success' : 'border-border text-text-muted')}>{state === 'revoked' ? 'Stopped, nobody can open it now' : STATE_TEXT[state]}</span>
                        </td>
                        <td className="px-3 py-2 text-right max-sm:block max-sm:px-0 max-sm:py-1 max-sm:text-left">
                          {mayChange && state === 'revoked' && s.ticket && <Button size="sm" variant="ghost" onClick={() => onShareAgain(s.ticket!)}>Share again</Button>}
                          {mayChange && (state === 'live' || state === 'sending') && (
                            <Button size="sm" variant="ghost" className="h-7 text-[12px]" aria-label={stopName(s, fmtDateTime)} onClick={() => onRevoke(s)}>
                              Stop sharing
                            </Button>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function ShareAgain({ ws, ticketKey, onClose }: { ws: string; ticketKey: string; onClose: () => void }) {
  const ticket = useQuery(queries.ticket(ws, ticketKey))
  if (ticket.data) return <ShareFlow ws={ws} ticket={ticket.data} onClose={onClose} />
  return <div role="status">
    {ticket.isError ? <LoadFailed what="ticket" onRetry={() => void ticket.refetch()} /> : 'Reading the ticket…'}
    <Button variant="ghost" onClick={onClose}>Cancel</Button>
  </div>
}

/**
 * The page. The dialogs sit in this wrapper, at one fixed place in the tree, before and apart from the body: a failed
 * refetch, an unavailable answer or a flipped feature changes the body only, and can never unmount a dialog that holds
 * the link (shown once) until the person presses Done.
 */
export function SharesPage() {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const [sharing, setSharing] = useState<string | null>(null)
  const [revoking, setRevoking] = useState<Share | null>(null)
  const [signing, setSigning] = useState<Staged | null>(null)
  return (
    <>
      {ws && sharing && <ShareAgain ws={ws} ticketKey={sharing} onClose={() => setSharing(null)} />}
      {ws && revoking && <RevokeShare ws={ws} share={revoking} onClose={() => setRevoking(null)} />}
      {ws && signing && <ShareFlow ws={ws} ticket={{ key: signing.ticket ?? '', artifacts: [] }} resume={signing} onClose={() => setSigning(null)} />}
      <SharesBody onShareAgain={setSharing} onRevoke={setRevoking} onSign={setSigning} />
    </>
  )
}
