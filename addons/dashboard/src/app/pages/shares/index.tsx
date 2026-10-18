// Shares of a real host (the Publish add-on): every link made from a ticket's files, with its kind, end time, opens
// and state, and Revoke. A link itself is never listed: orch keeps none, it is shown once when it is made.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { hasFeature } from '@/api/features'
import { atLeast } from '@/api/permissions'
import { effectiveState, kindText, newIdem, shareError, shareQueries, sharesApi, STATE_TEXT, viewsText, type Share, type Staged } from '@/api/shares'
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

export function SharesPage() {
  usePageHeader('Shares')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const [revoking, setRevoking] = useState<Share | null>(null)
  const [signing, setSigning] = useState<Staged | null>(null)
  const served = !!ws && hasFeature('publish', ws)
  const list = useQuery({ ...shareQueries.list(ws ?? ''), enabled: served })
  if (!workspace || !ws) return null
  if (!served || (list.data && !list.data.available)) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Shares</h1>
        <p data-testid="shares-unavailable" className="text-[13px] text-text-muted">
          Publish is not on for this workspace, so there is nothing to list here. Turn it on in Settings, Add-ons.
        </p>
      </div>
    )
  }
  if (list.isError) return <LoadFailed what="shares" onRetry={() => void list.refetch()} />
  const data = list.data && list.data.available ? list.data : null
  const mayChange = atLeast(role, 'member')
  const shares = data?.shares ?? []
  const staged = data?.staged ?? []
  return (
    <div className="w-full max-w-4xl space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Shares</h1>
      <p className="text-[13px] text-text-muted">
        A share is a link to files of a ticket. You make one from the ticket, sign what will be sent, and see the link once. This page lists the shares and lets you revoke one: its link then stops working at once.
      </p>
      {data && !data.configured && (
        <p data-testid="shares-not-configured" className="rounded-lg border border-warning/40 px-3 py-2 text-[13px]">
          No publish server is set up yet. <Link to="/settings/$tab" params={{ tab: 'addons' }} className="text-brand hover:underline">Set one up in Settings, Add-ons</Link>.
        </p>
      )}
      {data && data.configured && !data.live && (
        <p role="status" className="text-[12px] text-text-muted">
          The server could not be reached, so views and states are the ones this computer last knew.
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
                  <StagedRow key={s.staged_id} s={s} ws={ws} mayChange={mayChange} onSign={setSigning} />
                ))}
              </ul>
            </section>
          )}
          {shares.length === 0 ? (
            <p data-testid="shares-empty" className="text-[13px] text-text-muted">
              Nothing is shared yet. Open a ticket and choose Share files.
            </p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-border bg-surface">
              <table className="w-full text-left text-[13px]" aria-label="Shares">
                <thead className="text-[11px] uppercase tracking-wide text-text-faint">
                  <tr>
                    <th className="px-3 py-2 font-medium">Ticket</th>
                    <th className="px-3 py-2 font-medium">Kind</th>
                    <th className="px-3 py-2 font-medium">Ends</th>
                    <th className="px-3 py-2 font-medium">Opens</th>
                    <th className="px-3 py-2 font-medium">State</th>
                    <th className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {shares.map((s) => {
                    const state = effectiveState(s)
                    return (
                      <tr key={s.id} data-share={s.id}>
                        <td className="px-3 py-2">
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
                        <td className="px-3 py-2">{kindText(s.kind, s.view_limit)}</td>
                        <td className="px-3 py-2">{s.expires_at ? fmtDateTime(s.expires_at) : 'not known'}</td>
                        <td className="px-3 py-2">{viewsText(s)}</td>
                        <td className="px-3 py-2">
                          <span className={cn('rounded-full border px-2 py-px text-[11px]', state === 'live' ? 'border-border text-success' : 'border-border text-text-muted')}>{STATE_TEXT[state]}</span>
                        </td>
                        <td className="px-3 py-2 text-right">
                          {mayChange && (state === 'live' || state === 'sending') && (
                            <Button size="sm" variant="ghost" className="h-7 text-[12px]" aria-label={`Revoke the share of ${s.ticket ?? 'this ticket'}`} onClick={() => setRevoking(s)}>
                              Revoke
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
      {revoking && <RevokeShare ws={ws} share={revoking} onClose={() => setRevoking(null)} />}
      {signing && <ShareFlow ws={ws} ticket={{ key: signing.ticket ?? '', artifacts: [] }} resume={signing} onClose={() => setSigning(null)} />}
    </div>
  )
}
