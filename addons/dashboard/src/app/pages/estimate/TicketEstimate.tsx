// The ticket page's Estimate box (real host, Estimate add-on on). A task shows its points and, for a person who may edit
// it, the scale to pick from; an epic shows the sum of its children and how many have no estimate yet. A change is the
// ordinary signed size change of the ticket (one review), nothing else.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Gauge } from 'lucide-react'
import { useState } from 'react'
import { ESTIMATE_SCALE, estimateOn, notEstimatedText, pointsOf, pointsText, rollup, type SizeName } from '@/api/estimate'
import type { HandoffAction } from '@/api/handoff'
import { atLeast } from '@/api/permissions'
import { queries } from '@/api/queries'
import type { TicketDocument } from '@/api/types'
import { HandoffDialog } from '@/components/sign/HandoffDialog'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'

type Pick = SizeName | 'none'

function EpicRollup({ ws, epic }: { ws: string; epic: string }) {
  const kids = useQuery(queries.tickets(ws, { parent: epic }))
  if (kids.isError) return <p className="text-[13px] text-text-muted">Could not read the tickets in this epic. Reload to try again.</p>
  if (!kids.data) return <p className="text-[13px] text-text-muted">Adding up…</p>
  // Closed without being done (won't do) tickets are not work left: they are neither summed nor missing.
  const r = rollup(kids.data.filter((t) => t.status !== 'closed'))
  if (r.estimated === 0 && r.notEstimated === 0) return <p className="text-[13px] text-text-muted">No tickets in this epic yet.</p>
  return (
    <p className="text-[13px]" data-testid="estimate-rollup">
      <span className="font-medium">{pointsText(r.total)}</span>
      <span className="text-text-muted"> in this epic</span>
      {r.notEstimated > 0 && <span className="text-text-muted"> · {notEstimatedText(r.notEstimated)}</span>}
    </p>
  )
}

export function TicketEstimate({ ticket }: { ticket: TicketDocument }) {
  const { workspace } = useWorkspace()
  const role = useRole()
  const qc = useQueryClient()
  const [pick, setPick] = useState<Pick | null>(null)
  const ws = workspace?.id
  if (!ws || !estimateOn(workspace)) return null
  const epic = ticket.type === 'epic'
  const points = pointsOf(ticket.size)
  const mayChange = atLeast(role, 'member') && ticket.status !== 'done' && ticket.status !== 'closed'
  const action: HandoffAction | null = pick ? { action: 'details', key: ticket.key, size: pick, via: 'estimate' } : null
  const wording = pick === 'none' ? 'Not estimated' : pointsText(ESTIMATE_SCALE.find((s) => s.size === pick)?.points ?? 0)
  return (
    <section aria-label="Estimate" className="space-y-2 rounded-lg border border-border bg-surface p-3" data-testid="ticket-estimate">
      <h3 className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-text-faint">
        <Gauge className="size-3.5" aria-hidden />
        Estimate
      </h3>
      {epic ? (
        <EpicRollup ws={ws} epic={ticket.key} />
      ) : (
        <>
          <p className="text-[13px]" data-testid="estimate-current">
            {points === null ? <span className="text-text-muted">Not estimated yet</span> : <span className="font-medium">{pointsText(points)}</span>}
          </p>
          {mayChange && (
            <div role="group" aria-label="Set the estimate" className="flex flex-wrap gap-1.5">
              {ESTIMATE_SCALE.map((s) => (
                <Button key={s.size} type="button" size="sm" variant={ticket.size === s.size ? 'secondary' : 'outline'} aria-pressed={ticket.size === s.size} aria-label={s.label} disabled={ticket.size === s.size} className={cn('h-7 min-w-8 px-2 font-mono text-[12px]')} onClick={() => setPick(s.size)}>
                  {s.points}
                </Button>
              ))}
              {ticket.size && (
                <Button type="button" size="sm" variant="ghost" className="h-7 text-[12px]" onClick={() => setPick('none')}>
                  Not estimated
                </Button>
              )}
            </div>
          )}
          {!mayChange && atLeast(role, 'member') && <p className="text-[12px] text-text-muted">A finished ticket keeps its estimate.</p>}
        </>
      )}
      {action && (
        <HandoffDialog
          key={`${ticket.key}-${pick}`}
          title={`Estimate ${ticket.key}`}
          summary={
            <p>
              Estimate: <strong>{wording}</strong>
            </p>
          }
          ws={ws}
          action={action}
          autoStart
          onClose={() => setPick(null)}
          onSettled={(phase) => {
            if (phase !== 'done') return
            void qc.invalidateQueries({ queryKey: ['ticket', ws, ticket.key] })
            void qc.invalidateQueries({ queryKey: ['tickets', ws] })
            void qc.invalidateQueries({ queryKey: ['board', ws] })
          }}
        />
      )}
    </section>
  )
}
