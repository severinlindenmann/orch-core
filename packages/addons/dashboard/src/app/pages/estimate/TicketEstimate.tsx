// The ticket page's Estimate box (real host, Estimate add-on on). A task shows its points and, for a person who may edit
// it, the scale to pick from; an epic shows the sum of its children and how many have no estimate yet. A change is the
// ordinary signed size change of the ticket (one review), nothing else.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ESTIMATE_SCALE, estimateOn, notEstimatedText, pointsOf, pointsText, rollup, type SizeName } from '@/api/estimate'
import type { HandoffAction } from '@/api/handoff'
import { atLeast } from '@/api/permissions'
import { queries } from '@/api/queries'
import type { TicketDocument } from '@/api/types'
import { SignPopover } from '@/components/sign/SignPopover'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Section } from '../ticket/shared'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'

const GATE_ORDER = ['requirements', 'plan', 'verify', 'code'] as const
const GATE_WORDS = { requirements: 'the requirements', plan: 'the plan', verify: 'the checks', code: 'the code' } as const

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
  // The size is part of what every gate's approval covers (core's binding list), and a raise of one gate raises the later
  // ones too, so a new estimate cancels every approval on this ticket, a partly collected one (2 of 3) included. The
  // gates come from the ticket's own data, not from a list kept here.
  const approved = GATE_ORDER.filter((g) => ticket.gates?.[g]?.state === 'approved' || (ticket.gates?.[g]?.approvals?.length ?? 0) > 0)
  const action: HandoffAction | null = pick ? { action: 'details', key: ticket.key, size: pick, via: 'estimate' } : null
  const wording = pick === 'none' ? 'No estimate' : pointsText(ESTIMATE_SCALE.find((s) => s.size === pick)?.points ?? 0)
  return (
    <Section title="Estimate" label="Estimate" testId="ticket-estimate">
      <div className="space-y-2">
      {epic ? (
        <EpicRollup ws={ws} epic={ticket.key} />
      ) : (
        <>
          <p className="text-[13px]" data-testid="estimate-current">
            {points === null ? <span className="text-text-muted">Not estimated yet</span> : <span className="font-medium">{pointsText(points)}</span>}
          </p>
          {mayChange && (
            <>
              <p className="text-[12px] text-text-muted">Points compare size, not hours. Choose a value to review and approve the change.</p>
              {approved.length > 0 && (
                <p className="text-[12px] text-warning" data-testid="estimate-cancels">
                  Changing the estimate cancels the approval of {approved.map((g) => GATE_WORDS[g]).join(', ')}. You will have to approve {approved.length === 1 ? 'it' : 'them'} again.
                </p>
              )}
              <div role="group" aria-label="Set the estimate" className="flex flex-wrap gap-1.5">
                {ESTIMATE_SCALE.map((s) => {
                  const on = ticket.size === s.size
                  return (
                    <Button
                      key={s.size}
                      type="button"
                      size="sm"
                      variant="outline"
                      aria-pressed={on}
                      aria-label={s.label}
                      className={cn('h-8 min-w-9 px-2 font-mono text-[12px]', on && 'border-brand bg-brand-soft text-brand hover:bg-brand-soft hover:text-brand')}
                      onClick={() => {
                        if (!on) setPick(s.size)
                      }}
                    >
                      {s.points}
                    </Button>
                  )
                })}
                {ticket.size && (
                  <Button type="button" size="sm" variant="ghost" className="h-8 text-[12px]" onClick={() => setPick('none')}>
                    Clear estimate
                  </Button>
                )}
              </div>
            </>
          )}
          {!mayChange && atLeast(role, 'member') && <p className="text-[12px] text-text-muted">A finished ticket keeps its estimate.</p>}
        </>
      )}
      </div>
      {action && (
        <SignPopover
          key={`${ticket.key}-${pick}`}
          title={`Estimate ${ticket.key}`}
          summary={
            <p>
              Estimate: <strong>{wording}</strong>
              {approved.length > 0 && <span className="block text-warning">This cancels the approval of {approved.map((g) => GATE_WORDS[g]).join(', ')}.</span>}
            </p>
          }
          ws={ws}
          action={action}
          onClose={() => setPick(null)}
          onSettled={(phase) => {
            if (phase !== 'done') return
            void qc.invalidateQueries({ queryKey: ['ticket', ws, ticket.key] })
            void qc.invalidateQueries({ queryKey: ['tickets', ws] })
            void qc.invalidateQueries({ queryKey: ['board', ws] })
          }}
        />
      )}
    </Section>
  )
}
