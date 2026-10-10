// Today: "Decided for you" (concept-mandates.md §3), PREVIEW ONLY. Below the real "needs you" queue, folded to one
// summary row ("Decided for you · 5 since you last looked · Review"): the preview never outranks real work. Open, it
// shows at most three of the mandate's decisions since the owner last looked (Looks right, Veto), then what the
// mandate refused or skipped (normal "needs you" rows), and "Show all" on Agents → Mandates. One "Revoke mandate and
// void…" in the header: it is mandate-wide (§2.13). Owners only, while the preview is on with a mandate (not revoked).
// Nothing here signs.
import { Link } from '@tanstack/react-router'
import { ChevronDown } from 'lucide-react'
import { useState } from 'react'
import { DECISION_KIND_LABEL, REFUSAL_LABEL, unseen, type PreviewMandate } from '@/api/mandatesPreview'
import { plain, Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { fmtWhen } from '@/lib/time'
import { cn } from '@/lib/utils'
import { useSessionState } from '../pages/today/shared'
import { Pill } from '../pages/ticket/shared'
import { DecisionLine, PreviewNote, RevokeDialog, shownMandate, useCanMandate, useMandatesOp, useMandatesPreview } from './shared'

/** Decisions shown when open; the rest are on Agents → Mandates. */
export const DIGEST_ROWS = 3

const ASK: Record<PreviewMandate['decisions'][number]['kind'], string> = {
  requirements: 'Approve requirements',
  plan: 'Approve plan',
  verdict: 'Give the verdict',
  code_review: 'Approve the code review',
  unblock: 'Unblock',
  permit: 'Answer the permit',
  factory_enabled: 'Enable the factory',
  factory_run: 'Start the factory run',
  grant: 'Issue the grant',
}

const rowCls = 'flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-2 last:border-b-0'

export function DecidedForYou({ ws, now }: { ws: string; now: string }) {
  const q = useMandatesPreview(ws)
  const m = shownMandate(q.data)
  const op = useMandatesOp(ws)
  const owner = useCanMandate()
  const [open, setOpen] = useSessionState(`orch.today.mandates.${ws}`, false)
  const [revoking, setRevoking] = useState(false)
  if (!m || !owner) return null
  const fresh = unseen(m).reverse()
  const shown = fresh.slice(0, DIGEST_ROWS)
  const summary = fresh.length ? `${fresh.length} since you last looked` : 'nothing new since you last looked'
  return (
    <section role="region" aria-labelledby="today-decided-for-you" className="overflow-hidden rounded-lg border border-border bg-surface" data-testid="decided-for-you">
      <h2 className="flex items-center gap-2 pr-3 text-[13px]">
        <button
          id="today-decided-for-you"
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex min-w-0 flex-1 items-center gap-1 px-3 py-2.5 text-left outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <ChevronDown className={cn('mr-1 size-4 shrink-0 text-text-muted transition-transform', !open && '-rotate-90')} aria-hidden />
          <span className="shrink-0 whitespace-nowrap font-semibold text-text">Decided for you</span>{' '}
          <span className="min-w-0 truncate tabular-nums text-text-muted">
            · {summary}
            {m.refused.length ? ` · ${m.refused.length} refused or skipped` : ''} · {open ? 'Hide' : 'Review'}
          </span>
        </button>
        <Pill className="shrink-0">Preview</Pill>
        <Button size="xs" variant="ghost" className="shrink-0" onClick={() => setRevoking(true)}>
          Revoke mandate and void…
        </Button>
      </h2>
      {open && (
        <>
          <PreviewNote chip={false} className="border-t border-border px-3 py-1.5" />
          {shown.length > 0 && (
            <ul className="border-t border-border" aria-label="Decided for you">
              {shown.map((d) => {
                const what = `${d.ticket ?? 'workspace'} ${DECISION_KIND_LABEL[d.kind].toLowerCase()}`
                return (
                  <li key={d.id} data-testid={`mandate-decision:${d.id}`} className={rowCls}>
                    <div className="flex min-w-[16rem] flex-1 gap-2 text-[13px] leading-5">
                      {d.ticket ? (
                        <Link to="/ticket/$key" params={{ key: d.ticket }} className="shrink-0 rounded outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring">
                          <Raw>{d.ticket}</Raw>
                        </Link>
                      ) : (
                        <span className="shrink-0 text-text-muted">workspace</span>
                      )}
                      <span className="min-w-0 flex-1">
                        <DecisionLine m={m} d={d} className="block font-medium text-text" />
                        <span className="block text-xs text-text-muted">
                          #{d.seq} · {fmtWhen(d.at, now)}
                        </span>
                      </span>
                    </div>
                    <div className="flex shrink-0 items-center gap-1">
                      <Button size="xs" variant="outline" aria-label={`Looks right: ${what}`} onClick={() => void op({ op: 'review', decision: d.id, review: 'looks_right' })}>
                        Looks right
                      </Button>
                      <Button size="xs" variant="ghost" aria-label={`Veto: ${what}`} onClick={() => void op({ op: 'review', decision: d.id, review: 'veto' }, `Veto recorded on ${plain(d.ticket ?? DECISION_KIND_LABEL[d.kind])}`)}>
                        Veto
                      </Button>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
          {m.refused.length > 0 && (
            <div className="border-t border-border" data-testid="mandate-refused-today">
              <h3 className="px-3 pt-2 text-[12px] font-semibold text-text">Refused or skipped by the mandate · you decide these</h3>
              <ul aria-label="Refused or skipped by the mandate">
                {m.refused.map((r) => (
                  <li key={r.id} data-testid={`mandate-refused:${r.id}`} className={rowCls}>
                    <div className="min-w-[16rem] flex-1 text-[13px] leading-5">
                      <p className="font-medium text-text">
                        {r.ticket && r.kind ? (
                          <>
                            {ASK[r.kind]} on <Raw>{r.ticket}</Raw>
                            {r.title ? ` · ${plain(r.title)}` : ''}
                          </>
                        ) : (
                          plain(r.asked ?? REFUSAL_LABEL[r.reason])
                        )}
                      </p>
                      {/* Core's reason, in full: why the item came to you. */}
                      <p className="text-xs text-text-muted">
                        <span className="mr-1.5 font-medium text-text">{REFUSAL_LABEL[r.reason]}:</span>
                        {r.detail} · {fmtWhen(r.at, now)}
                      </p>
                    </div>
                    {r.ticket && (
                      <Button size="xs" variant="outline" asChild>
                        <Link to="/ticket/$key" params={{ key: r.ticket }} aria-label={`Open ${r.ticket}`}>
                          Open
                        </Link>
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="border-t border-border px-3 py-2 text-xs text-text-muted">
            <Link to="/agents" search={{ tab: 'mandates' }} className="rounded underline-offset-2 outline-none hover:text-text hover:underline focus-visible:ring-2 focus-visible:ring-ring">
              Show all{fresh.length > shown.length ? ` (${fresh.length - shown.length} more)` : ''} on Agents → Mandates
            </Link>
          </div>
        </>
      )}
      {revoking && <RevokeDialog ws={ws} m={m} onClose={() => setRevoking(false)} />}
    </section>
  )
}
