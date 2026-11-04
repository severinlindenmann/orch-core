import { MessageSquareWarning, ShieldAlert } from 'lucide-react'
import type { GateName, TicketDocument } from '@/api/types'
import { cn } from '@/lib/utils'
import { GATE_LABEL } from './actions'
import type { Viewer } from './shared'

/** The gates this ticket shows: the code review only where it applies (or once it was signed). */
export const gatesOf = (t: TicketDocument): GateName[] =>
  t.gates.code.required || t.gates.code.approvals.length || t.gates.code.voided?.length ? ['requirements', 'plan', 'verify', 'code'] : ['requirements', 'plan', 'verify']

/** The words for a verdict nobody reviewed: auto-approved under the factory charter (owner decision 2026-10-10). */
export const CHARTER_VERDICT = 'Verdict: via the factory charter — no person reviewed this'

/** A note or invalidation reason stays visible under the progress line; a charter verdict is said plainly. */
export function GateNotes({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  const notes = gatesOf(ticket).flatMap((g): { g: GateName; text: string; tone: 'warning' | 'danger' }[] => {
    const s = ticket.gates[g]
    if (s.state === 'invalidated' && s.reason) return [{ g, text: s.reason, tone: 'warning' }]
    if (s.state === 'changes_requested' && s.note) return [{ g, text: s.note, tone: 'danger' }]
    // A charter verdict is said plainly: no person looked at this work.
    if (g === 'verify' && ticket.verdict?.via === 'factory_charter') return [{ g, text: `${CHARTER_VERDICT} (charter signed by ${viewer.name(ticket.verdict.charter_signed_by ?? '')}, commit ${ticket.verdict.source_sha ?? ticket.branch.head}).`, tone: 'warning' }]
    return []
  })
  if (notes.length === 0) return null
  return (
    <ul data-testid="gate-notes" className="space-y-1">
      {notes.map((n) => (
        <li
          key={n.g}
          className={cn(
            'flex items-start gap-1.5 rounded-md border px-2 py-1.5 text-[12px]',
            n.tone === 'warning' ? 'border-warning/30 bg-warning-soft text-warning' : 'border-danger/30 bg-danger-soft text-danger',
          )}
        >
          {n.tone === 'warning' ? <ShieldAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden /> : <MessageSquareWarning className="mt-0.5 size-3.5 shrink-0" aria-hidden />}
          <span>
            <span className="font-medium">{GATE_LABEL[n.g]}:</span> {n.text}
          </span>
        </li>
      ))}
    </ul>
  )
}
