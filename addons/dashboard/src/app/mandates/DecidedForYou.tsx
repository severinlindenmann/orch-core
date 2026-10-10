// Today: "Decided for you" (concept-mandates.md §3), PREVIEW ONLY. The mandate's decisions since the owner last
// looked, each with Looks right, Veto, and Revoke and void; below it what the mandate refused or skipped, as normal
// "needs you" rows. Shown only while the preview is on with a mandate (not revoked). Nothing here signs.
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { decisionLabel, REFUSAL_LABEL, unseen, type PreviewMandate } from '@/api/mandatesPreview'
import { Button } from '@/components/ui/button'
import { fmtWhen } from '@/lib/time'
import { RowShell } from '../pages/today/rows'
import { Pill } from '../pages/ticket/shared'
import { PreviewNote, RevokeDialog, shownMandate, useCanMandate, useMandatesOp, useMandatesPreview } from './shared'

const ASK: Record<PreviewMandate['decisions'][number]['kind'], string> = {
  requirements: 'Approve requirements',
  plan: 'Approve plan',
  verdict: 'Give the verdict',
}

export function DecidedForYou({ ws, now }: { ws: string; now: string }) {
  const q = useMandatesPreview(ws)
  const m = shownMandate(q.data)
  const op = useMandatesOp(ws)
  const [revoking, setRevoking] = useState(false)
  // The owner's own mandate: only owners see its digest.
  const owner = useCanMandate()
  if (!m || !owner) return null
  const fresh = unseen(m).reverse()
  return (
    <>
      <section role="region" aria-labelledby="today-decided-for-you" className="overflow-hidden rounded-lg border border-border bg-surface" data-testid="decided-for-you">
        <h2 id="today-decided-for-you" className="flex items-center gap-1 px-3 py-2.5 text-[13px]">
          <span className="font-semibold text-text">Decided for you</span>{' '}
          <span className="min-w-0 truncate tabular-nums text-text-muted">
            · {fresh.length} since you last looked · via mandate {m.id}, epic {m.epic.key}
          </span>
          <Pill className="ml-auto shrink-0">Preview</Pill>
        </h2>
        <PreviewNote chip={false} className="border-t border-border px-3 py-1.5" />
        {fresh.length > 0 ? (
          <ul className="border-t border-border">
            {fresh.map((d) => (
              // Core's label is shown in full (it wraps, never truncates): it must never pass for your own signature.
              <li key={d.id} data-testid={`mandate-decision:${d.id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-2 last:border-b-0">
                <div className="min-w-[16rem] flex-1">
                  <p className="text-[13px] font-medium leading-5 text-text">
                    <Link to="/ticket/$key" params={{ key: d.ticket }} className="mr-1.5 rounded font-mono text-[12px] text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring">
                      {d.ticket}
                    </Link>
                    {decisionLabel(d, m.id, m.issuer)}
                  </p>
                  <p className="text-xs leading-5 text-text-muted">
                    checked by checker {d.checker.identity} ({d.checker.result}) · #{d.seq} · {fmtWhen(d.at, now)}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <Button size="xs" variant="outline" onClick={() => void op({ op: 'review', decision: d.id, review: 'looks_right' })}>
                    Looks right
                  </Button>
                  <Button size="xs" variant="ghost" onClick={() => void op({ op: 'review', decision: d.id, review: 'veto' }, `Veto recorded on ${d.ticket}`)}>
                    Veto
                  </Button>
                  <Button size="xs" variant="ghost" onClick={() => setRevoking(true)}>
                    Revoke and void
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="border-t border-border px-3 py-3 text-[13px] text-text-muted">Nothing new since you last looked.</p>
        )}
      </section>

      {m.refused.length > 0 && (
        <section role="region" aria-labelledby="today-mandate-refused" className="overflow-hidden rounded-lg border border-border bg-surface" data-testid="mandate-refused-today">
          <h2 id="today-mandate-refused" className="flex items-center gap-1 px-3 py-2.5 text-[13px]">
            <span className="font-semibold text-text">Refused or skipped by the mandate</span>{' '}
            <span className="min-w-0 truncate tabular-nums text-text-muted">· {m.refused.length} · you decide these</span>
            <Pill className="ml-auto shrink-0">Preview</Pill>
          </h2>
          <ul className="border-t border-border">
            {m.refused.map((r) => (
              <RowShell
                key={r.id}
                testId={`mandate-refused:${r.id}`}
                ask={`${ASK[r.kind]} on ${r.ticket}${r.title ? ` · ${r.title}` : ''}`}
                sub={
                  <>
                    <span className="mr-1.5 font-medium text-text">{REFUSAL_LABEL[r.reason]}:</span>
                    {r.detail}
                  </>
                }
                age={fmtWhen(r.at, now)}
                action={
                  <Button size="xs" variant="outline" asChild>
                    <Link to="/ticket/$key" params={{ key: r.ticket }}>
                      Open
                    </Link>
                  </Button>
                }
              />
            ))}
          </ul>
        </section>
      )}
      {revoking && <RevokeDialog ws={ws} m={m} onClose={() => setRevoking(false)} />}
    </>
  )
}
