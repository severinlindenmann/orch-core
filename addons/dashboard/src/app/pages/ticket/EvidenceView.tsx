// The proving artifacts of a ticket's acceptance criteria, shown where the person decides: under each criterion on the
// Acceptance tab and inside the verdict dialog. Every preview is the one Viewer the Artifacts tab uses (same sandbox,
// same agent-HTML switch, same 64 KiB text rule): nothing here renders file content itself.
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { addonActive } from '@/api/addons'
import { queries } from '@/api/queries'
import { workspaceOfTicket } from '@/api/workspaces'
import type { AcceptanceStatus, Artifact, TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { visible } from '@/components/sign/visible'
import { fmtBytes, Pill } from './shared'
import { Viewer } from './Artifacts'

/** The artifacts a criterion lists as its proof (tasks are receipts, shown by the task rows). */
export function proofArtifacts(ticket: TicketDocument, ac: AcceptanceStatus): Artifact[] {
  return ac.evidence.flatMap((ev) => (ev.kind === 'artifact' ? ticket.artifacts.filter((a) => a.name === ev.ref) : []))
}

/** Whether agent HTML may be drawn in this ticket's workspace (the same switch as the Artifacts tab). */
function useAgentHtml(ticketKey: string): boolean {
  const workspaces = useQuery(queries.workspaces())
  return addonActive(workspaceOfTicket(ticketKey, workspaces.data ?? []), 'widgets')
}

/** One proving artifact: its name, what it proves, and its preview (open at first, one click to fold). */
export function EvidenceItem({ ticket, artifact, acId, compact = false, defaultOpen = true }: { ticket: TicketDocument; artifact: Artifact; acId: string; compact?: boolean; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const agentHtml = useAgentHtml(ticket.key)
  const body = `evidence-${acId}-${artifact.name}`
  return (
    <li data-testid="evidence-item" data-artifact={artifact.name} className="rounded-md border border-border bg-surface p-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px]">
        <span className="min-w-0 truncate font-mono text-[12px] font-medium" title={visible(artifact.name)}>
          {visible(artifact.name)}
        </span>
        <Pill tone="success">proves {acId}</Pill>
        {!!artifact.bytes && <span className="text-text-faint">{fmtBytes(artifact.bytes)}</span>}
        <Button type="button" variant="ghost" size="sm" className="ml-auto h-6 px-2 text-[12px]" aria-expanded={open} aria-controls={body} aria-label={`${open ? 'Hide' : 'Show'} preview of ${artifact.name}`} onClick={() => setOpen(!open)}>
          {open ? 'Hide preview' : 'Show preview'}
        </Button>
      </div>
      {artifact.label && <p className="mt-0.5 text-[12px] text-text-muted">{visible(artifact.label)}</p>}
      {open && (
        <div id={body} className={cn('mt-2 overflow-auto', compact && 'max-h-64')}>
          <Viewer a={artifact} agentHtml={agentHtml} ticket={ticket.key} />
        </div>
      )}
    </li>
  )
}

/** Under one criterion: its proving artifacts with previews. Renders nothing when it lists none. */
export function CriterionEvidence({ ticket, ac }: { ticket: TicketDocument; ac: AcceptanceStatus }) {
  const items = proofArtifacts(ticket, ac)
  if (items.length === 0) return null
  return (
    <ul className="mt-2 space-y-2 pl-[3.75rem]" aria-label={`Evidence for ${ac.id}`}>
      {items.map((a) => (
        <EvidenceItem key={a.name} ticket={ticket} artifact={a} acId={ac.id} />
      ))}
    </ul>
  )
}

/**
 * The verdict dialog's evidence: a one-line summary, then each criterion with what proves it. Previews are folded at
 * first (a dialog is short) and open on a click, so Pass or Send back is decided on the evidence itself.
 */
export function VerdictEvidence({ ticket }: { ticket: TicketDocument }) {
  const withProof = ticket.acceptance_state.map((ac) => ({ ac, items: proofArtifacts(ticket, ac) }))
  const without = withProof.filter((x) => x.items.length === 0 && x.ac.proof !== 'accepted')
  const total = withProof.reduce((n, x) => n + x.items.length, 0)
  return (
    <section aria-label="Evidence" className="space-y-2 rounded-md border border-border bg-bg px-3 py-2">
      <h3 className="text-[12px] font-medium text-text-muted">
        Evidence · {total} {total === 1 ? 'file' : 'files'} for {withProof.length - without.length} of {withProof.length} {withProof.length === 1 ? 'criterion' : 'criteria'}
      </h3>
      {total === 0 && <p className="text-[12px] text-text-muted">No file is attached as proof yet.</p>}
      {withProof.map(({ ac, items }) =>
        items.length === 0 ? null : (
          <div key={ac.id}>
            <p className="text-[12px] text-text">
              <span className="font-mono text-text-muted">{ac.id}</span> {visible(ac.text)}
            </p>
            <ul className="mt-1 space-y-1.5" aria-label={`Evidence for ${ac.id}`}>
              {items.map((a) => (
                <EvidenceItem key={a.name} ticket={ticket} artifact={a} acId={ac.id} compact defaultOpen={false} />
              ))}
            </ul>
          </div>
        ),
      )}
      {without.length > 0 && <p className="text-[12px] text-warning">No evidence file yet for {without.map((x) => x.ac.id).join(', ')}.</p>}
    </section>
  )
}
